# SPDX-License-Identifier: MIT
# Protocol tables and DPI codecs ported from OpenATK, Copyright (c) 2026 Sunnybee.
"""Drivers for ATK / VXE mice over Linux hidraw.

ATK mice speak one of two unrelated protocols, both over vendor report ID 8:

COMPX (16-byte frames, settings live in EEPROM):
    0 commandId  1 status  2-3 EEPROM address (big endian)  4 length
    5-14 payload  15 checksum = 85 - (8 + sum(bytes 0..14))

BITMOUSE (63-byte frames, one command per setting):
    0 checksum = sum(bytes 1..62)  1 0x72  2 paramLen  3 sequence  4 target
    5 commandId  6 cmdLen  7+ payload        (replies drop byte 0)

Protocol details come from OpenATK (MIT, see LICENSE-OpenATK):
https://github.com/SunnybeeXD/OpenATK/blob/main/docs/PROTOCOL.md
and were cross-checked against ATK HUB 3.2.28's own drivers. Use open_mouse().
"""

import glob
import json
import math
import os
import re
import select
import time
import urllib.request

VIDS = (0x373B, 0x3554)   # 0x3554: first-generation VXE R1 family
REPORT_ID = 8

CONFIG_DIR = os.path.expanduser("~/.config/dpideck")
_OLD_CONFIG_DIR = os.path.expanduser("~/.config/atk-mouse")    # before the rename to DPIdeck
if os.path.isdir(_OLD_CONFIG_DIR) and not os.path.exists(CONFIG_DIR):
    try:
        os.rename(_OLD_CONFIG_DIR, CONFIG_DIR)     # keep caches and backups
    except OSError:
        pass

# ---- device list --------------------------------------------------------------
# Which sensor each mouse has comes from ATK HUB's own device tables. They are
# not shipped with this app: on first use it downloads ATK HUB's public web app
# and extracts them, reading the JavaScript as text only (it is never run).
# The result is cached and refreshed at most once every 30 days.
HUB_URL = "https://hub.atk.pro/"
HUB_HOSTS = ("https://bpcdn.atkgear.com/",)
DEVICES_CACHE = os.path.join(CONFIG_DIR, "devices.json")
DEVICES_MAX_AGE = 30 * 24 * 3600
DOWNLOAD_LIMIT = 64 * 1024 * 1024
FAMILY_BY_USAGE_PAGE = {0xFF04: "compx", 0xFF05: "bitmouse"}
RECOVERY_USAGE_PAGE = 0xFF02    # ATK HUB lists mice in firmware-recovery mode under it
KEEP_FLAGS = {"noBottomButton", "noZeroKeyDebounce", "noAthleticsMax", "noFarDistance",
              "noDpiRGB"}
DEVICES_FORMAT = 3              # bumped when KEEP_FLAGS changes, so old caches refresh
EMPTY_REGISTRY = {"compx": {"cid": {}, "pid": {}}, "bitmouse": {"cid": {}, "pid": {}},
                  "receivers": {}, "receiver_flags": {}, "recovery": []}
RECEIVER_FLAGS = {"singleRgb", "gatherRgb", "noDongleRGB"}   # what kind of light it has
_registry = None


def _get(url, budget):
    if not url.startswith(HUB_HOSTS + (HUB_URL,)):
        raise ValueError(f"refusing to download {url}")
    with urllib.request.urlopen(url, timeout=10) as r:
        data = r.read(budget[0] + 1)
    budget[0] -= len(data)
    if budget[0] < 0:
        raise ValueError("ATK HUB download larger than expected")
    return data.decode("utf-8", "replace")


def _filter_objects(src):
    """Yield the text of every {vendorId:...} device filter in a script."""
    for m in re.finditer(r"\{vendorId:", src):
        depth = 0
        for j in range(m.start(), min(len(src), m.start() + 20000)):
            depth += src[j] == "{"
            depth -= src[j] == "}"
            if depth == 0:
                yield src[m.start():j + 1]
                break


def parse_device_tables(scripts):
    """Build the registry from ATK HUB's script texts."""
    reg = json.loads(json.dumps(EMPTY_REGISTRY))
    seen_pid = {}
    for src in scripts:
        for o in _filter_objects(src):
            v = re.match(r"\{vendorId:(\d+)", o)
            p = re.search(r"productId:(\d+)", o)
            page = re.search(r"usagePage:(\d+)", o)
            if not (v and p and page):
                continue
            usb = f"{int(v.group(1)):04x}:{int(p.group(1)):04x}"
            if int(page.group(1)) == RECOVERY_USAGE_PAGE and "sensor:" not in o:
                reg["recovery"].append(usb)
                continue
            if int(page.group(1)) not in FAMILY_BY_USAGE_PAGE:
                continue
            family = FAMILY_BY_USAGE_PAGE[int(page.group(1))]
            name = re.search(r'name:"([^"]*)"', o)
            name = name.group(1).strip() if name else ""
            sensor = re.search(r"sensor:[A-Za-z_$]+\.(\w+)", o)
            if "receiver:!0" in o:
                reg["receivers"][usb] = name
                flags = sorted(set(re.findall(r"(\w+):!0", o)) & RECEIVER_FLAGS)
                if flags:
                    reg["receiver_flags"][usb] = flags
                continue
            if not sensor:
                continue    # other ATK products (docks, monitors...) share the interface
            entry = [name, sensor.group(1), sorted(set(re.findall(r"(\w+):!0", o)) & KEEP_FLAGS)]
            cm = re.search(r'mouseCidMid:"(\d+,\d+)"', o)
            if cm:
                reg[family]["cid"].setdefault(cm.group(1), entry)
            seen_pid.setdefault((family, usb), []).append(entry)
    # a USB ID shared by models with different sensors can't identify the sensor
    for (family, usb), entries in seen_pid.items():
        if len({e[1] for e in entries}) == 1:
            reg[family]["pid"][usb] = entries[0]
    reg["recovery"] = sorted(set(reg["recovery"]))
    return reg


def fetch_registry():
    """Download ATK HUB and extract its device tables."""
    budget = [DOWNLOAD_LIMIT]
    page = _get(HUB_URL, budget)
    entry = re.search(r'src="(https://bpcdn\.atkgear\.com/[^"]+/static/index-[^"]+\.js)"', page)
    if not entry:
        raise ValueError("ATK HUB page layout not recognised")
    base = entry.group(1).rsplit("/static/", 1)[0] + "/"
    todo, done, scripts = [entry.group(1)], set(), []
    while todo:
        url = todo.pop()
        if url in done:
            continue
        done.add(url)
        try:
            src = _get(url, budget)
        except OSError:
            continue        # one missing chunk shouldn't stop the rest
        scripts.append(src)
        for dep in re.findall(r'"(?:\./|static/)([\w.-]+\.js)"', src):
            todo.append(base + "static/" + dep)
    reg = parse_device_tables(scripts)
    if len(reg["compx"]["cid"]) + len(reg["bitmouse"]["cid"]) < 20:
        raise ValueError("ATK HUB device tables not found")
    return reg


def registry(refresh=False):
    """The device list: cached copy, else a fresh download, else empty."""
    global _registry
    if _registry is not None and not refresh:
        return _registry
    cached = None
    try:
        with open(DEVICES_CACHE) as f:
            cached = json.load(f)
        if (not refresh and cached.get("format") == DEVICES_FORMAT
                and time.time() - cached["fetched"] < DEVICES_MAX_AGE):
            _registry = cached["registry"]
            return _registry
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        reg = fetch_registry()
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(DEVICES_CACHE, "w") as f:
            json.dump({"format": DEVICES_FORMAT, "fetched": time.time(), "registry": reg}, f)
        _registry = reg
    except (OSError, ValueError):
        # offline or ATK changed its site: keep an old copy, else know no mice
        _registry = cached["registry"] if cached and "registry" in cached else EMPTY_REGISTRY
    return _registry


