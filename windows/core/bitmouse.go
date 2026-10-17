// SPDX-License-Identifier: MIT
// Ported from atk.py (BitMouse); keep the two in step.

package core

import (
	"fmt"
	"strings"
	"time"
)

// BITMOUSE: 63-byte frames, one command per setting.
//
//	0 checksum = sum(bytes 1..62)  1 0x72  2 paramLen  3 sequence  4 target
//	5 commandId  6 cmdLen  7+ payload        (replies drop byte 0)
const (
	bmSetRate        = 0x01
	bmSetDPI         = 0x02
	bmSetLod         = 0x03
	bmSetButton      = 0x05
	bmBattery        = 0x07
	bmGetConfig      = 0x09
	bmSetLinearCorr  = 0x0B
	bmSetRipple      = 0x0C
	bmSetMotionSync  = 0x0D
	bmCharging       = 0x0F
	bmReset          = 0x13
	bmSetSleep       = 0x15
	bmSetDebounce    = 0x16
	bmGetAddress     = 0x17
	bmMouseVersion   = 0x1C
	bmGetLinkType    = 0x1A
	bmSetLongRange   = 0x1B
	bmSetSensorMode  = 0x1F
	bmSetLight       = 0x14
	bmCidMid         = 0x4A
	bmSetRxLight     = 0x85 // same number as an upgrade command in another frame format
	bmGetRxLight     = 0x86
	bmDongleVersion  = 0x88
	bmAddrSensorMode = 74
	bmAddrLongRange  = 75
	bmAddrLight      = 76 // [mode, brightness, speed]
	bmAddrDPI        = 1  // 70 bytes: [active, count, 8 x (x, y, B, G, R, pad)]
	bmAddrButtons    = 82 // 5 bytes per button
)

// Pairing, address-space writes and the upgrade commands are never sent.
var bmAllowed = map[byte]bool{bmSetRate: true, bmSetDPI: true, bmSetLod: true, bmSetButton: true,
	bmBattery: true, bmGetConfig: true, bmSetLinearCorr: true, bmSetRipple: true, bmSetMotionSync: true,
	bmCharging: true, bmReset: true, bmSetSleep: true, bmSetDebounce: true, bmGetAddress: true,
	bmMouseVersion: true, bmSetSensorMode: true, bmCidMid: true, bmDongleVersion: true,
	bmGetLinkType: true, bmSetLongRange: true, bmSetLight: true, bmSetRxLight: true, bmGetRxLight: true}

var BmRates = map[int]int{1000: 0, 500: 1, 250: 2, 125: 3, 8000: 4, 4000: 5, 2000: 6}
var BmSensorModes = [3]int{0, 4, 5} // Basic, Competitive (Game), Competitive Max

func bmRateHz(v int) int {
	for hz, code := range BmRates {
		if code == v {
			return hz
		}
	}
	return 0
}

var bmSimpleKeys = map[int]Action{10: {Kind: "dpi", Op: "cycle"}, 11: {Kind: "dpi", Op: "up"},
	12: {Kind: "dpi", Op: "down"}, 13: {Kind: "scroll", Dir: "up"}, 14: {Kind: "scroll", Dir: "down"},
	15: {Kind: "scroll", Dir: "left"}, 16: {Kind: "scroll", Dir: "right"}, 255: {Kind: "disable"},
	4: {Kind: "macro"}}

func BmDecodeKey(i, c, mod, value, k0, k1 int) Action {
	switch {
	case c == 0:
		if i < 5 {
			return Action{Kind: "mouse", Btn: i}
		}
		return Action{Kind: "dpi", Op: "cycle"}
	case c == 1:
		if value <= 4 {
			return Action{Kind: "mouse", Btn: value}
		}
		return Action{Kind: "unknown"}
	case c == 2 || c == 3:
		return Action{Kind: "shortcut"}
	}
	if a, ok := bmSimpleKeys[c]; ok {
		return a
	}
	return Action{Kind: "unknown", Raw: []int{c, mod, value, k0, k1}}
}

func BmEncodeKey(a Action) ([]int, error) {
	switch a.Kind {
	case "mouse":
		return []int{1, 0, a.Btn, 0}, nil
	case "disable":
		return []int{255, 0, 0, 0}, nil
	case "dpi":
		return []int{map[string]int{"cycle": 10, "up": 11, "down": 12}[a.Op], 0, 0, 0}, nil
	case "scroll":
		return []int{map[string]int{"up": 13, "down": 14, "left": 15, "right": 16}[a.Dir], 0, 0, 0}, nil
	}
	return nil, devErr("unsupported action %v", a)
}

