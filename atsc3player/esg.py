# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""ATSC 3.0 Electronic Service Guide (A/332, OMA BCAST Service Guide).

The ESG is its own ROUTE service (SLT serviceCategory 4). Its S-TSID lists
every file per TSI with TOI, Content-Location, Transfer-Length and encoding:
the SGDD (index, with the time range covered), SGDUs (containers of Service,
Content and Schedule XML fragments) and the station logos (PNG). Files are
collected by start_offset and handed on once all Transfer-Length bytes are in.

SGDU layout (OMA BCAST SG 5.4.1): extension_offset(32) reserved(16)
n_o_service_guide_fragments(24), then per fragment fragmentTransportID(32)
fragmentVersion(32) offset(32); then the fragments, each
fragmentEncoding(8) fragmentType(8) XML (0-terminated). Types: 1 Service,
2 Content, 3 Schedule. Times are NTP seconds (since 1900).
"""

import gzip
import re
import struct
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NTP_EPOCH_OFFSET = 2208988800


def ntp_to_unix(t):
    return int(t) - NTP_EPOCH_OFFSET


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _attr(el, name):
    for k, v in el.attrib.items():
        if k.rsplit("}", 1)[-1] == name:
            return v
    return None


def _child(el, name):
    return next((c for c in el if _local(c.tag) == name), None)


def _iter(el, name):
    return [c for c in el.iter() if _local(c.tag) == name]


def efdt_files(stsid_xml):
    """S-TSID -> {tsi: {toi: {name, length, encoding}}} from each LS's EFDT."""
    root = ET.fromstring(stsid_xml)
    out = {}
    for ls in _iter(root, "LS"):
        tsi = int(_attr(ls, "tsi"))
        files = {}
        for f in _iter(ls, "File"):
            files[int(_attr(f, "TOI"))] = {
                "name": _attr(f, "Content-Location") or "",
                "length": int(_attr(f, "Transfer-Length") or _attr(f, "Content-Length") or 0),
                "encoding": _attr(f, "Content-Encoding") or "",
            }
        out[tsi] = files
    return out


def split_sgdu(data):
    """SGDU -> [(fragment_type, xml_bytes)]."""
    if len(data) < 9:
        return []
    n = int.from_bytes(data[6:9], "big")
    table_end = 9 + 12 * n
    if len(data) < table_end:
        return []
    offsets = [struct.unpack("!III", data[9 + 12 * i:21 + 12 * i])[2] for i in range(n)]
    payload = data[table_end:]
    bounds = offsets + [len(payload)]
    out = []
    for i in range(n):
        frag = payload[bounds[i]:bounds[i + 1]]
        if len(frag) > 2 and frag[0] == 0:          # 0 = XML encoding
            out.append((frag[1], frag[2:].rstrip(b"\0")))
    return out


@dataclass
class GuideService:
    frag_id: str
    name: str
    global_id: str = ""
    major: int = 0
    minor: int = 0
    icon: str = ""


@dataclass
class Programme:
    content_id: str
    title: str
    description: str = ""
    rating: str = ""
    length: str = ""
    genre: str = ""
    poster_url: str = ""


@dataclass
class Slot:
    start: int                      # unix seconds
    end: int
    content_id: str


@dataclass
class Guide:
    services: dict = field(default_factory=dict)       # frag_id -> GuideService
    contents: dict = field(default_factory=dict)       # content_id -> Programme
    schedule: dict = field(default_factory=dict)       # service frag_id -> [Slot]
    icons: dict = field(default_factory=dict)          # file name -> PNG bytes

    def service_for(self, global_id="", major=0, minor=0):
        for s in self.services.values():
            if global_id and s.global_id == global_id:
                return s
        for s in self.services.values():
            if major and (s.major, s.minor) == (major, minor):
                return s
        return None

    def slots(self, svc):
        return sorted(self.schedule.get(svc.frag_id, []), key=lambda s: s.start) if svc else []

    def now_next(self, svc, now=None):
        now = now or time.time()
        slots = self.slots(svc)
        cur = next((s for s in slots if s.start <= now < s.end), None)
        nxt = next((s for s in slots if s.start >= (cur.end if cur else now)), None)
        return cur, nxt

    def programme(self, slot):
        return self.contents.get(slot.content_id) if slot else None


def _text(el, name):
    c = _child(el, name)
    return (_attr(c, "text") or (c.text or "")).strip() if c is not None else ""


