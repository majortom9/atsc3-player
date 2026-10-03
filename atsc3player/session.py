"""One receive session on an ALP interface: LLS/SLT, the selected service's
SLS and media, and the HTTP streams. Used by both the CLI and the GUI.

Callbacks (on_services, on_tracks, on_status) run on the capture thread; a
GUI must hop to its main loop (GLib.idle_add) before touching widgets.
"""

import os
import shutil
import socket
import struct
import threading
import time

from . import lls, sls
from .route import RouteSession
from .streams import StreamServer

CACHE_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "atsc3_cache")
SOL_PACKET, PACKET_STATISTICS = 263, 6


class Session:
    def __init__(self, interface="alp0", cache_dir=CACHE_DIR, http_port=8080, lan=False,
                 log=print, on_services=None, on_tracks=None, on_status=None):
        self.interface, self.cache_dir = interface, cache_dir
        self.log = log
        self.on_services, self.on_tracks, self.on_status = on_services, on_tracks, on_status
        self.stop_event = threading.Event()        # capture thread
        self.stream_stop = threading.Event()       # HTTP stream generators
        self.lls = lls.LlsMonitor(on_slt=self._slt)
        self.service = None                        # lls.Service, or None
        self.sls_addr = None                       # (dst, port) of the selected SLS session
        self.route = None
        self.tracks = []                           # [sls.Track] of the selected service
        self.lang = None
        self.video = self.audio = None
        self.delivery = ""
        self.stats = {"packets": 0, "bytes": 0, "drops": 0, "rate_mbps": 0.0}
        self._sock = None
        self._thread = None
        self._reset_cache()
        self.server = StreamServer(self, port=http_port, lan=lan)

    # -- control ---------------------------------------------------------------
    def start(self):
        s = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(0x0800))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        s.bind((self.interface, 0))
        s.settimeout(0.5)
        self._sock = s
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._capture, daemon=True, name="capture")
        self._thread.start()

    def close(self):
        self.stop_event.set()
        self.stream_stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()
        self.server.close()

    def set_lan(self, lan):
        if lan != self.server.lan:
            port = self.server.port
            self.server.close()
            self.server = StreamServer(self, port=port, lan=lan)

    def select_service(self, service=None, sls_addr=None, lang=None):
        """Watch a service: an lls.Service, or a bare SLS (dst, port) without an SLT."""
        self.service = service
        self.sls_addr = (service.sls_dst, service.sls_port) if service else tuple(sls_addr)
        self.lang = lang
        self.tracks, self.video, self.audio, self.delivery = [], None, None, ""
        self._reset_cache()
        self.route = RouteSession(self.cache_dir, on_sls=self._sls, log=self.log)
        name = f"{service.name} ({service.service_id})" if service else "%s:%d" % self.sls_addr
        self.log(f"[Session] watching {name} on {self.sls_addr[0]}:{self.sls_addr[1]}")

    def set_language(self, lang):
        self.lang = lang
        self._pick()

    def current_tsi(self, track):
        t = self.video if track == "video" else self.audio if track == "audio" else None
        return t.tsi if t else None

    # -- internals ---------------------------------------------------------------
    def _reset_cache(self):
        # a new service starts clean, so old segments can't leak into its streams
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        os.makedirs(self.cache_dir, exist_ok=True)

    def _slt(self, services):
        self.log(f"[LLS] SLT: {len(services)} services")
        if self.on_services:
            self.on_services(services)

    def _sls(self, bundle):
        if not sls.is_bundle(bundle):
            return
        try:
            parts = sls.find_parts(sls.split_bundle(bundle))
        except Exception as e:                      # damaged bundle: wait for the next copy
            self.log(f"[SLS] unreadable bundle: {e!r}")
            return
        self.delivery = sls.delivery(parts.get("usbd")) or self.delivery
        if "stsid" not in parts:
            if self.delivery == "broadband" and not self.tracks:
                self.log("[SLS] broadband-only service: nothing is broadcast to play")
                self._notify_tracks()
            return
        try:
            tracks = sls.resolve_tracks(parts["stsid"], parts.get("mpd"), *self.sls_addr)
        except Exception as e:
            self.log(f"[SLS] bad S-TSID/MPD: {e!r}")
            return
        if [(t.tsi, t.lang, t.init_toi) for t in tracks] == [(t.tsi, t.lang, t.init_toi) for t in self.tracks]:
            return                                  # same signalling, new version number
        self.tracks = tracks
        self.log("[SLS] tracks: " + ", ".join(f"TSI {t.tsi} {t.label}" for t in tracks))
        self._pick()

    def _pick(self):
        self.video, self.audio = sls.pick_tracks(self.tracks, self.lang)
        others = [t for t in (self.video, self.audio) if t and (t.dst, t.port) != self.sls_addr]
        if others:
            self.log("[SLS] some components are on another ROUTE session; not supported yet")
        if self.route:
            self.route.set_tracks({t.tsi: t.init_toi for t in (self.video, self.audio) if t})
        self._notify_tracks()

    def _notify_tracks(self):
        if self.on_tracks:
            self.on_tracks(self.tracks, self.video, self.audio, self.delivery)

    def _capture(self):
        lls_addr = socket.inet_aton(lls.LLS_ADDRESS[0]), lls.LLS_ADDRESS[1]
        t_rate, b_rate = time.time(), 0
        while not self.stop_event.is_set():
            try:
                pkt = self._sock.recv(65535)
            except socket.timeout:
                pkt = None
            except OSError:
                break
            now = time.time()
            if pkt:
                self.stats["packets"] += 1
                self.stats["bytes"] += len(pkt)
                b_rate += len(pkt)
                if len(pkt) >= 28 and (pkt[0] >> 4) == 4 and pkt[9] == 17:
                    ihl = (pkt[0] & 0x0F) * 4
                    dst = pkt[16:20]
                    dport = struct.unpack("!H", pkt[ihl + 2:ihl + 4])[0]
                    payload = pkt[ihl + 8:]
                    try:
                        if (dst, dport) == lls_addr:
                            self.lls.feed(payload)
                        elif self.sls_addr and dport == self.sls_addr[1] and \
                                dst == socket.inet_aton(self.sls_addr[0]) and self.route:
                            self.route.feed(payload)
                    except Exception as e:
                        self.log(f"[capture] {e!r}")
            if now - t_rate >= 1.0:
                self.stats["rate_mbps"] = b_rate * 8 / (now - t_rate) / 1e6
                try:
                    self.stats["drops"] += struct.unpack("II", self._sock.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8))[1]
                except OSError:
                    pass
                t_rate, b_rate = now, 0
                if self.on_status:
                    self.on_status(dict(self.stats))
