"""DVBv5 frontend control for ATSC 3.0 tuning (a Python port of atsc3-zap).

Tunes a frontend with the DVBv5 property API, reports lock and signal
statistics, and brings the adapter's ALP network interface up once locked
(and down again on close), which is what makes the demod start delivering
IP traffic on alp0.

The ctypes structures follow <linux/dvb/frontend.h>, so struct sizes - and
with them the ioctl numbers - come out right on 64-bit userland and on a
32-bit userland running on a 64-bit kernel (the Raspberry Pi setup).
"""

import ctypes
import fcntl
import glob
import os
import socket
import struct

# --- <linux/dvb/frontend.h> -------------------------------------------------

DTV_TUNE = 1
DTV_CLEAR = 2
DTV_FREQUENCY = 3
DTV_BANDWIDTH_HZ = 5
DTV_DELIVERY_SYSTEM = 17
DTV_STREAM_ID = 42
DTV_ENUM_DELSYS = 44
DTV_STAT_SIGNAL_STRENGTH = 62
DTV_STAT_CNR = 63
DTV_STAT_POST_ERROR_BIT_COUNT = 66

FE_HAS_SIGNAL = 0x01
FE_HAS_CARRIER = 0x02
FE_HAS_LOCK = 0x10

FE_SCALE_DECIBEL = 1
FE_SCALE_COUNTER = 3

SYS_ATSC = 11
# SYS_ATSC3 is a local enum value in this project's kernels (after SYS_DCII),
# not upstream: 21 here, 35 in koreapyj's out-of-tree compat header.
SYS_ATSC3_DEFAULT = 21

NO_STREAM_ID_FILTER = 0xFFFFFFFF


class _DtvStats(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("scale", ctypes.c_uint8), ("value", ctypes.c_int64)]


class _DtvFeStats(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("len", ctypes.c_uint8), ("stat", _DtvStats * 4)]


class _DtvBuffer(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("data", ctypes.c_uint8 * 32), ("len", ctypes.c_uint32),
                ("reserved1", ctypes.c_uint32 * 3), ("reserved2", ctypes.c_void_p)]


class _DtvU(ctypes.Union):
    _pack_ = 1
    _fields_ = [("data", ctypes.c_uint32), ("st", _DtvFeStats), ("buffer", _DtvBuffer)]


class _DtvProperty(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("cmd", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 3),
                ("u", _DtvU), ("result", ctypes.c_int)]


class _DtvProperties(ctypes.Structure):
    _fields_ = [("num", ctypes.c_uint32), ("props", ctypes.POINTER(_DtvProperty))]


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("o") << 8) | nr


_IOC_WRITE, _IOC_READ = 1, 2
FE_GET_INFO = _ioc(_IOC_READ, 61, 168)          # struct dvb_frontend_info
FE_READ_STATUS = _ioc(_IOC_READ, 69, 4)
FE_SET_PROPERTY = _ioc(_IOC_WRITE, 82, ctypes.sizeof(_DtvProperties))
FE_GET_PROPERTY = _ioc(_IOC_READ, 83, ctypes.sizeof(_DtvProperties))

SIOCGIFFLAGS, SIOCSIFFLAGS, IFF_UP = 0x8913, 0x8914, 0x1


def pack_plps(plps):
    """PLP IDs -> DTV_STREAM_ID: one ID per byte, unused slots 0xFF (as atsc3-zap)."""
    if not plps:
        return NO_STREAM_ID_FILTER
    if len(plps) > 4 or any(not 0 <= p <= 63 for p in plps):
        raise ValueError("up to 4 PLP IDs, each 0-63")
    value = 0
    for slot in range(4):
        value |= (plps[slot] if slot < len(plps) else 0xFF) << (slot * 8)
    return value


def frontends():
    """[(adapter, frontend, name)] for every DVB frontend on the system."""
    found = []
    for path in sorted(glob.glob("/dev/dvb/adapter*/frontend*")):
        a = int(path.split("adapter")[1].split("/")[0])
        f = int(path.rsplit("frontend", 1)[1])
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                info = bytearray(168)
                fcntl.ioctl(fd, FE_GET_INFO, info)
                name = info[:128].split(b"\0", 1)[0].decode(errors="replace")
            finally:
                os.close(fd)
        except OSError as e:
            name = f"<{e.strerror}>"
        found.append((a, f, name))
    return found


def find_atsc3_frontend():
    """The first frontend that offers SYS_ATSC3 (the HDTV Mate's CXD2878)."""
    for a, f, name in frontends():
        try:
            with Frontend(a, f, readonly=True) as fe:
                if fe.supports_atsc3():
                    return a, f, name
        except OSError:
            continue
    return None