// BmPack builds a 63-byte BITMOUSE frame (without the report ID).
func BmPack(cmd byte, paramLen, cmdLen int, payload []int, target int) []byte {
	b := make([]byte, 63)
	b[1], b[2], b[3], b[4], b[5], b[6] = 0x72, byte(paramLen), 0, byte(target), cmd, byte(cmdLen)
	for i, v := range payload {
		b[7+i] = byte(v)
	}
	sum := 0
	for _, v := range b[1:] {
		sum += int(v)
	}
	b[0] = byte(sum)
	return b
}

type BitMouse struct {
	T        Transport
	VID, PID uint16
	Receiver bool
	Reg      *Registry
	target   int
	probed   bool
	// rxLightRaw: the receiver light reply, so a write keeps the bytes it doesn't change
	rxLightRaw []int
}

// NewBitMouse: ATK HUB addresses the mouse as target 1 through a receiver and 0
// over a cable. For USB IDs missing from its table that's a guess, so probe.
func NewBitMouse(t Transport, vid, pid uint16, reg *Registry) *BitMouse {
	m := &BitMouse{T: t, VID: vid, PID: pid, Reg: reg, Receiver: reg.IsReceiver(vid, pid)}
	if m.Receiver {
		m.target = 1
	}
	_, known := reg.Bitmouse.Pid[usbKey(vid, pid)]
	m.probed = m.Receiver || known
	return m
}

func (m *BitMouse) Family() string   { return "bitmouse" }
func (m *BitMouse) IsReceiver() bool { return m.Receiver }

func (m *BitMouse) xferTo(cmd byte, paramLen, cmdLen int, payload []int, target int) ([]int, error) {
	if !bmAllowed[cmd] {
		return nil, devErr("command 0x%02x is not allowed", cmd)
	}
	if cmd == bmCidMid && (len(payload) == 0 || payload[0] != 0) {
		return nil, devErr("only reading the mouse ID is allowed")
	}
	if (cmd == bmSetRxLight || cmd == bmGetRxLight) && (paramLen != 11 || cmdLen != 9) {
		return nil, devErr("receiver light commands go only in ATK HUB's own form")
	}
	if cmd == bmDongleVersion || cmd == bmGetLinkType || cmd == bmSetRxLight || cmd == bmGetRxLight {
		target = 0 // answered by the receiver itself
	}
	r, err := exchange(m.T, BmPack(cmd, paramLen, cmdLen, payload, target), func(raw []byte) ([]byte, error) {
		// replies drop the checksum byte: [0x72, status, seq, target, cmd, len, payload...]
		if len(raw) < 7 || raw[0] == 0x73 || raw[4] != cmd {
			return nil, nil
		}
		if raw[1] == 0xFF {
			return nil, devErr("device returned an error for 0x%02x", cmd)
		}
		return raw[6:], nil
	})
	if err != nil {
		return nil, err
	}
	out := make([]int, len(r))
	for i, v := range r {
		out[i] = int(v)
	}
	return out, nil
}

func (m *BitMouse) xfer(cmd byte, paramLen, cmdLen int, payload ...int) ([]int, error) {
	return m.xferTo(cmd, paramLen, cmdLen, payload, m.target)
}

func (m *BitMouse) config() ([]int, error) { return m.xfer(bmGetConfig, 20, 18) }

// LinkType: the link the mouse reports (see LinkMaxRate), or -1.
func (m *BitMouse) LinkType() int {
	r, err := m.xfer(bmGetLinkType, 2, 1)
	if err != nil || len(r) == 0 {
		return -1
	}
	return r[0]
}

func (m *BitMouse) readAddr(addr, length int) ([]int, error) {
	p, err := m.xfer(bmGetAddress, 10, 13, addr&0xFF, addr>>8, length)
	if err != nil {
		return nil, err
	}
	if len(p) < 3+length {
		return nil, devErr("short address reply")
	}
	return p[3 : 3+length], nil
}

func (m *BitMouse) probe() error {
	if m.probed {
		return nil
	}
	for _, t := range []int{m.target, m.target ^ 1} {
		if _, err := m.xferTo(bmGetConfig, 20, 18, nil, t); err == nil {
			m.target, m.probed = t, true
			return nil
		} else if err != ErrAsleep {
			return err
		}
	}
	return ErrAsleep
}

func (m *BitMouse) Wake(tries int) error {
	for i := 0; i < tries; i++ {
		var err error
		if m.probed {
			_, err = m.config()
		} else {
			err = m.probe()
		}
		if err == nil {
			return nil
		}
		if err != ErrAsleep {
			return err
		}
		time.Sleep(300 * time.Millisecond)
	}
	return ErrAsleep
}

