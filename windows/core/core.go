// SPDX-License-Identifier: MIT
// Protocol tables and DPI codecs ported from OpenATK, Copyright (c) 2026 Sunnybee.

// Package core is the Go port of atk.py: the COMPX and BITMOUSE protocols,
// the per-sensor DPI codecs and capabilities, and the ATK HUB device list.
// It has no platform code; a Transport carries the HID reports.
// Keep it in step with atk.py (see PARITY.md).
package core

import (
	"errors"
	"fmt"
	"math"
	"sort"
	"time"
)

const ReportID = 8

// VIDs: 0x373B ATK / VXE, 0x3554 first-generation VXE R1 family.
var VIDs = []uint16{0x373B, 0x3554}

// Transport moves vendor reports (report ID first) to and from the mouse.
type Transport interface {
	Write(report []byte) error
	// Read returns the next input report, or nil when the timeout passes.
	Read(timeout time.Duration) ([]byte, error)
	Flush()
}

var (
	ErrAsleep      = errors.New("Mouse did not answer. Move it to wake it up.")
	ErrUnsupported = errors.New("not supported on this mouse")
)

// DeviceError is any error the mouse reported or a check that failed.
type DeviceError struct{ Msg string }

func (e *DeviceError) Error() string { return e.Msg }

func devErr(format string, a ...any) error { return &DeviceError{fmt.Sprintf(format, a...)} }

// exchange sends one frame and returns the first reply match() accepts.
func exchange(t Transport, frame []byte, match func(r []byte) ([]byte, error)) ([]byte, error) {
	for attempt := 0; attempt < 3; attempt++ {
		t.Flush()
		if err := t.Write(append([]byte{ReportID}, frame...)); err != nil {
			return nil, err
		}
		deadline := time.Now().Add(600 * time.Millisecond)
		for {
			left := time.Until(deadline)
			if left <= 0 {
				break
			}
			raw, err := t.Read(left)
			if err != nil {
				return nil, err
			}
			if raw == nil {
				break
			}
			if len(raw) == 0 || raw[0] != ReportID {
				continue // mouse motion, media keys, other reports
			}
			reply, err := match(raw[1:])
			if err != nil {
				return nil, err
			}
			if reply != nil {
				return reply, nil
			}
		}
	}
	return nil, ErrAsleep
}

func inv(v int) byte    { return byte((85 - v) & 0xFF) }
func pair(v int) []byte { return []byte{byte(v), inv(v)} }

// ---- sensors ------------------------------------------------------------------

// Segment: values up to Until go in steps of Step.
type Segment struct{ Until, Step int }

type sensorInfo struct {
	Min      int
	Seg      []Segment
	Codec    string
	CompxMax int // 0: no extra COMPX limit
}

// DPI range per sensor as ATK HUB offers it; Codec is how COMPX mice store it.
// The table sensors stop at 10000 on COMPX: ATK HUB can't store more for them.
var sensors = map[string]sensorInfo{
	"PAW3395":       {100, []Segment{{30000, 50}}, "B", 0},
	"PAW3395Ultra":  {100, []Segment{{30000, 50}}, "B", 0},
	"PAW3950":       {50, []Segment{{30000, 50}, {36000, 100}}, "B", 0},
	"PAW3950DM":     {50, []Segment{{30000, 50}, {36000, 100}}, "B", 0},
	"PAW3950Ultra":  {10, []Segment{{10000, 10}, {30000, 50}, {42000, 100}}, "A", 0},
	"PAW3955MASTER": {1, []Segment{{40000, 1}, {50000, 2}}, "M", 0},
	"CORE26K":       {50, []Segment{{10000, 50}, {26000, 100}}, "C", 0},
	"PAW3395SE":     {200, []Segment{{18000, 50}}, "SE", 10000},
	"PAW3311":       {200, []Segment{{15000, 100}}, "SE", 10000},
	"PAW3315":       {200, []Segment{{16000, 100}}, "T15", 10000},
}

var lodLevelsSensors = map[string]bool{"PAW3395SE": true, "CORE26K": true, "PAW3395": true,
	"PAW3395Ultra": true, "PAW3950": true, "PAW3950DM": true}
var lodContinuousSensors = map[string]bool{"PAW3950Ultra": true, "PAW3955MASTER": true}

// SnapDPI returns the nearest DPI the mouse can store, clamped to its range.
func SnapDPI(dpi int, c *Caps) int {
	if dpi < c.MinDPI {
		dpi = c.MinDPI
	}
	if dpi > c.MaxDPI {
		dpi = c.MaxDPI
	}
	start := c.MinDPI
	for _, s := range c.DPISegments {
		if dpi <= s.Until {
			v := start + pyRound(float64(dpi-start)/float64(s.Step))*s.Step
			return min(v, s.Until, c.MaxDPI)
		}
		start = s.Until
	}
	return c.MaxDPI
}