# ATK HUB's public config listing, per COMPX mouse ("cid,mid"), the lowest
# firmware that offers Competitive Max. Fetched on first need, cached, and
# refreshed at most once a week; this is the app's only network request.
UPGRADE_URL = "https://bpcdn.atkgear.com/device/constant/mouse-upgrade.json"
UPGRADE_CACHE = os.path.join(CONFIG_DIR, "competitive-max.json")
UPGRADE_MAX_AGE = 7 * 24 * 3600
_upgrade_table = None


def competitive_max_table():
    """{"cid,mid": "min firmware"}; empty if never fetched and offline."""
    global _upgrade_table
    if _upgrade_table is not None:
        return _upgrade_table
    cached = None
    try:
        with open(UPGRADE_CACHE) as f:
            cached = json.load(f)
        if time.time() - cached["fetched"] < UPGRADE_MAX_AGE:
            _upgrade_table = cached["table"]
            return _upgrade_table
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        with urllib.request.urlopen(UPGRADE_URL, timeout=3) as r:
            compx = json.load(r)["3"]
        table = {k: v["cordedMode"] for k, v in compx.items()
                 if isinstance(v, dict) and "cordedMode" in v}
        os.makedirs(os.path.dirname(UPGRADE_CACHE), exist_ok=True)
        with open(UPGRADE_CACHE, "w") as f:
            json.dump({"fetched": time.time(), "table": table}, f)
        _upgrade_table = table
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # offline or ATK changed the file: use a stale copy, else show no Competitive Max
        _upgrade_table = cached["table"] if cached and "table" in cached else {}
    return _upgrade_table


BUTTON_NAMES = ["Left", "Right", "Middle", "Back", "Forward", "DPI button"]


class DeviceError(Exception):
    pass


class Unsupported(DeviceError):
    """The mouse speaks a protocol this driver does not implement."""


class Asleep(DeviceError):
    """The mouse did not answer; usually it is asleep and needs a nudge."""


def inv(v):
    return (85 - v) & 0xFF


def pair(v):
    return [v & 0xFF, inv(v)]


# ---- sensors ----------------------------------------------------------------
# DPI range per sensor as ATK HUB offers it: lowest value, then (up to, step)
# segments. "codec" is how COMPX mice store it; BITMOUSE stores plain numbers.
# The table sensors (PAW3395SE, PAW3311, PAW3315) stop at 10000 on COMPX:
# ATK HUB's own encoder has no values above that for them.
SENSORS = {
    "PAW3395":       {"min": 100, "seg": [(30000, 50)], "codec": "B"},
    "PAW3395Ultra":  {"min": 100, "seg": [(30000, 50)], "codec": "B"},
    "PAW3950":       {"min": 50, "seg": [(30000, 50), (36000, 100)], "codec": "B"},
    "PAW3950DM":     {"min": 50, "seg": [(30000, 50), (36000, 100)], "codec": "B"},
    "PAW3950Ultra":  {"min": 10, "seg": [(10000, 10), (30000, 50), (42000, 100)], "codec": "A"},
    "PAW3955MASTER": {"min": 1, "seg": [(40000, 1), (50000, 2)], "codec": "M"},
    "CORE26K":       {"min": 50, "seg": [(10000, 50), (26000, 100)], "codec": "C"},
    "PAW3395SE":     {"min": 200, "seg": [(18000, 50)], "codec": "SE", "compx_max": 10000},
    "PAW3311":       {"min": 200, "seg": [(15000, 100)], "codec": "SE", "compx_max": 10000},
    "PAW3315":       {"min": 200, "seg": [(16000, 100)], "codec": "T15", "compx_max": 10000},
}
# Lift-off controls ATK HUB shows per sensor (none for PAW3311 / PAW3315)
LOD_LEVELS_SENSORS = {"PAW3395SE", "CORE26K", "PAW3395", "PAW3395Ultra", "PAW3950", "PAW3950DM"}
LOD_CONTINUOUS_SENSORS = {"PAW3950Ultra", "PAW3955MASTER"}


def snap_dpi(dpi, caps):
    """Nearest DPI the mouse can store, clamped to its range."""
    low, segments = caps["min_dpi"], caps["dpi_segments"]
    dpi = max(low, min(caps["max_dpi"], int(dpi)))
    start = low
    for until, step in segments:
        if dpi <= until:
            return min(until, caps["max_dpi"], start + round((dpi - start) / step) * step)
        start = until
    return caps["max_dpi"]


def lod_mm_to_code(mm):
    """Continuous lift-off sensors: code 1 = 0.7 mm ... code 11 = 1.7 mm."""
    return min(11, max(1, round(mm * 10) - 6))


def lod_code_to_mm(code):
    return (code + 6) / 10


# ---- COMPX DPI codecs -------------------------------------------------------
# Each stage stores one byte per axis plus a nibble: bits 2-3 extend the byte,
# bit 1 selects a coarser range, bit 0 doubles the value.

# B (PAW3395 / PAW3950): 50-DPI steps, 100-DPI steps above 30000.
def enc_b(dpi):
    doubled = 1 if dpi > 30000 else 0
    code = round(dpi / (100 if doubled else 50)) - 1
    return code & 0xFF, ((code >> 8) << 2) | doubled


def dec_b(byte, nib):
    code = (((nib >> 2) & 3) << 8) | byte
    return 50 * (code + 1) * (2 if nib & 1 else 1)


# A (PAW3950 Ultra): 10-DPI steps to 10000, then 50-DPI steps.
def enc_a(dpi):
    v, doubled = dpi, 0
    if v > 30000:
        doubled, v = 1, round(v / 2)
    if v <= 10000:
        c = v // 10 - 1
        return c & 0xFF, ((c >> 8) << 2) | doubled
    c = (v - 10050) // 50
    return c & 0xFF, ((c >> 8) << 2) | 2 | doubled


def dec_a(byte, nib):
    c = (((nib >> 2) & 3) << 8) | byte
    base = 10050 + c * 50 if nib & 2 else (c + 1) * 10
    return base * 2 if nib & 1 else base


# C (CORE26K): the DPI in 50-DPI steps. The firmware may also set the range
# bit (100-DPI steps) or the doubling bit, so decoding honours both.
def enc_c(dpi):
    code = round(dpi / 50) - 1
    return code & 0xFF, (code >> 8) << 2


def dec_c(byte, nib):
    code = (((nib >> 2) & 3) << 8) | byte
    return (code + 1) * (100 if nib & 2 else 50) * (2 if nib & 1 else 1)


# SE / T15 (PAW3395SE and PAW3311 / PAW3315): one byte per 50 DPI from a
# sensor-specific scale, which ATK HUB keeps as a lookup table. These formulas
# reproduce that table exactly, including its two hand-tuned entries.
def _scale_table(code_of, overrides):
    table = {dpi: code_of(dpi) for dpi in range(50, 10001, 50)}
    table.update(overrides)
    return table, {code: dpi for dpi, code in sorted(table.items(), reverse=True)}


_SE_TABLE = _scale_table(lambda d: math.ceil(d / 42.5) - 1, {300: 6})
_T15_TABLE = _scale_table(lambda d: math.floor(d / 41.982), {300: 6, 550: 12})


def _enc_scaled(table):
    def enc(dpi):
        return table[0][min(10000, max(50, round(dpi / 50) * 50))], 0
    return enc


def _dec_scaled(table, fallback):
    def dec(byte, nib):
        # An unused or blank slot can hold any byte; like ATK HUB, show a
        # default for that slot instead of failing the whole table.
        dpi = table[1].get(byte)
        if dpi is None:
            return fallback
        return dpi * 2 if nib & 2 else dpi
    return dec


