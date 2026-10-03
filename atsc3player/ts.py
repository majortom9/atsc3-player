# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""MPEG-2 transport stream helpers: PSI section assembly, CRC32, and a reader
for the whole TS of a DVB adapter (demux PID 0x2000 -> dvr0)."""

import fcntl
import os
import struct
import threading

TS_SIZE = 188

# <linux/dvb/dmx.h>: struct dmx_pes_filter_params {u16 pid; enum input, output,
# pes_type; u32 flags} (packed to 2+4*4 with padding after pid), sizes are the
# same on 32- and 64-bit userland.
DMX_SET_PES_FILTER = 0x40146F2C
DMX_SET_BUFFER_SIZE = 0x6F2D
DMX_STOP = 0x6F2A
DMX_IN_FRONTEND, DMX_OUT_TS_TAP, DMX_PES_OTHER, DMX_IMMEDIATE_START = 0, 2, 20, 4


def _crc_table():
    t = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
        t.append(c & 0xFFFFFFFF)
    return t


_CRC = _crc_table()


def crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) ^ b) & 0xFF]
    return crc


def payload_of(pkt):
    """(payload_unit_start, payload bytes) of one 188-byte packet, or None."""
    if pkt[0] != 0x47 or pkt[1] & 0x80:
        return None
    afc = (pkt[3] >> 4) & 3
    if not afc & 1:
        return None
    off = 4
    if afc & 2:
        off += 1 + pkt[4]
    if off >= TS_SIZE:
        return None
    return bool(pkt[1] & 0x40), pkt[off:]


class SectionAssembler:
    """Reassemble PSI/PSIP sections for a set of PIDs; calls on_section(pid, section)
    for each complete section whose CRC checks out."""

    def __init__(self, on_section):
        self.on_section = on_section
        self.buf = {}

    def feed(self, pid, pkt):
        r = payload_of(pkt)
        if r is None:
            return
        start, pl = r
        if start:
            ptr = pl[0]
            if pid in self.buf and ptr:
                self._append(pid, pl[1:1 + ptr])
            self.buf[pid] = bytearray(pl[1 + ptr:])
            self._drain(pid)
        elif pid in self.buf:
            self._append(pid, pl)

    def _append(self, pid, data):
        self.buf[pid] += data
        self._drain(pid)

    def _drain(self, pid):
        b = self.buf.get(pid)
        while b is not None and len(b) >= 3:
            if b[0] == 0xFF:                       # stuffing: rest of the packet is padding
                self.buf[pid] = bytearray()
                return
            length = 3 + (((b[1] & 0x0F) << 8) | b[2])
            if len(b) < length:
                return
            section = bytes(b[:length])
            del b[:length]
            if section[1] & 0x80 and crc32_mpeg(section) != 0:   # syntax indicator -> CRC'd
                continue
            self.on_section(pid, section)


class DvrReader(threading.Thread):
    """Read the full TS of adapterN (all PIDs) and hand 188-byte packets to `sink`."""

    def __init__(self, adapter, sink, log=print, demux=0):
        super().__init__(daemon=True, name="dvr")
        self.sink, self.log = sink, log
        base = f"/dev/dvb/adapter{adapter}"
        self.dmx = os.open(f"{base}/demux{demux}", os.O_RDWR)
        try:
            fcntl.ioctl(self.dmx, DMX_SET_BUFFER_SIZE, 16 << 20)
        except OSError:
            pass
        fcntl.ioctl(self.dmx, DMX_SET_PES_FILTER,
                    struct.pack("=HxxIIII", 0x2000, DMX_IN_FRONTEND, DMX_OUT_TS_TAP,
                                DMX_PES_OTHER, DMX_IMMEDIATE_START))
        self.dvr = os.open(f"{base}/dvr{demux}", os.O_RDONLY | os.O_NONBLOCK)
        self.stop_event = threading.Event()
        self.overflows = 0
        self.bytes = 0

    def run(self):
        import select
        rest = b""
        while not self.stop_event.is_set():
            r, _, _ = select.select([self.dvr], [], [], 0.25)
            if not r:
                continue
            try:
                data = os.read(self.dvr, 188 * 1024)
            except BlockingIOError:
                continue
            except OSError as e:
                if e.errno == 75:                  # EOVERFLOW: we fell behind; keep going
                    self.overflows += 1
                    rest = b""
                    continue
                self.log(f"[dvr] {e}")
                break
            if not data:
                continue
            self.bytes += len(data)
            data = rest + data
            i = 0
            # resync on the 0x47 sync byte if a read ever lands mid-packet
            while i + TS_SIZE <= len(data):
                if data[i] != 0x47:
                    i += 1
                    continue
                self.sink(data[i:i + TS_SIZE])
                i += TS_SIZE
            rest = data[i:]

    def close(self):
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=2)
        for fd in (self.dvr, self.dmx):
            try:
                os.close(fd)
            except OSError:
                pass
