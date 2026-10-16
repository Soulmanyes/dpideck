"""Regenerate golden.json from atk.py: run `python3 core/testdata/gen_golden.py` in windows/.

The Go tests check the port against these answers, so the two versions agree.
"""
import itertools, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
import atk  # noqa: E402

out = {}
vals = sorted(set(range(1, 50001, 37)) | {1, 10, 50, 100, 200, 9990, 10000, 10010, 10020, 10049,
              10050, 10100, 18000, 26000, 29950, 30000, 30049, 30050, 30080, 30100, 35950, 36000,
              39999, 40000, 40001, 40002, 40003, 42000, 49998, 50000})
out["enc"] = {c: [[v, *atk.CODECS[c][0](v)] for v in vals if not (c in ("A", "B", "C") and v > 42000)]
              for c in ("A", "B", "C", "SE", "T15")}
out["dec"] = {c: [[b, n, atk.CODECS[c][1](b, n)] for b in range(256) for n in range(16)]
              for c in ("A", "B", "C", "SE", "T15")}
out["enc_m"] = [[v, *atk.enc_m(v)] for v in vals]
out["dec_m"] = [[v, d, atk.dec_m(v, d)] for v in range(0, 65536, 97) for d in (0, 1)]
out["lod_mm"] = [[round(x / 100, 2), atk.lod_mm_to_code(x / 100)] for x in range(40, 211, 5)]
out["lod_code"] = [[c, atk.lod_code_to_mm(c)] for c in range(0, 13)]

caps = []
for family, rates, modes in (("compx", atk.CX_RATES, atk.CX_SENSOR_MODES),
                             ("bitmouse", atk.BM_RATES, atk.BM_SENSOR_MODES)):
    for sensor in list(atk.SENSORS) + [None, "PAW3320"]:
        for flags in ([], ["noBottomButton"], ["noZeroKeyDebounce", "noBottomButton"]):
            for max_rate, cmax, mode in itertools.product((1000, 4000, 8000), (False, True),
                                                          (modes[0], modes[2])):
                c = atk.capabilities(family, sensor, flags, max_rate, rates, cmax, mode, modes)
                caps.append({"args": [family, sensor, flags, max_rate, cmax, mode], "caps": c})
out["caps"] = caps

snaps = []
for family in ("compx", "bitmouse"):
    for sensor in list(atk.SENSORS) + [None]:
        c = atk.capabilities(family, sensor, [], 1000, atk.CX_RATES, False, 0, atk.CX_SENSOR_MODES)
        for v in list(range(0, 52000, 211)) + [9, 10, 11, 49, 51, 99, 101, 10004, 10006, 10025,
                                                10026, 30049, 30051, 40001, 40003]:
            snaps.append([family, sensor, v, atk.snap_dpi(v, c)])
out["snap"] = snaps

out["cx_pack"] = [[cmd, addr, n, p, list(atk.CompxMouse.pack(cmd, addr, n, p))]
                  for cmd, addr, n, p in ((0x08, 0x00, 10, []), (0x07, 0xB5, 6, [0, 85, 6, 79, 2, 83]),
                                          (0x10, 0, 0, []), (0x07, 0x1B00, 6, [1, 2, 3, 4, 5, 6]),
                                          (0x08, 0x24, 8, []))]
bm = atk.BitMouse.__new__(atk.BitMouse); bm.target = 1
out["bm_pack"] = [[cmd, pl, cl, p, t, list(bm.pack(cmd, pl, cl, p, t))]
                  for cmd, pl, cl, p, t in ((0x09, 20, 18, [], 1), (0x17, 10, 13, [74, 0, 1], 0),
                                            (0x02, 12, 10, [1, 64, 6, 64, 6, 0, 0, 255, 0, 1], 1),
                                            (0x4A, 8, 6, [0], 0))]
out["cx_decode"] = [[c, v1, v2, atk.cx_decode_key(c, v1, v2)] for c in range(0, 13)
                    for v1 in (0, 1, 2, 3, 4, 8, 16, 32) for v2 in (0, 5)]
out["bm_decode"] = [[i, c, v, atk.bm_decode_key(i, c, 0, v, 0, 0)] for i in range(6)
                    for c in (0, 1, 2, 3, 4, 9, 10, 11, 12, 13, 14, 15, 16, 17, 255) for v in (0, 2, 4, 5)]
actions = [{"kind": "mouse", "btn": b} for b in range(5)] + [{"kind": "disable"}] + \
          [{"kind": "dpi", "op": o} for o in ("cycle", "up", "down")] + \
          [{"kind": "scroll", "dir": d} for d in ("up", "down", "left", "right")]
out["cx_encode"] = [[a, atk.cx_encode_key(a)] for a in actions]
out["bm_encode"] = [[a, atk.bm_encode_key(a)] for a in actions]
table = {"2,49": "3.00", "2,47": "3.00", "2,10": "1.5"}
cm = []
orig = atk.competitive_max_table
atk.competitive_max_table = lambda: table
for k, v in (("2,49", "3.00"), ("2,49", "2.99"), ("2,49", "a.00"), ("1,55", "0.1"), (None, "3"),
             ("2,10", "1.50"), ("9,9", "9.9"), ("2,47", "3.01")):
    cm.append([k or "", v, atk.CompxMouse.competitive_max_allowed(k, v)])
atk.competitive_max_table = orig
out["cmax"] = cm
out["cmax_table"] = table
json.dump(out, open(os.path.join(os.path.dirname(__file__), "golden.json"), "w"))
print({k: len(v) for k, v in out.items()})