# M (PAW3955): stored in separate 6-byte rows; the DPI itself minus one, and
# from 40000 up half of it with a flag bit.
def enc_m(dpi):
    if dpi < 40000:
        return max(1, dpi) - 1, 0
    return min((dpi - dpi % 2) // 2 - 1, 0xFFFF), 1


def dec_m(value, doubled):
    return (value + 1) * (2 if doubled else 1)


CODECS = {"A": (enc_a, dec_a), "B": (enc_b, dec_b), "C": (enc_c, dec_c),
          "SE": (_enc_scaled(_SE_TABLE), _dec_scaled(_SE_TABLE, 10000)),
          "T15": (_enc_scaled(_T15_TABLE), _dec_scaled(_T15_TABLE, 10000)),
          "M": (enc_m, dec_m)}


# ---- capabilities -----------------------------------------------------------
def capabilities(family, sensor, flags, max_rate, rates, competitive_max, sensor_mode,
                 sensor_modes):
    """What the app may offer for this mouse, following ATK HUB's own rules."""
    info = SENSORS.get(sensor)
    caps = {"family": family, "sensor": sensor, "dpi_codec": None,
            "min_dpi": 50, "max_dpi": 26000, "dpi_segments": [(26000, 50)], "dpi_step": 50}
    if info:
        top = info["seg"][-1][0]
        if family == "compx":
            top = min(top, info.get("compx_max", top))
            caps["dpi_codec"] = info["codec"]
        caps.update(min_dpi=info["min"], max_dpi=top, dpi_segments=info["seg"],
                    dpi_step=info["seg"][0][1])
    if sensor in LOD_CONTINUOUS_SENSORS:
        caps["lod"] = "continuous"                         # 0.7-1.7 mm in 0.1 mm steps
    elif sensor in LOD_LEVELS_SENSORS:
        # ATK HUB adds the 0.7 mm step only for COMPX PAW3950 / 3950DM mice
        caps["lod"] = ("levels07" if family == "compx" and sensor in ("PAW3950", "PAW3950DM")
                       else "levels")
    else:
        caps["lod"] = None                                 # no lift-off control
    basic, comp, cmax = sensor_modes
    modes = [(basic, "Basic"), (comp, "Competitive")]
    if competitive_max or sensor_mode == cmax:             # never hide the current mode
        modes.append((cmax, "Competitive Max"))
    caps.update(rates=[hz for hz in sorted(rates) if hz <= max_rate],
                sensor_modes=modes,
                dpi_button="noBottomButton" not in flags,
                debounce_off="noZeroKeyDebounce" not in flags,
                # How BITMOUSE mice add or remove stages isn't known, so the
                # count is only shown there; stage values and the active stage work.
                stage_count_editable=family == "compx",
                backup=family == "compx")
    return caps


def lookup(family, key, vid, pid):
    """Registry entry for a mouse: by CID/MID first, then by USB ID (cable)."""
    reg = registry()
    fam = reg[family]
    usb = f"{vid:04x}:{pid:04x}"
    entry = fam["cid"].get(key) or fam["pid"].get(usb)
    receiver = reg["receivers"].get(usb)
    rx_flags = reg.get("receiver_flags", {}).get(usb, [])
    if entry:
        return {"key": key, "name": entry[0], "sensor": entry[1], "flags": entry[2],
                "receiver": receiver, "receiver_flags": rx_flags}
    return {"key": key, "name": f"Unknown ATK mouse ({key or 'no ID'})", "sensor": None,
            "flags": [], "receiver": receiver, "receiver_flags": rx_flags}


# ---- device discovery -------------------------------------------------------
def report_bytes(desc, report_id):
    """Byte length of an input report in a HID report descriptor, or None."""
    i, rid, size, count = 0, 0, 0, 0
    while i < len(desc):
        b = desc[i]
        n = (0, 1, 2, 4)[b & 3]
        val = int.from_bytes(desc[i + 1:i + 1 + n], "little")
        if b & 0xFC == 0x84:      # Report ID
            rid = val
        elif b & 0xFC == 0x74:    # Report Size (bits)
            size = val
        elif b & 0xFC == 0x94:    # Report Count
            count = val
        elif b & 0xFC == 0x80 and rid == report_id:   # Input
            return size * count // 8
        i += 1 + n
    return None


def find_nodes():
    """All ATK vendor config nodes as (node, vid, pid, family).

    Both ATK protocols use report ID 8: COMPX sends 16-byte reports,
    BITMOUSE 63-byte ones.
    """
    found = []
    for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(f"{sys_path}/device/uevent") as f:
                uevent = f.read()
            with open(f"{sys_path}/device/report_descriptor", "rb") as f:
                desc = f.read()
        except OSError:
            continue
        m = re.search(r"HID_ID=\w+:0000(\w{4}):0000(\w{4})", uevent)
        if not m or int(m.group(1), 16) not in VIDS:
            continue
        if f"{m.group(1)}:{m.group(2)}".lower() in registry().get("recovery", []):
            continue        # a mouse in firmware-recovery mode: leave it alone
        length = report_bytes(desc, REPORT_ID)
        if length is None:
            continue
        family = "compx" if length == 16 else "bitmouse"
        found.append(("/dev/" + os.path.basename(sys_path),
                      int(m.group(1), 16), int(m.group(2), 16), family))
    return found


def is_receiver(vid, pid):
    return f"{vid:04x}:{pid:04x}" in registry()["receivers"]


def open_mouse(log=None):
    """Open the connected mouse, preferring a cable over a wireless receiver."""
    nodes = find_nodes()
    if not nodes:
        raise DeviceError("No ATK/VXE mouse found. Is the dongle plugged in?")
    node, vid, pid, family = sorted(nodes, key=lambda n: is_receiver(n[1], n[2]))[0]
    cls = CompxMouse if family == "compx" else BitMouse
    return cls(node, vid, pid, log)


class _HidDevice:
    def __init__(self, node, vid, pid, log=None):
        self.node, self.vid, self.pid, self.log = node, vid, pid, log
        try:
            self.fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise DeviceError(f"No permission for {node}. Run install.sh, "
                              "then unplug the mouse or dongle and plug it back in.")
        self.receiver = is_receiver(vid, pid)

    def close(self):
        os.close(self.fd)

    def _drain(self):
        while select.select([self.fd], [], [], 0)[0]:
            try:
                os.read(self.fd, 128)
            except BlockingIOError:
                break

    def _exchange(self, frame, match):
        """Send a report-8 frame; return the first reply that match() accepts."""
        for _attempt in range(3):
            self._drain()
            os.write(self.fd, bytes([REPORT_ID]) + frame)
            if self.log:
                self.log("TX " + frame.hex(" "))
            deadline = time.monotonic() + 0.6
            while (left := deadline - time.monotonic()) > 0:
                if not select.select([self.fd], [], [], left)[0]:
                    break
                raw = os.read(self.fd, 128)
                if not raw or raw[0] != REPORT_ID:
                    continue          # mouse motion, media keys, other reports
                if self.log:
                    self.log("RX " + raw[1:].hex(" "))
                reply = match(raw[1:])
                if reply is not None:
                    return reply
        raise Asleep("Mouse did not answer. Move it to wake it up.")

    def max_rate(self):
        """Highest polling rate to offer: what the mouse reports, else all of them."""
        return LINK_MAX_RATE.get(self.link_type(), 8000)


# The link type a mouse reports, as ATK HUB numbers it, and the fastest rate it
# carries. ATK HUB offers only 1000 Hz on "wired 2K/4K"; their names are used here.
# A mouse that doesn't report its link is offered every rate.
LINK_MAX_RATE = {0: 1000,    # 1K receiver
                 1: 4000,    # 4K receiver
                 2: 1000,    # cable, 1K
                 3: 8000,    # cable, 8K
                 4: 2000,    # 2K receiver
                 5: 8000,    # 8K receiver
                 6: 2000,    # cable, 2K
                 7: 4000,    # cable, 4K
                 8: 8000}    # NearLink


# The DPI light: mode "off"/"steady"/"breathing", brightness and speed as
# 0 (low/slow), 1 or 2 (high/fast). Each family stores its own raw values.
LIGHT_MODES = ("off", "steady", "breathing")


def nearest_level(raw, table):
    """The level whose raw value is closest to what the mouse stores."""
    return min(range(len(table)), key=lambda i: abs(table[i] - raw))


def rgb_hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def hex_rgb(color):
    return [int(color[i:i + 2], 16) for i in (1, 3, 5)]


# Receiver lights, as ATK HUB offers them. "full" receivers take an effect,
# colour, brightness and speed; "simple" ones (singleRgb) only show a status.
RX_FULL_MODES = [(0, "Off"), (1, "Colour flow"), (2, "Breathing, one colour"),
                 (3, "Steady"), (4, "Neon"), (5, "Breathing, mixed colours"), (6, "Flashing")]
RX_SIMPLE_MODES = [(0, "Off"), (1, "Show polling rate"), (2, "Show battery level"),
                   (3, "Low battery warning")]
RX_NO_SPEED = {0, 3, 6}          # modes where ATK HUB greys out each control
RX_NO_BRIGHTNESS = {0}
RX_COLOR = {2, 3}


def receiver_light_kind(flags):
    """'full', 'simple' or None, from the receiver's ATK HUB flags.

    gatherRgb receivers use another mode list that isn't supported yet.
    """
    if "noDongleRGB" in flags or "gatherRgb" in flags:
        return None
    return "simple" if "singleRgb" in flags else "full"


# ---- COMPX ------------------------------------------------------------------
CX_MOUSE_INFO = 0x01      # ATK HUB's "DownLoadData"; sent only as its info read
CX_BATTERY = 0x04
CX_SET_EEPROM = 0x07
CX_GET_EEPROM = 0x08
CX_FACTORY_RESET = 0x09
CX_CID_MID = 0x10
CX_MOUSE_VERSION = 0x12
CX_SET_RX_SIMPLE_LIGHT = 0x14
CX_GET_RX_SIMPLE_LIGHT = 0x15
CX_SET_LONG_RANGE = 0x16
CX_GET_LONG_RANGE = 0x17
CX_SET_RX_LIGHT = 0x18
CX_GET_RX_LIGHT = 0x19
CX_DONGLE_VERSION = 0x1D

# Anything else (firmware upgrade 0x0D, pairing) is refused. 0x01 goes only
# through link_type(), in the exact form ATK HUB reads it.
CX_ALLOWED = {CX_BATTERY, CX_SET_EEPROM, CX_GET_EEPROM, CX_FACTORY_RESET,
              CX_CID_MID, CX_MOUSE_VERSION, CX_DONGLE_VERSION, CX_SET_LONG_RANGE,
              CX_GET_LONG_RANGE, CX_SET_RX_LIGHT, CX_GET_RX_LIGHT, CX_SET_RX_SIMPLE_LIGHT,
              CX_GET_RX_SIMPLE_LIGHT}

ADDR_RATE_ROW = 0x00      # [rate,~, maxDpi(stage count),~, activeStage,~, bhop,~, btnMode,~]
ADDR_LOD = 0x0A           # [code,~]
ADDR_DPI_ROWS = [0x0C, 0x14, 0x1C, 0x24]    # two stages per 8-byte row
ADDR_DPI_3955 = 0x1B00    # PAW3955 mice: one 6-byte row per stage instead
ADDR_KEY0 = 0x60          # 4 bytes per button
ADDR_FUNC_ROW = 0xA9      # [debounce,~, motionSync,~, sleep/10,~, lineCorr,~, ripple,~]
ADDR_DPI_COLOR_ROWS = [0x2C, 0x34, 0x3C, 0x44]  # two stages per row: [R, G, B, ~] each
ADDR_LIGHT_ROW = 0x4C     # [mode,~, brightness,~, speed,~, on,~]
CX_LIGHT_MODES = {"steady": 1, "breathing": 2}
CX_LIGHT_BRIGHTNESS = (16, 128, 255)
CX_LIGHT_SPEED = (1, 3, 5)
CX_RX_SPEED = (0, 5, 9)
CX_RX_BRIGHTNESS = (0, 5, 9)
CX_RX_SLEEP = (1, 5, 10)  # minutes
ADDR_SENSOR_ROW = 0xB5    # [sleepOn,~, sleep/10,~, sensorMode,~]
ADDR_SHORTCUT0 = 0x100    # 32 bytes per button

CX_RATES = {1000: 0x01, 500: 0x02, 250: 0x04, 125: 0x08,
            2000: 0x10, 4000: 0x20, 8000: 0x40}
CX_RATES_REV = {v: k for k, v in CX_RATES.items()}

# Sensor modes as ATK HUB reads and writes them for COMPX mice. (OpenATK lists
# 0/4/5, which is the BITMOUSE encoding; the COMPX driver stores 0/1/2 as-is.)
CX_SENSOR_MODES = (0, 1, 2)


def cx_decode_key(c, v1, v2):
    masks = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4}
    if c == 0 or (c == 1 and v1 == 0):
        return {"kind": "disable"}
    if c == 1 and v1 in masks:
        return {"kind": "mouse", "btn": masks[v1]}
    if c == 2 and 1 <= v1 <= 3:
        return {"kind": "dpi", "op": ["cycle", "up", "down"][v1 - 1]}
    if c == 3 and v1 in (1, 2):
        return {"kind": "scroll", "dir": "left" if v1 == 1 else "right"}
    if c == 11 and v1 in (1, 2):
        return {"kind": "scroll", "dir": "up" if v1 == 1 else "down"}
    if c == 5:
        return {"kind": "shortcut"}
    if c == 6:
        return {"kind": "macro"}
    return {"kind": "unknown", "raw": [c, v1, v2]}


