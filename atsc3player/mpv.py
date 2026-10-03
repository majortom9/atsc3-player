# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""Find and run an mpv that can decode AC-4 (mpv-ac4), pointed at our streams."""

import json
import os
import shutil
import socket
import subprocess
import tempfile

_HOME = os.path.expanduser("~")
CANDIDATES = [
    # Raspberry Pi package (packaging/rpi/mpv-ac4): finds rpi-ffmpeg-ac4 by itself
    ("/usr/bin/mpv-ac4", None),
    # x86 build in ~/local (wiki section 9): needs jellyfin-ffmpeg's libraries
    (f"{_HOME}/local/mpv-ac4/bin/mpv", f"{_HOME}/local/jellyfin-ffmpeg-dev/lib"),
]


def find_mpv(configured=None):
    """(binary, LD_LIBRARY_PATH or None)."""
    if configured:
        return configured, None
    for binary, libdir in CANDIDATES:
        if os.access(binary, os.X_OK):
            return binary, libdir
    found = shutil.which("mpv-ac4") or shutil.which("mpv")
    return found, None


class Mpv:
    def __init__(self, video_url, audio_url, binary=None, extra_args=(), title="ATSC 3.0",
                 log=print, local_display=False):
        self.binary, libdir = find_mpv(binary)
        if not self.binary:
            raise FileNotFoundError("no mpv-ac4 / mpv found")
        env = os.environ.copy()
        if libdir:
            env["LD_LIBRARY_PATH"] = libdir + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        if local_display:
            # started from an ssh session: put the window on this machine's own
            # desktop, not the (far too slow) forwarded X display
            env["DISPLAY"] = local_display if isinstance(local_display, str) else ":0"
            env.pop("WAYLAND_DISPLAY", None)
            xauth = os.path.join(_HOME, ".Xauthority")
            if os.path.exists(xauth):
                env["XAUTHORITY"] = xauth
        self.ipc_path = os.path.join(tempfile.gettempdir(), f"atsc3-mpv-{os.getpid()}.sock")
        args = [self.binary, video_url] + ([f"--audio-file={audio_url}"] if audio_url else []) + [
                f"--input-ipc-server={self.ipc_path}", f"--title={title}",
                "--force-window=yes", "--keep-open=no", *extra_args]
        log("[mpv] " + " ".join(args))
        self.proc = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL)

    def running(self):
        return self.proc.poll() is None

    def command(self, *cmd):
        """mpv JSON IPC command; returns the 'data' field or None."""
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(1.0)
                s.connect(self.ipc_path)
                s.sendall((json.dumps({"command": list(cmd)}) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    buf += s.recv(65536)
                for line in buf.split(b"\n"):
                    if line.strip():
                        r = json.loads(line)
                        if "error" in r:
                            return r.get("data")
        except (OSError, ValueError):
            return None

    def get(self, prop):
        return self.command("get_property", prop)

    def stop(self):
        if self.running():
            self.command("quit")
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        try:
            os.unlink(self.ipc_path)
        except OSError:
            pass
