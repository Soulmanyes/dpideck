#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""Print the ATK/VXE mouse battery as JSON for the Noctalia bar widget.

Uses the atk.py that DPIdeck installs. Output is one of:
  {"level": 95, "charging": false}
  {"asleep": true}
  {"error": "..."}
"""

import fcntl
import json
import os
import subprocess
import sys
import time

# atk.py sits two folders up, both in the repo and in an install
# (dpideck/noctalia/dpideck/battery.py). Noctalia may run a copy of the
# plugin from elsewhere, so fall back to $DPIDECK_DIR or the default install.
HERE = os.path.dirname(os.path.abspath(__file__))
DPIDECK = os.path.dirname(os.path.dirname(HERE))
if not os.path.exists(os.path.join(DPIDECK, "atk.py")):
    DPIDECK = os.path.expanduser(os.environ.get("DPIDECK_DIR", "~/.local/share/dpideck"))
# Each monitor's bar runs its own widget, so several polls start together.
# The lock keeps them off the hidraw node at the same time, and the cache
# lets the later ones reuse the first reading.
RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
LOCK = os.path.join(RUNTIME, "noctalia-dpideck.lock")
CACHE = os.path.join(RUNTIME, "noctalia-dpideck.json")
CACHE_AGE = 10
# One low-battery notification per discharge: the flag file stops repeats
# until the mouse charges or climbs back to NOTIFY_BELOW.
NOTIFY_BELOW = 15
NOTIFIED = os.path.join(RUNTIME, "noctalia-dpideck.notified")
# One "fully charged" notification per charge. It needs the mouse to have been
# seen charging, so a full mouse at login doesn't trigger it, and it only
# re-arms once the level drops below FULL_RESET.
FULL_AT = 100
FULL_RESET = 95
CHARGING_SEEN = os.path.join(RUNTIME, "noctalia-dpideck.charging")
FULL_NOTIFIED = os.path.join(RUNTIME, "noctalia-dpideck.full-notified")


def main():
    if not os.path.exists(os.path.join(DPIDECK, "atk.py")):
        return {"error": "DPIdeck is not installed"}
    sys.path.insert(0, DPIDECK)
    import atk

    # Use the cached device list whatever its age: a poll must never start
    # the ATK HUB download. DPIdeck itself refreshes the cache.
    try:
        with open(atk.DEVICES_CACHE) as f:
            atk._registry = json.load(f)["registry"]
    except (OSError, ValueError, KeyError, TypeError):
        atk._registry = atk.EMPTY_REGISTRY

    try:
        mouse = atk.open_mouse()
    except atk.DeviceError as e:
        return {"error": str(e)}
    try:
        # no wake(): polling must not keep the mouse from sleeping
        bat = mouse.battery()
    except atk.Asleep:
        return {"asleep": True}
    except (atk.DeviceError, OSError, IndexError) as e:
        return {"error": str(e) or "read failed"}
    finally:
        mouse.close()
    return {"level": bat["level"], "charging": bat["charging"]}


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def send(urgency, title, body):
    """Shows a notification; returns False if it couldn't, so the caller retries."""
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-a", "DPIdeck",
             "-i", "input-mouse", title, body],
            timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def notify_low(data):
    if "level" not in data:
        return  # asleep or error: keep the current state
    if data["charging"] or data["level"] >= NOTIFY_BELOW:
        remove(NOTIFIED)
        return
    if os.path.exists(NOTIFIED):
        return
    if send("critical", "Mouse battery low", f"{data['level']}% left. Plug in the mouse."):
        open(NOTIFIED, "w").close()


def notify_full(data):
    if "level" not in data:
        return  # asleep or error: keep the current state
    if data["charging"]:
        open(CHARGING_SEEN, "w").close()
    elif data["level"] < FULL_RESET:
        remove(CHARGING_SEEN)
    if data["level"] < FULL_RESET:
        remove(FULL_NOTIFIED)
        return
    if data["level"] < FULL_AT or not os.path.exists(CHARGING_SEEN) or os.path.exists(FULL_NOTIFIED):
        return
    if send("normal", "Mouse fully charged", "You can unplug the mouse."):
        open(FULL_NOTIFIED, "w").close()
        remove(CHARGING_SEEN)


def cached_or_read():
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if time.time() - os.path.getmtime(CACHE) < CACHE_AGE:
                with open(CACHE) as f:
                    return f.read().strip()
        except OSError:
            pass
        data = main()
        notify_low(data)
        notify_full(data)
        out = json.dumps(data)
        with open(CACHE, "w") as f:
            f.write(out)
        return out


if __name__ == "__main__":
    print(cached_or_read())
