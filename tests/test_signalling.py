"""Offline checks of the LLS/SLS parsers against 40 s of real signalling
captured from a live ATSC 3.0 multiplex (tests/fixtures/live-485mhz.pkl:
LLS UDP payloads and the TSI-0 ALC packets of every service).

Run: python3 -m tests.test_signalling   (from the repo root)
"""

import collections
import os
import pickle
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from atsc3player import lls, sls  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "live-485mhz.pkl")


def sls_bundles(packets):
    """{(dst, port): bundle bytes} - reassemble TSI-0 objects by start_offset."""
    objs = collections.defaultdict(dict)
    for dst, port, p in packets:
        hdr = p[2] * 4
        toi = struct.unpack("!I", p[12:16])[0]
        off = struct.unpack("!I", p[hdr:hdr + 4])[0]
        objs[(dst, port, toi)][off] = p[hdr + 4:]
    bundles = {}
    for (dst, port, toi), frags in objs.items():
        data = b"".join(v for _, v in sorted(frags.items()))
        if toi and sls.is_bundle(data):
            bundles[(dst, port)] = data
    return bundles


def main():
    f = pickle.load(open(FIXTURE, "rb"))

    mon = lls.LlsMonitor()
    for p in f["lls"]:
        mon.feed(p)
    services = mon.services
    print(f"SLT bsid {mon.bsid} version {mon.slt_version}: {len(services)} services")
    for s in services:
        print(f"  {s.service_id} {s.channel:>6} {s.name:8} {s.category_name:12} "
              f"{'PROTECTED ' if s.protected else ''}{'playable ' if s.playable else ''}"
              f"{s.sls_dst}:{s.sls_port}")
    assert len(services) == 10, len(services)
    by_id = {s.service_id: s for s in services}
    assert by_id[5002].name == "WUTV" and by_id[5002].playable
    assert by_id[5003].protected and not by_id[5003].playable

    bundles = sls_bundles(f["sls"])
    print(f"\n{len(bundles)} SLS bundles")
    for s in services:
        b = bundles.get((s.sls_dst, s.sls_port))
        if not b:
            print(f"  {s.service_id} {s.name}: no bundle in the capture")
            continue
        parts = sls.find_parts(sls.split_bundle(b))
        how = sls.delivery(parts.get("usbd"))
        if "stsid" not in parts:
            print(f"  {s.service_id} {s.name}: {how or 'no USBD'}, no S-TSID ({list(parts)})")
            if s.service_id in (5006, 5009):
                assert how == "broadband", how
            continue
        tracks = sls.resolve_tracks(parts["stsid"], parts.get("mpd"), s.sls_dst, s.sls_port)
        v, a = sls.pick_tracks(tracks)
        print(f"  {s.service_id} {s.name}: " + ", ".join(
            f"tsi {t.tsi} {t.label} init toi {t.init_toi} '{t.file_template}'" for t in tracks))
        print(f"      -> video tsi {v.tsi if v else None}, audio tsi {a.tsi if a else None}")
        if s.service_id == 5002:
            v, a = sls.pick_tracks(tracks)
            assert (v.tsi, a.tsi, a.lang) == (100, 200, "eng"), (v, a)
            v, a = sls.pick_tracks(tracks, "spa")
            assert a.lang == "spa" and a.file_template.startswith("a1-a13_3-"), a
            assert sls.segment_toi(a.file_template, "a1-a13_3-894563441.m4s") == 894563441
    print("\nOK")


if __name__ == "__main__":
    main()
