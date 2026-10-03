"""ROUTE/ALC object reassembly for one ROUTE session (one destination IP:port).

Packets are placed at their start_offset (A/331 A.3.5.1, the 4 bytes after
the LCT header), so a lost packet leaves a zero-filled hole in place instead
of shifting everything after it. Objects are finalized when the carousel
restarts them, when the LCT B flag is set, or once nothing new has arrived for
them for a few seconds (this broadcast never sends the B flag reliably).

Which TSIs are kept is decided by the caller from the S-TSID: TSI 0 (the SLS)
always goes to `on_sls`; a media TSI is only stored once it is in `tracks`,
with its init segment recognised by the S-TSID's init TOI.
"""

import os
import struct
import threading
import time

SEGMENT_KEEP = 60                 # ~2 minutes of 2 s segments per TSI
_MAX_OBJECT_BYTES = 32 * 1024 * 1024
_STALE_SECONDS = 5.0
_REORDER_GRACE_SECONDS = 3.0


def init_path(cache_dir, tsi):
    return os.path.join(cache_dir, f"init_tsi_{tsi}.mp4")


def segment_prefix(tsi):
    return f"seg_tsi_{tsi}_toi_"


def atomic_write(path, data):
    # Readers stream these files from other threads while they are being
    # rewritten; temp-file + rename means they only ever see a whole file.
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class RouteSession:
    def __init__(self, cache_dir, on_sls=None, log=print):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.on_sls = on_sls
        self.log = log
        self.lock = threading.RLock()      # on_sls (under the lock) may call set_tracks
        self.tracks = {}            # tsi -> init_toi (None = unknown)
        self.objects = {}
        self.closed = {}
        self.last_seen = {}
        self.init_logged = set()

    def set_tracks(self, tracks):
        """tracks: {tsi: init_toi} - the media TSIs to store."""
        with self.lock:
            self.tracks = dict(tracks)

    # -- finalize ------------------------------------------------------------
    def _finalize(self, key):
        if self.closed.get(key):
            return
        self.closed[key] = True
        tsi, toi = key
        data = bytes(self.objects.pop(key, b""))
        if not data:
            return
        if tsi == 0:
            if self.on_sls and toi != 0:
                self.on_sls(data)
            return
        if tsi not in self.tracks:
            return
        init_toi = self.tracks[tsi]
        is_init = (toi == init_toi) if init_toi is not None else (
            len(data) > 100 and b"ftyp" in data[:32] and b"moov" in data)
        if is_init:
            if not (b"ftyp" in data[:32] and b"moov" in data):
                return                     # damaged init: wait for the next carousel copy
            atomic_write(init_path(self.cache_dir, tsi), data)
            if tsi not in self.init_logged:
                self.init_logged.add(tsi)
                self.log(f"[ROUTE] init segment for TSI {tsi} ({len(data)} bytes)")
            return
        atomic_write(os.path.join(self.cache_dir, f"{segment_prefix(tsi)}{toi}.m4s"), data)
        try:
            os.remove(os.path.join(self.cache_dir, f"{segment_prefix(tsi)}{toi - SEGMENT_KEEP}.m4s"))
        except FileNotFoundError:
            pass

    # -- packets -------------------------------------------------------------
    def feed(self, payload):
        """One ALC/LCT UDP payload of this session."""
        if len(payload) < 16 or (payload[0] >> 4) != 1:
            return
        hdr_len = payload[2] * 4             # LCT header length incl. extensions
        if len(payload) < hdr_len + 4:
            return
        flag_b = payload[1] & 0x01
        tsi, toi = struct.unpack("!II", payload[8:16])
        with self.lock:
            if tsi != 0 and tsi not in self.tracks:
                return
            start_offset = struct.unpack("!I", payload[hdr_len:hdr_len + 4])[0]
            if start_offset > _MAX_OBJECT_BYTES:
                return
            data = payload[hdr_len + 4:]
            key = (tsi, toi)
            now = time.time()

            buf = self.objects.get(key)
            # a carousel resend starting over at offset 0 = the previous copy is complete
            restart = (start_offset == 0 and buf is not None and len(buf) >= len(data)
                       and bytes(buf[:len(data)]) == data)
            if restart and not self.closed.get(key):
                self._finalize(key)
            last = self.last_seen.get(key)
            if (key not in self.objects or self.closed.get(key)
                    or (last is not None and now - last > _STALE_SECONDS) or restart):
                self.objects[key] = bytearray()
                self.closed[key] = False
            self.last_seen[key] = now
            buf = self.objects[key]
            end = start_offset + len(data)
            if end > len(buf):
                buf.extend(bytes(end - len(buf)))
            buf[start_offset:end] = data

            for other in list(self.objects):
                if other[0] != tsi or other == key or self.closed.get(other):
                    continue
                if now - self.last_seen.get(other, 0) > _REORDER_GRACE_SECONDS:
                    self._finalize(other)
            if flag_b:
                self._finalize(key)
            # forget long-closed objects so the bookkeeping doesn't grow forever
            if len(self.last_seen) > 4096:
                for k in [k for k, t in self.last_seen.items() if now - t > 60 and self.closed.get(k)]:
                    self.last_seen.pop(k, None)
                    self.closed.pop(k, None)