def cx_encode_key(a):
    k = a["kind"]
    if k == "mouse":
        return [1, 1 << a["btn"], 0]
    if k == "disable":
        return [1, 0, 0]
    if k == "dpi":
        return [2, {"cycle": 1, "up": 2, "down": 3}[a["op"]], 0]
    if k == "scroll":
        if a["dir"] in ("left", "right"):
            return [3, 1 if a["dir"] == "left" else 2, 0]
        return [11, 1 if a["dir"] == "up" else 2, 0]
    raise ValueError(f"unsupported action {a}")


class CompxMouse(_HidDevice):
    family = "compx"

    def __init__(self, node, vid, pid, log=None):
        super().__init__(node, vid, pid, log)
        self.codec = None
        self.stage_count = 0

    @staticmethod
    def pack(cmd, addr=0, length=0, payload=()):
        b = bytearray(16)
        b[0] = cmd
        b[2], b[3] = (addr >> 8) & 0xFF, addr & 0xFF
        b[4] = length
        for i, v in enumerate(payload):
            b[5 + i] = v & 0xFF
        b[15] = (85 - ((8 + sum(b[:15])) & 0xFF)) & 0xFF
        return bytes(b)

    def xfer(self, cmd, addr=0, length=0, payload=()):
        if cmd not in CX_ALLOWED and not (cmd == CX_MOUSE_INFO and addr == 0 and length == 8
                                          and len(payload) == 4):
            raise DeviceError(f"command 0x{cmd:02x} is not allowed")

        def match(raw):
            r = raw[:16]
            if len(r) < 16 or r[0] != cmd:
                return None
            if cmd in (CX_GET_EEPROM, CX_SET_EEPROM) and (r[2] << 8 | r[3]) != addr:
                return None
            if (85 - ((8 + sum(r[:15])) & 0xFF)) & 0xFF != r[15]:
                raise DeviceError(f"bad checksum in reply: {r.hex(' ')}")
            if r[1] != 0:
                raise DeviceError(f"device returned status {r[1]} for 0x{cmd:02x}")
            return r
        return self._exchange(self.pack(cmd, addr, length, payload), match)

    def read(self, addr, length):
        """Read up to 10 bytes of EEPROM."""
        return list(self.xfer(CX_GET_EEPROM, addr, length)[5:5 + length])

    def read_long(self, addr, length):
        out = []
        for off in range(0, length, 10):
            out += self.read(addr + off, min(10, length - off))
        return out

    def write(self, addr, data, verify=True):
        data = [v & 0xFF for v in data]
        self.xfer(CX_SET_EEPROM, addr, len(data), data)
        if verify:
            back = self.read(addr, len(data))
            if back != data:
                raise DeviceError(f"write to 0x{addr:04x} did not stick "
                                  f"(wanted {bytes(data).hex(' ')}, got {bytes(back).hex(' ')})")

    def patch_row(self, addr, length, index, value):
        """Read a shared row fresh, change one [value, ~value] pair, write it back."""
        row = self.read(addr, length)
        row[index], row[index + 1] = pair(value)
        self.write(addr, row)

    # ---- reads ----------------------------------------------------------
    def wake(self, tries=10):
        """Return once the mouse answers an EEPROM read (works on dongle and cable)."""
        for _ in range(tries):
            try:
                self.read(ADDR_RATE_ROW, 2)
                return
            except Asleep:
                time.sleep(0.3)
        raise Asleep("Mouse did not answer. Move it to wake it up.")

    def identify(self):
        """Look the mouse up by CID/MID, falling back to its USB ID when plugged in by cable."""
        key = None
        try:
            r = self.xfer(CX_CID_MID)
            key = f"{r[5]},{r[6]}"
        except Asleep:
            pass
        return lookup("compx", key, self.vid, self.pid)

    def link_type(self):
        """The link the mouse reports (see LINK_MAX_RATE), or None.

        ATK HUB sends 4 random bytes; the reply echoes them scrambled, then
        CID, MID and the link type.
        """
        try:
            return self.xfer(CX_MOUSE_INFO, 0, 8, list(os.urandom(4)))[11]
        except DeviceError:
            return None

    @staticmethod
    def competitive_max_allowed(key, version):
        """ATK HUB's rule: CID 1 mice always; others from a listed firmware version on."""
        if not key:
            return False
        if key.split(",")[0] == "1":
            return True
        need = competitive_max_table().get(key)
        try:
            return bool(need) and float(version) >= float(need)
        except (TypeError, ValueError):
            return False

    def versions(self):
        out = {}
        for key, cmd in (("mouse", CX_MOUSE_VERSION), ("dongle", CX_DONGLE_VERSION)):
            try:
                r = self.xfer(cmd)
                out[key] = f"{r[5]:x}.{r[6]:02x}"
            except DeviceError:
                pass
        return out

    def battery(self):
        r = self.xfer(CX_BATTERY)
        return {"level": r[5], "charging": bool(r[6]), "volts": (r[7] << 8 | r[8]) / 1000}

    def read_stages(self, count):
        """Decode the DPI table with the codec of this mouse's sensor.

        The codecs can't be told apart from the bytes: the MAD R's 1600 DPI
        reads as a plausible 320 under the wrong one. So without a known
        sensor DPI stays unreadable.
        """
        if self.codec not in CODECS:
            raise DeviceError("DPI format unknown for this mouse")
        dec = CODECS[self.codec][1]
        if self.codec == "M":
            stages = []
            for i in range(count):
                row = self.read(ADDR_DPI_3955 + 6 * i, 6)
                stages.append(dec_m(row[0] | row[1] << 8, row[4] & 1))
            return stages
        flat = []
        for a in ADDR_DPI_ROWS:
            flat += self.read(a, 8)
        return [dec(flat[4 * i], flat[4 * i + 2] & 0x0F) for i in range(count)]

    def read_state(self):
        self.wake()
        s = {"id": self.identify(), "versions": self.versions()}
        try:
            s["battery"] = self.battery()
        except DeviceError:
            pass
        rate = self.read(ADDR_RATE_ROW, 10)
        s["rate_hz"] = CX_RATES_REV.get(rate[0])
        s["sensor_mode"] = self.read(ADDR_SENSOR_ROW, 6)[4]
        # never hide a rate the mouse is already set to
        max_rate = max(self.max_rate(), s["rate_hz"] or 0)
        s["caps"] = capabilities(
            "compx", s["id"]["sensor"], s["id"]["flags"], max_rate, CX_RATES,
            self.competitive_max_allowed(s["id"]["key"], s["versions"].get("mouse")),
            s["sensor_mode"], CX_SENSOR_MODES)
        self.codec = s["caps"]["dpi_codec"]
        self.stage_count = rate[2] if 1 <= rate[2] <= 8 else 4
        s["stage_count"] = self.stage_count
        s["active_stage"] = rate[4]
        try:
            s["stages"] = self.read_stages(8)
        except Asleep:
            raise
        except DeviceError:
            s["stages"] = None   # DPI format unknown; the other settings still work
        s["lod_code"] = self.read(ADDR_LOD, 2)[0]
        func = self.read(ADDR_FUNC_ROW, 10)
        s.update(debounce_ms=func[0], motion_sync=bool(func[2]), sleep_s=func[4] * 10,
                 angle_snap=bool(func[6]), ripple=bool(func[8]))
        s["long_range"] = self.long_range(s["id"]["flags"])
        s["light"], s["stage_colors"] = self.light(s["id"]["flags"])
        s["receiver_light"] = self.receiver_light(s["id"]["receiver_flags"])
        keys = self.read_long(ADDR_KEY0, 24)
        s["buttons"] = [cx_decode_key(*keys[4 * i:4 * i + 3]) for i in range(6)]
        return s

    def backup(self):
        """Raw dump of every settings row, so a bad write can be undone by hand."""
        dump = {}
        for addr in range(0x00, 0xF0, 10):
            try:
                dump[f"0x{addr:04x}"] = self.read(addr, 10)
            except DeviceError as e:
                dump[f"0x{addr:04x}"] = str(e)
        for i in range(6):
            a = ADDR_SHORTCUT0 + 32 * i
            dump[f"0x{a:04x}"] = self.read_long(a, 30)
        return dump

    def restore(self, dump):
        """Write every settings row back. Keeps going past failures; returns failed addresses."""
        failed = []
        for key, data in dump.items():
            addr = int(key, 16)
            if not isinstance(data, list):
                continue
            try:
                if addr < 0xF0:
                    self.write(addr, data)
                else:   # 30-byte shortcut blocks
                    for off in range(0, len(data), 10):
                        self.write(addr + off, data[off:off + 10])
            except Asleep:
                raise
            except DeviceError:
                failed.append(key)
        return failed

    # ---- writes ---------------------------------------------------------
    def set_rate(self, hz):
        self.patch_row(ADDR_RATE_ROW, 10, 0, CX_RATES[hz])

    def set_active_stage(self, i):
        self.patch_row(ADDR_RATE_ROW, 10, 4, i)

    def set_stage_count(self, n):
        self.patch_row(ADDR_RATE_ROW, 10, 2, n)

    def set_stage_dpi(self, i, dpi):
        """Write one stage; returns the DPI the mouse actually stored (codecs round)."""
        if self.codec not in CODECS:
            raise DeviceError("DPI format unknown for this mouse")
        enc, dec = CODECS[self.codec]
        if self.codec == "M":
            value, doubled = enc(dpi)
            lo, hi, flags = value & 0xFF, value >> 8, doubled | doubled << 4
            row = [lo, hi, lo, hi, flags]
            self.write(ADDR_DPI_3955 + 6 * i, row + [inv(sum(row))])
            return dec_m(value, doubled)
        row_addr = ADDR_DPI_ROWS[i >> 1]
        row = self.read(row_addr, 8)
        xb, xn = enc(dpi)
        mode = ((xn & 0x0F) << 4) | (xn & 0x0F)
        o = 4 * (i & 1)
        row[o:o + 4] = [xb, xb, mode, inv(xb + xb + mode)]
        self.write(row_addr, row)
        return dec(xb, xn)

    def set_lod(self, code):
        self.write(ADDR_LOD, pair(code))

    def set_debounce(self, ms):
        self.patch_row(ADDR_FUNC_ROW, 10, 0, ms)

    def set_motion_sync(self, on):
        self.patch_row(ADDR_FUNC_ROW, 10, 2, int(on))

    def set_angle_snap(self, on):
        self.patch_row(ADDR_FUNC_ROW, 10, 6, int(on))

    def set_ripple(self, on):
        self.patch_row(ADDR_FUNC_ROW, 10, 8, int(on))

    def long_range(self, flags):
        """Long range mode on/off, or None where ATK HUB doesn't offer it."""
        if "noFarDistance" in flags:
            return None
        try:
            return bool(self.xfer(CX_GET_LONG_RANGE)[5])
        except DeviceError:
            return None

    def light(self, flags):
        """(DPI light settings, 8 stage colours), or (None, None) without a light."""
        if "noDpiRGB" in flags:
            return None, None
        row = self.read(ADDR_LIGHT_ROW, 8)
        mode = {v: k for k, v in CX_LIGHT_MODES.items()}.get(row[0], "steady")
        light = {"mode": mode if row[6] else "off",
                 "brightness": nearest_level(row[2], CX_LIGHT_BRIGHTNESS),
                 "speed": nearest_level(row[4], CX_LIGHT_SPEED)}
        colors = []
        for a in ADDR_DPI_COLOR_ROWS:
            row = self.read(a, 8)
            colors += [rgb_hex(row[0:3]), rgb_hex(row[4:7])]
        return light, colors

    def set_light(self, mode, brightness, speed):
        row = self.read(ADDR_LIGHT_ROW, 8)
        if mode != "off":                      # off keeps the last effect for next time
            row[0:2] = pair(CX_LIGHT_MODES[mode])
        row[2:6] = pair(CX_LIGHT_BRIGHTNESS[brightness]) + pair(CX_LIGHT_SPEED[speed])
        row[6:8] = pair(int(mode != "off"))
        self.write(ADDR_LIGHT_ROW, row)

    def set_stage_color(self, i, color):
        addr = ADDR_DPI_COLOR_ROWS[i >> 1]
        row = self.read(addr, 8)
        r, g, b = hex_rgb(color)
        o = 4 * (i & 1)
        row[o:o + 4] = [r, g, b, inv(r + g + b)]
        self.write(addr, row)

    def receiver_light(self, flags):
        """The receiver's light settings, or None if it has none we can drive.

        A receiver without a light refuses the read, whatever ATK HUB's list says.
        """
        kind = receiver_light_kind(flags) if self.receiver else None
        try:
            if kind == "simple":
                return {"kind": kind, "mode": self.xfer(CX_GET_RX_SIMPLE_LIGHT)[5]}
            if kind == "full":
                r = self.xfer(CX_GET_RX_LIGHT)
                return {"kind": kind, "mode": r[5], "color": rgb_hex(r[6:9]),
                        "speed": nearest_level(r[9], CX_RX_SPEED),
                        "brightness": nearest_level(r[10], CX_RX_BRIGHTNESS),
                        "sleep": nearest_level(r[11], CX_RX_SLEEP)}
        except DeviceError:
            pass
        return None

    def set_receiver_light(self, light):
        """Write a dict shaped like receiver_light()'s, then read it back."""
        if light["kind"] == "simple":
            self.xfer(CX_SET_RX_SIMPLE_LIGHT, 0, 10, [light["mode"]])
        else:
            self.xfer(CX_SET_RX_LIGHT, 0, 10,
                      [light["mode"], *hex_rgb(light["color"]), CX_RX_SPEED[light["speed"]],
                       CX_RX_BRIGHTNESS[light["brightness"]], CX_RX_SLEEP[light["sleep"]]])
        if self.receiver_light(["singleRgb"] if light["kind"] == "simple" else []) != light:
            raise DeviceError("receiver light did not stick")

    def set_long_range(self, on):
        self.xfer(CX_SET_LONG_RANGE, 0, 10, [int(on)])
        if self.xfer(CX_GET_LONG_RANGE)[5] != int(on):
            raise DeviceError("long range mode did not stick")

    def set_sleep(self, seconds):
        # Two timers: closeLedTime in the 0xA9 row and the sensor's own in 0xB5.
        units = round(seconds / 10)
        self.patch_row(ADDR_FUNC_ROW, 10, 4, units)
        row = self.read(ADDR_SENSOR_ROW, 6)
        row[0:4] = pair(1) + pair(units)
        self.write(ADDR_SENSOR_ROW, row)

    def set_sensor_mode(self, mode):
        self.patch_row(ADDR_SENSOR_ROW, 6, 4, mode)

    def set_button(self, i, action):
        c, v1, v2 = cx_encode_key(action)
        self.write(ADDR_KEY0 + 4 * i, [c, v1, v2, inv(c + v1 + v2)])

    def factory_reset(self):
        self.xfer(CX_FACTORY_RESET)


