#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""Read and change mouse settings for the Noctalia panel, through DPIdeck's atk.py.

    control.py state              print the mouse's settings
    control.py set KEY VALUE      change one setting (VALUE is JSON), then print them again
    control.py app                open DPIdeck's window

Output is one JSON object: the settings (see describe()), or {"asleep": true},
or {"error": "..."}. Unlike battery.py this wakes the mouse: it only runs when
the user opens the panel or changes something in it.

Every write starts from a fresh read, the same as DPIdeck: a value is only
written if it is one the app itself would offer this mouse.
"""

import fcntl
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True   # no __pycache__ in the plugin folder
sys.path.insert(0, HERE)   # python3 -I leaves the script's folder off sys.path
import battery  # noqa: E402

# The same choices DPIdeck's app.py offers (it can't be imported: it needs GTK)
LOD_LEVELS = {"levels": [(1, "1 mm"), (2, "2 mm")],
              "levels07": [(3, "0.7 mm"), (1, "1 mm"), (2, "2 mm")]}
LOD_CONTINUOUS = [(code, f"{(code + 6) / 10:.1f} mm") for code in range(1, 12)]
DEBOUNCE = [(0, "Off"), (1, "1 ms"), (2, "2 ms"), (4, "4 ms"),
            (8, "8 ms"), (15, "15 ms"), (20, "20 ms")]
LIGHT_MODES = [("off", "Off"), ("steady", "Steady"), ("breathing", "Breathing")]
BRIGHTNESS = [(0, "Low"), (1, "Medium"), (2, "High")]
SPEED = [(0, "Slow"), (1, "Medium"), (2, "Fast")]
SLEEP = [(30, "30 s"), (60, "1 min"), (120, "2 min"), (180, "3 min"),
         (300, "5 min"), (1200, "20 min"), (1500, "25 min"), (1800, "30 min")]
# The polling rates the panel's quick slider steps through, where the mouse offers them
QUICK_RATES = (1000, 2000, 4000, 8000)
SWITCHES = [("motion_sync", "Motion sync", "Smoother tracking, about 1 ms more latency",
             "set_motion_sync"),
            ("angle_snap", "Angle snapping", "Straightens lines you draw", "set_angle_snap"),
            ("ripple", "Ripple control", "Smooths jitter at high DPI", "set_ripple"),
            ("long_range", "Long range mode",
             "Stronger signal far from the receiver, uses more battery", "set_long_range")]


def choice(key, title, value, options, enabled=True, subtitle=None):
    """A drop-down; a value the list doesn't have is shown, never silently replaced."""
    options = list(options)
    if value not in [v for v, _l in options]:
        options.append((value, f"Other ({value})"))
    return {"type": "choice", "key": key, "title": title, "subtitle": subtitle,
            "value": value, "enabled": enabled,
            "options": [{"value": v, "label": lbl} for v, lbl in options]}


def offered(s):
    """{key: allowed values} for every setting this mouse lets the panel change."""
    caps = s["caps"]
    allowed = {
        "rate": caps["rates"],
        "sensor_mode": [v for v, _l in caps["sensor_modes"]],
        "debounce": [v for v, _l in (DEBOUNCE if caps["debounce_off"] else DEBOUNCE[1:])],
        "sleep": [v for v, _l in SLEEP],
    }
    if caps["lod"] in LOD_LEVELS:
        allowed["lod"] = [v for v, _l in LOD_LEVELS[caps["lod"]]]
    elif caps["lod"] == "continuous":
        allowed["lod"] = [v for v, _l in LOD_CONTINUOUS]
    for key, _t, _s, _f in SWITCHES:
        if s.get(key) is not None:
            allowed[key] = [False, True]
    if s.get("light") is not None:
        allowed["light_mode"] = [v for v, _l in LIGHT_MODES]
        allowed["light_brightness"] = [v for v, _l in BRIGHTNESS]
        allowed["light_speed"] = [v for v, _l in SPEED]
    return allowed


def describe(s, mouse):
    """The settings as the panel draws them: a DPI block plus sections of rows.

    The first section holds the quick controls: a polling-rate slider over
    1K-8K and the switches.
    """
    caps = s["caps"]
    allowed = offered(s)
    rates = [hz for hz in QUICK_RATES if hz in caps["rates"]]
    if s["rate_hz"] is not None and s["rate_hz"] not in rates:   # e.g. 500 Hz: still show it
        rates = sorted(rates + [s["rate_hz"]])
    quick = [{"type": "steps", "key": "rate", "title": "Polling rate", "value": s["rate_hz"],
              "enabled": True,
              "options": [{"value": hz, "label": f"{hz // 1000}K" if hz >= 1000 else str(hz)}
                          for hz in rates]}]
    for key, title, sub, _f in SWITCHES:
        if key in allowed:
            quick.append({"type": "toggle", "key": key, "title": title, "subtitle": sub,
                          "value": bool(s[key]), "enabled": True})
    sensor = [
        choice("sensor_mode", "Sensor mode", s["sensor_mode"], caps["sensor_modes"],
               subtitle="Competitive modes use more battery"),
    ]
    if caps["lod"] in LOD_LEVELS:
        sensor.append(choice("lod", "Lift-off distance", s["lod_code"], LOD_LEVELS[caps["lod"]]))
    elif caps["lod"] == "continuous":
        sensor.append(choice("lod", "Lift-off distance", s["lod_code"], LOD_CONTINUOUS))
    power = [
        choice("debounce", "Click debounce", s["debounce_ms"],
               DEBOUNCE if caps["debounce_off"] else DEBOUNCE[1:]),
        choice("sleep", "Sleep after", s["sleep_s"], SLEEP),
    ]
    sections = [{"title": "Quick settings", "items": quick},
                {"title": "Sensor", "items": sensor},
                {"title": "Clicks and power", "items": power}]
    light = s.get("light")
    if light is not None:
        sections.append({"title": "Lighting", "items": [
            choice("light_mode", "DPI light", light["mode"], LIGHT_MODES),
            choice("light_brightness", "Brightness", light["brightness"], BRIGHTNESS,
                   enabled=light["mode"] != "off"),
            choice("light_speed", "Breathing speed", light["speed"], SPEED,
                   enabled=light["mode"] == "breathing"),
        ]})
    bat = s.get("battery")
    return {
        "name": s["id"]["name"],
        "link": "wireless" if mouse.receiver else "cable",
        "battery": {"level": bat["level"], "charging": bat["charging"]} if bat else None,
        "dpi": {
            "stages": s["stages"][:s["stage_count"]] if s["stages"] is not None else None,
            "count": s["stage_count"],
            "count_editable": caps["stage_count_editable"],
            "active": s["active_stage"],
            "min": caps["min_dpi"],
            "max": caps["max_dpi"],
            "step": caps["dpi_step"],
        },
        "sections": sections,
    }