// pyRound rounds half to even, like Python's round().
func pyRound(x float64) int { return int(math.RoundToEven(x)) }

func LodMMToCode(mm float64) int { return min(11, max(1, pyRound(mm*10)-6)) }
func LodCodeToMM(code int) float64 {
	return float64(code+6) / 10
}

// ---- COMPX DPI codecs ---------------------------------------------------------
// One byte per axis plus a nibble: bits 2-3 extend the byte, bit 1 selects a
// coarser range, bit 0 doubles the value.

type codec struct {
	Enc func(dpi int) (byte, int)
	Dec func(b byte, nib int) int
}

func encB(dpi int) (byte, int) {
	doubled := 0
	step := 50.0
	if dpi > 30000 {
		doubled, step = 1, 100
	}
	code := pyRound(float64(dpi)/step) - 1
	return byte(code), (code>>8)<<2 | doubled
}

func decB(b byte, nib int) int {
	code := (nib>>2&3)<<8 | int(b)
	v := 50 * (code + 1)
	if nib&1 != 0 {
		v *= 2
	}
	return v
}

func encA(dpi int) (byte, int) {
	v, doubled := dpi, 0
	if v > 30000 {
		doubled, v = 1, pyRound(float64(v)/2)
	}
	if v <= 10000 {
		c := v/10 - 1
		return byte(c), (c>>8)<<2 | doubled
	}
	c := floorDiv(v-10050, 50)
	return byte(c), (c>>8)<<2 | 2 | doubled
}

func decA(b byte, nib int) int {
	c := (nib>>2&3)<<8 | int(b)
	base := (c + 1) * 10
	if nib&2 != 0 {
		base = 10050 + c*50
	}
	if nib&1 != 0 {
		return base * 2
	}
	return base
}

func encC(dpi int) (byte, int) {
	code := pyRound(float64(dpi)/50) - 1
	return byte(code), (code >> 8) << 2
}

func decC(b byte, nib int) int {
	code := (nib>>2&3)<<8 | int(b)
	step := 50
	if nib&2 != 0 {
		step = 100
	}
	v := (code + 1) * step
	if nib&1 != 0 {
		v *= 2
	}
	return v
}

func floorDiv(a, b int) int {
	q := a / b
	if (a%b != 0) && ((a < 0) != (b < 0)) {
		q--
	}
	return q
}

// scaleTable: PAW3395SE / PAW3311 and PAW3315 store one byte per 50 DPI from a
// sensor scale. These formulas reproduce ATK HUB's lookup tables exactly,
// including their hand-tuned entries.
type scaleTable struct {
	code map[int]int
	dpi  map[int]int
}

func newScale(codeOf func(d int) int, overrides map[int]int) scaleTable {
	t := scaleTable{code: map[int]int{}, dpi: map[int]int{}}
	for d := 50; d <= 10000; d += 50 {
		t.code[d] = codeOf(d)
	}
	for d, c := range overrides {
		t.code[d] = c
	}
	// highest DPI first, so a code shared by two DPIs maps to the lower one (as atk.py)
	keys := make([]int, 0, len(t.code))
	for d := range t.code {
		keys = append(keys, d)
	}
	sort.Sort(sort.Reverse(sort.IntSlice(keys)))
	for _, d := range keys {
		t.dpi[t.code[d]] = d
	}
	return t
}

var seTable = newScale(func(d int) int { return int(math.Ceil(float64(d)/42.5)) - 1 }, map[int]int{300: 6})
var t15Table = newScale(func(d int) int { return int(math.Floor(float64(d) / 41.982)) }, map[int]int{300: 6, 550: 12})

func encScaled(t scaleTable) func(int) (byte, int) {
	return func(dpi int) (byte, int) {
		d := min(10000, max(50, pyRound(float64(dpi)/50)*50))
		return byte(t.code[d]), 0
	}
}

func decScaled(t scaleTable, fallback int) func(byte, int) int {
	return func(b byte, nib int) int {
		d, ok := t.dpi[int(b)]
		if !ok {
			return fallback // blank slot: a default, like ATK HUB
		}
		if nib&2 != 0 {
			return d * 2
		}
		return d
	}
}

// PAW3955: separate 6-byte rows; DPI-1, and from 40000 up half of it plus a flag.
func EncM(dpi int) (int, int) {
	if dpi < 40000 {
		return max(1, dpi) - 1, 0
	}
	return min((dpi-dpi%2)/2-1, 0xFFFF), 1
}

func DecM(value, doubled int) int {
	if doubled != 0 {
		return (value + 1) * 2
	}
	return value + 1
}

var codecs = map[string]codec{
	"A":   {encA, decA},
	"B":   {encB, decB},
	"C":   {encC, decC},
	"SE":  {encScaled(seTable), decScaled(seTable, 10000)},
	"T15": {encScaled(t15Table), decScaled(t15Table, 10000)},
}

