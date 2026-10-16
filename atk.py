# SPDX-License-Identifier: MIT
# Protocol tables and DPI codecs ported from OpenATK, Copyright (c) 2026 Sunnybee.
"""Driver for COMPX-family ATK / VXE mice (VXE MAD R and relatives) over Linux hidraw.

Protocol details come from OpenATK (MIT, see LICENSE-OpenATK):
https://github.com/SunnybeeXD/OpenATK/blob/main/docs/PROTOCOL.md

Frame (16 bytes, sent as output report 8):
    0 commandId  1 status  2-3 EEPROM address (big endian)  4 length
    5-14 payload  15 checksum = 85 - (8 + sum(bytes 0..14))
"""

import glob
import json
import os
import re
import select
import time
import urllib.request

VID = 0x373B
REPORT_ID = 8

CMD_BATTERY = 0x04
CMD_SET_EEPROM = 0x07
CMD_GET_EEPROM = 0x08
CMD_FACTORY_RESET = 0x09
CMD_GET_CONFIG = 0x0E
CMD_CID_MID = 0x10
CMD_MOUSE_VERSION = 0x12
CMD_DONGLE_VERSION = 0x1D

# Anything else (firmware upgrade 0x0D, raw download 0x01, pairing) is refused.
ALLOWED_CMDS = {CMD_BATTERY, CMD_SET_EEPROM, CMD_GET_EEPROM, CMD_FACTORY_RESET,
                CMD_GET_CONFIG, CMD_CID_MID, CMD_MOUSE_VERSION, CMD_DONGLE_VERSION}

ADDR_RATE_ROW = 0x00      # [rate,~, maxDpi(stage count),~, activeStage,~, bhop,~, btnMode,~]
ADDR_LOD = 0x0A           # [code,~]
ADDR_DPI_ROWS = [0x0C, 0x14, 0x1C, 0x24]    # two stages per 8-byte row
ADDR_DPI_COLOR = [0x2C, 0x34, 0x3C, 0x44]
ADDR_KEY0 = 0x60          # 4 bytes per button
ADDR_FUNC_ROW = 0xA9      # [debounce,~, motionSync,~, sleep/10,~, lineCorr,~, ripple,~]
ADDR_SENSOR_ROW = 0xB5    # [sleepOn,~, sleep/10,~, sensorMode,~]
ADDR_ANGLE = 0xBD         # [deg,~, on,~]
ADDR_SHORTCUT0 = 0x100    # 32 bytes per button

RATES = {1000: 0x01, 500: 0x02, 250: 0x04, 125: 0x08,
         2000: 0x10, 4000: 0x20, 8000: 0x40}
RATES_REV = {v: k for k, v in RATES.items()}

# Known mice behind COMPX dongles: (cid, mid) -> (name, DPI codec group)
# Device registry from OpenATK: mice by CID/MID ("2,49") and by USB PID ("103f").
# Flags: A/B/M/X = DPI format, u = continuous lift-off, 7 = 0.7 mm step, r = receiver.
with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "devices.json")) as _f:
    REGISTRY = json.load(_f)

# ATK HUB's public config listing, per COMPX mouse ("cid,mid"), the lowest
# firmware that offers Competitive Max. Fetched on first need, cached, and
# refreshed at most once a week; this is the app's only network request.
UPGRADE_URL = "https://bpcdn.atkgear.com/device/constant/mouse-upgrade.json"
UPGRADE_CACHE = os.path.expanduser("~/.config/atk-mouse/competitive-max.json")
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


# Mice with a sixth button slot in firmware but no physical button (VXE MAD R)
NO_DPI_BUTTON = {"cid": {"2,49"}, "pid": {0x103F}}

# Sensor modes as ATK HUB reads and writes them for COMPX mice. (OpenATK lists
# 0/4/5, which is the BITMOUSE encoding; the COMPX driver stores 0/1/2 as-is.)
SENSOR_BASIC, SENSOR_COMPETITIVE, SENSOR_COMPETITIVE_MAX = 0, 1, 2

# Highest DPI per sensor, from the sensor datasheets
MAX_DPI = {"B": 26000, "B7": 30000, "A": 42000}

