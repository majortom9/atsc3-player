# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 Bill Murphy <gc2majortom@gmail.com>
"""Programme guide window (from the ESG) for atsc3-gui."""

import threading
import time
import urllib.request

from gi.repository import Gdk, GLib, Gtk

_posters = {}                  # url -> bytes (or None while loading / on failure)


def hhmm(t):
    return time.strftime("%H:%M", time.localtime(t))


def texture(data):
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
    except GLib.Error:
        return None


class GuideWindow(Gtk.Window):
    def __init__(self, parent, session, on_watch):
        super().__init__(title="Programme guide", transient_for=parent)
        self.set_default_size(900, 600)
        self.session, self.on_watch = session, on_watch
        self.selected_slot = None
        self.channel = None

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=230)
        self.set_child(paned)

        self.chan_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.chan_list.connect("row-selected", self._on_channel)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.chan_list)
        paned.set_start_child(sc)

        right = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=330)
        paned.set_end_child(right)
        self.prog_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.prog_list.connect("row-selected", self._on_programme)
        self.prog_list.connect("row-activated", lambda *_: self._watch())
        psc = Gtk.ScrolledWindow()
        psc.set_child(self.prog_list)
        right.set_start_child(psc)

        detail = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        self.poster = Gtk.Picture(width_request=120, height_request=180, can_shrink=True,
                                  content_fit=Gtk.ContentFit.CONTAIN)
        detail.append(self.poster)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.d_title = Gtk.Label(xalign=0, wrap=True, use_markup=True, selectable=True)
        self.d_when = Gtk.Label(xalign=0, css_classes=["dim-label"])
        self.d_desc = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True, vexpand=True)
        self.watch_btn = Gtk.Button(label="Watch", halign=Gtk.Align.START, sensitive=False)
        self.watch_btn.connect("clicked", lambda *_: self._watch())
        for w in (self.d_title, self.d_when, self.d_desc, self.watch_btn):
            info.append(w)
        detail.append(info)
        dsc = Gtk.ScrolledWindow()
        dsc.set_child(detail)
        right.set_end_child(dsc)

        self.refresh()

    # -- data ----------------------------------------------------------------
    def _channels(self):
        """[(lls.Service, GuideService)] for guide services found in the SLT."""
        g = self.session.esg.guide
        out = []
        for s in self.session.lls.services:
            gs = g.service_for(s.global_id, s.major, s.minor)
            if gs:
                out.append((s, gs))
        return out

    def refresh(self):
        keep = self.channel[0].service_id if self.channel else None
        while (row := self.chan_list.get_first_child()) is not None:
            self.chan_list.remove(row)
        g = self.session.esg.guide
        for s, gs in self._channels():
            hb = Gtk.Box(spacing=8, margin_top=3, margin_bottom=3)
            pic = Gtk.Picture(width_request=48, height_request=36, can_shrink=True,
                              content_fit=Gtk.ContentFit.CONTAIN)
            if gs.icon in g.icons:
                tex = texture(g.icons[gs.icon])
                if tex:
                    pic.set_paintable(tex)
            hb.append(pic)
            note = "" if s.playable else "  (DRM)" if s.protected else f"  ({s.category_name})"
            hb.append(Gtk.Label(label=f"{s.channel}  {s.name}{note}", xalign=0))
            row = Gtk.ListBoxRow(child=hb)
            row.chan = (s, gs)
            self.chan_list.append(row)
            if s.service_id == keep or (keep is None and self.session.service
                                        and s.service_id == self.session.service.service_id):
                self.chan_list.select_row(row)
        if self.chan_list.get_selected_row() is None and self.chan_list.get_first_child():
            self.chan_list.select_row(self.chan_list.get_first_child())

    def _on_channel(self, _lb, row):
        if row is None:
            return
        self.channel = row.chan
        s, gs = row.chan
        g = self.session.esg.guide
        while (r := self.prog_list.get_first_child()) is not None:
            self.prog_list.remove(r)
        now = time.time()
        day = None
        first_now = None
        for slot in g.slots(gs):
            if slot.end <= now:
                continue
            d = time.strftime("%A %d %B", time.localtime(slot.start))
            if d != day:
                day = d
                hdr = Gtk.ListBoxRow(selectable=False, activatable=False,
                                     child=Gtk.Label(label=f"<b>{d}</b>", use_markup=True, xalign=0,
                                                     margin_top=6))
                self.prog_list.append(hdr)
            p = g.programme(slot)
            title = p.title if p else "?"
            airing = slot.start <= now < slot.end
            lbl = Gtk.Label(xalign=0, use_markup=True,
                            label=f"<tt>{hhmm(slot.start)}</tt>  "
                                  + (f"<b>{GLib.markup_escape_text(title)}</b>  <small>(on now)</small>"
                                     if airing else GLib.markup_escape_text(title)))
            r = Gtk.ListBoxRow(child=lbl)
            r.slot = slot
            self.prog_list.append(r)
            if airing and first_now is None:
                first_now = r
        if first_now:
            self.prog_list.select_row(first_now)

    def _on_programme(self, _lb, row):
        slot = getattr(row, "slot", None) if row else None
        self.selected_slot = slot
        if not slot:
            return
        g = self.session.esg.guide
        p = g.programme(slot)
        s, gs = self.channel
        self.d_title.set_markup(f"<big><b>{GLib.markup_escape_text(p.title if p else '?')}</b></big>")
        when = time.strftime("%a %d %b ", time.localtime(slot.start)) + f"{hhmm(slot.start)}-{hhmm(slot.end)}"
        extras = [x for x in (p.rating if p else "", f"{s.channel} {s.name}") if x]
        self.d_when.set_text(when + "   " + "   ".join(extras))
        self.d_desc.set_text(p.description if p else "")
        now = time.time()
        self.watch_btn.set_sensitive(slot.start <= now < slot.end and s.playable)
        self.poster.set_paintable(None)
        if p and p.poster_url:
            self._load_poster(p.poster_url, slot)

    def _load_poster(self, url, slot):
        data = _posters.get(url)
        if data:
            self.poster.set_paintable(texture(data))
            return
        if url in _posters:
            return                                   # loading, or failed

        def fetch():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "atsc3-player"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    _posters[url] = r.read()
            except OSError:
                _posters[url] = None
                return
            GLib.idle_add(lambda: (self.selected_slot is slot and
                                   self.poster.set_paintable(texture(_posters[url]))) and False)
        _posters[url] = None
        threading.Thread(target=fetch, daemon=True).start()

    def _watch(self):
        slot, chan = self.selected_slot, self.channel
        if not slot or not chan:
            return
        s, _ = chan
        now = time.time()
        if s.playable and slot.start <= now < slot.end:
            self.on_watch(s)
