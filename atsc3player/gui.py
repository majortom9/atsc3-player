# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""atsc3-gui: scan for ATSC 3.0 and ATSC 1.0 channels, pick one and a language,
and play it in mpv-ac4 - optionally serving it to other machines over HTTP."""

import dataclasses
import json
import os
import threading
import time
import types

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import esg, scan, tuner            # noqa: E402
from .atsc1 import Atsc1Session           # noqa: E402
from .guide import GuideWindow, hhmm, texture  # noqa: E402
from .mpv import Mpv, find_mpv            # noqa: E402
from .session import Session              # noqa: E402
from .streams import lan_address          # noqa: E402

CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                      "atsc3-player", "config.json")

STANDARDS = ["Auto", "ATSC 3.0", "ATSC 1.0"]
AUTO_ATSC1_WAIT_S = 2.0          # Auto: how long to try 8VSB before trying ATSC 3.0
HIDDEN_CATEGORIES = (4, 5, 6)    # ESG, EAS and DRM-data services are not channels


def us_channels():
    """[(label, Hz)] for US broadcast RF channels 2-36."""
    return [(f"RF {rf}  ({hz // 1_000_000} MHz)", hz) for rf, hz in scan.us_channels()]


_RF_OF = {hz: rf for rf, hz in scan.us_channels()}


def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)


def _key(e):
    """Channel identity: (standard, frequency, service_id / program_number)."""
    return (e["standard"], e["freq"], e["service_id"])


def _entry(s, std, freq):
    """A channel-list entry (the scan's dict form) for a live lls.Service."""
    return {"standard": std, "rf": _RF_OF.get(freq, 0), "freq": freq, "major": s.major,
            "minor": s.minor, "name": s.name, "service_id": s.service_id,
            "category": s.category, "protected": s.protected}


def _sort_key(e):
    return (e["major"] or 999, e["minor"], e["standard"], e["freq"])


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="ATSC 3.0 / 1.0")
        self.set_default_size(760, 680)
        self.cfg = load_config()
        self.fe = None
        self.session = None
        self.player = None
        self.tracks = []
        self.locked = False
        self.tuned = None                 # (standard, freq) the tuner is on
        self.auto_deadline = None         # Auto: when to give up on ATSC 1.0
        self.pending = None               # channel key to select once its mux is up
        self.mux_services = {}            # (standard, freq) -> [lls.Service] seen this session
        self.guides = {}                  # (standard, freq) -> esg.Guide seen this session
        self.scan_stop = None
        self.scan_thread = None
        self._rows_busy = False
        self._play_when_ready = False
        self._token = None
        self.channels = us_channels()
        self.connect("close-request", self._on_close)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=10, margin_bottom=10, margin_start=10, margin_end=10)
        self.set_child(box)

        # -- tuning -----------------------------------------------------------
        row = Gtk.Box(spacing=6)
        box.append(row)
        row.append(Gtk.Label(label="RF"))
        self.chan = Gtk.DropDown.new_from_strings([c[0] for c in self.channels])
        freq = self.cfg.get("freq_hz", 485_000_000)
        idx = next((i for i, c in enumerate(self.channels) if c[1] == freq), 16 - 2)
        self.chan.set_selected(idx)
        row.append(self.chan)
        self.std = Gtk.DropDown.new_from_strings(STANDARDS)
        self.std.set_selected(self.cfg.get("standard", 0))
        self.std.set_tooltip_text("Auto uses the scan result for this RF channel, or tries "
                                  "ATSC 1.0 first and then ATSC 3.0")
        row.append(self.std)
        row.append(Gtk.Label(label="PLPs"))
        self.plp = Gtk.Entry(placeholder_text="auto", width_chars=6,
                             text=self.cfg.get("plps", ""))
        self.plp.set_tooltip_text("ATSC 3.0 only. Blank = auto (the driver selects every PLP "
                                  "the channel carries). Or a comma list, e.g. 0,1")
        row.append(self.plp)
        self.tune_btn = Gtk.Button(label="Tune")
        self.tune_btn.connect("clicked", self._on_tune)
        row.append(self.tune_btn)
        self.untune_btn = Gtk.Button(label="Stop tuner", sensitive=False)
        self.untune_btn.connect("clicked", lambda *_: self._untune())
        row.append(self.untune_btn)
        self.scan_btn = Gtk.Button(label="Scan")
        self.scan_btn.set_tooltip_text("Search RF 2-36 for ATSC 1.0 and ATSC 3.0 channels "
                                       "(about two minutes); replaces the channel list")
        self.scan_btn.connect("clicked", self._on_scan)
        row.append(self.scan_btn)

        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        box.append(self.progress)

        srow = Gtk.Box(spacing=6)
        box.append(srow)
        self.status = Gtk.Label(label="Not tuned", xalign=0, hexpand=True, selectable=True)
        srow.append(self.status)
        self.stats_btn = Gtk.Button(label="Refresh signal", sensitive=False)
        self.stats_btn.connect("clicked", lambda *_: self._read_stats())
        srow.append(self.stats_btn)
        self.rate = Gtk.Label(label="", xalign=1)
        srow.append(self.rate)

        # -- channels -----------------------------------------------------------
        hrow = Gtk.Box(spacing=6)
        box.append(hrow)
        hrow.append(Gtk.Label(label="<b>Channels</b>", use_markup=True, xalign=0, hexpand=True))
        self.guide_btn = Gtk.Button(label="Guide", sensitive=False)
        self.guide_btn.set_tooltip_text("Programme guide: ATSC 3.0 ESG, and the ATSC 1.0 "
                                        "PSIP of every channel tuned this session")
        self.guide_btn.connect("clicked", lambda *_: self._open_guide())
        hrow.append(self.guide_btn)
        self.guide_win = None
        self.guide_view = types.SimpleNamespace(lls=types.SimpleNamespace(services=[]),
                                                esg=types.SimpleNamespace(guide=esg.Guide()),
                                                service=None)
        self._guide_sig = None
        self.svc_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                    activate_on_single_click=False)
        self.svc_list.connect("row-selected", self._on_row)
        self.svc_list.connect("row-activated", self._on_row_activated)
        self.svc_list.set_placeholder(Gtk.Label(
            label="No channels yet: press Scan, or tune an RF channel", margin_top=20,
            css_classes=["dim-label"]))
        sc = Gtk.ScrolledWindow(vexpand=True, min_content_height=200)
        sc.set_child(self.svc_list)
        box.append(sc)

        # -- playback -----------------------------------------------------------
        self.now_playing = Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        box.append(self.now_playing)
        prow = Gtk.Box(spacing=6)
        box.append(prow)
        prow.append(Gtk.Label(label="Audio"))
        self.lang_model = Gtk.StringList()
        self.lang = Gtk.DropDown(model=self.lang_model, sensitive=False)
        self.lang.connect("notify::selected", self._on_lang)
        prow.append(self.lang)
        self.track_info = Gtk.Label(label="", xalign=0, hexpand=True)
        prow.append(self.track_info)
        self.play_btn = Gtk.Button(label="Play", sensitive=False)
        self.play_btn.connect("clicked", lambda *_: self._play())
        prow.append(self.play_btn)
        self.stop_btn = Gtk.Button(label="Stop", sensitive=False)
        self.stop_btn.connect("clicked", lambda *_: self._stop_player())
        prow.append(self.stop_btn)

        lrow = Gtk.Box(spacing=6)
        box.append(lrow)
        lrow.append(Gtk.Label(label="Serve on LAN"))
        self.lan = Gtk.Switch(active=self.cfg.get("lan", False), valign=Gtk.Align.CENTER)
        self.lan.connect("notify::active", self._on_lan)
        lrow.append(self.lan)
        self.remote = Gtk.Label(label="", xalign=0, hexpand=True, selectable=True, wrap=True)
        lrow.append(self.remote)

        # Over ssh, mpv would inherit the forwarded DISPLAY and open its window
        # on the remote machine; offer the tuner machine's own screen instead.
        self.over_ssh = bool(os.environ.get("SSH_CONNECTION"))
        self.on_screen = Gtk.CheckButton(label="Show video on this machine's own screen (:0)",
                                         active=self.cfg.get("local_display", True),
                                         visible=self.over_ssh)
        self.on_screen.connect("toggled", lambda b: (self.cfg.__setitem__("local_display", b.get_active()), self._save()))
        box.append(self.on_screen)

        # -- log ----------------------------------------------------------------
        exp = Gtk.Expander(label="Log")
        self.logbuf = Gtk.TextBuffer()
        tv = Gtk.TextView(buffer=self.logbuf, editable=False, monospace=True)
        lsc = Gtk.ScrolledWindow(min_content_height=120)
        lsc.set_child(tv)
        exp.set_child(lsc)
        box.append(exp)

        binary, _ = find_mpv(self.cfg.get("mpv"))
        self.log(f"mpv: {binary or 'NOT FOUND - install mpv-ac4'}")
        self._update_remote()
        self._rebuild_list()
        GLib.timeout_add(500, self._poll)
        GLib.timeout_add_seconds(20, self._refresh_epg)
        self._last_title = None

    # -- helpers ----------------------------------------------------------------
    def log(self, msg):
        def add():
            end = self.logbuf.get_end_iter()
            self.logbuf.insert(end, time.strftime("%H:%M:%S ") + msg + "\n")
            return False
        GLib.idle_add(add)

    def _save(self):
        save_config(self.cfg)

    def _freq(self):
        return self.channels[self.chan.get_selected()][1]

    def _plps(self):
        txt = self.plp.get_text().strip()
        return [int(x) for x in txt.replace(" ", "").split(",") if x] if txt else []

    def _scanning(self):
        return self.scan_stop is not None

    # -- tuning -------------------------------------------------------------------
    def _ensure_fe(self):
        if self.fe is None:
            found = tuner.find_atsc3_frontend()
            if not found:
                self.status.set_text("No ATSC 3.0 tuner found (is the HDTV Mate plugged in?)")
                return False
            a, f, name = found
            self.fe = tuner.Frontend(a, f)
            self.log(f"tuner: adapter {a} frontend {f} ({name})")
        return True

    def _on_tune(self, *_):
        try:
            tuner.pack_plps(self._plps())
        except ValueError:
            self.status.set_text("PLPs: up to four numbers 0-63, comma separated, or blank")
            return
        freq = self._freq()
        std = {1: 3, 2: 1}.get(self.std.get_selected())
        if std is None:                           # Auto: what the scan found here, if anything
            known = {e["standard"] for e in self.cfg.get("channels", []) if e["freq"] == freq}
            std = 3 if 3 in known else 1 if 1 in known else None
        self.cfg.update(freq_hz=freq, plps=self.plp.get_text().strip(),
                        standard=self.std.get_selected())
        self._save()
        self.pending = None
        if self._tune(std or 1, freq) and std is None:
            self.auto_deadline = time.time() + AUTO_ATSC1_WAIT_S

    def _tune(self, std, freq):
        """Tune `freq` as ATSC 1.0 (std 1) or 3.0 (std 3); the session opens on lock."""
        self._stop_player()
        self._close_session()
        self.auto_deadline = None
        try:
            if not self._ensure_fe():
                return False
            if std == 1:
                self.fe.tune_atsc1(freq)
            else:
                self.fe.tune(freq, self._plps())
        except (OSError, ValueError) as e:
            self.status.set_text(f"Tune failed: {getattr(e, 'strerror', None) or e}")
            self.tuned = None
            return False
        self.tuned = (std, freq)
        self.locked = False
        idx = next((i for i, c in enumerate(self.channels) if c[1] == freq), None)
        if idx is not None:
            self.chan.set_selected(idx)
        what = "ATSC 1.0" if std == 1 else f"ATSC 3.0, PLPs {self._plps() or 'auto'}"
        self.status.set_text(f"Tuning RF {_RF_OF.get(freq, '?')} ({freq / 1e6:.0f} MHz), {what} ...")
        self.untune_btn.set_sensitive(True)
        self.stats_btn.set_sensitive(False)
        self.rate.set_text("")
        return True

    def _cancel_scan(self):
        if self._scanning():
            self.scan_stop.set()
            if self.scan_thread:
                self.scan_thread.join(timeout=10)

    def _untune(self):
        self._cancel_scan()
        self._stop_player()
        self._close_session()
        if self.fe:
            self.fe.close()          # brings alp0 down
            self.fe = None
        self.locked = False
        self.tuned = self.pending = self.auto_deadline = None
        self.status.set_text("Not tuned")
        self.rate.set_text("")
        self.untune_btn.set_sensitive(False)
        self.stats_btn.set_sensitive(False)
        self._update_rows()

    def _read_stats(self):
        if not self.fe or not self.tuned:
            return
        try:
            st = self.fe.stats()
        except OSError:
            return
        bits = ["Locked"]
        if "cnr_db" in st:
            bits.append(f"SNR {st['cnr_db']:.1f} dB")
        if "strength_dbm" in st:
            bits.append(f"signal {st['strength_dbm']:.1f} dBm")
        if "post_ber" in st:
            bits.append(f"BER {st['post_ber']}")
        std, freq = self.tuned
        self.status.set_text(", ".join(bits) + f"  (ATSC {'1.0' if std == 1 else '3.0'}, "
                                               f"RF {_RF_OF.get(freq, '?')}, {freq / 1e6:.0f} MHz)")

    def _poll(self):
        if self.fe and self.tuned and not self._scanning():
            try:
                locked = self.fe.locked()
            except OSError:
                locked = False
            if locked and not self.locked:
                self.locked = True
                self.auto_deadline = None
                if self.tuned[0] == 3:
                    try:
                        ifname = self.fe.set_alp_up(True)
                    except OSError as e:
                        self.status.set_text(f"Locked, but can't bring the ALP interface up: {e}")
                        return True
                    self.log(f"locked (ATSC 3.0); {ifname} up")
                    self._read_stats()
                    self._open_session(ifname)
                else:
                    self.log("locked (ATSC 1.0)")
                    self._read_stats()
                    self._open_session(None)
                self.stats_btn.set_sensitive(True)
            elif not locked and self.locked:
                self.locked = False
                self.status.set_text("Lock lost - waiting ...")
            elif not locked and self.auto_deadline and time.time() > self.auto_deadline:
                freq = self.tuned[1]
                self.log(f"no ATSC 1.0 lock on {freq / 1e6:.0f} MHz, trying ATSC 3.0")
                self._tune(3, freq)
        if self.player and not self.player.running():
            self.log("mpv exited")
            self.player = None
            self._update_buttons()
        return True

    # -- channel scan ---------------------------------------------------------------
    def _on_scan(self, *_):
        if self._scanning():
            self.scan_stop.set()
            self.scan_btn.set_sensitive(False)
            return
        self._stop_player()
        self._close_session()
        self.tuned = self.pending = self.auto_deadline = None
        self.locked = False
        if not self._ensure_fe():
            return
        self.scan_stop = threading.Event()
        self.scan_btn.set_label("Cancel scan")
        for w in (self.tune_btn, self.svc_list, self.stats_btn):
            w.set_sensitive(False)
        self.untune_btn.set_sensitive(True)
        self.progress.set_fraction(0)
        self.progress.set_text("Scanning ...")
        self.progress.set_visible(True)
        self.status.set_text("Scanning ...")
        self.rate.set_text("")
        stop, fe = self.scan_stop, self.fe

        def run():
            res, err = None, None
            try:
                res = scan.scan(fe, stop=stop, log=self.log,
                                on_progress=lambda *a: GLib.idle_add(self._scan_progress, *a))
            except Exception as e:      # noqa: BLE001 - report anything to the user
                err = e
            GLib.idle_add(self._scan_done, res, err)
        self.scan_thread = threading.Thread(target=run, daemon=True, name="scan")
        self.scan_thread.start()

    def _scan_progress(self, i, n, rf, found):
        if not self._scanning():
            return False
        self.progress.set_fraction(i / n if n else 1)
        self.progress.set_text(f"RF {rf} ({i + 1}/{n}), {found} channels found" if rf
                               else f"{found} channels found")
        return False

    def _scan_done(self, res, err):
        cancelled = self.scan_stop.is_set() if self.scan_stop else True
        self.scan_stop = self.scan_thread = None
        self.scan_btn.set_label("Scan")
        self.scan_btn.set_sensitive(True)
        self.tune_btn.set_sensitive(True)
        self.svc_list.set_sensitive(True)
        self.progress.set_visible(False)
        if err:
            self.status.set_text(f"Scan failed: {err}")
        elif cancelled or res is None:
            self.status.set_text("Scan cancelled - the channel list is unchanged")
        else:
            self.cfg["channels"] = res
            self._save()
            muxes = {(e["standard"], e["freq"]) for e in res}
            n3 = sum(1 for m in muxes if m[0] == 3)
            shown = sum(1 for e in res if e.get("category", 1) not in HIDDEN_CATEGORIES)
            self.status.set_text(f"Scan: {shown} channels on {len(muxes)} RF channels "
                                 f"({len(muxes) - n3} ATSC 1.0, {n3} ATSC 3.0)")
            self.log(self.status.get_text())
            self._rebuild_list()
        if self.fe and not self.tuned:
            self.untune_btn.set_sensitive(True)
        return False

    # -- session ----------------------------------------------------------------
    def _open_session(self, ifname):
        if self.session:
            return
        # callbacks queued by a session that has since been closed must not
        # land on the next one (possibly another mux)
        token = self._token = object()

        def later(fn):
            return lambda *a: GLib.idle_add(lambda: fn(*a) if self._token is token else False)
        cb = dict(lan=self.lan.get_active(), log=self.log,
                  on_services=later(self._set_services), on_tracks=later(self._set_tracks),
                  on_status=later(self._show_rate), on_guide=later(self._on_guide))
        try:
            if ifname:
                self.session = Session(ifname, **cb)
            else:
                self.session = Atsc1Session(self.fe.adapter, **cb)
            self.session.start()
        except OSError as e:
            self.session = None
            where = ifname or f"/dev/dvb/adapter{self.fe.adapter}/dvr0"
            self.status.set_text(f"Can't capture on {where}: {e}"
                                 + (" (setcap cap_net_raw on python?)" if ifname else ""))
        self._update_remote()

    def _close_session(self):
        self._token = None
        if self.session:
            if self.tuned:
                self.guides[self.tuned] = self.session.esg.guide
            self.session.close()
            self.session = None
        self.tracks = []
        self._fill_langs()
        self._update_remote()

    def _show_rate(self, st):
        if not self.session:
            return False
        drops = f", {st['drops']} dropped" if st["drops"] else ""
        self.rate.set_text(f"{st['rate_mbps']:.1f} Mbit/s{drops}")
        return False

    # -- channel list ---------------------------------------------------------------
    def _entries(self):
        """Saved scan results plus anything found by manual tuning, sorted."""
        out = {_key(e): e for e in self.cfg.get("channels", [])}
        for (std, freq), svcs in self.mux_services.items():
            for s in svcs:
                k = (std, freq, s.service_id)
                if k not in out:
                    out[k] = _entry(s, std, freq)
        return sorted((e for e in out.values() if e.get("category", 1) not in HIDDEN_CATEGORIES),
                      key=_sort_key)

    def _live(self, key):
        """The live lls.Service for a channel key, if its mux is the one tuned."""
        std, freq, sid = key
        if not self.session or self.tuned != (std, freq):
            return None
        return next((s for s in self.mux_services.get((std, freq), []) if s.service_id == sid), None)

    def _rows(self):
        row = self.svc_list.get_first_child()
        while row is not None:
            if hasattr(row, "entry"):
                yield row
            row = row.get_next_sibling()

    def _rebuild_list(self):
        entries = self._entries()
        if [_key(e) for e in entries] == [r.key for r in self._rows()]:
            self._update_rows()
            return
        self._rows_busy = True
        while (row := self.svc_list.get_first_child()) is not None:
            self.svc_list.remove(row)
        for e in entries:
            hb = Gtk.Box(spacing=10, margin_top=2, margin_bottom=2)
            icon = Gtk.Picture(width_request=64, height_request=48, can_shrink=True,
                               content_fit=Gtk.ContentFit.CONTAIN)
            hb.append(icon)
            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            head = Gtk.Label(xalign=0, use_markup=True)
            epg = Gtk.Label(xalign=0, label="", ellipsize=3, css_classes=["dim-label"])
            vb.append(head)
            vb.append(epg)
            hb.append(vb)
            row = Gtk.ListBoxRow(child=hb)
            row.entry, row.key, row.icon, row.head, row.epg = e, _key(e), icon, head, epg
            self.svc_list.append(row)
        self._rows_busy = False
        self._update_rows()

    def _update_rows(self):
        """Labels, sensitivity and selection from the live state."""
        cur = self._current_key()
        self._rows_busy = True
        for row in self._rows():
            e = row.entry
            live = self._live(row.key)
            name = live.name if live else e["name"]
            note = []
            protected = live.protected if live else e.get("protected")
            category = live.category if live else e.get("category", 1)
            if protected:
                note.append("DRM protected")
            elif category != 1:
                note.append(live.category_name if live else f"category {category}")
            tag = "ATSC 3.0" if e["standard"] == 3 else "ATSC 1.0"
            ch = f"{e['major']}.{e['minor']}" if e["major"] else "-"
            row.head.set_markup(
                f"<b>{GLib.markup_escape_text(ch)}  {GLib.markup_escape_text(name)}</b>"
                f"  <small>{tag}  RF {e['rf']}  {GLib.markup_escape_text(', '.join(note))}</small>")
            row.set_sensitive(live.playable if live else (category == 1 and not protected))
            if row.key == cur:
                self.svc_list.select_row(row)
        if cur is None and self.pending is None:
            self.svc_list.unselect_all()
        self._rows_busy = False
        self._refresh_epg()

    def _current_key(self):
        if self.pending:
            return self.pending
        if self.session and self.session.service and self.tuned:
            return (*self.tuned, self.session.service.service_id)
        return None

    def _set_services(self, services):
        if not self.session or not self.tuned:
            return False
        self.mux_services[self.tuned] = list(services)
        self._rebuild_list()
        if self.pending and self.pending[:2] == self.tuned:
            svc = self._live(self.pending)
            if svc:
                self.pending = None
                self._select_live(svc)
        self._update_guide_view()
        return False

    def _on_row(self, _lb, row):
        if self._rows_busy or row is None or not hasattr(row, "entry"):
            return
        key = row.key
        self.cfg["selected"] = list(key)
        self._save()
        if self.tuned == key[:2] and self.session:
            svc = self._live(key)
            if svc:
                self.pending = None
                self._select_live(svc)
            else:
                self.pending = key            # selected once the SLT / VCT lists it
            return
        self.pending = key
        self.track_info.set_text("tuning ...")
        self._tune(key[0], key[1])

    def _on_row_activated(self, _lb, row):
        if not hasattr(row, "entry"):
            return
        if self.player and self._current_key() == row.key:
            return
        self._play_when_ready = True
        self._update_buttons()

    def _select_live(self, svc):
        if self.session.service and self.session.service.service_id == svc.service_id:
            return
        self._stop_player()
        self.tracks = []
        self._fill_langs()
        self.track_info.set_text("waiting for the service signalling ...")
        self.session.select_service(svc, lang=self.cfg.get("lang"))

    # -- programme guide -------------------------------------------------------
    def _on_guide(self, guide):
        if self.session and self.tuned:
            self.guides[self.tuned] = self.session.esg.guide
        self._update_guide_view()
        self._refresh_epg()
        return False

    def _guide_for(self, mux):
        if self.session and self.tuned == mux:
            return self.session.esg.guide
        return self.guides.get(mux)

    def _now_next(self, key, e=None):
        """(Programme, Slot, Programme, Slot) for a channel key, from its mux's guide."""
        g = self._guide_for(key[:2])
        if g is None:
            return None, None, None, None
        live = self._live(key)
        if live:
            gid, major, minor = live.global_id, live.major, live.minor
        else:
            e = e or {}
            major, minor = e.get("major", 0), e.get("minor", 0)
            gid = f"atsc1:{major}.{minor}" if key[0] == 1 else ""
        gs = g.service_for(gid, major, minor)
        cur, nxt = g.now_next(gs)
        return g.programme(cur), cur, g.programme(nxt), nxt

    def _update_guide_view(self):
        """Merge the guides of every mux seen this session into one view for the
        Guide window. Each channel gets a unique id (standard:freq:service)."""
        merged, services = esg.Guide(), []
        muxes = dict(self.guides)
        if self.session and self.tuned:
            muxes[self.tuned] = self.session.esg.guide
        for (std, freq), g in muxes.items():
            for s in self.mux_services.get((std, freq), []):
                if s.category in HIDDEN_CATEGORIES:
                    continue
                gs = g.service_for(s.global_id, s.major, s.minor)
                if not gs:
                    continue
                uid = f"{std}:{freq}:{s.service_id}"
                merged.services[uid] = dataclasses.replace(gs, frag_id=uid, global_id=uid)
                slots = []
                for sl in g.slots(gs):
                    cid = f"{uid}/{sl.content_id}"
                    if sl.content_id in g.contents:
                        merged.contents[cid] = g.contents[sl.content_id]
                    slots.append(esg.Slot(sl.start, sl.end, cid))
                merged.schedule[uid] = slots
                if gs.icon in g.icons:
                    merged.icons[gs.icon] = g.icons[gs.icon]
                services.append(dataclasses.replace(s, global_id=uid,
                                                    extra={**s.extra, "key": (std, freq, s.service_id)}))
        services.sort(key=lambda s: (s.major or 999, s.minor, s.standard))
        v = self.guide_view
        v.lls.services, v.esg.guide = services, merged
        cur = self._current_key()
        v.service = next((s for s in services if s.extra["key"] == cur), None)
        self.guide_btn.set_sensitive(bool(services))
        # the guide window rebuilds its lists on refresh - only when something changed
        sig = (tuple(merged.services), sum(len(x) for x in merged.schedule.values()),
               sum(1 for p in merged.contents.values() if p.description))
        if self.guide_win and sig != self._guide_sig:
            self.guide_win.refresh()
        self._guide_sig = sig

    def _refresh_epg(self):
        for row in self._rows():
            key, e = row.key, row.entry
            g = self._guide_for(key[:2])
            if g is None:
                row.epg.set_text("")
                continue
            live = self._live(key)
            gs = g.service_for(live.global_id if live else
                               (f"atsc1:{e['major']}.{e['minor']}" if key[0] == 1 else ""),
                               e["major"], e["minor"])
            if gs and gs.icon in g.icons and not getattr(row, "icon_set", False):
                tex = texture(g.icons[gs.icon])
                if tex:
                    row.icon.set_paintable(tex)
                    row.icon_set = True
            pc, cs, pn, ns = self._now_next(key, e)
            parts = []
            if pc:
                parts.append(f"Now: {pc.title} (until {hhmm(cs.end)})")
            if pn:
                parts.append(f"Next {hhmm(ns.start)}: {pn.title}")
            row.epg.set_text("   ".join(parts))
        self._update_now_playing()
        return True

    def _update_now_playing(self):
        svc = self.session.service if self.session else None
        if not svc or not self.player:
            self.now_playing.set_text("")
            self._last_title = None
            return
        pc, cs, pn, ns = self.session.now_next(svc)
        title = f"{svc.channel} {svc.name}" + (f" - {pc.title}" if pc else "")
        info = title + (f"   ({hhmm(cs.start)}-{hhmm(cs.end)}" + (f", {pc.rating}" if pc.rating else "") + ")" if pc else "")
        if pc and pc.description:
            info += "\n" + pc.description
        self.now_playing.set_text(info)
        if title != self._last_title:
            self._last_title = title
            self.player.command("set_property", "force-media-title", title)

    def _open_guide(self):
        if self.guide_win is None:
            self._update_guide_view()
            self.guide_win = GuideWindow(self, self.guide_view, on_watch=self._watch)
            self.guide_win.connect("close-request", self._guide_closed)
        self.guide_win.present()

    def _guide_closed(self, *_):
        self.guide_win = None
        return False

    def _watch(self, service):
        """From the guide: select `service` (retuning if needed) and play it."""
        key = service.extra.get("key")
        for row in self._rows():
            if row.key == key:
                if self.svc_list.get_selected_row() is row:
                    self._on_row(self.svc_list, row)
                else:
                    self.svc_list.select_row(row)
                break
        self._play_when_ready = True
        self._update_buttons()

    # -- tracks and languages ----------------------------------------------------------
    def _set_tracks(self, tracks, video, audio, delivery):
        if not self.session:
            return False
        self.tracks = tracks
        self._fill_langs(audio)
        self._update_guide_view()
        self._update_rows()
        return False

    def _fill_langs(self, current=None):
        self._filling = True
        while self.lang_model.get_n_items():
            self.lang_model.remove(0)
        self.langs = [t for t in self.tracks if t.kind == "audio"]
        for t in self.langs:
            self.lang_model.append(t.label)
        if current:
            idx = next((i for i, t in enumerate(self.langs)
                        if (t.rep_id, t.lang) == (current.rep_id, current.lang)), None)
            if idx is not None:
                self.lang.set_selected(idx)
        self.lang.set_sensitive(len(self.langs) > 1)
        v = self.session.video if self.session else None
        if v and current:
            where = "internet, " if self.session.broadband else ""
            res = f" {v.role}" if v.role else ""
            self.track_info.set_text(f"{where}video {v.codecs.split('.')[0]}{res}, audio {current.label}")
        elif not self.session:
            self.track_info.set_text("")
        self._filling = False
        self._update_buttons()

    def _on_lang(self, *_):
        if getattr(self, "_filling", False) or not self.session:
            return
        i = self.lang.get_selected()
        if 0 <= i < len(self.langs):
            lang = self.langs[i].lang
            self.cfg["lang"] = lang
            self._save()
            if getattr(self.session, "single_stream", False):
                # ATSC 1.0: every audio track is in /live.ts - switch mpv's track live
                self.session.set_language(lang)
                self._mpv_audio()
                return
            was_playing = self.player is not None
            self._stop_player()
            self.session.set_language(lang)
            if was_playing:
                self._play()

    def _mpv_audio(self):
        """ATSC 1.0: point mpv at the chosen audio PID (mpv's src-id for TS)."""
        a = self.session.audio if self.session else None
        if not (self.player and a):
            return False
        for t in self.player.get("track-list") or []:
            if t.get("type") == "audio" and t.get("src-id") == a.tsi:
                if not t.get("selected"):
                    self.player.command("set_property", "aid", t["id"])
                return False
        return False

    # -- playback -------------------------------------------------------------------
    def _ready(self):
        return bool(self.session and self.session.video and self.session.audio)

    def _update_buttons(self):
        if self._play_when_ready and self._ready() and self.player is None:
            self._play_when_ready = False
            self._play()
            return
        self.play_btn.set_sensitive(self._ready() and self.player is None)
        self.stop_btn.set_sensitive(self.player is not None)

    def _play(self):
        if not self._ready() or self.player:
            return
        v, a = self.session.server.urls("127.0.0.1")
        svc = self.session.service
        extra = []
        single = getattr(self.session, "single_stream", False)
        if single and self.session.audio.lang:
            extra.append(f"--alang={self.session.audio.lang}")
        try:
            self.player = Mpv(v, a, binary=self.cfg.get("mpv"), log=self.log, extra_args=extra,
                              title=f"{svc.channel} {svc.name}" if svc else "ATSC",
                              local_display=self.over_ssh and self.on_screen.get_active())
        except (OSError, FileNotFoundError) as e:
            self.log(f"can't start mpv: {e}")
        self._last_title = None
        self._update_buttons()
        GLib.timeout_add(1500, lambda: (self._update_now_playing(), False)[1])
        if single:
            # --alang can't tell two tracks of one language apart; pick by PID once
            # mpv has probed the stream
            GLib.timeout_add(2500, self._mpv_audio)
            GLib.timeout_add(6000, self._mpv_audio)

    def _stop_player(self):
        if self.player:
            self.player.stop()
            self.player = None
        self._update_buttons()

    def _on_lan(self, *_):
        on = self.lan.get_active()
        self.cfg["lan"] = on
        self._save()
        if self.session:
            playing = self.player is not None
            self._stop_player()
            self.session.set_lan(on)
            if playing:
                self._play()
        self._update_remote()

    def _update_remote(self):
        if self.lan.get_active():
            h = lan_address()
            port = self.session.server.port if self.session else 8080
            if self.session and getattr(self.session, "single_stream", False):
                self.remote.set_text(f"mpv http://{h}:{port}/live.ts")
            elif self.session:
                self.remote.set_text(f"mpv http://{h}:{port}/video.mp4 --audio-file=http://{h}:{port}/audio.mp4")
            else:
                self.remote.set_text(f"ATSC 3.0: mpv http://{h}:{port}/video.mp4 --audio-file=http://{h}:{port}/audio.mp4"
                                     f"    ATSC 1.0: mpv http://{h}:{port}/live.ts")
        else:
            self.remote.set_text("")

    def _on_close(self, *_):
        self._untune()
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.atsc3player.Gui",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        Window(self).present()


def main():
    App().run(None)
