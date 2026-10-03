# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""ATSC 1.0 program information: MPEG-2 PAT/PMT and ATSC A/65 PSIP
(MGT, TVCT/CVCT, STT, EIT, ETT) parsed from PSI sections."""

import struct
import time
from dataclasses import dataclass, field

PSIP_PID = 0x1FFB
TID_PAT, TID_PMT = 0x00, 0x02
TID_MGT, TID_TVCT, TID_CVCT, TID_EIT, TID_ETT, TID_STT = 0xC7, 0xC8, 0xC9, 0xCB, 0xCC, 0xCD
GPS_EPOCH = 315964800              # 1980-01-06 00:00:00 UTC in unix seconds

STREAM_TYPES = {
    0x02: ("video", "MPEG-2"), 0x1B: ("video", "H.264"), 0x24: ("video", "HEVC"),
    0x81: ("audio", "AC-3"), 0x87: ("audio", "E-AC-3"), 0x0F: ("audio", "AAC"),
    0x03: ("audio", "MP1/2"), 0x04: ("audio", "MP2"), 0x86: ("data", "SCTE-35"),
}


@dataclass
class Stream:
    pid: int
    stream_type: int
    kind: str
    codec: str
    lang: str = ""


@dataclass
class Program:
    number: int
    pmt_pid: int
    pcr_pid: int = 0
    streams: list = field(default_factory=list)


@dataclass
class VirtualChannel:
    major: int
    minor: int
    name: str
    program_number: int
    source_id: int
    service_type: int
    hidden: bool = False

    @property
    def channel(self):
        return f"{self.major}.{self.minor}"


@dataclass
class Event:
    source_id: int
    event_id: int
    start: int                      # unix seconds
    end: int
    title: str
    description: str = ""


def _mss(data):
    """A/65 Multiple String Structure -> first string (uncompressed modes)."""
    if not data:
        return ""
    n, i, out = data[0], 1, []
    for _ in range(n):
        if i + 4 > len(data):
            break
        i += 3                                      # ISO 639 language
        segs = data[i]; i += 1
        text = ""
        for _ in range(segs):
            if i + 3 > len(data):
                break
            comp, mode, nb = data[i], data[i + 1], data[i + 2]
            raw = data[i + 3:i + 3 + nb]
            i += 3 + nb
            if comp != 0:
                text += ""                          # Huffman (Annex C) not supported
            elif mode == 0x00:
                text += raw.decode("latin-1")
            elif mode == 0x3F:
                text += raw.decode("utf-16-be", errors="replace")
            else:                                   # other 8-bit Unicode pages: upper byte = mode
                text += "".join(chr((mode << 8) | b) for b in raw)
        out.append(text)
    return out[0] if out else ""


def _descriptors(data):
    i = 0
    while i + 2 <= len(data):
        tag, ln = data[i], data[i + 1]
        yield tag, data[i + 2:i + 2 + ln]
        i += 2 + ln


def parse_pat(sec):
    """-> {program_number: pmt_pid} (program 0 = NIT is skipped)."""
    n = ((sec[1] & 0x0F) << 8) | sec[2]
    body = sec[8:3 + n - 4]
    out = {}
    for i in range(0, len(body) - 3, 4):
        prog, pid = struct.unpack("!HH", body[i:i + 4])
        if prog:
            out[prog] = pid & 0x1FFF
    return out


def parse_pmt(sec, pmt_pid):
    n = ((sec[1] & 0x0F) << 8) | sec[2]
    prog = struct.unpack("!H", sec[3:5])[0]
    pcr = struct.unpack("!H", sec[8:10])[0] & 0x1FFF
    pil = struct.unpack("!H", sec[10:12])[0] & 0x0FFF
    p = Program(prog, pmt_pid, pcr)
    i, end = 12 + pil, 3 + n - 4
    while i + 5 <= end:
        st = sec[i]
        pid = struct.unpack("!H", sec[i + 1:i + 3])[0] & 0x1FFF
        eil = struct.unpack("!H", sec[i + 3:i + 5])[0] & 0x0FFF
        kind, codec = STREAM_TYPES.get(st, ("other", f"0x{st:02x}"))
        lang = ""
        for tag, d in _descriptors(sec[i + 5:i + 5 + eil]):
            if tag == 0x0A and len(d) >= 3:                  # ISO_639_language
                lang = d[:3].decode("latin-1").strip("\0")
        p.streams.append(Stream(pid, st, kind, codec, lang))
        i += 5 + eil
    return p


def parse_mgt(sec):
    """-> {table_type: pid}; EIT-k = 0x0100+k, ETT for EIT-k = 0x0200+k."""
    n = struct.unpack("!H", sec[9:11])[0]
    i, out = 11, {}
    for _ in range(n):
        if i + 11 > len(sec):
            break
        ttype = struct.unpack("!H", sec[i:i + 2])[0]
        pid = struct.unpack("!H", sec[i + 2:i + 4])[0] & 0x1FFF
        dl = struct.unpack("!H", sec[i + 9:i + 11])[0] & 0x0FFF
        out[ttype] = pid
        i += 11 + dl
    return out


def parse_vct(sec):
    n = sec[9]
    i, out = 10, []
    for _ in range(n):
        if i + 32 > len(sec):
            break
        name = sec[i:i + 14].decode("utf-16-be", errors="replace").rstrip("\0 ")
        b = struct.unpack("!I", sec[i + 14:i + 18])[0]
        major, minor = (b >> 18) & 0x3FF, (b >> 8) & 0x3FF
        prog = struct.unpack("!H", sec[i + 24:i + 26])[0]
        flags = struct.unpack("!H", sec[i + 26:i + 28])[0]
        hidden = bool(flags & 0x0010)
        service_type = flags & 0x3F
        source_id = struct.unpack("!H", sec[i + 28:i + 30])[0]
        dl = struct.unpack("!H", sec[i + 30:i + 32])[0] & 0x03FF
        out.append(VirtualChannel(major, minor, name, prog, source_id, service_type, hidden))
        i += 32 + dl
    return out


def parse_stt(sec):
    """-> GPS-UTC offset in seconds."""
    return sec[13]


def parse_eit(sec, gps_utc):
    source_id = struct.unpack("!H", sec[3:5])[0]
    n = sec[9]
    i, out = 10, []
    for _ in range(n):
        if i + 10 > len(sec):
            break
        event_id = struct.unpack("!H", sec[i:i + 2])[0] & 0x3FFF
        start = struct.unpack("!I", sec[i + 2:i + 6])[0]
        length = int.from_bytes(sec[i + 6:i + 9], "big") & 0x0FFFFF
        tl = sec[i + 9]
        title = _mss(sec[i + 10:i + 10 + tl])
        j = i + 10 + tl
        dl = struct.unpack("!H", sec[j:j + 2])[0] & 0x0FFF
        t0 = GPS_EPOCH + start - gps_utc
        out.append(Event(source_id, event_id, t0, t0 + length, title))
        i = j + 2 + dl
    return out


def parse_ett(sec):
    """-> (source_id, event_id or None, text). ETM_id: source(16) event(14) type(2)."""
    etm = struct.unpack("!I", sec[9:13])[0]
    source_id = etm >> 16
    event_id = (etm >> 2) & 0x3FFF if etm & 0x2 else None
    return source_id, event_id, _mss(sec[13:-4])


class PsipMonitor:
    """Feed sections; keeps programs, virtual channels and the EPG."""

    def __init__(self, on_channels=None, on_epg=None):
        self.on_channels, self.on_epg = on_channels, on_epg
        self.pat = {}
        self.programs = {}                 # program_number -> Program
        self.channels = []
        self.mgt = {}
        self.gps_utc = 18                  # until the STT says otherwise
        self.events = {}                   # (source_id, event_id) -> Event
        self.texts = {}                    # (source_id, event_id) -> description
        self._versions = {}

    def wanted_pids(self):
        pids = {0, PSIP_PID} | {p for p in self.pat.values()}
        pids |= {pid for t, pid in self.mgt.items() if 0x0100 <= t <= 0x027F}
        return pids

    def _new(self, key, sec):
        """True if this (table, extension, section) changed since last seen."""
        ver = (sec[5] >> 1) & 0x1F
        if self._versions.get(key) == (ver, len(sec)):
            return False
        self._versions[key] = (ver, len(sec))
        return True

    def feed(self, pid, sec):
        tid = sec[0]
        ext = struct.unpack("!H", sec[3:5])[0] if len(sec) > 8 else 0
        if not self._new((pid, tid, ext, sec[6] if len(sec) > 6 else 0), sec):
            return
        if pid == 0 and tid == TID_PAT:
            self.pat = parse_pat(sec)
        elif tid == TID_PMT and pid in self.pat.values():
            p = parse_pmt(sec, pid)
            self.programs[p.number] = p
            if self.on_channels:
                self.on_channels(self.channels, self.programs)
        elif pid == PSIP_PID and tid == TID_MGT:
            self.mgt = parse_mgt(sec)
        elif pid == PSIP_PID and tid in (TID_TVCT, TID_CVCT):
            self.channels = [c for c in parse_vct(sec) if not c.hidden]
            if self.on_channels:
                self.on_channels(self.channels, self.programs)
        elif pid == PSIP_PID and tid == TID_STT:
            self.gps_utc = parse_stt(sec)
        elif tid == TID_EIT:
            for e in parse_eit(sec, self.gps_utc):
                e.description = self.texts.get((e.source_id, e.event_id), "")
                self.events[(e.source_id, e.event_id)] = e
            if self.on_epg:
                self.on_epg()
        elif tid == TID_ETT:
            sid, eid, text = parse_ett(sec)
            if eid is not None:
                self.texts[(sid, eid)] = text
                if (sid, eid) in self.events:
                    self.events[(sid, eid)].description = text

    def now_next(self, source_id, now=None):
        now = now or time.time()
        evs = sorted((e for e in self.events.values() if e.source_id == source_id), key=lambda e: e.start)
        cur = next((e for e in evs if e.start <= now < e.end), None)
        nxt = next((e for e in evs if e.start >= (cur.end if cur else now)), None)
        return cur, nxt