# ---- BITMOUSE ---------------------------------------------------------------
BM_SET_RATE = 0x01
BM_SET_DPI = 0x02
BM_SET_LOD = 0x03
BM_SET_BUTTON = 0x05
BM_BATTERY = 0x07
BM_GET_CONFIG = 0x09
BM_SET_LINEAR_CORRECTION = 0x0B
BM_SET_RIPPLE = 0x0C
BM_SET_LIGHT = 0x14
BM_SET_MOTION_SYNC = 0x0D
BM_CHARGING = 0x0F
BM_RESET = 0x13
BM_SET_SLEEP = 0x15
BM_SET_DEBOUNCE = 0x16
BM_GET_ADDRESS = 0x17
BM_MOUSE_VERSION = 0x1C
BM_GET_LINK_TYPE = 0x1A
BM_SET_LONG_RANGE = 0x1B
BM_SET_SENSOR_MODE = 0x1F
BM_CID_MID = 0x4A
BM_SET_RX_LIGHT = 0x85                  # same number as an upgrade command in another frame format
BM_GET_RX_LIGHT = 0x86
BM_DONGLE_VERSION = 0x88

# Pairing, address-space writes and the upgrade commands are never sent.
BM_ALLOWED = {BM_SET_RATE, BM_SET_DPI, BM_SET_LOD, BM_SET_BUTTON, BM_BATTERY, BM_GET_CONFIG,
              BM_SET_LINEAR_CORRECTION, BM_SET_RIPPLE, BM_SET_MOTION_SYNC, BM_CHARGING,
              BM_RESET, BM_SET_SLEEP, BM_SET_DEBOUNCE, BM_GET_ADDRESS, BM_MOUSE_VERSION,
              BM_SET_SENSOR_MODE, BM_CID_MID, BM_DONGLE_VERSION, BM_GET_LINK_TYPE,
              BM_SET_LONG_RANGE, BM_SET_LIGHT, BM_SET_RX_LIGHT, BM_GET_RX_LIGHT}