func (m *BitMouse) identify() Ident {
	key := ""
	if p, err := m.xfer(bmCidMid, 8, 6, 0); err == nil && len(p) >= 6 {
		key = fmt.Sprintf("%d,%d", p[1], p[2]|p[3]<<8|p[4]<<16|p[5]<<24)
	}
	return m.Reg.Lookup("bitmouse", key, m.VID, m.PID)
}

func joinVersion(p []int) string {
	parts := make([]string, 0, 3)
	for _, v := range p[:min(3, len(p))] {
		parts = append(parts, fmt.Sprint(v))
	}
	return strings.Join(parts, ".")
}

func (m *BitMouse) versions() map[string]string {
	out := map[string]string{}
	if p, err := m.xfer(bmMouseVersion, 5, 3); err == nil {
		out["mouse"] = joinVersion(p)
	}
	if m.Receiver {
		if p, err := m.xfer(bmDongleVersion, 5, 3); err == nil {
			out["dongle"] = joinVersion(p)
		}
	}
	return out
}

type bmStage struct {
	X, Y int
	RGB  [3]int
}

type bmDPITable struct {
	Active, Count int
	Stages        [8]bmStage
}

func (m *BitMouse) readDPITable() (*bmDPITable, error) {
	var raw []int
	for n := 0; n < 7; n++ {
		p, err := m.readAddr(bmAddrDPI+10*n, 10)
		if err != nil {
			return nil, err
		}
		raw = append(raw, p...)
	}
	t := &bmDPITable{Active: raw[0], Count: raw[1]}
	for i := range t.Stages {
		o := 2 + 8*i
		t.Stages[i] = bmStage{raw[o] | raw[o+1]<<8, raw[o+2] | raw[o+3]<<8, [3]int{raw[o+6], raw[o+5], raw[o+4]}}
	}
	return t, nil
}

func (m *BitMouse) ReadState() (*State, error) {
	if err := m.Wake(10); err != nil {
		return nil, err
	}
	s := &State{ID: m.identify(), Versions: m.versions()}
	if p, err := m.xfer(bmBattery, 2, 1); err == nil {
		b := &Battery{Level: p[0]}
		if c, err := m.xfer(bmCharging, 2, 1); err == nil {
			b.Charging = c[0] == 1
		}
		s.Battery = b
	}
	c, err := m.config()
	if err != nil {
		return nil, err
	}
	s.RateHz = bmRateHz(c[2])
	mode, err := m.readAddr(bmAddrSensorMode, 1)
	if err != nil {
		return nil, err
	}
	s.SensorMode = mode[0]
	maxRate := max(MaxRate(m.LinkType()), s.RateHz)
	s.Caps = Capabilities("bitmouse", s.ID.Sensor, s.ID.Flags, maxRate, BmRates,
		!contains(s.ID.Flags, "noAthleticsMax") && s.ID.Sensor != "", s.SensorMode, BmSensorModes)
	t, err := m.readDPITable()
	if err != nil {
		return nil, err
	}
	// A stage exists while it holds a DPI; the reported count can overshoot.
	filled := 0
	for i, st := range t.Stages {
		if st.X != 0 {
			filled = i + 1
		}
	}
	count := filled
	if t.Count >= 1 && t.Count <= 8 {
		count = min(t.Count, filled)
	}
	s.StageCount = max(1, count)
	if t.Active < s.StageCount {
		s.ActiveStage = t.Active
	}
	for _, st := range t.Stages {
		if st.X != 0 {
			s.Stages = append(s.Stages, st.X)
		} else {
			s.Stages = append(s.Stages, 1600)
		}
	}
	s.LodCode = c[5] + 1 // stored as code - 1
	s.MotionSync, s.AngleSnap, s.Ripple = c[6] != 0, c[7] != 0, c[8] != 0
	s.SleepS, s.DebounceMs = c[9]|c[10]<<8, c[11]
	s.LongRange = m.longRange(s.ID.Flags)
	s.Light, s.StageColors = m.light(s.ID.Flags, t)
	s.RxLight = m.receiverLight(s.ID.ReceiverFlags)
	var keys []int
	for r := 0; r < 3; r++ {
		p, err := m.readAddr(bmAddrButtons+10*r, 10)
		if err != nil {
			return nil, err
		}
		keys = append(keys, p...)
	}
	for i := 0; i < 6; i++ {
		k := keys[5*i : 5*i+5]
		s.Buttons = append(s.Buttons, BmDecodeKey(i, k[0], k[1], k[2], k[3], k[4]))
	}
	return s, nil
}

