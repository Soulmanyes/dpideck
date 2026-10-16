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
import select
import time

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
MICE = {(2, 49): ("VXE MAD R", "B"), (2, 54): ("VXE MAD R+", "B"),
        (2, 47): ("VXE MAD R MAJOR", "B"), (2, 55): ("VXE MAD R MAJOR+", "B"),
        (2, 133): ("VXE MAD R", "A")}

# Mice plugged in by cable enumerate under their own PID
WIRED_PIDS = {0x103F: ("VXE MAD R", "B"), 0x104B: ("VXE MAD R+", "B"),
              0x103E: ("VXE MAD R MAJOR", "B"), 0x104C: ("VXE MAD R MAJOR+", "B"),
              0x132B: ("VXE MAD R", "A")}

BUTTON_NAMES =["Left", "Right", "Middle", "Back", "Forward", "DPI button"]


class DeviceError(Exception):
    pass


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


def find_nodes():
    """All /dev/hidrawN nodes carrying the vendor config report (ID 8), as (node, pid)."""
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
        # vendor usage page (06 xx FF) and report ID 8 (85 08)
        if b"\x85\x08" in desc and any(desc[i] == 0x06 and desc[i + 2] == 0xFF
                                       for i in range(len(desc) - 2)):
            pid = uevent.upper().split(f"{VID:08X}:")[1][:8]
            found.append(("/dev/" + os.path.basename(sys_path), int(pid, 16)))
    return found


def find_node():
    """Pick one node, preferring a cable connection over a wireless dongle."""
    nodes = find_nodes()
    nodes.sort(key=lambda n: n[1] in DONGLE_PIDS)
    return nodes[0] if nodes else (None, None)


class Mouse:
    def __init__(self, node=None, log=None):
        self.node, self.pid = (node, None) if node else find_node()
        if not self.node:
            raise DeviceError("No ATK/VXE mouse found. Is the dongle plugged in?")
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
        """Ask for CID/MID; over a cable that query may go unanswered, so fall back to the PID."""
        try:
            r = self.xfer(CMD_CID_MID)
            cid, mid = r[5], r[6]
            name, group = MICE.get((cid, mid), (f"Unknown ATK mouse ({cid},{mid})", None))
            return {"cid": cid, "mid": mid, "name": name, "group": group}
        except Asleep:
            name, group = WIRED_PIDS.get(self.pid, ("ATK mouse", None))
            return {"cid": None, "mid": None, "name": name, "group": group}

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

    def read_stages(self, count, expected_group=None):
        """Decode the DPI table with both codecs and keep the one that looks right.

        A wrong codec returns plausible but scaled numbers instead of failing,
        so pick on evidence: real stages are 50-42000, on a 10-DPI step, x == y.
        """
        flat = []
        for a in ADDR_DPI_ROWS:
            flat += self.read(a, 8)
        best = None
        for g, (_enc, dec) in CODECS.items():
            stages = []
            for i in range(count):
                o = 4 * i
                stages.append((dec(flat[o], flat[o + 2] & 0x0F),
                               dec(flat[o + 1], (flat[o + 2] >> 4) & 0x0F)))
            score = sum(10 for x, y in stages if 50 <= x <= 42000 and x % 10 == 0 and x == y)
            score += 1 if g == expected_group else 0
            if best is None or score > best[0]:
                best = (score, g, stages)
        if best[0] < 10:
            raise DeviceError("could not decode the DPI table: " + bytes(flat).hex(" "))
        self.group = best[1]
        return [x for x, _y in best[2]]

    def read_state(self):
        self.wake()
        s = {"id": self.identify(), "versions": self.versions()}
        try:
            s["battery"] = self.battery()
        except DeviceError:
            pass
        rate = self.read(ADDR_RATE_ROW, 10)
        s["rate_hz"] = RATES_REV.get(rate[0])
        self.stage_count = rate[2] if 1 <= rate[2] <= 8 else 4
        s["stage_count"] = self.stage_count
        s["active_stage"] = rate[4]
        s["stages"] = self.read_stages(8, s["id"]["group"])
        s["dpi_codec"] = self.group
        s["lod_code"] = self.read(ADDR_LOD, 2)[0]
        func = self.read(ADDR_FUNC_ROW, 10)
        s.update(debounce_ms=func[0], motion_sync=bool(func[2]), sleep_s=func[4] * 10,
                 angle_snap=bool(func[6]), ripple=bool(func[8]))
        s["sensor_mode"] = self.read(ADDR_SENSOR_ROW, 6)[4]
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
        if self.group is None:
            raise DeviceError("read the DPI table first")
        enc = CODECS[self.group][0]
        row_addr = ADDR_DPI_ROWS[i >> 1]
        row = self.read(row_addr, 8)
        xb, xn = enc(dpi)
        mode = ((xn & 0x0F) << 4) | (xn & 0x0F)
        o = 4 * (i & 1)
        row[o:o + 4] = [xb, xb, mode, inv(xb + xb + mode)]
        self.write(row_addr, row)

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
    print("nodes:", [(n, hex(p)) for n, p in find_nodes()])
    print("using:", m.node, "pid:", hex(m.pid or 0),
          "(dongle)" if m.pid in DONGLE_PIDS else "(cable)")
    print(json.dumps(m.read_state(), indent=2))
    if "--backup" in sys.argv:
        print(json.dumps(m.backup()))