BM_DONGLE_CMDS = {BM_DONGLE_VERSION, BM_GET_LINK_TYPE, BM_SET_RX_LIGHT, BM_GET_RX_LIGHT}    # answered by the receiver itself (target 0)

BM_RATES = {1000: 0, 500: 1, 250: 2, 125: 3, 8000: 4, 4000: 5, 2000: 6}
BM_RATES_REV = {v: k for k, v in BM_RATES.items()}
BM_SENSOR_MODES = (0, 4, 5)             # Basic, Competitive (Game), Competitive Max
BM_ADDR_SENSOR_MODE = 74
BM_ADDR_LONG_RANGE = 75
BM_ADDR_LIGHT = 76                      # [mode, brightness, speed]
BM_LIGHT_MODES = {"off": 0, "steady": 2, "breathing": 1}
BM_LIGHT_BRIGHTNESS = (85, 170, 255)
BM_LIGHT_SPEED = (10, 15, 20)
BM_RX_BRIGHTNESS = (10, 50, 100)        # receiver speed is stored as the level itself
BM_ADDR_DPI = 1                         # 70 bytes: [active, count, 8 x (x, y, B, G, R, pad)]
BM_ADDR_BUTTONS = 82                    # 5 bytes per button

BM_SIMPLE_KEYS = {10: {"kind": "dpi", "op": "cycle"}, 11: {"kind": "dpi", "op": "up"},
                  12: {"kind": "dpi", "op": "down"}, 13: {"kind": "scroll", "dir": "up"},
                  14: {"kind": "scroll", "dir": "down"}, 15: {"kind": "scroll", "dir": "left"},
                  16: {"kind": "scroll", "dir": "right"}, 255: {"kind": "disable"},
                  4: {"kind": "macro"}}
