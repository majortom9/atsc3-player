# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""Broadband (internet-delivered) ATSC 3.0 services.

Some services are announced in the broadcast but carried over the internet:
their USBD says UnicastAppService, there is no S-TSID, and their DASH MPD
(still delivered over the air, refreshed every couple of seconds) has an
https BaseURL. This module reads that MPD, picks a video Representation (the
highest up to a height limit) and the audio in the wanted language, downloads
the init and media segments as they appear, and stores them in the cache under
stand-in TSI numbers - so streams.track_stream / the HTTP server / mpv treat
them exactly like broadcast segments.
"""

import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .route import atomic_write, init_path, segment_prefix
from .sls import Track

VIDEO_TSI, AUDIO_TSI = 9000, 9001          # stand-in TSIs, never used on the air
SEGMENT_KEEP = 60
_USER_AGENT = "atsc3-player"


def _local(tag):
    return tag.rsplit("}", 1)[-1]


@dataclass
class Rep:
    rep_id: str
    kind: str
    lang: str
    codecs: str
    bandwidth: int
    height: int
    base: str                         # absolute base URL for this representation
    init: str                         # initialization template
    media: str                        # media template
    timescale: int
    start_number: int
    timeline: list = field(default_factory=list)   # [(t, d)] expanded
    duration: int = 0                 # $Number$ templates without a timeline


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _base_url(el, parent):
    b = _children(el, "BaseURL")
    return urllib.parse.urljoin(parent, b[0].text.strip()) if b and b[0].text else parent


def parse_mpd(xml_bytes, mpd_url=""):
    """MPD -> [Rep] for the first Period (all a live ATSC 3.0 MPD has)."""
    root = ET.fromstring(xml_bytes)
    base = _base_url(root, mpd_url)
    period = next((p for p in root if _local(p.tag) == "Period"), None)
    if period is None:
        return []
    base = _base_url(period, base)
    reps = []
    for aset in _children(period, "AdaptationSet"):
        a_base = _base_url(aset, base)
        kind = aset.get("contentType") or (aset.get("mimeType") or "").split("/")[0]
        lang = aset.get("lang", "")
        a_tmpl = (_children(aset, "SegmentTemplate") or [None])[0]
        for rep in _children(aset, "Representation"):
            tmpl = (_children(rep, "SegmentTemplate") or [a_tmpl])[0]
            if tmpl is None:
                continue
            timeline = []
            tl = _children(tmpl, "SegmentTimeline")
            if tl:
                t = 0
                for s in _children(tl[0], "S"):
                    t = int(s.get("t", t))
                    d = int(s.get("d"))
                    for _ in range(int(s.get("r", 0)) + 1):
                        timeline.append((t, d))
                        t += d
            reps.append(Rep(
                rep_id=rep.get("id"), kind=kind, lang=lang,
                codecs=rep.get("codecs") or aset.get("codecs") or "",
                bandwidth=int(rep.get("bandwidth", 0)), height=int(rep.get("height", 0) or 0),
                base=_base_url(rep, a_base), init=tmpl.get("initialization", ""),
                media=tmpl.get("media", ""), timescale=int(tmpl.get("timescale", 1)),
                start_number=int(tmpl.get("startNumber", 1)), timeline=timeline,
                duration=int(tmpl.get("duration", 0) or 0)))
    return reps


def fill(template, rep, time_=None, number=None):
    def sub(m):
        name, fmt = m.group(1), m.group(2)
        val = {"RepresentationID": rep.rep_id, "Bandwidth": rep.bandwidth,
               "Time": time_, "Number": number}.get(name)
        if val is None:
            return m.group(0)
        return (fmt % val) if fmt else str(val)
    return re.sub(r"\$(RepresentationID|Bandwidth|Time|Number)(%0\d+d)?\$", sub, template).replace("$$", "$")


def pick(reps, lang=None, max_height=1080):
    videos = [r for r in reps if r.kind == "video"]
    fitting = [r for r in videos if r.height <= max_height] or videos
    video = max(fitting, key=lambda r: (r.height, r.bandwidth), default=None)
    audios = [r for r in reps if r.kind == "audio"]
    audio = next((r for r in audios if lang and r.lang == lang), None) or (audios[0] if audios else None)
    return video, audio


def tracks_for(reps):
    """Audio/video Representations as sls.Track-like rows for the UI."""
    out = []
    for r in reps:
        if r.kind in ("video", "audio"):
            out.append(Track(tsi=0, kind=r.kind, rep_id=r.rep_id, dst="", port=0,
                             lang=r.lang, codecs=r.codecs,
                             role=f"{r.height}p" if r.kind == "video" and r.height else ""))
    return out


class _Fetcher(threading.Thread):
    """Download one Representation's init + live segments into the cache."""

    def __init__(self, owner, tsi, rep, log):
        super().__init__(daemon=True, name=f"broadband-{tsi}")
        self.owner, self.tsi, self.rep, self.log = owner, tsi, rep, log
        self.stop = threading.Event()
        self.last_time = None

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read()

    def run(self):
        cache = self.owner.cache_dir
        init_url = urllib.parse.urljoin(self.rep.base, fill(self.rep.init, self.rep))
        while not self.stop.is_set():
            try:
                atomic_write(init_path(cache, self.tsi), self._get(init_url))
                self.log(f"[Broadband] {self.rep.kind} {self.rep.rep_id} init from {init_url}")
                break
            except (urllib.error.URLError, OSError) as e:
                self.log(f"[Broadband] init {init_url}: {e}")
                self.stop.wait(3)
        while not self.stop.is_set():
            rep = self.owner.latest(self.rep.rep_id) or self.rep
            todo = []
            if rep.timeline:
                times = [t for t, _ in rep.timeline]
                # the newest entry may not be on the server yet: start one back
                if self.last_time is None and len(times) >= 2:
                    self.last_time = times[-3] if len(times) >= 3 else times[-2] - 1
                todo = [(t, None) for t in times[:-1] if self.last_time is None or t > self.last_time]
            for t, n in todo:
                if self.stop.is_set():
                    return
                url = urllib.parse.urljoin(rep.base, fill(rep.media, rep, time_=t, number=n))
                try:
                    data = self._get(url)
                except (urllib.error.URLError, OSError) as e:
                    self.log(f"[Broadband] {url}: {e}")
                    break                  # retry on the next pass
                atomic_write(os.path.join(cache, f"{segment_prefix(self.tsi)}{t}.m4s"), data)
                self.last_time = t
                self._prune(cache)
            self.stop.wait(0.5)

    def _prune(self, cache):
        prefix = segment_prefix(self.tsi)
        files = sorted((int(f[len(prefix):-4]), f) for f in os.listdir(cache)
                       if f.startswith(prefix) and f.endswith(".m4s"))
        for _, f in files[:-SEGMENT_KEEP]:
            try:
                os.remove(os.path.join(cache, f))
            except OSError:
                pass


