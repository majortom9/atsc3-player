"""atsc3-gui: tune an ATSC 3.0 channel, pick a service and language, and play
it in mpv-ac4 - optionally serving it to other machines over HTTP."""

import json
import os
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import tuner                       # noqa: E402
from .mpv import Mpv, find_mpv            # noqa: E402
from .session import Session              # noqa: E402
from .streams import lan_address          # noqa: E402

CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                      "atsc3-player", "config.json")


def us_channels():
    """[(label, Hz)] for US broadcast RF channels 2-36 (ATSC 3.0 lives on UHF)."""
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
        out.append((f"RF {ch}  ({mhz} MHz)", mhz * 1_000_000))
    return out


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


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="ATSC 3.0")
        self.set_default_size(720, 640)
        self.cfg = load_config()
        self.fe = None
        self.session = None
        self.player = None
        self.services = []
        self.tracks = []
        self.locked = False
        self.channels = us_channels()
        self.connect("close-request", self._on_close)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=10, margin_bottom=10, margin_start=10, margin_end=10)
        self.set_child(box)

        # -- tuning -----------------------------------------------------------
        row = Gtk.Box(spacing=6)
        box.append(row)
        row.append(Gtk.Label(label="Channel"))
        self.chan = Gtk.DropDown.new_from_strings([c[0] for c in self.channels])
        freq = self.cfg.get("freq_hz", 485_000_000)
        idx = next((i for i, c in enumerate(self.channels) if c[1] == freq), 16 - 2)
        self.chan.set_selected(idx)
        row.append(self.chan)
        row.append(Gtk.Label(label="PLPs"))
        self.plp = Gtk.Entry(placeholder_text="auto", width_chars=8,
                             text=self.cfg.get("plps", ""))
        self.plp.set_tooltip_text("Blank = auto (the driver selects every PLP the channel "
                                  "carries). Or a comma list, e.g. 0,1")
        row.append(self.plp)
        self.tune_btn = Gtk.Button(label="Tune")
        self.tune_btn.connect("clicked", self._on_tune)
        row.append(self.tune_btn)
        self.untune_btn = Gtk.Button(label="Stop tuner", sensitive=False)
        self.untune_btn.connect("clicked", lambda *_: self._untune())
        row.append(self.untune_btn)

        srow = Gtk.Box(spacing=6)
        box.append(srow)
        self.status = Gtk.Label(label="Not tuned", xalign=0, hexpand=True, selectable=True)
        srow.append(self.status)
        self.stats_btn = Gtk.Button(label="Refresh signal", sensitive=False)
        self.stats_btn.connect("clicked", lambda *_: self._read_stats())
        srow.append(self.stats_btn)
        self.rate = Gtk.Label(label="", xalign=1)
        srow.append(self.rate)

        # -- services ---------------------------------------------------------
        box.append(Gtk.Label(label="<b>Services</b>", use_markup=True, xalign=0))
        self.svc_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.svc_list.connect("row-selected", self._on_service)
        sc = Gtk.ScrolledWindow(vexpand=True, min_content_height=180)
        sc.set_child(self.svc_list)
        box.append(sc)

        # -- playback -----------------------------------------------------------
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
        GLib.timeout_add(500, self._poll)

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

    # -- tuning -------------------------------------------------------------------
    def _on_tune(self, *_):
        try:
            plps = self._plps()
            tuner.pack_plps(plps)
        except ValueError:
            self.status.set_text("PLPs: up to four numbers 0-63, comma separated, or blank")
            return
        self._stop_player()
        self._close_session()
        try:
            if self.fe is None:
                found = tuner.find_atsc3_frontend()
                if not found:
                    self.status.set_text("No ATSC 3.0 tuner found (is the HDTV Mate plugged in?)")
                    return
                a, f, name = found
                self.fe = tuner.Frontend(a, f)
                self.log(f"tuner: adapter {a} frontend {f} ({name})")
            self.fe.tune(self._freq(), plps)
        except OSError as e:
            self.status.set_text(f"Tune failed: {e.strerror or e}")
            return
        self.locked = False
        self.cfg.update(freq_hz=self._freq(), plps=self.plp.get_text().strip())
        self._save()
        self.status.set_text(f"Tuning {self._freq() / 1e6:.0f} MHz, PLPs {plps or 'auto'} ...")
        self.untune_btn.set_sensitive(True)
        self._set_services([])

    def _untune(self):
        self._stop_player()
        self._close_session()
        if self.fe:
            self.fe.close()          # brings alp0 down
            self.fe = None
        self.locked = False
        self.status.set_text("Not tuned")
        self.untune_btn.set_sensitive(False)
        self.stats_btn.set_sensitive(False)
        self._set_services([])

    def _read_stats(self):
        if not self.fe:
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
        self.status.set_text(", ".join(bits) + f"  ({self._freq() / 1e6:.0f} MHz)")

    def _poll(self):
        if self.fe:
            try:
                locked = self.fe.locked()
            except OSError:
                locked = False
            if locked and not self.locked:
                self.locked = True
                try:
                    ifname = self.fe.set_alp_up(True)
                except OSError as e:
                    self.status.set_text(f"Locked, but can't bring the ALP interface up: {e}")
                    return True
                self.log(f"locked; {ifname} up")
                self._read_stats()
                self.stats_btn.set_sensitive(True)
                self._open_session(ifname)
            elif not locked and self.locked:
                self.locked = False
                self.status.set_text("Lock lost - waiting ...")
        if self.player and not self.player.running():
            self.log("mpv exited")
            self.player = None
            self._update_buttons()
        return True

    # -- session ----------------------------------------------------------------
    def _open_session(self, ifname):
        if self.session:
            return
        try:
            self.session = Session(ifname, lan=self.lan.get_active(), log=self.log,
                                   on_services=lambda s: GLib.idle_add(self._set_services, s),
                                   on_tracks=lambda *a: GLib.idle_add(self._set_tracks, *a),
                                   on_status=lambda st: GLib.idle_add(self._show_rate, st))
            self.session.start()
        except OSError as e:
            self.session = None
            self.status.set_text(f"Can't capture on {ifname}: {e} (setcap cap_net_raw on python?)")

    def _close_session(self):
        if self.session:
            self.session.close()
            self.session = None
        self.tracks = []
        self._fill_langs()

    def _show_rate(self, st):
        drops = f", {st['drops']} dropped" if st["drops"] else ""
        self.rate.set_text(f"{st['rate_mbps']:.1f} Mbit/s{drops}")
        return False

    # -- services -----------------------------------------------------------------
    def _set_services(self, services):
        keep = self._selected_service()
        self.services = list(services)
        while (row := self.svc_list.get_first_child()) is not None:
            self.svc_list.remove(row)
        for s in self.services:
            note = []
            if s.protected:
                note.append("DRM protected")
            if s.category != 1:
                note.append(s.category_name)
            text = f"{s.channel:>6}   {s.name:<10} {s.service_id:>6}   {', '.join(note)}"
            lbl = Gtk.Label(label=text, xalign=0, css_classes=["monospace"])
            row = Gtk.ListBoxRow(child=lbl, sensitive=s.playable)
            row.service = s
            self.svc_list.append(row)
            want = (keep.service_id if keep else self.cfg.get("service_id"))
            if s.service_id == want and s.playable:
                self.svc_list.select_row(row)
        return False

    def _selected_service(self):
        row = self.svc_list.get_selected_row()
        return getattr(row, "service", None) if row else None

    def _on_service(self, _lb, row):
        svc = getattr(row, "service", None) if row else None
        if not svc or not self.session:
            return
        if self.session.service and self.session.service.service_id == svc.service_id:
            return
        self._stop_player()
        self.cfg["service_id"] = svc.service_id
        self._save()
        self.tracks = []
        self._fill_langs()
        self.track_info.set_text("waiting for the service signalling ...")
        self.session.select_service(svc, lang=self.cfg.get("lang"))

    def _set_tracks(self, tracks, video, audio, delivery):
        self.tracks = tracks
        if delivery == "broadband" and not tracks:
            self.track_info.set_text("broadband-only service (internet delivery), not on the air")
        self._fill_langs(audio)
        self._update_buttons()
        return False

    def _fill_langs(self, current=None):
        self._filling = True
        while self.lang_model.get_n_items():
            self.lang_model.remove(0)
        self.langs = [t for t in self.tracks if t.kind == "audio"]
        for t in self.langs:
            self.lang_model.append(t.label)
        if current in self.langs:
            self.lang.set_selected(self.langs.index(current))
        self.lang.set_sensitive(len(self.langs) > 1)
        v = next((t for t in self.tracks if t.kind == "video"), None)
        if v and current:
            self.track_info.set_text(f"video {v.codecs.split('.')[0]}, audio {current.label}")
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
            was_playing = self.player is not None
            self._stop_player()
            self.session.set_language(lang)
            if was_playing:
                self._play()

    # -- playback -------------------------------------------------------------------
    def _ready(self):
        return bool(self.session and self.session.video and self.session.audio)

    def _update_buttons(self):
        self.play_btn.set_sensitive(self._ready() and self.player is None)
        self.stop_btn.set_sensitive(self.player is not None)

    def _play(self):
        if not self._ready() or self.player:
            return
        v, a = self.session.server.urls("127.0.0.1")
        svc = self.session.service
        try:
            self.player = Mpv(v, a, binary=self.cfg.get("mpv"), log=self.log,
                              title=f"{svc.channel} {svc.name}" if svc else "ATSC 3.0",
                              local_display=self.over_ssh and self.on_screen.get_active())
        except (OSError, FileNotFoundError) as e:
            self.log(f"can't start mpv: {e}")
        self._update_buttons()

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
            self.remote.set_text(f"mpv http://{h}:{port}/video.mp4 --audio-file=http://{h}:{port}/audio.mp4")
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
