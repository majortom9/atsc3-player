# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""ATSC 1.0 (8VSB) receive session: the full TS from dvr0, PSIP channels and
guide, and the selected virtual channel served as one MPEG-TS at /live.ts.

It offers the same surface the GUI uses from session.Session (lls.services,
esg.guide, now_next, service, video/audio, tracks, server), so the service
list and the Guide window work unchanged.
"""

import queue
import threading
import time
import types

from . import esg, lls, psip, sls, ts
from .streams import StreamServer

_FLUSH_PACKETS = 56                    # ~10 KB per queued chunk
_QUEUE_CHUNKS = 600                    # ~6 MB / ~2.5 s of 19.4 Mbit/s per client


def _pat_packet(program_number, pmt_pid, ts_id, cc):
    """One TS packet carrying a PAT with just this program."""
    body = bytes([0x00, 0xB0, 13]) + ts_id.to_bytes(2, "big") + bytes([0xC1, 0x00, 0x00]) \
        + program_number.to_bytes(2, "big") + (0xE000 | pmt_pid).to_bytes(2, "big")
    section = body + ts.crc32_mpeg(body).to_bytes(4, "big")
    payload = b"\x00" + section
    return bytes([0x47, 0x40, 0x00, 0x10 | (cc & 0x0F)]) + payload + b"\xff" * (184 - len(payload))


class Atsc1Session:
    single_stream = True                # one URL (/live.ts), audio inside

    def __init__(self, adapter, http_port=8080, lan=False, log=print,
                 on_services=None, on_tracks=None, on_status=None, on_guide=None):
        self.adapter, self.log = adapter, log
        self.on_services, self.on_tracks, self.on_status, self.on_guide = \
            on_services, on_tracks, on_status, on_guide
        self.stream_stop = threading.Event()
        self.mon = psip.PsipMonitor(on_channels=self._channels, on_epg=self._epg)
        self.asm = ts.SectionAssembler(self.mon.feed)
        self.lls = types.SimpleNamespace(services=[])          # Session-compatible
        self.esg = types.SimpleNamespace(guide=esg.Guide())
        self.broadband = None
        self.delivery = "broadcast"
        self.service = None
        self.tracks, self.video, self.audio, self.lang = [], None, None, None
        self.cache_dir = None
        self._out_pids = set()
        self._program = None
        self._pat_cc = 0
        self._clients = []
        self._clients_lock = threading.Lock()
        self._batch = bytearray()
        self._batch_n = 0
        self.stats = {"packets": 0, "bytes": 0, "drops": 0, "rate_mbps": 0.0}
        self._t_rate, self._b_rate = time.time(), 0
        self._last_epg = 0
        self.reader = None
        self.server = StreamServer(self, port=http_port, lan=lan)

    # -- lifecycle ---------------------------------------------------------------
    def start(self):
        self.reader = ts.DvrReader(self.adapter, self._packet, log=self.log)
        self.reader.start()

    def close(self):
        self.stream_stop.set()
        if self.reader:
            self.reader.close()
        self._end_clients()
        self.server.close()

    def set_lan(self, lan):
        if lan != self.server.lan:
            port = self.server.port
            self.server.close()
            self.server = StreamServer(self, port=port, lan=lan)

    # -- selection ---------------------------------------------------------------
    def select_service(self, service=None, sls_addr=None, lang=None):
        self.service, self.lang = service, lang
        self._program = None
        self._end_clients()
        self._apply()

    def set_language(self, lang):
        self.lang = lang
        self._pick_audio()
        self._notify_tracks()

    def _apply(self):
        """Set up output PIDs once the selected channel's PMT is known."""
        if not self.service:
            return
        p = self.mon.programs.get(self.service.service_id)
        if not p:
            self.video = self.audio = None
            self._notify_tracks()
            return
        if self._program is not p:
            self._program = p
            self._out_pids = {p.pmt_pid, p.pcr_pid} | {s.pid for s in p.streams}
            self.tracks = [sls.Track(tsi=s.pid, kind=s.kind, rep_id=str(s.pid), dst="", port=0,
                                     lang=s.lang, codecs=s.codec)
                           for s in p.streams if s.kind in ("video", "audio")]
            self.video = next((t for t in self.tracks if t.kind == "video"), None)
            self._pick_audio()
            self.log(f"[ATSC1] {self.service.channel} {self.service.name}: "
                     + ", ".join(f"{s.kind} {s.codec}{' ' + s.lang if s.lang else ''}" for s in p.streams))
            self._notify_tracks()

    def _pick_audio(self):
        audios = [t for t in self.tracks if t.kind == "audio"]
        self.audio = next((t for t in audios if self.lang and t.lang == self.lang), audios[0] if audios else None)

    def _notify_tracks(self):
        if self.on_tracks:
            self.on_tracks(self.tracks, self.video, self.audio, self.delivery)

    def current_tsi(self, track):
        return None                     # no /video.mp4 and /audio.mp4 for ATSC 1.0

    # -- packets ------------------------------------------------------------------
    def _packet(self, pkt):
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid in self.mon.wanted_pids():
            self.asm.feed(pid, pkt)
            if pid == 0 and self._program and self._clients:
                # replace the PAT with one that lists only our program
                self._emit(_pat_packet(self._program.number, self._program.pmt_pid, 1, self._pat_cc))
                self._pat_cc += 1
        if self.service and self._program is None and self.service.service_id in self.mon.programs:
            self._apply()
        if pid in self._out_pids and pid != 0 and self._clients:
            self._emit(pkt)
        now = time.time()
        self.stats["packets"] += 1
        self.stats["bytes"] += 188
        self._b_rate += 188
        if now - self._t_rate >= 1.0:
            self.stats["rate_mbps"] = self._b_rate * 8 / (now - self._t_rate) / 1e6
            self.stats["drops"] = self.reader.overflows if self.reader else 0
            self._t_rate, self._b_rate = now, 0
            if self.on_status:
                self.on_status(dict(self.stats))

    def _emit(self, pkt):
        self._batch += pkt
        self._batch_n += 1
        if self._batch_n >= _FLUSH_PACKETS:
            chunk, self._batch, self._batch_n = bytes(self._batch), bytearray(), 0
            with self._clients_lock:
                for q in self._clients:
                    if q.full():
                        try:
                            q.get_nowait()          # a stalled client loses its oldest data
                        except queue.Empty:
                            pass
                    q.put_nowait(chunk)

    def ts_subscribe(self):
        if not self._program:
            return None
        q = queue.Queue(maxsize=_QUEUE_CHUNKS)
        with self._clients_lock:
            self._clients.append(q)
        return q

    def ts_unsubscribe(self, q):
        with self._clients_lock:
            if q in self._clients:
                self._clients.remove(q)

    def _end_clients(self):
        with self._clients_lock:
            for q in self._clients:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self._clients = []

    # -- PSIP -> services and guide ---------------------------------------------
    def _channels(self, channels, programs):
        services = []
        for c in channels:
            services.append(lls.Service(service_id=c.program_number, major=c.major, minor=c.minor,
                                        name=c.name, category=1 if c.program_number in programs or
                                        not programs else 1, global_id=f"atsc1:{c.major}.{c.minor}",
                                        standard=1, extra={"source_id": c.source_id}))
        if not services and programs:                    # no VCT: fall back to the PAT
            services = [lls.Service(service_id=n, major=0, minor=n, name=f"Program {n}", category=1,
                                    standard=1, global_id=f"atsc1:prog{n}") for n in sorted(programs)]
        if [(s.service_id, s.name) for s in services] != [(s.service_id, s.name) for s in self.lls.services]:
            self.lls.services = services
            self._rebuild_guide()
            if self.on_services:
                self.on_services(services)
        if self.service and self._program is None:
            self._apply()

    def _epg(self):
        if time.time() - self._last_epg < 2:
            return
        self._last_epg = time.time()
        self._rebuild_guide()
        if self.on_guide:
            self.on_guide(self.esg.guide)

    def _rebuild_guide(self):
        g = esg.Guide()
        for s in self.lls.services:
            src = s.extra.get("source_id")
            if src is None:
                continue
            fid = f"atsc1:{src}"
            g.services[fid] = esg.GuideService(frag_id=fid, name=s.name, global_id=s.global_id,
                                              major=s.major, minor=s.minor)
            slots = []
            for e in self.mon.events.values():
                if e.source_id == src:
                    cid = f"{src}:{e.event_id}:{e.start}"
                    g.contents[cid] = esg.Programme(content_id=cid, title=e.title, description=e.description)
                    slots.append(esg.Slot(e.start, e.end, cid))
            g.schedule[fid] = slots
        self.esg.guide = g

    def now_next(self, service, now=None):
        g = self.esg.guide
        gs = g.service_for(service.global_id, service.major, service.minor) if service else None
        cur, nxt = g.now_next(gs, now)
        return g.programme(cur), cur, g.programme(nxt), nxt


