# atsc3-player

A standalone ATSC 3.0 receiver in one Python file. It captures the IP traffic a
tuner driver delivers on its ALP network interface (`alp0`), reassembles the
ROUTE/DASH objects itself (LCT/ALC parsing, no libatsc3), and then either:

- plays the service locally in mpv (HEVC video + AC-4 audio), and/or
- serves it over HTTP as two live streams, `/video.mp4` and `/audio.mp4`, for
  playback on another machine.

Tested on Linux Mint 22 (Python 3.12) with a GTMEDIA HDTV Mate (USB
`048d:9306`) on the modified koreapyj it930x/cxd2878 driver, against a live
ATSC 3.0 broadcast.

## Requirements

- **A tuner driver that exposes ATSC 3.0 ALP as a network interface.** The
  script only reads `alp0`; something else must tune, lock and bring the
  interface up (see [Running](#running)).
- **Python 3.8+.** Standard library only; nothing to `pip install`.
- **For local playback:** an mpv build with an AC-4 decoder, and its ffmpeg
  libraries. The script expects them at these fixed paths:
  - `~/local/mpv-ac4/bin/mpv` (libraries in `~/local/mpv-ac4/lib/x86_64-linux-gnu`)
  - `~/local/jellyfin-ffmpeg-dev/lib`

  Edit `start_local_player()` in `atsc3-player.py` if yours live elsewhere.
  `--serve-only` needs neither.
- **For remote playback:** mpv on the viewing machine. Audio needs an AC-4
  decoder there too; without one you get video only. Use a decoder that
  includes jellyfin-ffmpeg patch `0101-ac4dec-reject-damaged-frames-instead-of-asserting.patch`,
  or damaged audio frames abort mpv.

## Setting up the Python environment

The script opens a raw `AF_PACKET` socket, which needs `CAP_NET_RAW`. Rather
than running it as root, give that one capability to a private copy of the
Python interpreter inside a virtual environment:

```sh
python3 -m venv --copies ~/atsc3-env
sudo setcap cap_net_raw+ep "$(readlink -f ~/atsc3-env/bin/python3)"
getcap ~/atsc3-env/bin/python3*     # should list cap_net_raw=ep
cp atsc3-player.py ~/atsc3-env/
```

**`--copies` is essential.** Without it, `bin/python3` is a symlink to the
system interpreter, and `setcap` would grant raw-socket access to every Python
program on the machine. With `--copies`, only this environment's own binary
gets the capability.

Repeat the `setcap` step whenever you recreate the venv. The capability lives
on the binary file, so it isn't carried over by copying or by git.

Optional: the script asks for an 8 MB socket receive buffer, which the kernel
caps at `net.core.rmem_max`. If packets are dropped on a slow machine (the
script reports kernel drops every 50,000 frames), raise the cap:

```sh
sudo sysctl -w net.core.rmem_max=16777216
```

## Running

1. **Tune and lock**, which also brings `alp0` up:

   ```sh
   atsc3-zap 485000000 --plp 0,1 -a 1
   ```

   Or tune from updateDVB, then `sudo ip link set alp0 up`. With the modified
   driver, leaving the PLP unset selects every PLP the channel carries;
   otherwise name them, as above.

   Check the link before starting the player: `ip link show alp0` should show
   `LOWER_UP` and no `NO-CARRIER`.

2. **Start the player** from the venv:

   ```sh
   source ~/atsc3-env/bin/activate
   cd ~/atsc3-env
   python atsc3-player.py alp0                 # play locally + serve over HTTP
   python atsc3-player.py alp0 --serve-only    # HTTP only, no local window
   ```

   Positional arguments are `interface target_ip port`, defaulting to
   `alp0 239.255.29.1 5002`, the ROUTE session of the tested service.

3. **Watch remotely.** The player prints the exact command at startup, using
   the machine's LAN address:

   ```sh
   mpv http://192.168.1.110:8080/video.mp4 --audio-file=http://192.168.1.110:8080/audio.mp4
   ```

   Each viewer joins at the newest segment, and several can watch at once.

4. **Stop** with `q` in the local mpv window or Ctrl+C in the terminal. Either
   shuts everything down cleanly; a second Ctrl+C forces an exit.

## How it works

- Segments and init files are cached in `/tmp/atsc3_cache`. About the last two
  minutes of segments are kept; init segments survive restarts because the
  broadcast only resends them on its own carousel.
- Tracks are chosen by transport session (TSI): `100` is HEVC video and `200`
  is the English AC-4 main audio (`TRACKS` at the top of the script). `201` is
  Spanish and `300` is captions on the tested station; other stations may
  differ.
- Before any segment reaches mpv or an HTTP client it is checked: a segment
  that doesn't start with a valid MP4 box (the tail of an object caught
  mid-way at startup) is dropped, and one whose last `mdat` is short (a lost
  packet) is zero-padded so the next segment stays aligned. A demuxer reading
  a pipe can't recover from either on its own.

## Troubleshooting

- **Stuck at "Waiting for DASH Manifest".** No ROUTE traffic is arriving.
  Check that `alp0` shows `LOWER_UP`, and that packets are flowing:
  `cat /sys/class/net/alp0/statistics/rx_packets` should climb by thousands a
  second. A trickle of a few a second means only the signalling PLP is
  selected; tune with `--plp 0,1` or use the auto-PLP driver.
- **"Raw sockets require root privileges or setcap".** The `setcap` step is
  missing, or you're not running the venv's `python`.
- **mpv aborts with `Assertion ... failed at libavcodec/ac4dec.c`.** The
  decoder lacks patch 0101; see [Requirements](#requirements).
- **Brief picture corruption every so often.** Some segments arrive one
  1400-byte packet short. They're padded so playback continues, but the
  missing data isn't recovered.
