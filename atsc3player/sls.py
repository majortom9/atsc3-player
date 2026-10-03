# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""ROUTE Service Layer Signalling (A/331 section 7): S-TSID + MPD -> tracks.

A ROUTE service's SLS travels on TSI 0 of its SLS session (the address in the
SLT). TOI 0 there is an EFDT naming the bundle; the bundle is a MIME envelope
(often multipart/signed around multipart/related) holding the USBD, the
S-TSID and the DASH MPD. The S-TSID says which transport session (TSI)
carries which DASH Representation and how its objects are named; the MPD
gives each Representation's language and codecs.
"""

import email
import email.policy
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class Track:
    tsi: int
    kind: str              # "video", "audio", "text", ...
    rep_id: str
    dst: str               # transport session address (default: the SLS session)
    port: int
    init_toi: int = None
    init_name: str = ""
    file_template: str = ""
    lang: str = ""
    codecs: str = ""
    role: str = ""

    @property
    def label(self):
        bits = [self.kind]
        if self.lang:
            bits.append(self.lang)
        if self.role and self.role != "main":
            bits.append(self.role)
        if self.codecs:
            bits.append(self.codecs.split(".")[0])
        return " ".join(bits)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _attr(el, name):
    """Attribute by local name, ignoring any namespace prefix."""
    for k, v in el.attrib.items():
        if k.rsplit("}", 1)[-1] == name:
            return v
    return None


def is_bundle(data):
    head = data[:400].lower()
    return b"mime-version" in head or b"multipart/" in head


def split_bundle(data):
    """MIME SLS bundle -> {Content-Location: bytes}. Signatures are not verified."""
    msg = email.message_from_bytes(data, policy=email.policy.compat32)
    parts = {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        loc = part.get("Content-Location")
        if loc:
            parts[loc] = part.get_payload(decode=True) or b""
    return parts


def find_parts(parts):
    """Pick the S-TSID, MPD and USBD out of a split bundle, by content."""
    found = {}
    for name, body in parts.items():
        if b"<S-TSID" in body:
            found["stsid"] = body
        elif b"<MPD" in body:
            found["mpd"] = body
        elif b"BundleDescriptionROUTE" in body or b"UserServiceDescription" in body:
            found["usbd"] = body
    return found


def delivery(usbd_xml):
    """'broadcast', 'broadband' or 'both', from the USBD's DeliveryMethod.
    Broadband-only services (UnicastAppService, an https BaseURL in the MPD)
    carry no S-TSID: nothing of them is on the air."""
    if not usbd_xml:
        return ""
    bc = b"BroadcastAppService" in usbd_xml
    bb = b"UnicastAppService" in usbd_xml
    return "both" if bc and bb else "broadcast" if bc else "broadband" if bb else ""


def parse_stsid(xml_bytes, sls_dst, sls_port):
    """S-TSID -> [Track] (kind/lang filled in later from the MPD)."""
    root = ET.fromstring(xml_bytes)
    tracks = []
    for rs in root:
        if _local(rs.tag) != "RS":
            continue
        dst = _attr(rs, "dIpAddr") or sls_dst
        port = int(_attr(rs, "dport") or sls_port)
        for ls in rs:
            if _local(ls.tag) != "LS":
                continue
            t = Track(tsi=int(_attr(ls, "tsi")), kind="", rep_id="", dst=dst, port=port)
            for el in ls.iter():
                name = _local(el.tag)
                if name == "FDT-Instance":
                    t.file_template = _attr(el, "fileTemplate") or ""
                elif name == "File" and t.init_toi is None:
                    t.init_toi = int(_attr(el, "TOI"))
                    t.init_name = _attr(el, "Content-Location") or ""
                elif name == "MediaInfo":
                    t.kind = _attr(el, "contentType") or ""
                    t.rep_id = _attr(el, "repId") or ""
            tracks.append(t)
    return tracks


def parse_mpd(xml_bytes):
    """MPD -> {repId: {kind, lang, codecs, role}}."""
    root = ET.fromstring(xml_bytes)
    reps = {}
    for aset in root.iter():
        if _local(aset.tag) != "AdaptationSet":
            continue
        kind = _attr(aset, "contentType") or (_attr(aset, "mimeType") or "").split("/")[0]
        lang = _attr(aset, "lang") or ""
        role = ""
        for el in aset:
            if _local(el.tag) == "Role":
                role = _attr(el, "value") or ""
        for rep in aset:
            if _local(rep.tag) == "Representation":
                reps[_attr(rep, "id")] = {
                    "kind": kind, "lang": lang, "role": role,
                    "codecs": _attr(rep, "codecs") or _attr(aset, "codecs") or "",
                }
    return reps


def resolve_tracks(stsid_xml, mpd_xml, sls_dst, sls_port):
    tracks = parse_stsid(stsid_xml, sls_dst, sls_port)
    reps = parse_mpd(mpd_xml) if mpd_xml else {}
    for t in tracks:
        r = reps.get(t.rep_id)
        if r:
            t.kind = t.kind or r["kind"]
            t.lang, t.codecs, t.role = r["lang"], r["codecs"], r["role"]
    return tracks


def pick_tracks(tracks, lang=None):
    """(video, audio) for playback: the main video and the audio in `lang`
    (falling back to the main/first audio)."""
    video = next((t for t in tracks if t.kind == "video"), None)
    audios = [t for t in tracks if t.kind == "audio"]
    audio = None
    if lang:
        audio = next((t for t in audios if t.lang == lang), None)
    if audio is None:
        audio = next((t for t in audios if t.role == "main"), audios[0] if audios else None)
    return video, audio


def segment_toi(file_template, filename):
    """TOI from an object name made with the S-TSID fileTemplate ($TOI$)."""
    pat = re.escape(file_template).replace(re.escape("$TOI$"), r"(\d+)")
    m = re.fullmatch(pat, filename)
    return int(m.group(1)) if m else None
