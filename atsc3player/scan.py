# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""Channel scan: try every US RF channel for ATSC 1.0 and ATSC 3.0 and collect
the virtual channels (PSIP) and services (SLT) found."""

import socket
import struct
import threading
import time

from . import lls, psip, ts

ATSC1_LOCK_S = 1.5
ATSC1_PSIP_S = 4.0
ATSC3_LOCK_S = 3.0
ATSC3_SLT_S = 6.0


def us_channels():
    """[(rf, Hz)] for US broadcast RF channels 2-36."""
    out = []
    for ch in range(2, 37):
        if ch <= 4:
            mhz = 57 + (ch - 2) * 6
        elif ch <= 6:
            mhz = 79 + (ch - 5) * 6
        elif ch <= 13:
            mhz = 177 + (ch - 7) * 6
        else:
            mhz = 473 + (ch - 14) * 6
        out.append((ch, mhz * 1_000_000))
    return out


def _atsc1_channels(adapter, stop):
    mon = psip.PsipMonitor()
    asm = ts.SectionAssembler(mon.feed)
    got = threading.Event()

    def sink(pkt):
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid in mon.wanted_pids():
            asm.feed(pid, pkt)
            if mon.channels and all(c.program_number in mon.programs for c in mon.channels):
                got.set()

    r = ts.DvrReader(adapter, sink)
    r.start()
    end = time.time() + ATSC1_PSIP_S
    while time.time() < end and not got.is_set() and not stop.is_set():
        got.wait(0.1)
    r.close()
    if mon.channels:
        return [{"major": c.major, "minor": c.minor, "name": c.name, "service_id": c.program_number}
                for c in mon.channels]
    return [{"major": 0, "minor": n, "name": f"Program {n}", "service_id": n} for n in sorted(mon.pat)]


def _atsc3_services(fe, stop):
    ifname = fe.set_alp_up(True)
    mon = lls.LlsMonitor()
    s = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(0x0800))
    try:
        s.bind((ifname, 0))
        s.settimeout(0.3)
        dst, port = socket.inet_aton(lls.LLS_ADDRESS[0]), lls.LLS_ADDRESS[1]
        end = time.time() + ATSC3_SLT_S
        while time.time() < end and not mon.services and not stop.is_set():
            try:
                pkt = s.recv(65535)
            except socket.timeout:
                continue
            if len(pkt) >= 28 and pkt[9] == 17 and pkt[16:20] == dst:
                ihl = (pkt[0] & 0x0F) * 4
                if struct.unpack("!H", pkt[ihl + 2:ihl + 4])[0] == port:
                    mon.feed(pkt[ihl + 8:])
    finally:
        s.close()
        fe.set_alp_up(False)
    return [{"major": v.major, "minor": v.minor, "name": v.name, "service_id": v.service_id,
             "category": v.category, "protected": v.protected} for v in mon.services]


def scan(fe, on_progress=None, stop=None, channels=None, log=print):
    """Scan with an open tuner.Frontend. Returns a list of channel dicts:
    {standard, rf, freq, major, minor, name, service_id, category, protected}."""
    stop = stop or threading.Event()
    found = []
    chans = channels or us_channels()
    for i, (rf, freq) in enumerate(chans):
        if stop.is_set():
            break
        if on_progress:
            on_progress(i, len(chans), rf, len(found))
        entries, std = [], None
        try:
            fe.tune_atsc1(freq)
            if fe.wait_lock(ATSC1_LOCK_S, stop):
                std, entries = 1, _atsc1_channels(fe.adapter, stop)
            else:
                fe.tune(freq, None)
                if fe.wait_lock(ATSC3_LOCK_S, stop):
                    std, entries = 3, _atsc3_services(fe, stop)
        except OSError as e:
            log(f"[scan] RF {rf}: {e}")
            continue
        if std:
            log(f"[scan] RF {rf} ({freq / 1e6:.0f} MHz): ATSC {'1.0' if std == 1 else '3.0'}, "
                f"{len(entries)} channels")
            for e in entries:
                e.update(standard=std, rf=rf, freq=freq)
                e.setdefault("category", 1)
                e.setdefault("protected", False)
                found.append(e)
    if on_progress:
        on_progress(len(chans), len(chans), None, len(found))
    found.sort(key=lambda e: (e["major"] or 999, e["minor"], e["standard"]))
    return found


def main():
    import argparse, sys
    from . import tuner
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description="Scan US RF channels 2-36 for ATSC 1.0 and 3.0")
    ap.add_argument("--from-rf", type=int, default=2)
    ap.add_argument("--to-rf", type=int, default=36)
    args = ap.parse_args()
    a, f, name = tuner.find_atsc3_frontend() or sys.exit("no tuner")
    chans = [(rf, hz) for rf, hz in us_channels() if args.from_rf <= rf <= args.to_rf]
    t0 = time.time()
    with tuner.Frontend(a, f) as fe:
        res = scan(fe, channels=chans,
                   on_progress=lambda i, n, rf, k: rf and print(f"  RF {rf} ({i + 1}/{n}) ...", end="\r"))
    print(f"\nscan took {time.time() - t0:.0f} s")
    for e in res:
        tag = "3.0" if e["standard"] == 3 else "1.0"
        note = " (DRM)" if e["protected"] else "" if e["category"] == 1 else f" (category {e['category']})"
        print(f"  {e['major']}.{e['minor']:<3} {e['name']:10} ATSC {tag}  RF {e['rf']}{note}")


if __name__ == "__main__":
    main()