def write(mouse, s, key, value):
    """Write one setting after checking it against what this mouse offers.

    Returns a short message for the panel's status line.
    """
    allowed = offered(s)
    if key == "stage_dpi":
        i, dpi = value
        if s["stages"] is None:
            raise ValueError("This mouse stores DPI in a format DPIdeck can't read yet")
        if not (isinstance(i, int) and 0 <= i < s["stage_count"]):
            raise ValueError(f"No DPI stage {i}")
        if not isinstance(dpi, (int, float)) or isinstance(dpi, bool):
            raise ValueError("DPI must be a number")
        import atk
        dpi = atk.snap_dpi(dpi, s["caps"])
        stored = mouse.set_stage_dpi(i, dpi)
        return f"Stage {i + 1}: {stored} DPI"
    if key == "active_stage":
        if not (isinstance(value, int) and 0 <= value < s["stage_count"]):
            raise ValueError(f"No DPI stage {value}")
        mouse.set_active_stage(value)
        return f"Active: stage {value + 1}"
    if key == "stage_count":
        if not s["caps"]["stage_count_editable"]:
            raise ValueError("This mouse can't change its number of stages")
        if not (isinstance(value, int) and 1 <= value <= 8):
            raise ValueError("1 to 8 stages")
        mouse.set_stage_count(value)
        if s["active_stage"] >= value:   # keep the active stage among the remaining ones
            mouse.set_active_stage(value - 1)
        return f"{value} DPI stage{'s' if value > 1 else ''}"

    if key not in allowed:
        raise ValueError(f"{key} can't be changed on this mouse")
    if type(value) is not type(allowed[key][0]) or value not in allowed[key]:
        raise ValueError(f"{value!r} isn't a valid {key}")
    setters = {"rate": "set_rate", "sensor_mode": "set_sensor_mode", "lod": "set_lod",
               "debounce": "set_debounce", "sleep": "set_sleep"}
    setters.update({k: f for k, _t, _s, f in SWITCHES})
    if key in setters:
        getattr(mouse, setters[key])(value)
    else:
        light = dict(s["light"])
        light[key.removeprefix("light_")] = value
        mouse.set_light(light["mode"], light["brightness"], light["speed"])
    return "Saved"


def run(args):
    atk = battery.load_atk()
    if atk is None:
        return {"error": "DPIdeck is not installed"}
    if not args or args[0] not in ("state", "set") or (args[0] == "set" and len(args) != 3):
        return {"error": "usage: control.py state | set KEY VALUE"}
    try:
        mouse = atk.open_mouse()
    except atk.DeviceError as e:
        return {"error": str(e)}
    try:
        s = mouse.read_state()
        message = None
        if args[0] == "set":
            try:
                value = json.loads(args[2])
                message = write(mouse, s, args[1], value)
            except (ValueError, TypeError) as e:
                return {"error": str(e)}
            s = mouse.read_state()   # show what the mouse really stored
        out = describe(s, mouse)
        if message:
            out["message"] = message
        return out
    except atk.Asleep:
        return {"asleep": True}
    except (atk.DeviceError, OSError, IndexError, KeyError) as e:
        return {"error": str(e) or "Mouse did not answer"}
    finally:
        mouse.close()


def open_app():
    """Start DPIdeck's own window, for the settings the panel leaves out."""
    app = os.path.join(battery.DPIDECK, "app.py")
    if not os.path.exists(app):
        return {"error": "DPIdeck is not installed"}
    subprocess.Popen([sys.executable, app], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    return {"opened": True}


def main():
    if sys.argv[1:] == ["app"]:
        print(json.dumps(open_app()))
        return
    # The bar widgets poll through the same lock: only one of us on the hidraw node.
    with open(battery.LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        out = run(sys.argv[1:])
        if out.get("battery"):
            # fresh reading for the bar too, so it doesn't lag behind the panel
            try:
                with open(battery.CACHE, "w") as f:
                    f.write(json.dumps(out["battery"]))
            except OSError:
                pass
    print(json.dumps(out))


if __name__ == "__main__":
    main()