// ---- capabilities -------------------------------------------------------------

type ModeOption struct {
	Value int
	Label string
}

type Caps struct {
	Family             string
	Sensor             string
	DPICodec           string
	MinDPI, MaxDPI     int
	DPISegments        []Segment
	DPIStep            int
	Lod                string // "levels", "levels07", "continuous" or ""
	Rates              []int
	SensorModes        []ModeOption
	DPIButton          bool
	DebounceOff        bool
	StageCountEditable bool
	Backup             bool
}

// Capabilities: what the app may offer for this mouse, following ATK HUB's rules.
func Capabilities(family, sensor string, flags []string, maxRate int, rates map[int]int,
	competitiveMax bool, sensorMode int, modes [3]int) Caps {
	c := Caps{Family: family, Sensor: sensor, MinDPI: 50, MaxDPI: 26000,
		DPISegments: []Segment{{26000, 50}}, DPIStep: 50}
	if info, ok := sensors[sensor]; ok {
		top := info.Seg[len(info.Seg)-1].Until
		if family == "compx" {
			if info.CompxMax != 0 {
				top = min(top, info.CompxMax)
			}
			c.DPICodec = info.Codec
		}
		c.MinDPI, c.MaxDPI, c.DPISegments, c.DPIStep = info.Min, top, info.Seg, info.Seg[0].Step
	}
	switch {
	case lodContinuousSensors[sensor]:
		c.Lod = "continuous"
	case lodLevelsSensors[sensor]:
		c.Lod = "levels"
		if family == "compx" && (sensor == "PAW3950" || sensor == "PAW3950DM") {
			c.Lod = "levels07"
		}
	}
	c.SensorModes = []ModeOption{{modes[0], "Basic"}, {modes[1], "Competitive"}}
	if competitiveMax || sensorMode == modes[2] {
		c.SensorModes = append(c.SensorModes, ModeOption{modes[2], "Competitive Max"})
	}
	for hz := range rates {
		if hz <= maxRate {
			c.Rates = append(c.Rates, hz)
		}
	}
	sort.Ints(c.Rates)
	c.DPIButton = !contains(flags, "noBottomButton")
	c.DebounceOff = !contains(flags, "noZeroKeyDebounce")
	c.StageCountEditable = family == "compx"
	c.Backup = family == "compx"
	return c
}

func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

// ---- state shared by both protocols ------------------------------------------

// Action is a button mapping, as in atk.py: kind plus btn / op / dir.
type Action struct {
	Kind string
	Btn  int
	Op   string
	Dir  string
	Raw  []int
}

type Ident struct {
	Key      string // "cid,mid", or "" when the mouse didn't say
	Name     string
	Sensor   string
	Flags    []string
	Receiver string // receiver name when connected through one
	// ReceiverFlags: what kind of light the receiver has (singleRgb, gatherRgb, noDongleRGB)
	ReceiverFlags []string
}

type Battery struct {
	Level    int
	Charging bool
}

type State struct {
	ID          Ident
	Versions    map[string]string
	Battery     *Battery
	RateHz      int
	SensorMode  int
	Caps        Caps
	StageCount  int
	ActiveStage int
	Stages      []int // nil when the DPI format isn't known
	LodCode     int
	DebounceMs  int
	MotionSync  bool
	AngleSnap   bool
	Ripple      bool
	LongRange   *bool          // nil where ATK HUB doesn't offer long range mode
	Light       *Light         // nil without a DPI light
	StageColors []string       // "#rrggbb" for each of the 8 stages; nil without a light
	RxLight     *ReceiverLight // nil when the receiver has no light we can drive
	SleepS      int
	Buttons     []Action
}

// Mouse is what the UI drives; CompxMouse and BitMouse implement it.
type Mouse interface {
	Family() string
	IsReceiver() bool
	Wake(tries int) error
	ReadState() (*State, error)
	SetRate(hz int) error
	SetLod(code int) error
	SetDebounce(ms int) error
	SetMotionSync(on bool) error
	SetAngleSnap(on bool) error
	SetRipple(on bool) error
	SetLongRange(on bool) error
	SetLight(l Light) error
	SetStageColor(i int, color string) error
	SetReceiverLight(l ReceiverLight) error
	SetSleep(seconds int) error
	SetSensorMode(mode int) error
	SetButton(i int, a Action) error
	SetStageDPI(i, dpi int) (int, error)
	SetActiveStage(i int) error
	SetStageCount(n int) error
	FactoryReset() error
	Backup() (map[string]any, error)
	Restore(dump map[string]any) ([]string, error)
}

var ButtonNames = []string{"Left", "Right", "Middle", "Back", "Forward", "DPI button"}
