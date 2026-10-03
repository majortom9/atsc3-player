# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""Offline checks of the ATSC 1.0 PSIP/PAT/PMT parsers against the PSI packets
of 25 s of a live 8VSB multiplex (tests/fixtures/atsc1-581mhz-psip.ts).

Run: python3 -m tests.test_psip   (from the repo root)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from atsc3player import psip, ts  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "atsc1-581mhz-psip.ts")


def main():
    d = open(FIXTURE, "rb").read()
    mon = psip.PsipMonitor()
    asm = ts.SectionAssembler(mon.feed)
    for i in range(0, len(d) - 187, 188):
        p = d[i:i + 188]
        pid = ((p[1] & 0x1F) << 8) | p[2]
        if pid in mon.wanted_pids():
            asm.feed(pid, p)

    chans = {c.channel: c for c in mon.channels}
    print("channels:", ", ".join(f"{c.channel} {c.name}" for c in mon.channels))
    assert set(chans) == {"29.1", "29.2", "29.3", "49.1"}, chans
    assert chans["29.1"].name == "WUTVFOX"
    p = mon.programs[chans["29.1"].program_number]
    kinds = [(s.kind, s.codec, s.lang) for s in p.streams]
    print("29.1 streams:", kinds)
    assert ("video", "MPEG-2", "") in kinds and ("audio", "AC-3", "spa") in kinds
    assert mon.gps_utc == 18
    print(f"{len(mon.events)} events, {sum(1 for e in mon.events.values() if e.description)} with descriptions")
    assert len(mon.events) >= 60
    assert sum(1 for e in mon.events.values() if e.description) >= 60
    e = min(mon.events.values(), key=lambda e: e.start)
    assert e.end > e.start and e.title
    # a CRC'd section with one flipped bit must be rejected
    bad = []
    asm2 = ts.SectionAssembler(lambda pid, s: bad.append(s))
    pkt = bytearray(d[0:188])
    pkt[20] ^= 0x01
    asm2.feed(0, bytes(pkt))
    assert not bad
    print("OK")


if __name__ == "__main__":
    main()
