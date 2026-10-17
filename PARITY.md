# Linux / Windows parity

The app exists twice: `app.py` + `atk.py` (Linux, Python/GTK) and `windows/`
(Windows, Go/Walk). Every change goes into both. `windows/core` is a line-by-line
port of `atk.py`, and `windows/core/testdata/golden.json` holds `atk.py`'s answers
for thousands of inputs. After changing `atk.py`, run
`python3 windows/core/testdata/gen_golden.py`, then `go test ./core/` in
`windows/` (or `windows/build.sh`, which runs it too).

| Behaviour | Linux | Windows |
|---|---|---|
| COMPX and BITMOUSE drivers, command allowlists | ✓ | ✓ |
| Writes read back; "Not saved" + reload on failure | ✓ | ✓ |
| Never write after a failed read | ✓ | ✓ |
| Per-sensor DPI ranges, codecs and snapping | ✓ | ✓ |
| Lift-off: 1/2 mm, +0.7 mm (COMPX PAW3950), 0.7–1.7 mm, or hidden | ✓ | ✓ |
| Sensor modes: COMPX 0/1/2, BITMOUSE 0/4/5 | ✓ | ✓ |
| Competitive Max gating (ATK's list / noAthleticsMax) | ✓ | ✓ |
| Hide DPI button (noBottomButton), no "Off" debounce (noZeroKeyDebounce) | ✓ | ✓ |
| Long range mode, hidden on noFarDistance mice | ✓ | ✓ |
| DPI light and stage colours, hidden on noDpiRGB mice | ✓ | ✓ |
| Receiver light (full and singleRgb receivers; hidden when the receiver refuses the read) | ✓ | ✓ |
| BITMOUSE stage count read-only | ✓ | ✓ |
| Backup / restore (COMPX only, same file format) | ✓ | ✓ |
| Factory reset with confirmation | ✓ | ✓ |
| Mouse wheel never changes a setting | ✓ | ✓ |
| Device list downloaded from ATK HUB, 30-day cache | ✓ | ✓ |
| Skip mice in firmware-recovery mode | ✓ | ✓ |
| Prefer cable over receiver; reconnect on plug/unplug | ✓ | ✓ |
| Polling-rate cap | link type the mouse reports; else none | same |
| Install | `install.sh` (udev rule, deps) | none: single exe |
