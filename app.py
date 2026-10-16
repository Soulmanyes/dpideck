#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""Desktop settings app for the VXE MAD R (and other COMPX-family ATK mice)."""

import json
import os
import queue
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import atk  # noqa: E402

APP_ID = "local.atkmouse.Settings"
CONFIG_DIR = os.path.expanduser("~/.config/atk-mouse")

RATES = [(125, "125 Hz"), (250, "250 Hz"), (500, "500 Hz"), (1000, "1000 Hz")]
SENSOR_MODES = [(0, "Basic"), (2, "Factory default"),
                (4, "Competitive"), (5, "Competitive Max")]
LOD = [(1, "1 mm"), (2, "2 mm")]
DEBOUNCE = [(0, "Off"), (1, "1 ms"), (2, "2 ms"), (4, "4 ms"),
            (8, "8 ms"), (15, "15 ms"), (20, "20 ms")]
SLEEP = [(30, "30 s"), (60, "1 min"), (120, "2 min"), (180, "3 min"),
         (300, "5 min"), (1200, "20 min"), (1500, "25 min"), (1800, "30 min")]
ACTIONS = [
    ({"kind": "mouse", "btn": 0}, "Left click"),
    ({"kind": "mouse", "btn": 1}, "Right click"),
    ({"kind": "mouse", "btn": 2}, "Middle click"),
    ({"kind": "mouse", "btn": 3}, "Back"),
    ({"kind": "mouse", "btn": 4}, "Forward"),
    ({"kind": "dpi", "op": "cycle"}, "DPI cycle"),
    ({"kind": "dpi", "op": "up"}, "DPI up"),
    ({"kind": "dpi", "op": "down"}, "DPI down"),
    ({"kind": "scroll", "dir": "up"}, "Scroll up"),
    ({"kind": "scroll", "dir": "down"}, "Scroll down"),
    ({"kind": "scroll", "dir": "left"}, "Scroll left"),
    ({"kind": "scroll", "dir": "right"}, "Scroll right"),
    ({"kind": "disable"}, "Disabled"),
]
MAX_DPI = 26000   # PAW3395


class Worker:
    """Runs device I/O off the UI thread, one job at a time."""

    def __init__(self):
        self.jobs = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, fn, done=None, fail=None):
        self.jobs.put((fn, done, fail))

    def _run(self):
        while True:
            fn, done, fail = self.jobs.get()
            try:
                result = fn()
            except Exception as e:   # reported to the UI, never fatal
                if fail:
                    GLib.idle_add(fail, e)
                continue
            if done:
                GLib.idle_add(done, result)