// set sends a command and reads the setting back.
func (m *BitMouse) set(cmd byte, paramLen, cmdLen int, payload []int, check func() (bool, error)) error {
	if _, err := m.xferTo(cmd, paramLen, cmdLen, payload, m.target); err != nil {
		return err
	}
	ok, err := check()
	if err != nil {
		return err
	}
	if !ok {
		return devErr("setting 0x%02x did not stick", cmd)
	}
	return nil
}

func (m *BitMouse) configIs(index, want int) func() (bool, error) {
	return func() (bool, error) {
		c, err := m.config()
		if err != nil {
			return false, err
		}
		return c[index] == want, nil
	}
}

func (m *BitMouse) SetRate(hz int) error {
	return m.set(bmSetRate, 3, 1, []int{BmRates[hz]}, m.configIs(2, BmRates[hz]))
}
func (m *BitMouse) SetLod(code int) error {
	return m.set(bmSetLod, 4, 2, []int{0, code - 1}, m.configIs(5, code-1))
}
func (m *BitMouse) SetDebounce(ms int) error {
	return m.set(bmSetDebounce, 3, 1, []int{ms}, m.configIs(11, ms))
}
func (m *BitMouse) SetMotionSync(on bool) error {
	return m.set(bmSetMotionSync, 3, 1, []int{b2i(on)}, m.configIs(6, b2i(on)))
}
func (m *BitMouse) SetAngleSnap(on bool) error {
	return m.set(bmSetLinearCorr, 3, 1, []int{b2i(on)}, m.configIs(7, b2i(on)))
}

var (
	bmLightModes      = map[string]int{"off": 0, "steady": 2, "breathing": 1}
	bmLightBrightness = [3]int{85, 170, 255}
	bmLightSpeed      = [3]int{10, 15, 20}
	bmRxBrightness    = [3]int{10, 50, 100} // receiver speed is stored as the level itself
)

// light: the DPI light and the 8 stage colours, or nil without a light.
func (m *BitMouse) light(flags []string, t *bmDPITable) (*Light, []string) {
	if contains(flags, "noDpiRGB") {
		return nil, nil
	}
	raw, err := m.readAddr(bmAddrLight, 3)
	if err != nil {
		return nil, nil
	}
	l := &Light{Mode: "steady", Brightness: nearestLevel(raw[1], bmLightBrightness),
		Speed: nearestLevel(raw[2], bmLightSpeed)}
	for k, v := range bmLightModes {
		if v == raw[0] {
			l.Mode = k
		}
	}
	var colors []string
	for _, st := range t.Stages {
		colors = append(colors, rgbHex(st.RGB[0], st.RGB[1], st.RGB[2]))
	}
	return l, colors
}

func (m *BitMouse) SetLight(l Light) error {
	want := []int{bmLightModes[l.Mode], bmLightBrightness[l.Brightness], bmLightSpeed[l.Speed]}
	return m.set(bmSetLight, 6, 3, want, func() (bool, error) {
		v, err := m.readAddr(bmAddrLight, 3)
		return err == nil && v[0] == want[0] && v[1] == want[1] && v[2] == want[2], err
	})
}

func (m *BitMouse) SetStageColor(i int, color string) error {
	t, err := m.readDPITable()
	if err != nil {
		return err
	}
	if err := m.writeStage(i, t.Stages[i].X, hexRGB(color), i == t.Active); err != nil {
		return err
	}
	if t, err = m.readDPITable(); err != nil {
		return err
	}
	if c := t.Stages[i].RGB; rgbHex(c[0], c[1], c[2]) != color {
		return devErr("colour did not stick")
	}
	return nil
}

// receiverLight: the receiver's light, or nil if it has none we can drive.
func (m *BitMouse) receiverLight(flags []string) *ReceiverLight {
	kind := ""
	if m.Receiver {
		kind = receiverLightKind(flags)
	}
	if kind == "" {
		return nil
	}
	p, err := m.xfer(bmGetRxLight, 11, 9)
	if err != nil || len(p) < 9 {
		return nil
	}
	// [on, mode, brightness, speed, ?, R, G, B, sleep]; ATK HUB shows no sleep here
	m.rxLightRaw = append([]int(nil), p[:9]...)
	l := &ReceiverLight{Kind: kind, Mode: p[1], Sleep: -1}
	if kind == "full" {
		l.Color = rgbHex(p[5], p[6], p[7])
		l.Speed, l.Brightness = min(p[3], 2), nearestLevel(p[2], bmRxBrightness)
	}
	return l
}

