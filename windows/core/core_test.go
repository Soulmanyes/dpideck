package core

import (
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"sort"
	"strconv"
	"testing"
)

// golden.json holds atk.py's answers (see testdata/gen_golden.py); the port must match.
func loadGolden(t *testing.T) map[string]json.RawMessage {
	b, err := os.ReadFile("testdata/golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var g map[string]json.RawMessage
	if err := json.Unmarshal(b, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

func golden[T any](t *testing.T, g map[string]json.RawMessage, key string) T {
	var v T
	if err := json.Unmarshal(g[key], &v); err != nil {
		t.Fatalf("%s: %v", key, err)
	}
	return v
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// asPython renders values the way atk.py's JSON does, for comparison.
func capsAsPython(c Caps) map[string]any {
	segs := [][]int{}
	for _, s := range c.DPISegments {
		segs = append(segs, []int{s.Until, s.Step})
	}
	modes := [][]any{}
	for _, m := range c.SensorModes {
		modes = append(modes, []any{m.Value, m.Label})
	}
	rates := c.Rates
	if rates == nil {
		rates = []int{}
	}
	return normalize(map[string]any{"family": c.Family, "sensor": nilIfEmpty(c.Sensor),
		"dpi_codec": nilIfEmpty(c.DPICodec), "min_dpi": c.MinDPI, "max_dpi": c.MaxDPI,
		"dpi_segments": segs, "dpi_step": c.DPIStep, "lod": nilIfEmpty(c.Lod), "rates": rates,
		"sensor_modes": modes, "dpi_button": c.DPIButton, "debounce_off": c.DebounceOff,
		"stage_count_editable": c.StageCountEditable, "backup": c.Backup}).(map[string]any)
}

func actionAsPython(a Action) map[string]any {
	m := map[string]any{"kind": a.Kind}
	switch a.Kind {
	case "mouse":
		m["btn"] = a.Btn
	case "dpi":
		m["op"] = a.Op
	case "scroll":
		m["dir"] = a.Dir
	}
	if a.Raw != nil {
		m["raw"] = a.Raw
	}
	return normalize(m).(map[string]any)
}

func normalize(v any) any {
	b, _ := json.Marshal(v)
	var out any
	json.Unmarshal(b, &out)
	return out
}

func TestCodecsMatchPython(t *testing.T) {
	g := loadGolden(t)
	for name, rows := range golden[map[string][][]int](t, g, "enc") {
		for _, r := range rows {
			b, n := codecs[name].Enc(r[0])
			if int(b) != (r[1]&0xFF) || n != r[2] {
				t.Fatalf("enc %s(%d) = %d,%d want %d,%d", name, r[0], b, n, r[1], r[2])
			}
		}
	}
	for name, rows := range golden[map[string][][]int](t, g, "dec") {
		for _, r := range rows {
			if got := codecs[name].Dec(byte(r[0]), r[1]); got != r[2] {
				t.Fatalf("dec %s(%d,%d) = %d want %d", name, r[0], r[1], got, r[2])
			}
		}
	}
	for _, r := range golden[[][]int](t, g, "enc_m") {
		if v, d := EncM(r[0]); v != r[1] || d != r[2] {
			t.Fatalf("enc_m(%d) = %d,%d want %d,%d", r[0], v, d, r[1], r[2])
		}
	}
	for _, r := range golden[[][]int](t, g, "dec_m") {
		if got := DecM(r[0], r[1]); got != r[2] {
			t.Fatalf("dec_m(%d,%d) = %d want %d", r[0], r[1], got, r[2])
		}
	}
}

func TestLodMatchesPython(t *testing.T) {
	g := loadGolden(t)
	for _, r := range golden[[][]float64](t, g, "lod_mm") {
		if got := LodMMToCode(r[0]); got != int(r[1]) {
			t.Fatalf("lod_mm_to_code(%v) = %d want %v", r[0], got, r[1])
		}
	}
	for _, r := range golden[[][]float64](t, g, "lod_code") {
		if got := LodCodeToMM(int(r[0])); got != r[1] {
			t.Fatalf("lod_code_to_mm(%v) = %v want %v", r[0], got, r[1])
		}
	}
}

func TestCapsAndSnapMatchPython(t *testing.T) {
	g := loadGolden(t)
	type capCase struct {
		Args []any          `json:"args"`
		Caps map[string]any `json:"caps"`
	}
	for _, c := range golden[[]capCase](t, g, "caps") {
		family := c.Args[0].(string)
		sensor, _ := c.Args[1].(string)
		var flags []string
		for _, f := range c.Args[2].([]any) {
			flags = append(flags, f.(string))
		}
		maxRate, cmax, mode := int(c.Args[3].(float64)), c.Args[4].(bool), int(c.Args[5].(float64))
		rates, modes := CxRates, CxSensorModes
		if family == "bitmouse" {
			rates, modes = BmRates, BmSensorModes
		}
		got := capsAsPython(Capabilities(family, sensor, flags, maxRate, rates, cmax, mode, modes))
		if !reflect.DeepEqual(got, normalize(c.Caps)) {
			t.Fatalf("caps %v:\n got  %v\n want %v", c.Args, got, c.Caps)
		}
	}
	for _, r := range golden[[][]any](t, g, "snap") {
		sensor, _ := r[1].(string)
		c := Capabilities(r[0].(string), sensor, nil, 1000, CxRates, false, 0, CxSensorModes)
		if got := SnapDPI(int(r[2].(float64)), &c); got != int(r[3].(float64)) {
			t.Fatalf("snap %v = %d", r, got)
		}
	}
}

func TestFramesMatchPython(t *testing.T) {
	g := loadGolden(t)
	for _, r := range golden[[][]any](t, g, "cx_pack") {
		var p []byte
		for _, v := range r[3].([]any) {
			p = append(p, byte(v.(float64)))
		}
		got := CxPack(byte(r[0].(float64)), int(r[1].(float64)), int(r[2].(float64)), p)
		if !sameBytes(got, r[4]) {
			t.Fatalf("cx_pack %v = %v", r[:4], got)
		}
	}
	for _, r := range golden[[][]any](t, g, "bm_pack") {
		var p []int
		for _, v := range r[3].([]any) {
			p = append(p, int(v.(float64)))
		}
		got := BmPack(byte(r[0].(float64)), int(r[1].(float64)), int(r[2].(float64)), p, int(r[4].(float64)))
		if !sameBytes(got, r[5]) {
			t.Fatalf("bm_pack %v = %v", r[:5], got)
		}
	}
}

// The BITMOUSE receiver light shares its number (0x85) with an upgrade command
// that uses another frame layout. Pin ATK HUB's settings layout byte by byte:
// [checksum, 0x72, paramLen, 0, target, cmd, cmdLen, payload...].
func TestReceiverLightFrameLayout(t *testing.T) {
	f := BmPack(bmSetRxLight, 11, 9, []int{1, 3, 100, 1, 0, 255, 0, 0, 0}, 0)
	want := []byte{0x72, 11, 0, 0, 0x85, 9, 1, 3, 100, 1, 0, 255, 0, 0, 0}
	if len(f) != 63 || string(f[1:16]) != string(want) {
		t.Fatalf("frame % x", f[:16])
	}
	m := NewBitMouse(&fakeTransport{handle: func([]byte) []byte { return nil }}, 0x373B, 0x12CF, testRegistry())
	if _, err := m.xfer(bmSetRxLight, 3, 1, 0); err == nil {
		t.Fatal("0x85 was allowed in a form ATK HUB never sends")
	}
}

func TestLightTablesMatchPython(t *testing.T) {
	var py struct {
		CxModes        map[string]int `json:"cx_modes"`
		CxBrightness   [3]int         `json:"cx_brightness"`
		CxSpeed        [3]int         `json:"cx_speed"`
		CxRxSpeed      [3]int         `json:"cx_rx_speed"`
		CxRxBrightness [3]int         `json:"cx_rx_brightness"`
		CxRxSleep      [3]int         `json:"cx_rx_sleep"`
		BmModes        map[string]int `json:"bm_modes"`
		BmBrightness   [3]int         `json:"bm_brightness"`
		BmSpeed        [3]int         `json:"bm_speed"`
		BmRxBrightness [3]int         `json:"bm_rx_brightness"`
		RxFull         [][2]any       `json:"rx_full"`
		RxSimple       [][2]any       `json:"rx_simple"`
		RxNoSpeed      []int          `json:"rx_no_speed"`
		RxNoBrightness []int          `json:"rx_no_brightness"`
		RxColor        []int          `json:"rx_color"`
	}
	json.Unmarshal(loadGolden(t)["light_tables"], &py)
	same := func(name string, a, b any) {
		if fmt.Sprint(a) != fmt.Sprint(b) {
			t.Fatalf("%s: Go %v, Python %v", name, a, b)
		}
	}
	same("cx modes", cxLightModes, py.CxModes)
	same("cx brightness", cxLightBrightness, py.CxBrightness)
	same("cx speed", cxLightSpeed, py.CxSpeed)
	same("cx rx speed", cxRxSpeed, py.CxRxSpeed)
	same("cx rx brightness", cxRxBrightness, py.CxRxBrightness)
	same("cx rx sleep", cxRxSleep, py.CxRxSleep)
	same("bm modes", bmLightModes, py.BmModes)
	same("bm brightness", bmLightBrightness, py.BmBrightness)
	same("bm speed", bmLightSpeed, py.BmSpeed)
	same("bm rx brightness", bmRxBrightness, py.BmRxBrightness)
	modes := func(ms []LightMode) (out []string) {
		for _, m := range ms {
			out = append(out, fmt.Sprint(m.Value, m.Label))
		}
		return
	}
	pyModes := func(ms [][2]any) (out []string) {
		for _, m := range ms {
			out = append(out, fmt.Sprint(m[0], m[1]))
		}
		return
	}
	same("rx full modes", modes(RxFullModes), pyModes(py.RxFull))
	same("rx simple modes", modes(RxSimpleModes), pyModes(py.RxSimple))
	keys := func(m map[int]bool) (out []int) {
		for k := range m {
			out = append(out, k)
		}
		sort.Ints(out)
		return
	}
	same("rx no speed", keys(RxNoSpeed), py.RxNoSpeed)
	same("rx no brightness", keys(RxNoBrightness), py.RxNoBrightness)
	same("rx colour", keys(RxColor), py.RxColor)
}

func sameBytes(got []byte, want any) bool {
	w := want.([]any)
	if len(w) != len(got) {
		return false
	}
	for i := range got {
		if int(got[i]) != int(w[i].(float64)) {
			return false
		}
	}
	return true
}

func TestKeysMatchPython(t *testing.T) {
	g := loadGolden(t)
	for _, r := range golden[[][]any](t, g, "cx_decode") {
		got := actionAsPython(CxDecodeKey(int(r[0].(float64)), int(r[1].(float64)), int(r[2].(float64))))
		if !reflect.DeepEqual(got, normalize(r[3])) {
			t.Fatalf("cx_decode %v = %v", r, got)
		}
	}
	for _, r := range golden[[][]any](t, g, "bm_decode") {
		got := actionAsPython(BmDecodeKey(int(r[0].(float64)), int(r[1].(float64)), 0, int(r[2].(float64)), 0, 0))
		if !reflect.DeepEqual(got, normalize(r[3])) {
			t.Fatalf("bm_decode %v = %v", r, got)
		}
	}
	for name, enc := range map[string]func(Action) ([]int, error){"cx_encode": CxEncodeKey, "bm_encode": BmEncodeKey} {
		for _, r := range golden[[][]any](t, g, name) {
			m := r[0].(map[string]any)
			a := Action{Kind: m["kind"].(string)}
			if b, ok := m["btn"].(float64); ok {
				a.Btn = int(b)
			}
			a.Op, _ = m["op"].(string)
			a.Dir, _ = m["dir"].(string)
			got, err := enc(a)
			if err != nil || !reflect.DeepEqual(normalize(got), normalize(r[1])) {
				t.Fatalf("%s %v = %v %v", name, m, got, err)
			}
		}
	}
}

func TestLinkMaxRateMatchesPython(t *testing.T) {
	want := golden[map[string]int](t, loadGolden(t), "link_max_rate")
	if len(want) != len(LinkMaxRate) {
		t.Fatalf("%d link types, Python has %d", len(LinkMaxRate), len(want))
	}
	for k, hz := range want {
		n, _ := strconv.Atoi(k)
		if LinkMaxRate[n] != hz {
			t.Fatalf("link %s: %d Hz, Python says %d", k, LinkMaxRate[n], hz)
		}
	}
}

func TestCompetitiveMaxMatchesPython(t *testing.T) {
	g := loadGolden(t)
	table := golden[map[string]string](t, g, "cmax_table")
	for _, r := range golden[[][]any](t, g, "cmax") {
		if got := CompetitiveMaxAllowed(r[0].(string), r[1].(string), table); got != r[2].(bool) {
			t.Fatalf("cmax %v = %v", r, got)
		}
	}
}
