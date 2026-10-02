import socket
import struct
import sys
import os
import time
import threading
import http.server
import socketserver
import subprocess
import argparse
import functools
import signal

CACHE_DIR = "/tmp/atsc3_cache"
HTTP_PORT = 8080
SEGMENT_KEEP = 60
STOP = threading.Event()

# TSI 100 = HEVC video; TSI 200 = English/main AC-4 (ac-4.02.01.02, 10ch,
# manifest AdaptationSet id=1). TSI 201 is the Spanish stereo track.
TRACKS = {
    "video": ("video-init_tsi_100.mp4", "segment_tsi_100_toi_"),
    "audio": ("audio-init_tsi_200_toi_1.mp4", "segment_tsi_200_toi_"),
}

# ROUTE objects here are ~1MB DASH segments; anything claiming to start
# beyond this is a corrupt header, not a real offset.
_MAX_OBJECT_BYTES = 32 * 1024 * 1024


class RouteReassembler:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.objects = {}
        self.closed = {}
        self.last_seen = {}     # (tsi, toi) -> time of last packet appended
        self.init_logged = set()  # tsi's whose init capture we've already announced
        self.manifest_logged = False

    def _atomic_write(self, path, data):
        # The mpv-feed loop reads these files concurrently from another
        # thread while they're being (re)written. A plain open+write lets
        # the reader catch a partially-written file (e.g. a ~1MB segment
        # mid-write), handing mpv a truncated moof/mdat box whose declared
        # size doesn't match what's actually there - permanently desyncing
        # the demuxer's byte stream with no way to recover. Write to a temp
        # file and rename, which is atomic on the same filesystem: a reader
        # only ever sees the old (or nonexistent) file, or the complete one.
        tmp_path = path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)

    def _finalize_object(self, obj_key):
        if self.closed.get(obj_key):
            return
        self.closed[obj_key] = True

        tsi, toi = obj_key
        current_data = bytes(self.objects.get(obj_key, b""))
        self.objects.pop(obj_key, None)  # free the buffer, keep the closed/last_seen markers
        if not current_data:
            return

        # Universal DASH Manifest Sniffer
        if b"<MPD" in current_data:
            start_idx = current_data.find(b"<MPD")
            end_idx = current_data.find(b"</MPD>")

            if start_idx != -1 and end_idx != -1:
                end_idx += len(b"</MPD>")
                mpd_xml = current_data[start_idx:end_idx]

                bracket_idx = mpd_xml.find(b"<MPD")
                if bracket_idx > 0:
                    mpd_xml = mpd_xml[bracket_idx:]

                mpd_path = os.path.join(self.cache_dir, "manifest.mpd")
                if not os.path.exists(mpd_path) or os.path.getsize(mpd_path) != len(mpd_xml):
                    self._atomic_write(mpd_path, mpd_xml)
                    if not self.manifest_logged:
                        self.manifest_logged = True
                        print(f"[ROUTE] Extracted DASH Manifest (updates continue silently)")
        else:
            # Real init segments can be well under 1KB (e.g. a bare
            # HEVC moov with no sample entries beyond track setup was
            # observed at 788 bytes) - ftyp+moov together is already a
            # strong enough signal without an arbitrary size floor.
            is_init = (len(current_data) > 100 and b'ftyp' in current_data[:32]
                       and b'moov' in current_data)

            if is_init:
                # Init-shaped objects (e.g. TOI 1, resent on their own
                # carousel) must NOT also be saved as a regular segment -
                # they'd match the segment_*.m4s glob the playback loop
                # pipes to mpv and get delivered a second time, duplicating
                # the moov box mid-stream (mpv: "Found duplicated MOOV
                # Atom. Skipped it").
                if b'ac-4' in current_data:
                    init_path = os.path.join(self.cache_dir, f"audio-init_tsi_{tsi}_toi_{toi}.mp4")
                else:
                    init_path = os.path.join(self.cache_dir, f"video-init_tsi_{tsi}.mp4")

                self._atomic_write(init_path, current_data)
                if tsi not in self.init_logged:
                    self.init_logged.add(tsi)
                    print(f"[ROUTE] Captured init for TSI {tsi} (Size: {len(current_data)} bytes) - resends continue silently")
            else:
                filename = os.path.join(self.cache_dir, f"segment_tsi_{tsi}_toi_{toi}.m4s")
                self._atomic_write(filename, current_data)
                # TOIs advance by one per ~2s segment; keep ~2 minutes so a
                # long-running session doesn't grow the cache without bound.
                try:
                    os.remove(os.path.join(self.cache_dir, f"segment_tsi_{tsi}_toi_{toi - SEGMENT_KEEP}.m4s"))
                except FileNotFoundError:
                    pass

    def parse_lct_and_assemble(self, payload):
        if len(payload) < 16:
            return

        byte0 = payload[0]
        version = (byte0 >> 4) & 0x0F
        if version != 1:
            return

        # Fixed A/331-constrained LCT layout (matches libatsc3's
        # alc_rx_analyze_packet_a331_compliant): flags+codepoint (4B),
        # CCI (4B, offset 4), TSI (4B, offset 8), TOI (4B, offset 12).
        # HDR_LEN is in 32-bit words and must be read from the header,
        # not assumed, since LCT extensions (EXT_FDT etc.) push it past 16.
        hdr_len = payload[2] * 4
        if len(payload) < hdr_len + 4:
            return

        try:
            flag_b = payload[1] & 0x01
            tsi = struct.unpack('!I', payload[8:12])[0]
            toi = struct.unpack('!I', payload[12:16])[0]
            # ALC framing (RFC 5775) puts a 4-byte FEC Payload ID (SBN+ESI,
            # for Compact No-Code FEC) between the LCT header and the actual
            # encoding symbol/file data - confirmed via hex dump: every
            # reassembled object had 4 spurious zero bytes prepended,
            # exactly this field's size and typical zero value, which threw
            # off ISO-BMFF box parsing (moov unreachable) by 4 bytes.
            #
            # For ROUTE source flows (Compact No-Code FEC, A/331 A.3.5.1)
            # that field is start_offset: this packet's byte offset within
            # the object (verified live: 0, 1384, 2784, ... per TOI). An
            # earlier attempt treated it as a packet index and multiplied it
            # by 1400, which blew objects up to ~90MB; used as the byte
            # offset it is, a lost packet leaves a hole in place instead of
            # shifting everything after it - which fed the AC-4 decoder a
            # second or more of misaligned garbage and could leave it
            # producing a loud buzz until restarted.
            start_offset = struct.unpack('!I', payload[hdr_len:hdr_len + 4])[0]
            file_data = payload[hdr_len + 4:]
            if start_offset > _MAX_OBJECT_BYTES:
                return
            obj_key = (tsi, toi)
            now = time.time()

            # This broadcast never reliably signals object completion via
            # the B flag or an EXT_TOL length extension. Finalizing the
            # instant a new TOI is seen (the previous approach) turned out
            # to be actively harmful: minor UDP reordering routinely lets
            # the first packet of TOI N+1 arrive slightly before the last
            # packet(s) of TOI N, and closing on sight left objects a few
            # KB short of what their own mdat box declared - decodable
            # headers, garbage NAL data (confirmed via ffprobe trace: a
            # captured mdat claimed 992221 bytes, only ~964213 were ever
            # written). Instead, an object is only finalized once nothing
            # new has arrived for it for a grace window, tolerating
            # reordering across TOI boundaries; a resend cycle reusing the
            # same TOI still gets a fresh buffer via the same timeout.
            last_t = self.last_seen.get(obj_key)
            existing_buf = self.objects.get(obj_key)
            # 2026-09-25: a carousel-style retransmission of a small,
            # single-packet object (e.g. the video init segment) can repeat
            # far more often than the 5s reset threshold below - sometimes
            # every few milliseconds. Each retransmission carries the exact
            # same file_data from the start of the object, but the old code
            # only ever appended, never recognizing "this is the same object
            # starting over" - the buffer grew unbounded (784 bytes -> 75KB+
            # observed live) and last_seen kept refreshing, so it never went
            # stale enough to hit the grace-window finalize either. Detect a
            # restart directly: if this packet's data exactly matches what's
            # already at the start of the buffer, it's a fresh retransmission
            # of the same object, not a continuation - reset instead of append.
            is_retransmission_restart = (start_offset == 0 and existing_buf is not None
                                          and len(existing_buf) >= len(file_data)
                                          and bytes(existing_buf[:len(file_data)]) == file_data)
            # A retransmission restart IS the completion signal for this
            # object: a carousel resend starting over from byte 0 only makes
            # sense once the previous copy finished. This stream never sets
            # the LCT B flag and the grace-window loop below explicitly
            # skips key == obj_key (it only ever checks *other* TOIs on the
            # same TSI) - for a TSI that only ever produces this one TOI,
            # nothing else was ever going to finalize it. Finalize the
            # completed previous copy right here, before wiping the buffer
            # for the new one.
            if is_retransmission_restart and not self.closed.get(obj_key):
                try:
                    self._finalize_object(obj_key)
                except Exception as fe:
                    print(f"[DIAG] EXCEPTION in _finalize_object (retransmission restart) for key={obj_key}: {fe!r}")

            is_new_object = (obj_key not in self.objects or self.closed.get(obj_key)
                              or (last_t is not None and now - last_t > 5.0)
                              or is_retransmission_restart)
            if is_new_object:
                self.objects[obj_key] = bytearray()
                self.closed[obj_key] = False

            self.last_seen[obj_key] = now
            buf = self.objects[obj_key]
            end = start_offset + len(file_data)
            if end > len(buf):
                buf.extend(bytes(end - len(buf)))   # a lost packet before this one stays zero-filled
            buf[start_offset:end] = file_data

            _REORDER_GRACE_SECONDS = 3.0
            for key in list(self.objects.keys()):
                if key[0] != tsi or key == obj_key or self.closed.get(key):
                    continue
                age = now - self.last_seen.get(key, 0)
                if age > _REORDER_GRACE_SECONDS:
                    try:
                        self._finalize_object(key)
                    except Exception as fe:
                        print(f"[DIAG] EXCEPTION in _finalize_object for key={key}: {fe!r}")

            if flag_b:
                self._finalize_object(obj_key)

        except Exception as e:
            print(f"[DIAG] EXCEPTION in parse_lct_and_assemble: {e!r}")