func (m *BitMouse) SetReceiverLight(l ReceiverLight) error {
	raw := make([]int, 9)
	copy(raw, m.rxLightRaw)
	raw[0], raw[1] = 1, l.Mode
	flags := []string{"singleRgb"}
	if l.Kind == "full" {
		flags = nil
		c := hexRGB(l.Color)
		raw[2], raw[3], raw[5], raw[6], raw[7] = bmRxBrightness[l.Brightness], l.Speed, c[0], c[1], c[2]
	}
	if _, err := m.xfer(bmSetRxLight, 11, 9, raw...); err != nil {
		return err
	}
	if got := m.receiverLight(flags); got == nil || *got != l {
		return devErr("receiver light did not stick")
	}
	return nil
}

// longRange: long range mode on/off, or nil where ATK HUB doesn't offer it.
func (m *BitMouse) longRange(flags []string) *bool {
	if contains(flags, "noFarDistance") {
		return nil
	}
	v, err := m.readAddr(bmAddrLongRange, 1)
	if err != nil {
		return nil
	}
	on := v[0] != 0
	return &on
}

func (m *BitMouse) SetLongRange(on bool) error {
	return m.set(bmSetLongRange, 3, 1, []int{b2i(on)}, func() (bool, error) {
		v, err := m.readAddr(bmAddrLongRange, 1)
		return err == nil && v[0] == b2i(on), err
	})
}

func (m *BitMouse) SetRipple(on bool) error {
	return m.set(bmSetRipple, 3, 1, []int{b2i(on)}, m.configIs(8, b2i(on)))
}

func (m *BitMouse) SetSleep(seconds int) error {
	return m.set(bmSetSleep, 5, 2, []int{seconds & 0xFF, seconds >> 8}, func() (bool, error) {
		c, err := m.config()
		if err != nil {
			return false, err
		}
		return c[9]|c[10]<<8 == seconds, nil
	})
}

func (m *BitMouse) SetSensorMode(mode int) error {
	return m.set(bmSetSensorMode, 3, 1, []int{mode}, func() (bool, error) {
		v, err := m.readAddr(bmAddrSensorMode, 1)
		if err != nil {
			return false, err
		}
		return v[0] == mode, nil
	})
}

func (m *BitMouse) SetButton(i int, a Action) error {
	key, err := BmEncodeKey(a)
	if err != nil {
		return err
	}
	return m.set(bmSetButton, 8, 5, append([]int{i}, key...), func() (bool, error) {
		v, err := m.readAddr(bmAddrButtons+5*i, 4)
		if err != nil {
			return false, err
		}
		for j := range key {
			if v[j] != key[j] {
				return false, nil
			}
		}
		return true, nil
	})
}

// writeStage: one SetDpi frame carries a stage's DPI and colour; active selects it.
func (m *BitMouse) writeStage(i, dpi int, rgb [3]int, active bool) error {
	_, err := m.xfer(bmSetDPI, 12, 10, i, dpi&0xFF, dpi>>8, dpi&0xFF, dpi>>8, rgb[2], rgb[1], rgb[0], 0, b2i(active))
	return err
}

func (m *BitMouse) SetStageDPI(i, dpi int) (int, error) {
	t, err := m.readDPITable()
	if err != nil {
		return 0, err
	}
	if err := m.writeStage(i, dpi, t.Stages[i].RGB, i == t.Active); err != nil {
		return 0, err
	}
	t2, err := m.readDPITable()
	if err != nil {
		return 0, err
	}
	if t2.Stages[i].X != dpi {
		return 0, devErr("DPI did not stick")
	}
	return dpi, nil
}

func (m *BitMouse) SetActiveStage(i int) error {
	t, err := m.readDPITable()
	if err != nil {
		return err
	}
	if err := m.writeStage(i, t.Stages[i].X, t.Stages[i].RGB, true); err != nil {
		return err
	}
	t2, err := m.readDPITable()
	if err != nil {
		return err
	}
	if t2.Active != i {
		return devErr("stage change did not stick")
	}
	return nil
}

// SetStageCount: how ATK HUB changes the stage count on BITMOUSE isn't known.
func (m *BitMouse) SetStageCount(n int) error {
	return devErr("changing the number of stages isn't supported on this mouse")
}

func (m *BitMouse) FactoryReset() error {
	_, err := m.xfer(bmReset, 2, 1)
	return err
}

func (m *BitMouse) Backup() (map[string]any, error)          { return nil, ErrUnsupported }
func (m *BitMouse) Restore(map[string]any) ([]string, error) { return nil, ErrUnsupported }
