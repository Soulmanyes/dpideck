package core

import (
	"encoding/json"
	"os"
	"reflect"
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

func TestCompetitiveMaxMatchesPython(t *testing.T) {
	g := loadGolden(t)
	table := golden[map[string]string](t, g, "cmax_table")
	for _, r := range golden[[][]any](t, g, "cmax") {
		if got := CompetitiveMaxAllowed(r[0].(string), r[1].(string), table); got != r[2].(bool) {
			t.Fatalf("cmax %v = %v", r, got)
		}
	}
}