def main():
    import argparse, signal, sys
    from . import tuner
    from .streams import lan_address
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description="ATSC 1.0: tune, list channels, serve one as /live.ts")
    ap.add_argument("freq_hz", type=int)
    ap.add_argument("--channel", help="virtual channel to serve, e.g. 29.1")
    ap.add_argument("--lan", action="store_true")
    ap.add_argument("--http-port", type=int, default=8080)
    args = ap.parse_args()
    found = tuner.find_atsc3_frontend()
    if not found:
        sys.exit("no ATSC 3.0/1.0 capable tuner found")
    a, f, name = found
    fe = tuner.Frontend(a, f)
    fe.tune_atsc1(args.freq_hz)
    if not fe.wait_lock(5):
        fe.close()
        sys.exit("no ATSC 1.0 lock")
    print(f"locked on {args.freq_hz} Hz ({name}): {fe.stats()}")
    got = threading.Event()
    s = Atsc1Session(a, http_port=args.http_port, lan=args.lan, on_services=lambda sv: got.set())
    s.start()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *x: stop.set())
    try:
        if not got.wait(10):
            print("no PSIP channels seen")
        for c in s.lls.services:
            pc, cs, pn, ns = s.now_next(c)
            print(f"  {c.channel:>6} {c.name:10} program {c.service_id}"
                  + (f"  now: {pc.title}" if pc else ""))
        if args.channel:
            svc = next((c for c in s.lls.services if c.channel == args.channel), None)
            if not svc:
                sys.exit(f"{args.channel} not found")
            s.select_service(svc)
            host = lan_address() if args.lan else "127.0.0.1"
            print(f"serving {svc.channel} {svc.name}:  mpv http://{host}:{args.http_port}/live.ts")
            stop.wait()
    finally:
        s.close()
        fe.close()


if __name__ == "__main__":
    main()