BM_DEFAULT_KEYS = [{"kind": "mouse", "btn": b} for b in range(5)] + [{"kind": "dpi", "op": "cycle"}]


def bm_decode_key(i, c, mod, value, k0, k1):
    if c == 0:
        return BM_DEFAULT_KEYS[i]
    if c == 1:
        return {"kind": "mouse", "btn": value} if value <= 4 else {"kind": "unknown"}
    if c in (2, 3):
        return {"kind": "shortcut"}
    return BM_SIMPLE_KEYS.get(c, {"kind": "unknown", "raw": [c, mod, value, k0, k1]})


def bm_encode_key(a):
    k = a["kind"]
    if k == "mouse":
        return [1, 0, a["btn"], 0]
    if k == "disable":
        return [255, 0, 0, 0]
    if k == "dpi":
        return [{"cycle": 10, "up": 11, "down": 12}[a["op"]], 0, 0, 0]
    if k == "scroll":
        return [{"up": 13, "down": 14, "left": 15, "right": 16}[a["dir"]], 0, 0, 0]
    raise ValueError(f"unsupported action {a}")


class BitMouse(_HidDevice):
    family = "bitmouse"

    def __init__(self, node, vid, pid, log=None):
        super().__init__(node, vid, pid, log)
        # ATK HUB addresses the mouse as target 1 through a receiver and 0 over
        # a cable. For USB IDs missing from its table that rule is a guess, and
        # a wrong target gets no reply at all, so probe() tries both.
        self.target = 1 if self.receiver else 0
        usb = f"{vid:04x}:{pid:04x}"
        self.probed = self.receiver or usb in registry()["bitmouse"]["pid"]
        self.dpi_table = None

    def pack(self, cmd, param_len, cmd_len, payload=(), target=None):
        b = bytearray(63)
        b[1], b[2], b[3] = 0x72, param_len, 0
        b[4] = self.target if target is None else target
        b[5], b[6] = cmd, cmd_len
        for i, v in enumerate(payload):
            b[7 + i] = v & 0xFF
        b[0] = sum(b[1:]) & 0xFF
        return bytes(b)

    def xfer(self, cmd, param_len, cmd_len, payload=(), target=None):
        if cmd not in BM_ALLOWED:
            raise DeviceError(f"command 0x{cmd:02x} is not allowed")
        if cmd == BM_CID_MID and list(payload)[:1] != [0]:
            raise DeviceError("only reading the mouse ID is allowed")
        if cmd in (BM_SET_RX_LIGHT, BM_GET_RX_LIGHT) and (param_len, cmd_len) != (11, 9):
            raise DeviceError("receiver light commands go only in ATK HUB's own form")
        if cmd in BM_DONGLE_CMDS:
            target = 0

        def match(raw):
            # replies drop the checksum byte: [0x72, status, seq, target, cmd, len, payload...]
            if len(raw) < 7 or raw[0] == 0x73 or raw[4] != cmd:
                return None
            if raw[1] == 0xFF:
                raise DeviceError(f"device returned an error for 0x{cmd:02x}")
            return list(raw[6:])
        return self._exchange(self.pack(cmd, param_len, cmd_len, payload, target), match)

    def config(self):
        return self.xfer(BM_GET_CONFIG, 20, 18)

    def link_type(self):
        """The link the mouse reports (see LINK_MAX_RATE), or None."""
        try:
            return self.xfer(BM_GET_LINK_TYPE, 2, 1)[0]
        except (DeviceError, IndexError):
            return None

    def read_addr(self, addr, length):
        """Read up to 10 bytes of the mouse's address space."""
        p = self.xfer(BM_GET_ADDRESS, 10, 13, [addr & 0xFF, addr >> 8, length])
        return p[3:3 + length]

    # ---- reads ----------------------------------------------------------
    def probe(self):
        """Confirm which target byte this link answers on."""
        if self.probed:
            return
        for t in (self.target, self.target ^ 1):
            try:
                self.xfer(BM_GET_CONFIG, 20, 18, target=t)
                self.target, self.probed = t, True
                return
            except Asleep:
                pass
        raise Asleep("Mouse did not answer. Move it to wake it up.")

    def wake(self, tries=10):
        for _ in range(tries):
            try:
                if self.probed:
                    self.config()
                else:
                    self.probe()
                return
            except Asleep:
                time.sleep(0.3)
        raise Asleep("Mouse did not answer. Move it to wake it up.")

    def identify(self):
        key = None
        try:
            p = self.xfer(BM_CID_MID, 8, 6, [0])
            key = f"{p[1]},{p[2] | p[3] << 8 | p[4] << 16 | p[5] << 24}"
        except DeviceError:
            pass
        return lookup("bitmouse", key, self.vid, self.pid)

    def versions(self):
        out = {}
        try:
            p = self.xfer(BM_MOUSE_VERSION, 5, 3)
            out["mouse"] = ".".join(str(v) for v in p[:3])
        except DeviceError:
            pass
        if self.receiver:
            try:
                p = self.xfer(BM_DONGLE_VERSION, 5, 3)
                out["dongle"] = ".".join(str(v) for v in p[:3])
            except DeviceError:
                pass
        return out

    def battery(self):
        level = self.xfer(BM_BATTERY, 2, 1)[0]
        try:
            charging = self.xfer(BM_CHARGING, 2, 1)[0] == 1
        except DeviceError:
            charging = False
        return {"level": level, "charging": charging}

    def read_dpi_table(self):
        """[active, count, 8 x (x u16, y u16, B, G, R, pad)] as a dict."""
        raw = []
        for n in range(7):
            raw += self.read_addr(BM_ADDR_DPI + 10 * n, 10)
        stages = []
        for i in range(8):
            o = 2 + 8 * i
            stages.append({"x": raw[o] | raw[o + 1] << 8, "y": raw[o + 2] | raw[o + 3] << 8,
                           "rgb": [raw[o + 6], raw[o + 5], raw[o + 4]]})
        self.dpi_table = {"active": raw[0], "count": raw[1], "stages": stages}
        return self.dpi_table

    def read_state(self):
        self.wake()
        s = {"id": self.identify(), "versions": self.versions()}
        try:
            s["battery"] = self.battery()
        except DeviceError:
            pass
        c = self.config()
        s["rate_hz"] = BM_RATES_REV.get(c[2])
        s["sensor_mode"] = self.read_addr(BM_ADDR_SENSOR_MODE, 1)[0]
        flags = s["id"]["flags"]
        max_rate = max(self.max_rate(), s["rate_hz"] or 0)
        s["caps"] = capabilities(
            "bitmouse", s["id"]["sensor"], flags, max_rate, BM_RATES,
            "noAthleticsMax" not in flags and s["id"]["sensor"] is not None,
            s["sensor_mode"], BM_SENSOR_MODES)
        t = self.read_dpi_table()
        # A stage exists while it holds a DPI; the reported count can overshoot.
        filled = max((i + 1 for i, st in enumerate(t["stages"]) if st["x"]), default=0)
        count = min(t["count"], filled) if 1 <= t["count"] <= 8 else filled
        s["stage_count"] = max(1, count)
        s["active_stage"] = t["active"] if t["active"] < s["stage_count"] else 0
        s["stages"] = [st["x"] or 1600 for st in t["stages"]]
        s["lod_code"] = c[5] + 1                       # stored as code - 1
        s.update(motion_sync=bool(c[6]), angle_snap=bool(c[7]), ripple=bool(c[8]),
                 sleep_s=c[9] | c[10] << 8, debounce_ms=c[11])
        s["long_range"] = self.long_range(flags)
        s["light"], s["stage_colors"] = self.light(flags, t)
        s["receiver_light"] = self.receiver_light(s["id"]["receiver_flags"])
        keys = []
        for r in range(3):
            keys += self.read_addr(BM_ADDR_BUTTONS + 10 * r, 10)
        s["buttons"] = [bm_decode_key(i, *keys[5 * i:5 * i + 5]) for i in range(6)]
        return s

    # ---- writes (each one read back) ------------------------------------
    def _set(self, cmd, param_len, cmd_len, payload, check):
        self.xfer(cmd, param_len, cmd_len, payload)
        if not check():
            raise DeviceError(f"setting 0x{cmd:02x} did not stick")

    def set_rate(self, hz):
        self._set(BM_SET_RATE, 3, 1, [BM_RATES[hz]], lambda: self.config()[2] == BM_RATES[hz])

    def set_lod(self, code):
        self._set(BM_SET_LOD, 4, 2, [0, code - 1], lambda: self.config()[5] == code - 1)

    def set_debounce(self, ms):
        self._set(BM_SET_DEBOUNCE, 3, 1, [ms], lambda: self.config()[11] == ms)

    def set_motion_sync(self, on):
        self._set(BM_SET_MOTION_SYNC, 3, 1, [int(on)], lambda: self.config()[6] == int(on))

    def set_angle_snap(self, on):
        self._set(BM_SET_LINEAR_CORRECTION, 3, 1, [int(on)],
                  lambda: self.config()[7] == int(on))

    def set_ripple(self, on):
        self._set(BM_SET_RIPPLE, 3, 1, [int(on)], lambda: self.config()[8] == int(on))

    def long_range(self, flags):
        """Long range mode on/off, or None where ATK HUB doesn't offer it."""
        if "noFarDistance" in flags:
            return None
        try:
            return bool(self.read_addr(BM_ADDR_LONG_RANGE, 1)[0])
        except (DeviceError, IndexError):
            return None

    def light(self, flags, table):
        """(DPI light settings, 8 stage colours), or (None, None) without a light."""
        if "noDpiRGB" in flags:
            return None, None
        try:
            raw = self.read_addr(BM_ADDR_LIGHT, 3)
        except DeviceError:
            return None, None
        mode = {v: k for k, v in BM_LIGHT_MODES.items()}.get(raw[0], "steady")
        light = {"mode": mode, "brightness": nearest_level(raw[1], BM_LIGHT_BRIGHTNESS),
                 "speed": nearest_level(raw[2], BM_LIGHT_SPEED)}
        return light, [rgb_hex(st["rgb"]) for st in table["stages"]]

    def set_light(self, mode, brightness, speed):
        want = [BM_LIGHT_MODES[mode], BM_LIGHT_BRIGHTNESS[brightness], BM_LIGHT_SPEED[speed]]
        self._set(BM_SET_LIGHT, 6, 3, want, lambda: self.read_addr(BM_ADDR_LIGHT, 3) == want)

    def set_stage_color(self, i, color):
        t = self.read_dpi_table()
        st = t["stages"][i]
        self._write_stage(i, st["x"], hex_rgb(color), i == t["active"])
        if rgb_hex(self.read_dpi_table()["stages"][i]["rgb"]) != color:
            raise DeviceError("colour did not stick")

    def receiver_light(self, flags):
        """The receiver's light settings, or None if it has none we can drive."""
        kind = receiver_light_kind(flags) if self.receiver else None
        if kind is None:
            return None
        try:
            p = self.xfer(BM_GET_RX_LIGHT, 11, 9)
        except DeviceError:
            return None
        # [on, mode, brightness, speed, ?, R, G, B, sleep]; ATK HUB shows no sleep here
        self.rx_light_raw = p[:9]
        light = {"kind": kind, "mode": p[1]}
        if kind == "full":
            light.update(color=rgb_hex(p[5:8]), speed=min(p[3], 2),
                         brightness=nearest_level(p[2], BM_RX_BRIGHTNESS), sleep=None)
        return light

    def set_receiver_light(self, light):
        """Write a dict shaped like receiver_light()'s, then read it back."""
        raw = list(getattr(self, "rx_light_raw", [0] * 9))
        raw[0], raw[1] = 1, light["mode"]
        if light["kind"] == "full":
            raw[2], raw[3] = BM_RX_BRIGHTNESS[light["brightness"]], light["speed"]
            raw[5:8] = hex_rgb(light["color"])
        self.xfer(BM_SET_RX_LIGHT, 11, 9, raw)
        flags = ["singleRgb"] if light["kind"] == "simple" else []
        if self.receiver_light(flags) != light:
            raise DeviceError("receiver light did not stick")

    def set_long_range(self, on):
        self._set(BM_SET_LONG_RANGE, 3, 1, [int(on)],
                  lambda: self.read_addr(BM_ADDR_LONG_RANGE, 1)[0] == int(on))

    def set_sleep(self, seconds):
        def check():
            c = self.config()
            return c[9] | c[10] << 8 == seconds
        self._set(BM_SET_SLEEP, 5, 2, [seconds & 0xFF, seconds >> 8], check)

    def set_sensor_mode(self, mode):
        self._set(BM_SET_SENSOR_MODE, 3, 1, [mode],
                  lambda: self.read_addr(BM_ADDR_SENSOR_MODE, 1)[0] == mode)

    def set_button(self, i, action):
        key = bm_encode_key(action)
        self._set(BM_SET_BUTTON, 8, 5, [i] + key,
                  lambda: self.read_addr(BM_ADDR_BUTTONS + 5 * i, 4) == key)

    def _write_stage(self, i, dpi, rgb, active):
        """One SetDpi frame carries a stage's DPI and colour; active selects it."""
        r, g, b = rgb
        self.xfer(BM_SET_DPI, 12, 10,
                  [i, dpi & 0xFF, dpi >> 8, dpi & 0xFF, dpi >> 8, b, g, r, 0, int(active)])

    def set_stage_dpi(self, i, dpi):
        t = self.read_dpi_table()
        self._write_stage(i, dpi, t["stages"][i]["rgb"], i == t["active"])
        if self.read_dpi_table()["stages"][i]["x"] != dpi:
            raise DeviceError("DPI did not stick")
        return dpi

    def set_active_stage(self, i):
        t = self.read_dpi_table()
        st = t["stages"][i]
        self._write_stage(i, st["x"], st["rgb"], True)
        if self.read_dpi_table()["active"] != i:
            raise DeviceError("stage change did not stick")

    def set_stage_count(self, n):
        raise DeviceError("changing the number of stages isn't supported on this mouse")

    def factory_reset(self):
        self.xfer(BM_RESET, 2, 1)


if __name__ == "__main__":
    import sys
    if "--update-devices" in sys.argv:
        reg = registry(refresh=True)
        print(f"device list: {len(reg['compx']['cid'])} COMPX and "
              f"{len(reg['bitmouse']['cid'])} BITMOUSE mice")
        sys.exit(0 if reg is not EMPTY_REGISTRY else 1)
    print("nodes:", [(n, f"{v:04x}:{p:04x}", fam) for n, v, p, fam in find_nodes()])
    m = open_mouse(log=print if "-v" in sys.argv else None)
    m.wake(20)
    print("using:", m.node, f"{m.vid:04x}:{m.pid:04x}", m.family,
          "(receiver)" if m.receiver else "(cable)")
    print(json.dumps(m.read_state(), indent=2))
    if "--backup" in sys.argv and m.family == "compx":
        print(json.dumps(m.backup()))