class Frontend:
    def __init__(self, adapter=0, frontend=0, readonly=False, sys_atsc3=SYS_ATSC3_DEFAULT):
        self.adapter, self.frontend, self.sys_atsc3 = adapter, frontend, sys_atsc3
        self.path = f"/dev/dvb/adapter{adapter}/frontend{frontend}"
        self.fd = os.open(self.path, (os.O_RDONLY if readonly else os.O_RDWR) | os.O_NONBLOCK)
        self.alp_ifname = None
        self.alp_raised = False

    # -- property helpers ---------------------------------------------------
    def _props(self, cmds):
        arr = (_DtvProperty * len(cmds))()
        for i, (cmd, data) in enumerate(cmds):
            arr[i].cmd = cmd
            arr[i].u.data = data
        return arr, _DtvProperties(len(cmds), ctypes.cast(arr, ctypes.POINTER(_DtvProperty)))

    def _set(self, cmds):
        arr, props = self._props(cmds)
        fcntl.ioctl(self.fd, FE_SET_PROPERTY, props)

    def _get(self, cmds):
        arr, props = self._props([(c, 0) for c in cmds])
        fcntl.ioctl(self.fd, FE_GET_PROPERTY, props)
        return arr

    # -- API -----------------------------------------------------------------
    def info_name(self):
        info = bytearray(168)
        fcntl.ioctl(self.fd, FE_GET_INFO, info)
        return info[:128].split(b"\0", 1)[0].decode(errors="replace")

    def delivery_systems(self):
        p = self._get([DTV_ENUM_DELSYS])[0]
        return list(p.u.buffer.data[:p.u.buffer.len])

    def supports_atsc3(self):
        return self.sys_atsc3 in self.delivery_systems()

    def tune(self, freq_hz, plps=None, bandwidth_hz=6000000):
        """Start an ATSC 3.0 tune. plps: None/[] = auto, else up to 4 PLP IDs."""
        self._set([(DTV_CLEAR, 0)])
        cmds = [(DTV_DELIVERY_SYSTEM, self.sys_atsc3), (DTV_FREQUENCY, int(freq_hz)),
                (DTV_BANDWIDTH_HZ, bandwidth_hz)]
        stream_id = pack_plps(plps)
        if stream_id != NO_STREAM_ID_FILTER:
            cmds.append((DTV_STREAM_ID, stream_id))
        cmds.append((DTV_TUNE, 0))
        self._set(cmds)

    def status(self):
        buf = bytearray(4)
        fcntl.ioctl(self.fd, FE_READ_STATUS, buf)
        return struct.unpack("I", buf)[0]

    def locked(self):
        return bool(self.status() & FE_HAS_LOCK)

    def stats(self):
        """{'strength_dbm', 'cnr_db', 'post_ber'} - read on demand only: each read is
        a burst of I2C traffic over the same USB link that carries the stream."""
        arr = self._get([DTV_STAT_SIGNAL_STRENGTH, DTV_STAT_CNR, DTV_STAT_POST_ERROR_BIT_COUNT])
        out = {}
        for key, p, scale, div in (("strength_dbm", arr[0], FE_SCALE_DECIBEL, 1000.0),
                                   ("cnr_db", arr[1], FE_SCALE_DECIBEL, 1000.0),
                                   ("post_ber", arr[2], FE_SCALE_COUNTER, None)):
            st = p.u.st
            if st.len > 0 and st.stat[0].scale == scale:
                v = st.stat[0].value
                out[key] = v / div if div else v
        return out

    # -- ALP network interface ---------------------------------------------
    def find_alp_iface(self):
        """The alp* netdev on the same USB interface as this frontend, else the first alp*."""
        try:
            fe_dev = os.path.realpath(f"/sys/class/dvb/dvb{self.adapter}.frontend{self.frontend}/device")
        except OSError:
            fe_dev = None
        candidates = sorted(n for n in os.listdir("/sys/class/net") if n.startswith("alp") and n[3:].isdigit())
        for name in candidates:
            if fe_dev and os.path.realpath(f"/sys/class/net/{name}/device") == fe_dev:
                return name
        return candidates[0] if candidates else None

    def set_alp_up(self, up):
        name = self.alp_ifname or self.find_alp_iface()
        if not name:
            raise OSError("no alp* network interface for this adapter")
        self.alp_ifname = name
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            ifr = bytearray(struct.pack("16sh", name.encode(), 0) + bytes(24))
            fcntl.ioctl(s.fileno(), SIOCGIFFLAGS, ifr)
            flags = struct.unpack_from("h", ifr, 16)[0]
            flags = (flags | IFF_UP) if up else (flags & ~IFF_UP)
            struct.pack_into("h", ifr, 16, flags)
            fcntl.ioctl(s.fileno(), SIOCSIFFLAGS, ifr)
        finally:
            s.close()
        self.alp_raised = up
        return name

    def close(self):
        # leaving alp up keeps the bridge's ALP feed (and ADAP_STREAMING) alive,
        # which makes the next frontend open hang - always bring it down
        if self.alp_raised:
            try:
                self.set_alp_up(False)
            except OSError:
                pass
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main():
    import argparse, time
    ap = argparse.ArgumentParser(description="Tune an ATSC 3.0 channel (like atsc3-zap)")
    ap.add_argument("freq_hz", type=int)
    ap.add_argument("--plp", default="", help="comma-separated PLP IDs; omit for auto")
    ap.add_argument("-a", "--adapter", type=int, default=None, help="default: auto-detect")
    ap.add_argument("-f", "--frontend", type=int, default=0)
    args = ap.parse_args()
    plps = [int(x) for x in args.plp.split(",") if x.strip()]

    if args.adapter is None:
        found = find_atsc3_frontend()
        if not found:
            raise SystemExit("no frontend offers ATSC 3.0")
        args.adapter, args.frontend, name = found
        print(f"using adapter {args.adapter} frontend {args.frontend}: {name}")

    with Frontend(args.adapter, args.frontend) as fe:
        fe.tune(args.freq_hz, plps)
        print(f"tuning {args.freq_hz} Hz, PLPs {plps or 'auto'} ...")
        was_locked = None
        try:
            while True:
                locked = fe.locked()
                if locked != was_locked:
                    if locked:
                        name = fe.set_alp_up(True) if not fe.alp_raised else fe.alp_ifname
                        print(f"locked, {name} up, stats: {fe.stats()}")
                    elif was_locked is not None:
                        print("lock lost")
                    was_locked = locked
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("stopping")


if __name__ == "__main__":
    main()
