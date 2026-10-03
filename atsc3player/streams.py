# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""Turn cached ROUTE objects into live fMP4 streams, and serve them over HTTP.

Each track (video, audio) is one continuous byte stream: its init segment,
then every new media segment in TOI order. mpv plays /video.mp4 with
--audio-file=/audio.mp4 - locally (127.0.0.1) or from another machine.
"""

import functools
import http.server
import os
import socket
import socketserver
import struct
import threading
import time

from .route import init_path, segment_prefix

_SEGMENT_BOX_TYPES = {b"styp", b"sidx", b"moof", b"mdat", b"emsg", b"prft", b"free"}


def sanitize_segment(data):
    """None if the segment doesn't start with a valid box (a mid-object join or
    a lost first packet); zero-pad a short final mdat so the next segment stays
    aligned for a demuxer reading a pipe."""
    off = 0
    while off + 8 <= len(data):
        size, typ = struct.unpack(">I4s", data[off:off + 8])
        if typ not in _SEGMENT_BOX_TYPES or size < 8:
            return None
        off += size
    if off == 0:
        return None
    if off > len(data):
        return data + bytes(off - len(data))
    return data


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def track_stream(cache_dir, tsi, stop, live_join=True, log=print):
    init_file = init_path(cache_dir, tsi)
    while not stop.is_set():
        init = _read(init_file)
        if init and len(init) > 100:
            break
        time.sleep(0.1)
    if stop.is_set():
        return
    yield init

    prefix = segment_prefix(tsi)
    last = None
    while not stop.is_set():
        tois = []
        for f in os.listdir(cache_dir):
            if f.startswith(prefix) and f.endswith(".m4s"):
                try:
                    tois.append(int(f[len(prefix):-4]))
                except ValueError:
                    pass
        tois.sort()
        if last is None and live_join and tois:
            last = tois[-1] - 1
        for toi in tois:
            if last is not None and toi <= last:
                continue
            last = toi
            data = _read(os.path.join(cache_dir, f"{prefix}{toi}.m4s"))
            if not data or len(data) <= 100:
                continue
            data = sanitize_segment(data)
            if data is None:
                log(f"[Stream] skipped malformed segment TSI {tsi} TOI {toi}")
                continue
            yield data
        time.sleep(0.1)


class _Handler(http.server.BaseHTTPRequestHandler):
    timeout = 10            # a stalled client must not wedge its thread forever
    server_version = "atsc3-player"

    def do_GET(self):
        track = {"/video.mp4": "video", "/audio.mp4": "audio"}.get(self.path)
        app = self.server.app
        tsi = app.current_tsi(track) if track else None
        if tsi is None:
            self.send_error(404 if track is None else 503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4" if track == "video" else "audio/mp4")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        app.log(f"[HTTP] {self.client_address[0]} connected to {self.path}")
        try:
            for chunk in track_stream(app.cache_dir, tsi, app.stream_stop, log=app.log):
                self.wfile.write(chunk)
                self.wfile.flush()
                if app.current_tsi(track) != tsi:
                    break            # track changed (service / language switch)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        app.log(f"[HTTP] {self.client_address[0]} left {self.path}")

    def log_message(self, fmt, *args):
        pass


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class StreamServer:
    """HTTP server for /video.mp4 and /audio.mp4. `app` provides cache_dir,
    stream_stop (threading.Event), current_tsi(track) and log()."""

    def __init__(self, app, port=8080, lan=False):
        self.app, self.port, self.lan = app, port, lan
        self.httpd = _Server(("0.0.0.0" if lan else "127.0.0.1", port), _Handler)
        self.httpd.app = app
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def urls(self, host="127.0.0.1"):
        return (f"http://{host}:{self.port}/video.mp4", f"http://{host}:{self.port}/audio.mp4")


def lan_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))        # sends nothing; picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return socket.gethostname()
    finally:
        s.close()
