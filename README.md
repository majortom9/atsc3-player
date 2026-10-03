# atsc3-player

An ATSC 3.0 (NextGen TV) and ATSC 1.0 receiver for Linux, in Python. With a
tuner whose driver delivers ATSC 3.0 IP traffic on an ALP network interface
(`alp0`), and ATSC 1.0 on the usual DVB demux, it:

- **scans** RF channels 2-36 for both standards and keeps one combined channel
  list, sorted by channel number (a market usually has one ATSC 3.0
  frequency and many ATSC 1.0 stations),
- **tunes** the channel (DVBv5, auto or manual PLP selection) and brings
  `alp0` up for ATSC 3.0 (down for ATSC 1.0),
- **lists the services** from the broadcast's SLT, with station logos and
  what's on now and next from the programme guide (ESG),
- **plays a service** in mpv with HEVC video and Dolby AC-4 audio, in the audio
  language you pick,
- optionally **serves it on the LAN** as two live HTTP streams,
  `/video.mp4` and `/audio.mp4`, so any machine with an AC-4 capable mpv can
  watch,
- shows a **programme guide** (72 hours, descriptions, ratings, posters) with
  "Watch" for what's on now,
- also plays services the broadcast announces but **carries over the
  internet** (their manifest comes over the air, the video from the
  station's server), at up to 1080p,
- plays **ATSC 1.0** channels (MPEG-2/H.264 video, AC-3 audio) from the same
  list, with now/next and the guide from PSIP, served on the LAN as one
  MPEG-TS, `/live.ts`.

It does its own ROUTE/DASH reassembly; no libatsc3 is needed. Two front ends
share one package (`atsc3player/`): a GTK 4 app (`atsc3-gui.py`) and a
command-line player (`atsc3-player.py`).

Tested with a GTMEDIA HDTV Mate (USB `048d:9306`) on the it930x/cxd2878
driver ([majortom9/cxd28xx](https://github.com/majortom9/cxd28xx), also
in-tree in [majortom9/media-udl](https://gitlab.com/majortom9/media-udl)),
on x86_64 Linux (Linux Mint 22, Arch/Manjaro) and a Raspberry Pi 4 (32-bit
Arch Linux ARM userland), against a live US ATSC 3.0 multiplex. The full
from-scratch setup (driver, AC-4 ffmpeg and mpv, Raspberry Pi packages) is in
the [wiki](https://github.com/majortom9/atsc3-player/wiki).

For a C alternative with a terminal UI, see the libatsc3 fork
[majortom9/libatsc3](https://github.com/majortom9/libatsc3)
(`atsc3_listener_metrics_ncurses_httpd_isobmff`).

## Requirements

- **The tuner driver**, with ALP delivered on an `alp*` network interface
  ([majortom9/cxd28xx](https://github.com/majortom9/cxd28xx) or the media-udl
  kernel). The app tunes by itself; `atsc3-zap` is not needed.
- **Python 3.10+.** The player uses the standard library only; the GUI also
  needs **PyGObject with GTK 4**:
  - Arch / Manjaro / Arch Linux ARM: `sudo pacman -S --needed python-gobject gtk4`
  - Ubuntu / Debian / Mint: `sudo apt install python3-gi gir1.2-gtk-4.0`
- **mpv with an AC-4 decoder** on every machine that plays. The app finds,
  in this order: `/usr/bin/mpv-ac4` (the Raspberry Pi package in
  `packaging/rpi/mpv-ac4`), `~/local/mpv-ac4/bin/mpv` with
  `~/local/jellyfin-ffmpeg-dev/lib` (the x86_64 build in the wiki), then
  `mpv-ac4` / `mpv` on the PATH. The decoder must include patches 0101 and
  0102 (`packaging/patches/`), or damaged over-the-air frames crash mpv or
  turn the sound into a loud buzz.
- **Internet access** on the tuner machine for the internet-delivered
  services and the programme posters; everything else is over the air.

## Setting up

```sh
git clone https://github.com/majortom9/atsc3-player.git ~/atsc3-player
python3 -m venv --copies --system-site-packages ~/atsc3-gui-env
sudo setcap cap_net_raw,cap_net_admin+ep "$(readlink -f ~/atsc3-gui-env/bin/python3)"
getcap "$(readlink -f ~/atsc3-gui-env/bin/python3)"     # cap_net_admin,cap_net_raw=ep
```

- **`--copies`** gives the venv its own Python binary. Without it, `setcap`
  would land on the system Python and give every Python program these rights.
- **`--system-site-packages`** lets the venv see the system's PyGObject and
  GTK 4 (they aren't installable with pip).
- **The two capabilities:** `cap_net_raw` to capture on `alp0`,
  `cap_net_admin` to bring `alp0` up after lock and down on exit. Redo the
  `setcap` whenever the venv is recreated or the system Python is updated.
- Your user must be able to open `/dev/dvb/*` (usually the `video` group).

Optional: the capture asks for an 8 MB socket buffer, capped by
`net.core.rmem_max`. If the GUI shows dropped packets, raise it:
`sudo sysctl -w net.core.rmem_max=16777216`.

## The GUI

```sh
cd ~/atsc3-player && ~/atsc3-gui-env/bin/python3 atsc3-gui.py
```

1. **Scan** (once). It tries every RF channel 2-36, first as ATSC 1.0, then
   as ATSC 3.0, and reads the channel list of each one it locks (the PSIP
   virtual channel table, or the SLT). It takes about two minutes and can be
   cancelled. The result is saved and replaces the previous list. The tuner
   is found by itself, whichever adapter number it has.
2. **Pick a channel.** Each row shows the channel number, name, standard and
   RF channel. Clicking one tunes its RF channel if needed; double-clicking
   (or Enter) also starts playing it. DRM-protected and app-based services
   are greyed out. Logos and now/next appear once the guide has arrived: a
   minute or two for the ATSC 3.0 ESG, seconds for ATSC 1.0 PSIP.
3. **Play.** When the audio list fills, **Play** opens mpv in its own window.
   Change **Audio** to switch language (e.g. English/Spanish); on ATSC 1.0
   this switches mpv's track without restarting it. The current programme,
   its time, rating and description show under the list and in mpv's title.
4. **Tune by hand** (no scan needed): pick the RF channel and the standard,
   **Auto** (the scan's answer for that channel, else ATSC 1.0 and then
   ATSC 3.0), **ATSC 3.0** or **ATSC 1.0**, and press **Tune**. For ATSC 3.0,
   leave **PLPs** blank (the driver then selects every PLP the channel
   carries), or enter a list such as `0,1`. Channels found this way are added
   to the list. When locked, the status line shows SNR and signal level;
   **Refresh signal** reads them again.
5. **Serve on LAN** makes the streams reachable from other machines and shows
   the command to run there:

   ```sh
   # ATSC 3.0
   mpv http://TUNER-HOST:8080/video.mp4 --audio-file=http://TUNER-HOST:8080/audio.mp4
   # ATSC 1.0 (every audio track is in the stream; pick one with --alang=spa)
   mpv http://TUNER-HOST:8080/live.ts
   ```

   Several machines can watch at once; each joins at the newest segment (or
   the live edge of the TS).
6. **Guide** opens the programme guide: channels on the left, their schedule
   on the right, details (poster, time, rating, description) below. It holds
   the ATSC 3.0 ESG and the PSIP guide of every ATSC 1.0 RF channel tuned
   this session (PSIP only describes its own multiplex). **Watch**, or
   double-clicking what's on now, tunes to and plays that channel.
7. **Stop tuner** (or closing the window) stops playback, takes `alp0` down
   and releases the tuner.

**Over ssh:** run the GUI with `ssh -X` and tick *Show video on this
machine's own screen*, so mpv opens on the tuner machine's display. A
forwarded window is far too slow for 60 fps video. Untick it only for a
quick look.

**Internet-delivered services** (on the tested multiplex: T2 and PBTV) play
like the others. The track line says "internet". The highest video up to
1080p is used, and the tuner machine downloads it (about 7-8 Mbit/s).

Settings (the scanned channel list, RF channel, standard, PLPs, language,
serve on LAN, mpv path) are kept
in `~/.config/atsc3-player/config.json`. To use a particular mpv, add
`"mpv": "/path/to/mpv"` there.

## The command-line player

The tuner must already be locked (the GUI, `python3 -m atsc3player.tuner`,
`atsc3-zap` or updateDVB).

```sh
P=~/atsc3-gui-env/bin/python3
$P -m atsc3player.tuner 485000000 --plp 0,1       # tune + alp0 up; Ctrl+C to stop
$P atsc3-player.py --list                         # services in the SLT
$P atsc3-player.py --service 5002                 # play in mpv
$P atsc3-player.py --service 5002 --lang spa      # Spanish audio
$P atsc3-player.py --service 5002 --serve-only    # only serve on the LAN
$P atsc3-player.py alp0 239.255.29.1 5002         # by SLS address, no SLT needed
```

Other options: `--lan` (play locally and serve), `--http-port`, `--mpv`.
Stop with `q` in mpv or Ctrl+C.

ATSC 1.0 and the scan have their own small command-line tools:

```sh
$P -m atsc3player.scan                              # scan RF 2-36 (or --from-rf/--to-rf)
$P -m atsc3player.atsc1 581000000                   # tune 8-VSB, list the virtual channels
$P -m atsc3player.atsc1 581000000 --channel 29.1 --lan   # serve 29.1 at /live.ts
```

## How it works

- **One capture thread** reads `alp0` raw and splits the traffic: LLS (the
  SLT, inside a signed multi-table on the tested station), the selected
  service's ROUTE session, and the ESG's ROUTE session.
- **Service signalling:** the service's SLS bundle (MIME, often
  multipart/signed; signatures are not checked) gives the S-TSID and the DASH
  MPD. Together they say which transport session (TSI) carries the video and
  each audio language, and how the init and media objects are numbered.
- **ROUTE reassembly** places each packet at its `start_offset` (a lost
  packet leaves a hole in place instead of shifting the rest) and stores only
  the tracks being played, in a per-instance cache under
  `$XDG_RUNTIME_DIR`.
- **Streams:** each track becomes one continuous fMP4 stream (init, then
  segments in order). Segments that start mid-object are dropped; a short
  last `mdat` is zero-padded so a pipe-reading demuxer stays aligned.
- **Internet-delivered services:** no S-TSID, an https BaseURL in the MPD.
  The app downloads segments from the MPD's SegmentTimeline into the same
  cache.
- **Programme guide:** the ESG's S-TSID lists its files (index, SGDU
  containers of Service/Content/Schedule fragments, PNG logos); they are
  collected once complete and parsed in the background.
- **ATSC 1.0:** with `alp0` down, the bridge sends the 8-VSB transport stream
  to the DVB demux. The app reads all of it from `dvr0` (a PID 0x2000
  filter), parses PAT/PMT and PSIP (MGT, TVCT/CVCT, STT, EIT, ETT; A/65),
  and serves the selected virtual channel as one MPEG-TS: its PMT, PCR and
  elementary streams, with the PAT rewritten to list only that program.
  Audio languages come from the PMT's ISO 639 descriptors. The PSIP guide
  goes into the same guide model as the ESG, so now/next and the Guide
  window work for both.

Offline tests against data recorded from live multiplexes:
`python3 -m tests.test_signalling` (ATSC 3.0 SLT/SLS) and
`python3 -m tests.test_psip` (ATSC 1.0 PAT/PMT/PSIP, 25 s of PSI packets).

## Troubleshooting

- **Scan finds nothing, or an ATSC 1.0 channel locks but the list stays
  empty.** Another program has the demux or `alp0` open, or `alp0` was left
  up (ATSC 1.0 data then goes to ALP, not the demux). Close updateDVB /
  atsc3-zap and retry.
- **"No ATSC 3.0 tuner found".** The driver isn't loaded, the stick isn't
  plugged in, or another program (atsc3-zap, updateDVB) holds the tuner.
  `ls /dev/dvb`.
- **Locked, but the service list stays empty.** `alp0` isn't carrying data:
  `cat /sys/class/net/alp0/statistics/rx_packets` should climb by thousands a
  second. A trickle means only the signalling PLP is selected; leave PLPs
  blank or use `0,1`.
- **"Can't bring the ALP interface up" / "Can't capture".** The `setcap` step
  is missing or was lost; see [Setting up](#setting-up).
- **Sound but no picture when started over ssh.** The video went to the
  forwarded display; tick *Show video on this machine's own screen*.
- **mpv aborts with `Assertion ... ac4dec.c`, or the sound turns into a loud
  buzz after a while.** The AC-4 decoder lacks patch 0101 or 0102.
- **Raspberry Pi: choppy video.** Use `vo=gpu`, `hwdec=drm` and
  `profile=fast` (the `mpv-ac4` package's defaults), watch at native size and
  turn off the desktop's compositing; see the wiki.

## License

GPL-2.0-only; see [LICENSE](LICENSE). `atsc3player/tuner.py` ports the
tuning logic of atsc3-zap from [koreapyj/cxd28xx](https://github.com/koreapyj/cxd28xx),
Copyright (c) 2026 Yoonji Park, also GPL-2.0-only. The patches in
`packaging/patches/` apply to FFmpeg (jellyfin-ffmpeg / rpi-ffmpeg) and follow
its license.
