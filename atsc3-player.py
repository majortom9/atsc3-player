#!/usr/bin/env python3
"""ATSC 3.0 ROUTE/DASH player: capture a service from an ALP interface (alp0),
play it in mpv-ac4 and/or serve it over HTTP. The tuner must already be locked
(atsc3-zap, updateDVB or atsc3-gui); see atsc3-gui.py for the all-in-one app.

    atsc3-player.py                         # WUTV's SLS address, as before
    atsc3-player.py alp0 239.255.29.1 5002  # a service by its SLS address
    atsc3-player.py --list                  # services in the SLT
    atsc3-player.py --service 5001 --lang spa --lan
"""

import argparse
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atsc3player.session import Session          # noqa: E402
from atsc3player.streams import lan_address      # noqa: E402
from atsc3player.mpv import Mpv                  # noqa: E402


def main():
    sys.stdout.reconfigure(line_buffering=True)      # live logs when redirected
    ap = argparse.ArgumentParser(description="ATSC 3.0 ROUTE-DASH player")
    ap.add_argument("interface", nargs="?", default="alp0")
    ap.add_argument("target_ip", nargs="?", default="239.255.29.1", help="SLS session address")
    ap.add_argument("port", nargs="?", type=int, default=5002, help="SLS session port")
    ap.add_argument("--service", type=int, help="service id from the SLT (instead of an address)")
    ap.add_argument("--lang", help="audio language, e.g. eng or spa (default: the main audio)")
    ap.add_argument("--list", action="store_true", help="print the SLT services and exit")
    ap.add_argument("--serve-only", action="store_true", help="no local mpv, only the HTTP streams")
    ap.add_argument("--lan", action="store_true",
                    help="serve the HTTP streams to other machines (implied by --serve-only)")
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--mpv", help="mpv binary (default: mpv-ac4 auto-detect)")
    args = ap.parse_args()

    services_ready = threading.Event()
    tracks_ready = threading.Event()

    def on_services(services):
        services_ready.set()

    def on_tracks(tracks, video, audio, delivery):
        if video and audio:
            tracks_ready.set()

    lan = args.lan or args.serve_only
    try:
        session = Session(args.interface, http_port=args.http_port, lan=lan,
                          on_services=on_services, on_tracks=on_tracks)
        session.start()
    except PermissionError:
        sys.exit("raw capture needs cap_net_raw: setcap cap_net_raw+ep on this python")
    except OSError as e:
        sys.exit(f"cannot capture on {args.interface}: {e}")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    player = None
    try:
        if args.list or args.service is not None:
            print("waiting for the SLT ...")
            while not services_ready.wait(0.5):
                if stop.is_set():
                    return
            services = session.lls.services
            if args.list:
                for s in services:
                    print(f"{s.service_id:6} {s.channel:>6}  {s.name:10} {s.category_name:12}"
                          f"{' protected' if s.protected else ''}  {s.sls_dst}:{s.sls_port}")
                return
            svc = next((s for s in services if s.service_id == args.service), None)
            if not svc:
                sys.exit(f"service {args.service} is not in the SLT")
            if svc.protected:
                sys.exit(f"service {args.service} ({svc.name}) is DRM protected")
            session.select_service(svc, lang=args.lang)
        else:
            session.select_service(sls_addr=(args.target_ip, args.port), lang=args.lang)

        print("waiting for the service signalling (S-TSID) and init segments ...")
        while not tracks_ready.wait(0.5):
            if stop.is_set():
                return
            if session.delivery == "broadband":
                sys.exit("this service is broadband-only: nothing of it is on the air")
        print(f"video TSI {session.video.tsi}, audio TSI {session.audio.tsi} ({session.audio.label})")

        host = lan_address() if lan else "127.0.0.1"
        v, a = session.server.urls(host)
        if lan:
            print(f"remote playback:\n    mpv {v} --audio-file={a}")
        if not args.serve_only:
            v, a = session.server.urls("127.0.0.1")
            player = Mpv(v, a, binary=args.mpv)
        while not stop.wait(0.5):
            if player and not player.running():
                break
    finally:
        if player:
            player.stop()
        session.close()
        print("stopped")


if __name__ == "__main__":
    main()
