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
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import atk  # noqa: E402
import updater

APP_ID = "local.dpideck.DPIdeck"
VERSION = "2.2"   # bump together with the release tag (v2.2)
REPO = "Soulmanyes/dpideck"
CONFIG_DIR = atk.CONFIG_DIR

# Fixed lift-off steps; the codes are not in ascending order
LOD_LEVELS = {"levels": [(1, "1 mm"), (2, "2 mm")],
              "levels07": [(3, "0.7 mm"), (1, "1 mm"), (2, "2 mm")]}
DEBOUNCE = [(0, "Off"), (1, "1 ms"), (2, "2 ms"), (4, "4 ms"),
            (8, "8 ms"), (15, "15 ms"), (20, "20 ms")]
LIGHT_MODES = [("off", "Off"), ("steady", "Steady"), ("breathing", "Breathing")]
BRIGHTNESS = [(0, "Low"), (1, "Medium"), (2, "High")]
SPEED = [(0, "Slow"), (1, "Medium"), (2, "Fast")]
RX_SLEEP = [(0, "1 min"), (1, "5 min"), (2, "10 min")]
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


def hex_rgba(color):
    rgba = Gdk.RGBA()
    rgba.parse(color)
    return rgba


def rgba_hex(rgba):
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in (rgba.red, rgba.green, rgba.blue))


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

    def value(self):
        return self.options[self.row.get_selected()][0]

    def label(self):
        return self.options[self.row.get_selected()][1]

    def _changed(self, *_):
        if self.quiet:
            return
        i = self.row.get_selected()
        if 0 <= i < len(self.options):
            self.on_pick(self.options[i][0], self.options[i][1])


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="DPIdeck")
        self.set_default_size(560, 780)
        self.worker = Worker()
        self.mouse = None
        self.state = None
        self.loading = False
        self.connecting = False
        self.unsupported = False
        self.nodes = None
        self.dpi_timers = {}

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="DPIdeck", subtitle="Looking for mouse…")
        header.set_title_widget(self.title)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload from mouse")
        refresh.connect("clicked", lambda *_: self.reload())
        header.pack_start(refresh)

        menu = Gio.Menu()
        menu.append("Save backup…", "win.backup")
        menu.append("Restore backup…", "win.restore")
        menu.append("Factory reset", "win.reset")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        self.actions = {}
        for name, cb in (("backup", self.on_backup), ("restore", self.on_restore),
                         ("reset", self.on_reset)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            self.actions[name] = act

        view.add_top_bar(header)
        self.banner = Adw.Banner(button_label="Retry")
        self.banner.connect("button-clicked", lambda *_: self.connect_mouse())
        view.add_top_bar(self.banner)
        self.update_banner = Adw.Banner(button_label="Update")
        self.update_banner.connect("button-clicked", lambda *_: self.install_update())
        view.add_top_bar(self.update_banner)

        self.page = Adw.PreferencesPage()
        self._build_page()
        self.set_mouse_sensitive(False)
        self.toasts.set_child(self.page)
        view.set_content(self.toasts)
        self.set_content(view)

        # The mouse wheel must scroll the page, never change a number box: a
        # stray scroll over a DPI box would otherwise write a new DPI.
        for spin in (self.stage_count, *self.stage_rows, self.lod_mm):
            ctrl = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
            ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            ctrl.connect("scroll", self._scroll_page)
            spin.add_controller(ctrl)

        self.connect_mouse()
        GLib.timeout_add_seconds(3, self._poll)
        self.check_update()

    def _scroll_page(self, _ctrl, _dx, dy):
        w = self.page.get_first_child()
        while w and not isinstance(w, Gtk.ScrolledWindow):
            w = w.get_first_child()
        if w:
            adj = w.get_vadjustment()
            adj.set_value(adj.get_value() + dy * 50)
        return True   # swallow it so the number box never sees it

    # ---- layout ---------------------------------------------------------
    def _build_page(self):
        dpi = self.dpi_group = Adw.PreferencesGroup(
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
        self.stage_colors = []
        for i in range(8):
            row = Adw.SpinRow.new_with_range(50, 26000, 50)
            row.set_snap_to_ticks(True)
            row.set_title(f"Stage {i + 1}")
            row.connect("notify::value", self.on_stage_dpi, i)
            color = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                          valign=Gtk.Align.CENTER,
                                          tooltip_text=f"Stage {i + 1} light colour")
            color.connect("notify::rgba", self.on_stage_color, i)
            row.add_suffix(color)
            self.stage_colors.append(color)
            dpi.add(row)
            self.stage_rows.append(row)
        self.page.add(dpi)

        perf = Adw.PreferencesGroup(title="Sensor")
        self.rate = Choice("Polling rate", [(1000, "1000 Hz")],
                           lambda v, l: self.apply(lambda m: m.set_rate(v), f"Polling rate {l}"))
        self.mode = Choice("Sensor mode", [(0, "Basic")],
                           lambda v, l: self.apply(lambda m: m.set_sensor_mode(v), f"Sensor mode: {l}"),
                           subtitle="Competitive modes use more battery")
        self.lod = Choice("Lift-off distance", LOD_LEVELS["levels"],
                          lambda v, l: self.apply(lambda m: m.set_lod(v), f"Lift-off distance {l}"))
        for c in (self.rate, self.mode, self.lod):
            perf.add(c.row)
        # continuous lift-off sensors (PAW3950 Ultra, PAW3955) take any 0.1 mm step
        self.lod_mm = Adw.SpinRow.new_with_range(0.7, 1.7, 0.1)
        self.lod_mm.set_title("Lift-off distance (mm)")
        self.lod_mm.set_digits(1)
        self.lod_mm.set_snap_to_ticks(True)
        self.lod_mm.connect("notify::value", self.on_lod_mm)
        self.lod_timer = None
        perf.add(self.lod_mm)
        # sensors with an unknown lift-off format: show the raw value, don't write
        self.lod_raw = Adw.ActionRow(title="Lift-off distance",
                                     subtitle="Not adjustable for this mouse yet")
        self.lod_raw_value = Gtk.Label(css_classes=["dim-label"])
        self.lod_raw.add_suffix(self.lod_raw_value)
        perf.add(self.lod_raw)
        self.switches = {}
        for key, title, sub, setter in (
                ("motion_sync", "Motion sync", "Smoother tracking, about 1 ms more latency",
                 "set_motion_sync"),
                ("angle_snap", "Angle snapping", "Straightens lines you draw", "set_angle_snap"),
                ("ripple", "Ripple control", "Smooths jitter at high DPI", "set_ripple"),
                ("long_range", "Long range mode",
                 "Stronger wireless signal far from the receiver, uses more battery",
                 "set_long_range")):
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

        light = self.light_group = Adw.PreferencesGroup(
            title="Lighting", description="Stage colours are next to each DPI stage above.")
        self.light_mode = Choice("DPI light", LIGHT_MODES, lambda v, l: self.on_light())
        self.light_brightness = Choice("Brightness", BRIGHTNESS, lambda v, l: self.on_light())
        self.light_speed = Choice("Breathing speed", SPEED, lambda v, l: self.on_light())
        for c in (self.light_mode, self.light_brightness, self.light_speed):
            light.add(c.row)
        self.page.add(light)

        rx = self.rx_group = Adw.PreferencesGroup(title="Receiver light")
        self.rx_mode = Choice("Effect", atk.RX_FULL_MODES, lambda v, l: self.on_rx_light())
        self.rx_color_row = Adw.ActionRow(title="Colour")
        self.rx_color = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                              valign=Gtk.Align.CENTER)
        self.rx_color.connect("notify::rgba", lambda *_: self.on_rx_light())
        self.rx_color_row.add_suffix(self.rx_color)
        self.rx_brightness = Choice("Brightness", BRIGHTNESS, lambda v, l: self.on_rx_light())
        self.rx_speed = Choice("Speed", SPEED, lambda v, l: self.on_rx_light())
        self.rx_sleep = Choice("Turns off after", RX_SLEEP, lambda v, l: self.on_rx_light())
        rx.add(self.rx_mode.row)
        rx.add(self.rx_color_row)
        for c in (self.rx_brightness, self.rx_speed, self.rx_sleep):
            rx.add(c.row)
        self.page.add(rx)

        btns = Adw.PreferencesGroup(title="Buttons")
        self.buttons = []
        for i, name in enumerate(atk.BUTTON_NAMES):
            c = Choice(name, [(json.dumps(a, sort_keys=True), l) for a, l in ACTIONS],
                       lambda v, l, i=i, name=name: self.apply(
                           lambda m: m.set_button(i, json.loads(v)), f"{name}: {l}"))
            btns.add(c.row)
            self.buttons.append(c)
        self.page.add(btns)
        # the update group stays usable without a mouse, so only these go grey
        self.mouse_groups = [dpi, perf, misc, light, rx, btns]
        self.add_update_group()

    def set_mouse_sensitive(self, on):
        for g in self.mouse_groups:
            g.set_sensitive(on)

    # ---- connection -----------------------------------------------------
    def _poll(self):
        # reconnect when nothing is open, or a cable/dongle was plugged or pulled
        nodes = atk.find_nodes()
        if self.connecting:
            return True
        if nodes != self.nodes or (self.mouse is None and not self.unsupported):
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
            self.nodes = atk.find_nodes()
            m = atk.open_mouse()
            self.mouse = m
            return m.read_state()   # waits a few seconds for a sleeping mouse

        def done(s):
            self.connecting = False
            self.unsupported = False
            self.show_state(s)

        def fail(e):
            self.connecting = False
            # retrying an unsupported mouse won't help; wait until something is replugged
            self.unsupported = isinstance(e, atk.Unsupported)
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
            self.set_mouse_sensitive(False)
            self.title.set_subtitle("Not connected")

    def show_state(self, s):
        self.state = s
        self.banner.set_revealed(False)
        self.set_mouse_sensitive(True)
        link = "wireless" if self.mouse.receiver else "cable"
        bat = s.get("battery")
        sub = f"{link}"
        if bat:
            sub += f" · battery {bat['level']}%" + (" (charging)" if bat["charging"] else "")
        self.title.set_title(s["id"]["name"])
        self.title.set_subtitle(sub)

        self.loading = True
        caps = s["caps"]
        self.buttons[5].row.set_visible(caps["dpi_button"])
        self.dpi_group.set_sensitive(s["stages"] is not None)
        self.dpi_group.set_description(
            "To switch stages with the mouse, set a button to DPI cycle below."
            if s["stages"] is not None else
            "This mouse stores DPI in a format the app can't read yet.")
        self.stage_count.set_value(s["stage_count"])
        self.stage_count.set_sensitive(caps["stage_count_editable"])
        for i, row in enumerate(self.stage_rows):
            adj = row.get_adjustment()
            adj.set_lower(caps["min_dpi"])
            adj.set_upper(caps["max_dpi"])
            adj.set_step_increment(caps["dpi_step"])
            row.set_snap_to_ticks(False)   # stepped ranges: atk.snap_dpi rounds instead
            row.set_subtitle(f"Click the number to type a value "
                             f"({caps['min_dpi']}–{caps['max_dpi']})")
            if s["stages"] is not None:
                row.set_value(min(s["stages"][i], caps["max_dpi"]))
            row.set_visible(i < s["stage_count"])
        self.active.limit([(i, f"Stage {i + 1}") for i in range(s["stage_count"])])
        self.active.set(s["active_stage"])
        self.rate.limit([(hz, f"{hz} Hz") for hz in caps["rates"]])
        self.rate.set(s["rate_hz"])
        self.mode.limit(caps["sensor_modes"])
        self.mode.set(s["sensor_mode"])
        self.debounce.limit(DEBOUNCE if caps["debounce_off"] else DEBOUNCE[1:])
        # backups are raw EEPROM dumps, which only COMPX mice have
        self.actions["backup"].set_enabled(caps["backup"])
        self.actions["restore"].set_enabled(caps["backup"])
        self.lod.row.set_visible(caps["lod"] in LOD_LEVELS)
        self.lod_mm.set_visible(caps["lod"] == "continuous")
        self.lod_raw.set_visible(False)   # ATK HUB shows no lift-off control then either
        if caps["lod"] in LOD_LEVELS:
            self.lod.limit(LOD_LEVELS[caps["lod"]])
            self.lod.set(s["lod_code"])
        elif caps["lod"] == "continuous":
            self.lod_mm.set_value(atk.lod_code_to_mm(s["lod_code"]))
        else:
            self.lod_raw_value.set_label(f"code {s['lod_code']}")
        self.debounce.set(s["debounce_ms"])
        self.sleep.set(s["sleep_s"])
        self.show_lights(s)
        for key, row in self.switches.items():
            row.set_visible(s.get(key) is not None)   # long range: only where offered
            row.set_active(bool(s.get(key)))
        for c, a in zip(self.buttons, s["buttons"]):
            c.set(json.dumps(a, sort_keys=True))
        self.loading = False

    def show_lights(self, s):
        """Fill the light controls; called while self.loading is set."""
        light, colors = s.get("light"), s.get("stage_colors")
        self.light_group.set_visible(light is not None)
        for i, button in enumerate(self.stage_colors):
            button.set_visible(colors is not None)
            if colors:
                button.set_rgba(hex_rgba(colors[i]))
        if light:
            self.light_mode.set(light["mode"])
            self.light_brightness.set(light["brightness"])
            self.light_speed.set(light["speed"])
            self.update_light_rows()
        rx = s.get("receiver_light")
        self.rx_group.set_visible(rx is not None)
        if rx:
            full = rx["kind"] == "full"
            self.rx_mode.limit(atk.RX_FULL_MODES if full else atk.RX_SIMPLE_MODES)
            self.rx_mode.set(rx["mode"])
            if full:
                self.rx_color.set_rgba(hex_rgba(rx["color"]))
                self.rx_brightness.set(rx["brightness"])
                self.rx_speed.set(rx["speed"])
                if rx["sleep"] is not None:
                    self.rx_sleep.set(rx["sleep"])
            self.update_rx_rows()

    def update_light_rows(self):
        mode = self.light_mode.value()
        self.light_brightness.row.set_sensitive(mode != "off")
        self.light_speed.row.set_sensitive(mode == "breathing")

    def update_rx_rows(self):
        rx = self.state and self.state.get("receiver_light")
        if not rx:
            return
        full, mode = rx["kind"] == "full", self.rx_mode.value()
        self.rx_color_row.set_visible(full)
        self.rx_color_row.set_sensitive(mode in atk.RX_COLOR)
        self.rx_brightness.row.set_visible(full)
        self.rx_brightness.row.set_sensitive(mode not in atk.RX_NO_BRIGHTNESS)
        self.rx_speed.row.set_visible(full)
        self.rx_speed.row.set_sensitive(mode not in atk.RX_NO_SPEED)
        self.rx_sleep.row.set_visible(full and rx["sleep"] is not None)
        self.rx_sleep.row.set_sensitive(mode != 0)

    # ---- writes ---------------------------------------------------------
    def apply(self, fn, message, after=None):
        if self.loading or self.mouse is None:
            return
        m = self.mouse

        def done(result):
            self.toasts.add_toast(Adw.Toast(title=message, timeout=2))
            if after:
                after(result)

        def fail(e):
            self.show_error(e)
            self.toasts.add_toast(Adw.Toast(title="Not saved", timeout=3))
            self.reload()   # put the UI back to what the mouse really has

        self.worker.submit(lambda: fn(m), done, fail)

    def on_switch(self, row, _pspec, setter, title):
        on = row.get_active()
        self.apply(lambda m: getattr(m, setter)(on), f"{title} {'on' if on else 'off'}")

    def on_stage_color(self, button, _pspec, i):
        color = rgba_hex(button.get_rgba())
        self.apply(lambda m: m.set_stage_color(i, color), f"Stage {i + 1} colour {color}")

    def on_light(self):
        self.update_light_rows()
        mode, label = self.light_mode.value(), self.light_mode.label()
        b, sp = self.light_brightness.value(), self.light_speed.value()
        self.apply(lambda m: m.set_light(mode, b, sp), f"DPI light: {label}")

    def on_rx_light(self):
        rx = self.state and self.state.get("receiver_light")
        if not rx or self.loading:
            return
        self.update_rx_rows()
        want = {"kind": rx["kind"], "mode": self.rx_mode.value()}
        if rx["kind"] == "full":
            want.update(color=rgba_hex(self.rx_color.get_rgba()),
                        brightness=self.rx_brightness.value(), speed=self.rx_speed.value(),
                        sleep=self.rx_sleep.value() if rx["sleep"] is not None else None)

        def stored(_):
            self.state["receiver_light"] = want
        self.apply(lambda m: m.set_receiver_light(want),
                   f"Receiver light: {self.rx_mode.label()}", stored)

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
            if self.state:
                dpi = atk.snap_dpi(dpi, self.state["caps"])

            def stored(value):
                # show what the mouse really stored; high DPI rounds to coarser steps
                if value != dpi:
                    self.loading = True
                    row.set_value(value)
                    self.loading = False
            self.apply(lambda m: m.set_stage_dpi(i, dpi), f"Stage {i + 1}: {dpi} DPI", stored)
            return False
        self.dpi_timers[i] = GLib.timeout_add(500, fire)

    def on_lod_mm(self, row, _pspec):
        if self.loading:
            return
        if self.lod_timer:
            GLib.source_remove(self.lod_timer)

        def fire():
            self.lod_timer = None
            mm = round(row.get_value(), 1)
            code = atk.lod_mm_to_code(mm)
            self.apply(lambda m: m.set_lod(code), f"Lift-off distance {mm} mm")
            return False
        self.lod_timer = GLib.timeout_add(300, fire)

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

    # ---- updates --------------------------------------------------------
    def add_update_group(self):
        self.updates = updater.Updater(REPO, VERSION, os.path.dirname(
            os.path.realpath(__file__)), os.path.join(CONFIG_DIR, "updates.json"))
        self.latest = None       # a newer release, once the check finds one
        self.installing = False
        self.installed = False   # installed, by this window or an earlier one; a restart starts it
        group = Adw.PreferencesGroup(title="Updates")
        self.auto_update_row = Adw.SwitchRow(
            title="Install updates automatically",
            subtitle="Off: the app tells you about a new version and waits for you to "
                     "press Update. Checks GitHub once a day")
        self.auto_update_row.set_active(self.updates.auto)
        self.auto_update_row.connect("notify::active", self.toggle_auto_update)
        group.add(self.auto_update_row)
        self.update_row = Adw.ActionRow(title=f"Version {VERSION}")
        self.check_btn = Gtk.Button(label="Check now", valign=Gtk.Align.CENTER)
        self.check_btn.connect("clicked", lambda *_: self.check_update(force=True))
        self.update_row.add_suffix(self.check_btn)
        self.restart_btn = Gtk.Button(label="Restart", valign=Gtk.Align.CENTER, visible=False)
        self.restart_btn.add_css_class("suggested-action")
        self.restart_btn.connect("clicked", lambda *_: self.restart())
        self.update_row.add_suffix(self.restart_btn)
        group.add(self.update_row)
        self.page.add(group)

    def toggle_auto_update(self, row, _):
        self.updates.auto = row.get_active()
        if self.updates.auto and self.latest and not self.updates.failed(self.latest):
            self.install_update()

    def show_installed(self, tag):
        self.installed = True
        self.update_banner.set_revealed(False)
        self.update_row.set_subtitle(f"{tag} installed. Restart to use it")
        self.check_btn.set_visible(False)
        self.restart_btn.set_visible(True)

    def check_update(self, force=False):
        if self.installing or self.installed:
            return
        self.update_row.set_subtitle("Checking…")

        def work():
            try:
                latest, error = self.updates.check(force), None
            except (OSError, ValueError, KeyError) as e:  # offline, GitHub down, odd reply
                latest, error = None, e
            GLib.idle_add(found, latest, error)

        def found(latest, error):
            self.latest = latest
            if error:
                # only a check you asked for complains; the daily one stays quiet offline
                self.update_row.set_subtitle("Couldn't reach GitHub" if force else "")
            elif not latest:
                self.update_row.set_subtitle("Up to date")
            elif self.updates.installed(latest):
                self.show_installed(latest["tag"])
            else:
                self.update_row.set_subtitle(f"{latest['tag']} is available")
                if self.updates.auto and not self.updates.dev_checkout \
                        and not self.updates.failed(latest):
                    self.install_update()
                else:
                    self.update_banner.set_title(f"DPIdeck {latest['tag']} is available")
                    self.update_banner.set_revealed(True)
            return False

        threading.Thread(target=work, daemon=True).start()

    def install_update(self):
        if self.installing or self.installed or not self.latest:
            return
        if self.updates.dev_checkout:
            self.toasts.add_toast(Adw.Toast(
                title="Running from a git checkout: update it with git pull", timeout=5))
            return
        latest = self.latest
        self.installing = True
        self.update_banner.set_revealed(False)
        self.update_row.set_subtitle(f"Installing {latest['tag']}…")

        def work():
            try:
                self.updates.install(latest)
                error = None
            except (OSError, RuntimeError) as e:  # network, disk, installer: shown to the user
                error = str(e)
            GLib.idle_add(finished, error)

        def finished(error):
            self.installing = False
            if error:
                self.update_row.set_subtitle(f"Couldn't update: {error}")
                self.toasts.add_toast(Adw.Toast(title=f"Couldn't update: {error}", timeout=6))
                return False
            self.show_installed(latest["tag"])
            toast = Adw.Toast(title=f"Updated to {latest['tag']}. Restart to use it",
                              button_label="Restart", timeout=0)
            toast.connect("button-clicked", lambda *_: self.restart())
            self.toasts.add_toast(toast)
            return False

        threading.Thread(target=work, daemon=True).start()

    def restart(self):
        app = os.path.realpath(__file__)
        os.execv(sys.executable, [sys.executable, app])


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