BUTTON_NAMES =["Left", "Right", "Middle", "Back", "Forward", "DPI button"]


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


# ---- DPI codecs -------------------------------------------------------------
# Group B (PAW3395 / PAW3950): 50-DPI steps, 100-DPI steps above 30000.
def enc_b(dpi):
    doubled = 1 if dpi > 30000 else 0
    code = round(dpi / (100 if doubled else 50)) - 1
    return code & 0xFF, ((code >> 8) << 2) | doubled


def dec_b(byte, nib):
    code = (((nib >> 2) & 3) << 8) | byte
    return 50 * (code + 1) * (2 if nib & 1 else 1)


# Group A (PAW3950 Ultra): 10-DPI steps to 10000, then 50-DPI steps.
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


CODECS = {"A": (enc_a, dec_a), "B": (enc_b, dec_b)}


# ---- buttons ----------------------------------------------------------------
def decode_key(c, v1, v2):
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


def encode_key(a):
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


# ---- transport --------------------------------------------------------------
DONGLE_PIDS = {0x104D, 0x1040}   # MAD 1K / 8K receivers


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
    """All ATK vendor config nodes as (node, pid, family).

    Both ATK protocols use report ID 8. COMPX (this driver) sends 16-byte
    reports; BITMOUSE sends 63-byte ones and is not supported here.
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
        if f"{VID:08X}:" not in uevent.upper():
            continue
        length = report_bytes(desc, REPORT_ID)
        if length is None:
            continue
        family = "compx" if length == 16 else "bitmouse"
        pid = uevent.upper().split(f"{VID:08X}:")[1][:8]
        found.append(("/dev/" + os.path.basename(sys_path), int(pid, 16), family))
    return found


def find_node():
    """Pick one node: supported protocol first, then cable over wireless dongle."""
    nodes = find_nodes()
    nodes.sort(key=lambda n: (n[2] != "compx", n[1] in DONGLE_PIDS))
    return nodes[0] if nodes else (None, None, None)


class Mouse:
    def __init__(self, node=None, log=None):
        self.node, self.pid, family = (node, None, "compx") if node else find_node()
        if not self.node:
            raise DeviceError("No ATK/VXE mouse found. Is the dongle plugged in?")
        if family != "compx":
            raise Unsupported("This mouse uses ATK's other protocol (BITMOUSE), which "
                              "this app does not support. OpenATK supports it in "
                              "Chrome or Chromium: https://github.com/SunnybeeXD/OpenATK")
        try:
            self.fd = os.open(self.node, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise DeviceError(f"No permission for {self.node}. Run install.sh, "
                              "then unplug the mouse or dongle and plug it back in.")
        self.log = log
        self.group = None
        self.stage_count = 0

    def close(self):
        os.close(self.fd)

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

    def _drain(self):
        while select.select([self.fd], [], [], 0)[0]:
            try:
                os.read(self.fd, 64)
            except BlockingIOError:
                break

    def xfer(self, cmd, addr=0, length=0, payload=()):
        if cmd not in ALLOWED_CMDS:
            raise DeviceError(f"command 0x{cmd:02x} is not allowed")
        frame = self.pack(cmd, addr, length, payload)
        for _attempt in range(3):
            self._drain()
            os.write(self.fd, bytes([REPORT_ID]) + frame)
            if self.log:
                self.log("TX " + frame.hex(" "))
            deadline = time.monotonic() + 0.6
            while (left := deadline - time.monotonic()) > 0:
                if not select.select([self.fd], [], [], left)[0]:
                    break
                raw = os.read(self.fd, 64)
                if not raw or raw[0] != REPORT_ID:
                    continue          # mouse motion, media keys, other reports
                r = raw[1:17]
                if self.log:
                    self.log("RX " + r.hex(" "))
                if r[0] != cmd:
                    continue
                if cmd in (CMD_GET_EEPROM, CMD_SET_EEPROM) and (r[2] << 8 | r[3]) != addr:
                    continue
                if (85 - ((8 + sum(r[:15])) & 0xFF)) & 0xFF != r[15]:
                    raise DeviceError(f"bad checksum in reply: {r.hex(' ')}")
                if r[1] != 0:
                    raise DeviceError(f"device returned status {r[1]} for 0x{cmd:02x}")
                return r
        raise Asleep("Mouse did not answer. Move it to wake it up.")

    def read(self, addr, length):
        """Read up to 10 bytes of EEPROM."""
        return list(self.xfer(CMD_GET_EEPROM, addr, length)[5:5 + length])

    def read_long(self, addr, length):
        out = []
        for off in range(0, length, 10):
            out += self.read(addr + off, min(10, length - off))
        return out

    def write(self, addr, data, verify=True):
        data = [v & 0xFF for v in data]
        self.xfer(CMD_SET_EEPROM, addr, len(data), data)
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
        """Look the mouse up by CID/MID, falling back to its USB PID when plugged in by cable."""
        key = None
        try:
            r = self.xfer(CMD_CID_MID)
            key = f"{r[5]},{r[6]}"
        except Asleep:
            pass
        entry = REGISTRY["cid"].get(key)
        pid_entry = REGISTRY["pids"].get(f"{self.pid or 0:04x}")
        if entry is None and pid_entry and "r" not in pid_entry[1]:
            entry = pid_entry
        if entry:
            name, flags = entry
        else:
            name, flags = f"Unknown ATK mouse ({key or 'no ID'})", ""
        dpi_button = key not in NO_DPI_BUTTON["cid"] and self.pid not in NO_DPI_BUTTON["pid"]
        receiver = pid_entry[0] if pid_entry and "r" in pid_entry[1] else None
        return {"key": key, "name": name, "flags": flags,
                "dpi_button": dpi_button, "receiver": receiver}

    def usb_max_rate(self, receiver_name=None):
        """Highest polling rate the USB link can carry.

        Full-speed USB polls at most every 1 ms (1000 Hz). High-speed links
        report their interrupt interval, for example 125us for 8000 Hz.
        A receiver named "1K"/"4K"/"8K" caps it further.
        """
        cap = 1000
        try:
            hid_dev = os.path.realpath(f"/sys/class/hidraw/{os.path.basename(self.node)}/device")
            intf = os.path.dirname(hid_dev)
            with open(os.path.join(os.path.dirname(intf), "speed")) as f:
                speed = float(f.read())
            if speed >= 480:
                for iv in glob.glob(f"{intf}/ep_*/interval"):
                    with open(iv) as f:
                        t = f.read().strip()
                    us = float(t[:-2]) if t.endswith("us") else float(t[:-2]) * 1000
                    cap = max(cap, min(8000, int(1_000_000 / us)))
        except (OSError, ValueError):
            pass
        if receiver_name:
            m = re.search(r"(\d)\s*k\b", receiver_name, re.I)
            if m:
                cap = min(cap, int(m.group(1)) * 1000)
        return cap

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

    @staticmethod
    def capabilities(ident, max_rate, version=None, sensor_mode=None):
        flags = ident["flags"]
        group = next((g for g in "ABMX" if g in flags), None)
        if "u" in flags:
            lod = "continuous"                          # 0.7-1.7 mm in 0.1 mm steps
        elif group == "B":
            lod = "levels07" if "7" in flags else "levels"
        else:
            lod = None                                  # unknown sensor: read-only
        sensor = (group or "") + ("7" if "7" in flags else "")
        return {"dpi_codec": group if group in CODECS else None,
                "max_dpi": MAX_DPI.get(sensor, MAX_DPI.get(group, 26000)),
                "lod": lod,
                "rates": [hz for hz in sorted(RATES) if hz <= max_rate],
                "competitive_max": (Mouse.competitive_max_allowed(ident["key"], version)
                                    or sensor_mode == SENSOR_COMPETITIVE_MAX),
                "dpi_button": ident["dpi_button"]}

    def versions(self):
        out = {}
        for key, cmd in (("mouse", CMD_MOUSE_VERSION), ("dongle", CMD_DONGLE_VERSION)):
            try:
                r = self.xfer(cmd)
                out[key] = f"{r[5]:x}.{r[6]:02x}"
            except DeviceError:
                pass
        return out

    def battery(self):
        r = self.xfer(CMD_BATTERY)
        return {"level": r[5], "charging": bool(r[6]), "volts": (r[7] << 8 | r[8]) / 1000}

    def read_stages(self, count):
        """Decode the DPI table with the codec the registry names for this mouse.

        The codecs can't be told apart from the bytes: the MAD R's 1600 DPI
        reads as a plausible 320 under the wrong one. So without a known
        codec (unlisted mouse, PAW3955, unknown sensor) DPI stays unreadable.
        """
        if self.group not in CODECS:
            raise DeviceError("DPI format unknown for this mouse")
        dec = CODECS[self.group][1]
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
        s["rate_hz"] = RATES_REV.get(rate[0])
        s["sensor_mode"] = self.read(ADDR_SENSOR_ROW, 6)[4]
        # never hide a rate or mode the mouse is already set to
        max_rate = max(self.usb_max_rate(s["id"]["receiver"]), s["rate_hz"] or 0)
        s["caps"] = self.capabilities(s["id"], max_rate, s["versions"].get("mouse"),
                                      s["sensor_mode"])
        self.group = s["caps"]["dpi_codec"]
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
        keys = self.read_long(ADDR_KEY0, 24)
        s["buttons"] = [decode_key(*keys[4 * i:4 * i + 3]) for i in range(6)]
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
        self.patch_row(ADDR_RATE_ROW, 10, 0, RATES[hz])

    def set_active_stage(self, i):
        self.patch_row(ADDR_RATE_ROW, 10, 4, i)

    def set_stage_count(self, n):
        self.patch_row(ADDR_RATE_ROW, 10, 2, n)

    def set_stage_dpi(self, i, dpi):
        """Write one stage; returns the DPI the mouse actually stored (codecs round)."""
        if self.group not in CODECS:
            raise DeviceError("DPI format unknown for this mouse")
        enc = CODECS[self.group][0]
        row_addr = ADDR_DPI_ROWS[i >> 1]
        row = self.read(row_addr, 8)
        xb, xn = enc(dpi)
        mode = ((xn & 0x0F) << 4) | (xn & 0x0F)
        o = 4 * (i & 1)
        row[o:o + 4] = [xb, xb, mode, inv(xb + xb + mode)]
        self.write(row_addr, row)
        return CODECS[self.group][1](xb, xn)

    def set_lod(self, code):
        self.write(ADDR_LOD, pair(code))

    @staticmethod
    def lod_mm_to_code(mm):
        """Continuous lift-off sensors: code 1 = 0.7 mm ... code 11 = 1.7 mm."""
        return min(11, max(1, round(mm * 10) - 6))

    @staticmethod
    def lod_code_to_mm(code):
        return (code + 6) / 10

    def set_debounce(self, ms):
        self.patch_row(ADDR_FUNC_ROW, 10, 0, ms)

    def set_motion_sync(self, on):
        self.patch_row(ADDR_FUNC_ROW, 10, 2, int(on))

    def set_angle_snap(self, on):
        self.patch_row(ADDR_FUNC_ROW, 10, 6, int(on))

    def set_ripple(self, on):
        self.patch_row(ADDR_FUNC_ROW, 10, 8, int(on))

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
        c, v1, v2 = encode_key(action)
        self.write(ADDR_KEY0 + 4 * i, [c, v1, v2, inv(c + v1 + v2)])

    def factory_reset(self):
        self.xfer(CMD_FACTORY_RESET)


if __name__ == "__main__":
    import sys
    m = Mouse(log=print if "-v" in sys.argv else None)
    m.wake(20)
    print("nodes:", [(n, hex(p), fam) for n, p, fam in find_nodes()])
    print("using:", m.node, "pid:", hex(m.pid or 0),
          "(dongle)" if m.pid in DONGLE_PIDS else "(cable)")
    print(json.dumps(m.read_state(), indent=2))
    if "--backup" in sys.argv:
        print(json.dumps(m.backup()))