class Choice:
    """An Adw.ComboRow over (value, label) pairs that can show unknown values."""

    def __init__(self, title, options, on_pick, subtitle=None):
        self.options = list(options)
        self.base = list(options)
        self.on_pick = on_pick
        self.row = Adw.ComboRow(title=title)
        if subtitle:
            self.row.set_subtitle(subtitle)
        self._set_model()
        self.quiet = False
        self.row.connect("notify::selected", self._changed)

    def _set_model(self):
        self.row.set_model(Gtk.StringList.new([lbl for _v, lbl in self.options]))

    def limit(self, options):
        """Swap the offered options, keeping the current pick if it still fits."""
        cur = self.options[self.row.get_selected()][0] if self.options else None
        self.quiet = True
        self.base = self.options = list(options)
        self._set_model()
        self.quiet = False
        if cur is not None:
            self.set(cur)

    def set(self, value):
        self.quiet = True
        values = [v for v, _l in self.options]
        if value not in values:
            self.options = self.base + [(value, f"Other ({value})")]
            self._set_model()
            values = [v for v, _l in self.options]
        self.row.set_selected(values.index(value))
        self.quiet = False

    def _changed(self, *_):
        if self.quiet:
            return
        i = self.row.get_selected()
        if 0 <= i < len(self.options):
            self.on_pick(self.options[i][0], self.options[i][1])


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Mouse Settings")
        self.set_default_size(560, 780)
        self.worker = Worker()
        self.mouse = None
        self.state = None
        self.loading = False
        self.connecting = False
        self.nodes = None
        self.dpi_timers = {}

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="Mouse Settings", subtitle="Looking for mouse…")
        header.set_title_widget(self.title)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload from mouse")
        refresh.connect("clicked", lambda *_: self.reload())
        header.pack_start(refresh)

        menu = Gio.Menu()
        menu.append("Save backup…", "win.backup")
        menu.append("Restore backup…", "win.restore")
        menu.append("Factory reset", "win.reset")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        for name, cb in (("backup", self.on_backup), ("restore", self.on_restore),
                         ("reset", self.on_reset)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

        view.add_top_bar(header)
        self.banner = Adw.Banner(button_label="Retry")
        self.banner.connect("button-clicked", lambda *_: self.connect_mouse())
        view.add_top_bar(self.banner)

        self.page = Adw.PreferencesPage()
        self.page.set_sensitive(False)
        self._build_page()
        self.toasts.set_child(self.page)
        view.set_content(self.toasts)
        self.set_content(view)

        self.connect_mouse()
        GLib.timeout_add_seconds(3, self._poll)

    # ---- layout ---------------------------------------------------------
    def _build_page(self):
        dpi = Adw.PreferencesGroup(
            title="Sensitivity (DPI)",
            description="To switch stages with the mouse, set a button to DPI cycle below.")
        self.stage_count = Adw.SpinRow.new_with_range(1, 8, 1)
        self.stage_count.set_title("Number of stages")
        self.stage_count.connect("notify::value", self.on_stage_count)
        dpi.add(self.stage_count)
        self.active = Choice("Active stage", [(i, f"Stage {i + 1}") for i in range(8)],
                             self.on_active)
        dpi.add(self.active.row)
        self.stage_rows = []
        for i in range(8):
            row = Adw.SpinRow.new_with_range(50, MAX_DPI, 50)
            row.set_snap_to_ticks(True)
            row.set_title(f"Stage {i + 1}")
            row.connect("notify::value", self.on_stage_dpi, i)
            dpi.add(row)
            self.stage_rows.append(row)
        self.page.add(dpi)

        perf = Adw.PreferencesGroup(title="Sensor")
        self.rate = Choice("Polling rate", RATES,
                           lambda v, l: self.apply(lambda m: m.set_rate(v), f"Polling rate {l}"))
        self.mode = Choice("Sensor mode", SENSOR_MODES,
                           lambda v, l: self.apply(lambda m: m.set_sensor_mode(v), f"Sensor mode: {l}"),
                           subtitle="Competitive modes use more battery")
        self.lod = Choice("Lift-off distance", LOD,
                          lambda v, l: self.apply(lambda m: m.set_lod(v), f"Lift-off distance {l}"))
        for c in (self.rate, self.mode, self.lod):
            perf.add(c.row)
        self.switches = {}
        for key, title, sub, setter in (
                ("motion_sync", "Motion sync", "Smoother tracking, about 1 ms more latency",
                 "set_motion_sync"),
                ("angle_snap", "Angle snapping", "Straightens lines you draw", "set_angle_snap"),
                ("ripple", "Ripple control", "Smooths jitter at high DPI", "set_ripple")):
            row = Adw.SwitchRow(title=title, subtitle=sub)
            row.connect("notify::active", self.on_switch, setter, title)
            perf.add(row)
            self.switches[key] = row
        self.page.add(perf)

        misc = Adw.PreferencesGroup(title="Clicks and power")
        self.debounce = Choice("Click debounce", DEBOUNCE,
                               lambda v, l: self.apply(lambda m: m.set_debounce(v), f"Debounce {l}"))
        self.sleep = Choice("Sleep after", SLEEP,
                            lambda v, l: self.apply(lambda m: m.set_sleep(v), f"Sleeps after {l}"))
        misc.add(self.debounce.row)
        misc.add(self.sleep.row)
        self.page.add(misc)

        btns = Adw.PreferencesGroup(title="Buttons")
        self.buttons = []
        for i, name in enumerate(atk.BUTTON_NAMES):
            c = Choice(name, [(json.dumps(a, sort_keys=True), l) for a, l in ACTIONS],
                       lambda v, l, i=i, name=name: self.apply(
                           lambda m: m.set_button(i, json.loads(v)), f"{name}: {l}"))
            btns.add(c.row)
            self.buttons.append(c)
        # the MAD R board has a sixth (DPI) button slot but no physical button
        self.buttons[5].row.set_visible(False)
        self.page.add(btns)

    # ---- connection -----------------------------------------------------
    def _poll(self):
        # reconnect when nothing is open, or a cable/dongle was plugged or pulled
        if not self.connecting and (self.mouse is None or atk.find_nodes() != self.nodes):
            self.connect_mouse()
        return True

    def connect_mouse(self):
        if self.connecting:
            return
        self.connecting = True

        def job():
            if self.mouse:
                self.mouse.close()
            self.mouse = None
            m = atk.Mouse()
            self.mouse = m
            self.nodes = atk.find_nodes()
            return m.read_state()   # waits a few seconds for a sleeping mouse

        def done(s):
            self.connecting = False
            self.show_state(s)

        def fail(e):
            self.connecting = False
            self.show_error(e)
        self.worker.submit(job, done, fail)

    def reload(self):
        if self.mouse is None:
            return self.connect_mouse()
        self.worker.submit(self.mouse.read_state, self.show_state, self.show_error)

    def show_error(self, e):
        if isinstance(e, atk.Asleep):
            self.banner.set_title("Mouse is asleep. Move it, then press Retry.")
        elif isinstance(e, OSError):
            self.mouse = None
            self.banner.set_title("Mouse disconnected.")
        else:
            self.banner.set_title(str(e))
        self.banner.set_revealed(True)
        if self.mouse is None:
            self.page.set_sensitive(False)
            self.title.set_subtitle("Not connected")

    def show_state(self, s):
        self.state = s
        self.banner.set_revealed(False)
        self.page.set_sensitive(True)
        link = "cable" if self.mouse.pid not in atk.DONGLE_PIDS else "wireless"
        bat = s.get("battery")
        sub = f"{link}"
        if bat:
            sub += f" · battery {bat['level']}%" + (" (charging)" if bat["charging"] else "")
        self.title.set_title(s["id"]["name"])
        self.title.set_subtitle(sub)

        self.loading = True
        self.stage_count.set_value(s["stage_count"])
        for i, row in enumerate(self.stage_rows):
            row.set_value(min(s["stages"][i], MAX_DPI))
            row.set_visible(i < s["stage_count"])
        self.active.limit([(i, f"Stage {i + 1}") for i in range(s["stage_count"])])
        self.active.set(s["active_stage"])
        self.rate.set(s["rate_hz"])
        self.mode.set(s["sensor_mode"])
        self.lod.set(s["lod_code"])
        self.debounce.set(s["debounce_ms"])
        self.sleep.set(s["sleep_s"])
        for key, row in self.switches.items():
            row.set_active(s[key])
        for c, a in zip(self.buttons, s["buttons"]):
            c.set(json.dumps(a, sort_keys=True))
        self.loading = False

    # ---- writes ---------------------------------------------------------
    def apply(self, fn, message, after=None):
        if self.loading or self.mouse is None:
            return
        m = self.mouse

        def done(_):
            self.toasts.add_toast(Adw.Toast(title=message, timeout=2))
            if after:
                after()

        def fail(e):
            self.show_error(e)
            self.toasts.add_toast(Adw.Toast(title="Not saved", timeout=3))
            self.reload()   # put the UI back to what the mouse really has

        self.worker.submit(lambda: fn(m), done, fail)

    def on_switch(self, row, _pspec, setter, title):
        on = row.get_active()
        self.apply(lambda m: getattr(m, setter)(on), f"{title} {'on' if on else 'off'}")

    def on_stage_count(self, row, _pspec):
        n = int(row.get_value())
        for i, r in enumerate(self.stage_rows):
            r.set_visible(i < n)
        if not self.loading:
            self.active.limit([(i, f"Stage {i + 1}") for i in range(n)])
        self.apply(lambda m: m.set_stage_count(n), f"{n} DPI stage{'s' if n > 1 else ''}")

    def on_active(self, i, label):
        self.apply(lambda m: m.set_active_stage(i), f"Active: {label}")

    def on_stage_dpi(self, row, _pspec, i):
        if self.loading:
            return
        # wait until the user stops typing or clicking before writing
        if i in self.dpi_timers:
            GLib.source_remove(self.dpi_timers[i])

        def fire():
            del self.dpi_timers[i]
            dpi = int(row.get_value())
            self.apply(lambda m: m.set_stage_dpi(i, dpi), f"Stage {i + 1}: {dpi} DPI")
            return False
        self.dpi_timers[i] = GLib.timeout_add(500, fire)

    # ---- menu -----------------------------------------------------------
    def _file_dialog(self, save, cb):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        d = Gtk.FileDialog(initial_folder=Gio.File.new_for_path(CONFIG_DIR))
        if save:
            d.set_initial_name(f"backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
            d.save(self, None, lambda d, r: self._file_done(d.save_finish, r, cb))
        else:
            d.open(self, None, lambda d, r: self._file_done(d.open_finish, r, cb))

    @staticmethod
    def _file_done(finish, result, cb):
        try:
            f = finish(result)
        except GLib.Error:
            return   # cancelled
        if f:
            cb(f.get_path())

    def on_backup(self, *_):
        if self.mouse is None:
            return

        def save(path):
            m = self.mouse

            def job():
                with open(path, "w") as f:
                    json.dump(m.backup(), f, indent=1)
            self.worker.submit(job, lambda _: self.toasts.add_toast(
                Adw.Toast(title="Backup saved")), self.show_error)
        self._file_dialog(True, save)

    def on_restore(self, *_):
        if self.mouse is None:
            return

        def load(path):
            m = self.mouse

            def job():
                with open(path) as f:
                    return m.restore(json.load(f))

            def done(failed):
                msg = ("Backup restored" if not failed
                       else f"Restored, except {', '.join(failed)}")
                self.toasts.add_toast(Adw.Toast(title=msg))
                self.reload()
            self.worker.submit(job, done, self.show_error)
        self._file_dialog(False, load)

    def on_reset(self, *_):
        if self.mouse is None:
            return
        dlg = Adw.AlertDialog(heading="Factory reset?",
                              body="All settings on the mouse go back to factory defaults.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("reset", "Reset")
        dlg.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)

        def chosen(_d, resp):
            if resp == "reset":
                m = self.mouse
                self.worker.submit(lambda: (m.factory_reset(), time.sleep(1)),
                                   lambda _: self.reload(), self.show_error)
        dlg.connect("response", chosen)
        dlg.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