class SmartDASHHandler(http.server.SimpleHTTPRequestHandler):
    # A stalled remote client must not be able to wedge its handler thread
    # (and with it, shutdown's join) forever.
    timeout = 10

    def _stream_track(self, track):
        client = self.client_address[0]
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4" if track == "video" else "audio/mp4")
        self.end_headers()
        print(f"[HTTP] {client} connected to /{track}.mp4", flush=True)
        try:
            for chunk in track_stream(track, live_join=True):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        if not STOP.is_set():
            print(f"[HTTP] {client} disconnected from /{track}.mp4", flush=True)

    def do_GET(self):
        if self.path in ("/video.mp4", "/audio.mp4"):
            self._stream_track(self.path[1:-4])
            return
        print(f"[HTTP Server] GET {self.path}")
        path_clean = self.path.lstrip('/')
        local_file = os.path.join(CACHE_DIR, path_clean)
        
        # Explicitly handle manifest.mpd with the correct DASH MIME type
        if path_clean == "manifest.mpd" and os.path.exists(local_file):
            with open(local_file, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/dash+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # If the requested media/init file doesn't exist yet, fallback gracefully
        if not os.path.exists(local_file):
            segments = sorted([f for f in os.listdir(CACHE_DIR) if f.endswith(('.m4s', '.mp4'))])
            if segments:
                fallback_path = os.path.join(CACHE_DIR, segments[0])
                with open(fallback_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            else:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
                
        super().do_GET()

    def end_headers(self):
        # Add headers to prevent caching issues during live streaming
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def log_message(self, format, *args):
        pass

_SEGMENT_BOX_TYPES = {b"styp", b"sidx", b"moof", b"mdat", b"emsg", b"prft", b"free"}


def sanitize_segment(data):
    # mpv reads these off a non-seekable pipe, where one bad box header
    # permanently desyncs lavf's mov demuxer. Two real cases seen on disk:
    # - joining mid-object: the first object captured is only the tail of a
    #   segment, starting with arbitrary mdat bytes that parse as a ~3GB box.
    #   lavf tries to skip it and swallows the rest of the stream, so stream
    #   probing never finishes (and --audio-file is never even opened).
    # - a lost packet: the final mdat is short of its declared size, so the
    #   demuxer eats the start of the NEXT segment as payload.
    # Drop the first kind; zero-pad the second so the next segment stays
    # aligned (the decoder just sees one damaged frame).
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


def write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _read_if_present(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def track_stream(track, live_join=False):
    # One continuous fMP4 byte stream for a track: its init segment, then
    # every new media segment in TOI order. Video and audio are separate
    # DASH representations with their own moov boxes, so each track needs
    # its own stream - a single demuxer can only follow one of them.
    # live_join starts at the newest segment instead of replaying the whole
    # cache, for clients connecting to an already-running session.
    init_name, prefix = TRACKS[track]
    init_path = os.path.join(CACHE_DIR, init_name)
    init = None
    while not STOP.is_set():
        init = _read_if_present(init_path)
        if init and len(init) > 100:
            break
        time.sleep(0.1)
    if STOP.is_set():
        return
    yield init

    last_toi = None
    while not STOP.is_set():
        tois = []
        for f in os.listdir(CACHE_DIR):
            if f.startswith(prefix) and f.endswith(".m4s"):
                try:
                    tois.append(int(f[len(prefix):-4]))
                except ValueError:
                    continue
        tois.sort()
        if last_toi is None and live_join and tois:
            last_toi = tois[-1] - 1
        for toi in tois:
            if last_toi is not None and toi <= last_toi:
                continue
            last_toi = toi
            data = _read_if_present(os.path.join(CACHE_DIR, f"{prefix}{toi}.m4s"))
            if data is None or len(data) <= 100:
                continue
            data = sanitize_segment(data)
            if data is None:
                print(f"[Stream] Skipped malformed {track} segment toi={toi}", flush=True)
                continue
            yield data
        time.sleep(0.1)


def audio_feed_loop(fifo_path):
    # Opening a FIFO for writing blocks until a reader (mpv's --audio-file)
    # opens its end, which only happens once mpv has finished opening the
    # main stream - so this runs on its own thread, off the video feed path.
    audio_fifo = os.open(fifo_path, os.O_WRONLY)
    try:
        for n, chunk in enumerate(track_stream("audio")):
            write_all(audio_fifo, chunk)
            if n == 0:
                print(f"[Stream] Piped audio init ({len(chunk)} bytes)", flush=True)
    except BrokenPipeError:
        pass
    finally:
        os.close(audio_fifo)


def shutdown(sock, httpd, mpv_process, audio_fifo_path, threads):
    STOP.set()
    httpd.shutdown()
    httpd.server_close()
    if mpv_process is not None:
        if mpv_process.poll() is not None:
            print(f"[+] mpv exited (code {mpv_process.returncode}), shutting down...", flush=True)
        else:
            mpv_process.terminate()
            try:
                mpv_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                mpv_process.kill()
                mpv_process.wait()
        try:
            mpv_process.stdin.close()
        except OSError:
            pass
    if audio_fifo_path and os.path.exists(audio_fifo_path):
        # If mpv never opened the FIFO, the audio thread is still blocked in
        # open(O_WRONLY); a momentary reader releases it so it can see STOP.
        try:
            os.close(os.open(audio_fifo_path, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass
    # Threads must be finished before the socket closes and before the
    # interpreter exits - a daemon thread still printing at shutdown aborts
    # Python with "could not acquire lock for <stdout>".
    for t in threads:
        t.join(timeout=2)
    sock.close()
    if audio_fifo_path and os.path.exists(audio_fifo_path):
        os.remove(audio_fifo_path)
    print("[+] Shut down cleanly.", flush=True)


def lan_address():
    # The hostname often isn't resolvable from other machines; the address
    # of the interface that routes off-box is. connect() on UDP sends nothing.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostname()
    finally:
        s.close()


class ThreadingReuseServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # Each /video.mp4 or /audio.mp4 client holds its connection open for as
    # long as it watches, so every request needs its own thread. Non-daemon
    # handler threads let server_close() join them during shutdown.
    allow_reuse_address = True
    daemon_threads = False

def main():
    parser = argparse.ArgumentParser(description="ATSC 3.0 ROUTE-DASH Player")
    parser.add_argument("interface", nargs="?", default="alp0")
    parser.add_argument("target_ip", nargs="?", default="239.255.29.1")
    parser.add_argument("port", nargs="?", type=int, default=5002)
    parser.add_argument("--serve-only", action="store_true",
                        help="don't open a local mpv; only serve /video.mp4 and /audio.mp4 over HTTP")
    args = parser.parse_args()

    # Clean stale segments/manifest on start, but keep any already-captured
    # init segment ("-init" files) - those only get rebroadcast on their own
    # slow carousel cycle, so wiping them forces a fresh wait every restart.
    if os.path.exists(CACHE_DIR):
        for f in os.listdir(CACHE_DIR):
            if '-init' in f:
                continue
            try:
                os.remove(os.path.join(CACHE_DIR, f))
            except:
                pass

    print(f"Initializing ATSC 3.0 ROUTE Pipeline on [{args.interface}]...")
    
    reassembler = RouteReassembler(CACHE_DIR)
    httpd = ThreadingReuseServer(("", HTTP_PORT), functools.partial(SmartDASHHandler, directory=CACHE_DIR))
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    host = lan_address()
    print(f"HTTP server on port {HTTP_PORT} - remote playback:")
    print(f"    mpv http://{host}:{HTTP_PORT}/video.mp4 --audio-file=http://{host}:{HTTP_PORT}/audio.mp4")

    # Bind raw socket
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
        sock.bind((args.interface, 0))
        # Widen the kernel receive buffer - the default is easily
        # overwhelmed by this packet rate if userspace (file I/O for every
        # finalized object, running in the same loop as recv()) falls
        # behind even briefly, silently dropping packets before Python
        # ever sees them.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    except PermissionError:
        print("Error: Raw sockets require root privileges or setcap.")
        sys.exit(1)

    print(f"Capturing ROUTE packets from {args.target_ip}:{args.port}...")
    print("Waiting for DASH Manifest (.mpd) to appear in stream...")

    # Short timeout so the capture thread can notice STOP instead of sitting
    # in recv() while main() closes the socket out from under it.
    sock.settimeout(0.5)

    def capture_loop():
        packet_count = 0
        matched_count = 0
        while not STOP.is_set():
            try:
                frame = sock.recv(65535)
                packet_count += 1
                if packet_count % 50000 == 0:
                    # PACKET_STATISTICS (struct tpacket_stats: tp_packets,
                    # tp_drops) - tp_drops is the kernel's own count of
                    # packets dropped because userspace didn't drain the
                    # socket fast enough, reset to 0 after each read.
                    # SOL_PACKET (263) / PACKET_STATISTICS (6) aren't
                    # exposed as socket module attributes on this build,
                    # so use the raw Linux constants directly.
                    stats = sock.getsockopt(263, 6, 8)
                    tp_packets, tp_drops = struct.unpack('II', stats)
                    print(f"[Socket] Frames: {packet_count} total, {matched_count} matching ROUTE... "
                          f"(kernel: {tp_packets} recvd, {tp_drops} DROPPED since last check)")

                if len(frame) < 20:
                    continue
                if ((frame[0] >> 4) & 0x0F) == 4:  # IPv4
                    ihl = (frame[0] & 0x0F) * 4
                    if frame[9] == 17:  # UDP
                        udp_header = frame[ihl:ihl+8]
                        _, dst_port, _, _ = struct.unpack('!HHHH', udp_header)
                        payload = frame[ihl+8:]
                        src_ip = socket.inet_ntoa(frame[12:16])
                        dst_ip = socket.inet_ntoa(frame[16:20])
                        
                        # Match target IP/port (or let it pass if port matches to see if IP format differs)
                        if dst_port == args.port:
                            matched_count += 1
                            if matched_count == 1:
                                print(f"\n[Socket] First matching packet! Src: {src_ip}, Dst: {dst_ip}, Port: {dst_port}, Size: {len(payload)}")
                            reassembler.parse_lct_and_assemble(payload)
            except socket.timeout:
                continue
            except Exception as e:
                if STOP.is_set():
                    break
                # A transient error here (e.g. a bad diagnostic call) used
                # to permanently kill capture via break, with everything
                # downstream silently going quiet rather than erroring
                # loudly - log it and keep going instead.
                print(f"\n[Capture Loop Error] {e}")

    # First Ctrl+C asks every loop to wind down through shutdown(); a second
    # one falls back to the default handler and kills the process outright.
    def request_stop(signum, frame):
        print("\n[+] Stopping...", flush=True)
        STOP.set()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    cap_thread = threading.Thread(target=capture_loop, daemon=True)
    cap_thread.start()

    # Wait until manifest.mpd is written to disk
    manifest_path = os.path.join(CACHE_DIR, "manifest.mpd")
    print("Waiting for DASH Manifest...")
    while not os.path.exists(manifest_path) and not STOP.is_set():
        time.sleep(0.5)

    mpv_process = None
    audio_fifo_path = None
    threads = [cap_thread]
    try:
        if STOP.is_set():
            return
        if args.serve_only:
            print("[+] Manifest detected! Serving over HTTP only (Ctrl+C to stop)...", flush=True)
        else:
            mpv_process, audio_fifo_path = start_local_player(threads)
            # The feed can block indefinitely in a pipe write while mpv is
            # slow to read, so it gets its own thread; main stays free to
            # notice STOP, and shutdown() terminating mpv breaks that write.
            feed_thread = threading.Thread(target=feed_video, args=(mpv_process,), daemon=True)
            feed_thread.start()
            threads.append(feed_thread)
        while not STOP.is_set():
            time.sleep(0.5)
    finally:
        shutdown(sock, httpd, mpv_process, audio_fifo_path, threads)


def start_local_player(threads):
    print(f"[+] Manifest detected! Streaming directly to mpv...")

    # A FIFO for the audio track, fed on its own thread the same way video
    # is fed into mpv's stdin - a single lavf demuxer instance can only
    # make sense of one track's fragments, so audio needs its own pipe
    # rather than being interleaved into the video stream.
    audio_fifo_path = os.path.join(CACHE_DIR, "audio_pipe")
    if os.path.exists(audio_fifo_path):
        os.remove(audio_fifo_path)
    os.mkfifo(audio_fifo_path)

    # Path to your custom mpv build with AC-4 support
    mpv_bin = os.path.expanduser("~/local/mpv-ac4/bin/mpv")
    
    # Set up environment pointing to both custom library locations
    custom_env = os.environ.copy()
    ffmpeg_lib = os.path.expanduser("~/local/jellyfin-ffmpeg-dev/lib")
    mpv_lib = os.path.expanduser("~/local/mpv-ac4/lib/x86_64-linux-gnu")
    custom_env["LD_LIBRARY_PATH"] = f"{ffmpeg_lib}:{mpv_lib}"

    # Launch mpv with the combined library path
    mpv_process = subprocess.Popen(
        [
            'stdbuf', '-oL', '-eL',
            mpv_bin,
            '--no-cache',
            '--untimed',
            '--demuxer=lavf',
            # A tiny probesize (32KB) with analyzeduration=0 removed the
            # time-based fallback and left only a byte budget too small to
            # find consistent frame boundaries in ~1MB, 4Mbps HEVC segments
            # - the opener thread sat polling indefinitely never satisfying
            # stream-info analysis despite MBs of real data being fed.
            # Let ffmpeg's own defaults handle probing instead.
            # Skip the default vo auto-probe cascade (gpu/drm, gpu-next,
            # vdpau all fail with permission/device errors on this box
            # before landing on xv anyway - confirmed via a static-file
            # test) - go straight to a driver known to work here.
            # 2026-09-25: xv uses hardware X-Video overlay, which doesn't
            # work over ssh -X (forwarded DISPLAY=localhost:N.0) - mpv ran
            # fine and kept consuming stdin, but never produced a visible
            # window. x11 is plain software rendering, slower but works
            # over X11 forwarding.
            '--vo=x11',
            f'--audio-file={audio_fifo_path}',
            '-v',
            '-'
        ],
        stdin=subprocess.PIPE,
        stdout=None,
        stderr=None,
        env=custom_env
    )

    audio_thread = threading.Thread(target=audio_feed_loop, args=(audio_fifo_path,), daemon=True)
    audio_thread.start()
    threads.append(audio_thread)

    # Quitting mpv ('q' or closing the window) ends the whole session.
    def watch_mpv():
        mpv_process.wait()
        STOP.set()
    threading.Thread(target=watch_mpv, daemon=True).start()

    return mpv_process, audio_fifo_path


def feed_video(mpv_process):
    total_bytes = 0
    last_log = time.time()
    try:
        for n, chunk in enumerate(track_stream("video")):
            mpv_process.stdin.write(chunk)
            mpv_process.stdin.flush()
            if n == 0:
                print(f"[Stream] Piped video init ({len(chunk)} bytes)", flush=True)
                continue
            total_bytes += len(chunk)
            if time.time() - last_log > 2.0:
                last_log = time.time()
                print(f"[Stream] fed {n} segments, {total_bytes} bytes total", flush=True)
    except BrokenPipeError:
        pass

if __name__ == "__main__":
    main()