def parse_fragment(guide, ftype, xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return
    kind = _local(root.tag)
    if kind == "Service":
        s = GuideService(frag_id=root.get("id", ""), name=_text(root, "Name"),
                         global_id=root.get("globalServiceID", ""))
        for el in root.iter():
            n = _local(el.tag)
            if n == "MajorChannelNum":
                s.major = int(el.text or 0)
            elif n == "MinorChannelNum":
                s.minor = int(el.text or 0)
            elif n == "Icon" and not s.icon:
                s.icon = (el.text or "").strip()
        guide.services[s.frag_id] = s
    elif kind == "Content":
        p = Programme(content_id=root.get("id", ""), title=_text(root, "Name"),
                      description=_text(root, "Description"))
        for el in root.iter():
            n = _local(el.tag)
            if n == "RatingDescription" and not p.rating:
                p.rating = (el.text or "").strip()
            elif n == "Length":
                p.length = (el.text or "").strip()
            elif n == "Genre" and not p.genre:
                p.genre = (_attr(el, "href") or "").rsplit(":", 1)[-1]
            elif n == "ContentIcon" and not p.poster_url:
                p.poster_url = (el.text or "").strip()
        guide.contents[p.content_id] = p
    elif kind == "Schedule":
        svc_ref = _child(root, "ServiceReference")
        sid = _attr(svc_ref, "idRef") if svc_ref is not None else ""
        slots = []
        for cref in _iter(root, "ContentReference"):
            for pw in _iter(cref, "PresentationWindow"):
                slots.append(Slot(ntp_to_unix(pw.get("startTime")), ntp_to_unix(pw.get("endTime")),
                                  _attr(cref, "idRef")))
        # several Schedule fragments can cover one service: merge, keyed by start
        merged = {s.start: s for s in guide.schedule.get(sid, [])}
        merged.update({s.start: s for s in slots})
        guide.schedule[sid] = list(merged.values())


class EsgCollector:
    """Feed ALC payloads of the ESG ROUTE session; builds `guide`, calls on_update."""

    def __init__(self, on_update=None, log=print):
        self.on_update, self.log = on_update, log
        self.guide = Guide()
        self.files = {}                       # tsi -> {toi: info}
        self.buffers = {}                     # (tsi, toi) -> [bytearray, set(offsets), bytes_in]
        self.done = {}                        # (tsi, toi) -> length delivered
        self.lock = threading.Lock()

    def feed(self, payload):
        """One ALC payload of the ESG session (TSI 0 is handled by a RouteSession)."""
        if len(payload) < 16 or (payload[0] >> 4) != 1:
            return
        hdr = payload[2] * 4
        if len(payload) < hdr + 4:
            return
        tsi, toi = struct.unpack("!II", payload[8:16])
        if tsi == 0:
            return
        off = struct.unpack("!I", payload[hdr:hdr + 4])[0]
        data = payload[hdr + 4:]
        with self.lock:
            info = self.files.get(tsi, {}).get(toi)
            if not info or not info["length"] or self.done.get((tsi, toi)) == info["length"]:
                return
            key = (tsi, toi)
            buf = self.buffers.setdefault(key, [bytearray(), set(), 0])
            if off in buf[1]:
                return                         # carousel repeat of a piece we have
            end = off + len(data)
            if end > len(buf[0]):
                buf[0].extend(bytes(end - len(buf[0])))
            buf[0][off:end] = data
            buf[1].add(off)
            buf[2] += len(data)
            if buf[2] >= info["length"]:
                whole = bytes(buf[0][:info["length"]])
                del self.buffers[key]
                self.done[key] = info["length"]
        if buf[2] >= info["length"]:
            self._deliver(tsi, toi, info, whole)

    def set_files(self, files):
        with self.lock:
            if files != self.files:
                self.files = files
                self.done = {k: v for k, v in self.done.items()
                             if files.get(k[0], {}).get(k[1], {}).get("length") == v}

    def _deliver(self, tsi, toi, info, data):
        try:
            if info.get("encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
        except (OSError, EOFError):
            return
        name = info.get("name", "")
        changed = False
        if name.lower().endswith((".png", ".jpg", ".jpeg")) or data[:8] == b"\x89PNG\r\n\x1a\n":
            self.guide.icons[name] = data
            changed = True
        elif b"ServiceGuideDeliveryDescriptor" in data[:400]:
            pass
        elif name.startswith("sgdu") or (len(data) > 9 and b"<?xml" in data[:4096]):
            for ftype, xml in split_sgdu(data):
                parse_fragment(self.guide, ftype, xml)
            changed = True
            self.log(f"[ESG] {name}: {len(self.guide.services)} services, "
                     f"{len(self.guide.contents)} programmes")
        if changed and self.on_update:
            self.on_update(self.guide)