class BroadbandService:
    """Keeps the newest MPD and two fetchers (video, audio) running."""

    def __init__(self, cache_dir, log=print, max_height=1080):
        self.cache_dir, self.log, self.max_height = cache_dir, log, max_height
        self.reps = []
        self.video = self.audio = None
        self.fetchers = []
        self.lock = threading.Lock()

    def update_mpd(self, mpd_xml):
        reps = parse_mpd(mpd_xml)
        with self.lock:
            self.reps = reps
        return reps

    def latest(self, rep_id):
        with self.lock:
            return next((r for r in self.reps if r.rep_id == rep_id), None)

    def start(self, lang=None):
        self.stop()
        with self.lock:
            self.video, self.audio = pick(self.reps, lang, self.max_height)
        for tsi, rep in ((VIDEO_TSI, self.video), (AUDIO_TSI, self.audio)):
            if rep:
                f = _Fetcher(self, tsi, rep, self.log)
                f.start()
                self.fetchers.append(f)
        if self.video:
            self.log(f"[Broadband] video {self.video.rep_id} {self.video.height}p "
                     f"{self.video.bandwidth // 1000} kbit/s; audio {self.audio.rep_id if self.audio else '-'} "
                     f"{self.audio.lang if self.audio else ''}")

    def stop(self):
        for f in self.fetchers:
            f.stop.set()
        self.fetchers = []
