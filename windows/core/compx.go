// SPDX-License-Identifier: MIT
// Ported from atk.py (CompxMouse); keep the two in step.

package core

import (
	"crypto/rand"
	"fmt"
	"strconv"
	"strings"
	"time"
)

// COMPX: 16-byte frames, settings stored in EEPROM, each value followed by 85-value.
const (
	cxBattery       = 0x04
	cxSetEEPROM     = 0x07
	cxGetEEPROM     = 0x08
	cxFactoryReset  = 0x09
	cxCidMid        = 0x10
	cxMouseInfo     = 0x01 // ATK HUB's "DownLoadData"; sent only as its info read
	cxMouseVersion  = 0x12
	cxSetRxSimple   = 0x14 // simple receiver light (singleRgb)
	cxGetRxSimple   = 0x15
	cxSetLongRange  = 0x16
	cxGetLongRange  = 0x17
	cxSetRxLight    = 0x18
	cxGetRxLight    = 0x19
	cxDongleVersion = 0x1D

	addrRateRow   = 0x00 // [rate,~, stages,~, active,~, bhop,~, btnMode,~]
	addrLod       = 0x0A
	addrDPI3955   = 0x1B00
	addrKey0      = 0x60
	addrFuncRow   = 0xA9 // [debounce,~, motionSync,~, sleep/10,~, lineCorr,~, ripple,~]
	addrLightRow  = 0x4C // [mode,~, brightness,~, speed,~, on,~]
	addrSensorRow = 0xB5 // [sleepOn,~, sleep/10,~, sensorMode,~]
	addrShortcut0 = 0x100
)

var addrDPIRows = []int{0x0C, 0x14, 0x1C, 0x24}
var addrDPIColorRows = []int{0x2C, 0x34, 0x3C, 0x44} // two stages per row: [R, G, B, ~] each

var (
	cxLightModes      = map[string]int{"steady": 1, "breathing": 2}
	cxLightBrightness = [3]int{16, 128, 255}
	cxLightSpeed      = [3]int{1, 3, 5}
	cxRxSpeed         = [3]int{0, 5, 9}
	cxRxBrightness    = [3]int{0, 5, 9}
	cxRxSleep         = [3]int{1, 5, 10} // minutes
)

// Anything else (firmware upgrade, pairing) is refused. 0x01 goes only
// through LinkType, in the exact form ATK HUB reads it.
var cxAllowed = map[byte]bool{cxBattery: true, cxSetEEPROM: true, cxGetEEPROM: true,
	cxFactoryReset: true, cxCidMid: true, cxMouseVersion: true, cxDongleVersion: true,
	cxSetLongRange: true, cxGetLongRange: true, cxSetRxLight: true, cxGetRxLight: true,
	cxSetRxSimple: true, cxGetRxSimple: true}

var CxRates = map[int]int{1000: 0x01, 500: 0x02, 250: 0x04, 125: 0x08, 2000: 0x10, 4000: 0x20, 8000: 0x40}

// Sensor modes as ATK HUB stores them for COMPX mice (not OpenATK's 0/4/5).
var CxSensorModes = [3]int{0, 1, 2}

func cxRateHz(v int) int {
	for hz, code := range CxRates {
		if code == v {
			return hz
		}
	}
	return 0
}

func CxDecodeKey(c, v1, v2 int) Action {
	masks := map[int]int{1: 0, 2: 1, 4: 2, 8: 3, 16: 4}
	switch {
	case c == 0 || (c == 1 && v1 == 0):
		return Action{Kind: "disable"}
	case c == 1:
		if b, ok := masks[v1]; ok {
			return Action{Kind: "mouse", Btn: b}
		}
	case c == 2 && v1 >= 1 && v1 <= 3:
		return Action{Kind: "dpi", Op: []string{"cycle", "up", "down"}[v1-1]}
	case c == 3 && (v1 == 1 || v1 == 2):
		return Action{Kind: "scroll", Dir: map[int]string{1: "left", 2: "right"}[v1]}
	case c == 11 && (v1 == 1 || v1 == 2):
		return Action{Kind: "scroll", Dir: map[int]string{1: "up", 2: "down"}[v1]}
	case c == 5:
		return Action{Kind: "shortcut"}
	case c == 6:
		return Action{Kind: "macro"}
	}
	return Action{Kind: "unknown", Raw: []int{c, v1, v2}}
}

func CxEncodeKey(a Action) ([]int, error) {
	switch a.Kind {
	case "mouse":
		return []int{1, 1 << a.Btn, 0}, nil
	case "disable":
		return []int{1, 0, 0}, nil
	case "dpi":
		return []int{2, map[string]int{"cycle": 1, "up": 2, "down": 3}[a.Op], 0}, nil
	case "scroll":
		switch a.Dir {
		case "left":
			return []int{3, 1, 0}, nil
		case "right":
			return []int{3, 2, 0}, nil
		case "up":
			return []int{11, 1, 0}, nil
		case "down":
			return []int{11, 2, 0}, nil
		}
	}
	return nil, devErr("unsupported action %v", a)
}

// CxPack builds a 16-byte COMPX frame (without the report ID).
func CxPack(cmd byte, addr, length int, payload []byte) []byte {
	b := make([]byte, 16)
	b[0], b[2], b[3], b[4] = cmd, byte(addr>>8), byte(addr), byte(length)
	copy(b[5:15], payload)
	sum := 8
	for _, v := range b[:15] {
		sum += int(v)
	}
	b[15] = byte((85 - (sum & 0xFF)) & 0xFF)
	return b
}

type CompxMouse struct {
	T          Transport
	VID, PID   uint16
	Receiver   bool
	Reg        *Registry
	codec      string
	stageCount int
}

func (m *CompxMouse) Family() string   { return "compx" }
func (m *CompxMouse) IsReceiver() bool { return m.Receiver }

func (m *CompxMouse) xfer(cmd byte, addr, length int, payload []byte) ([]byte, error) {
	if !cxAllowed[cmd] && !(cmd == cxMouseInfo && addr == 0 && length == 8 && len(payload) == 4) {
		return nil, devErr("command 0x%02x is not allowed", cmd)
	}
	return exchange(m.T, CxPack(cmd, addr, length, payload), func(raw []byte) ([]byte, error) {
		if len(raw) < 16 || raw[0] != cmd {
			return nil, nil
		}
		r := raw[:16]
		if (cmd == cxGetEEPROM || cmd == cxSetEEPROM) && int(r[2])<<8|int(r[3]) != addr {
			return nil, nil
		}
		sum := 8
		for _, v := range r[:15] {
			sum += int(v)
		}
		if byte((85-(sum&0xFF))&0xFF) != r[15] {
			return nil, devErr("bad checksum in reply: % x", r)
		}
		if r[1] != 0 {
			return nil, devErr("device returned status %d for 0x%02x", r[1], cmd)
		}
		return r, nil
	})
}

// LinkType: the link the mouse reports (see LinkMaxRate), or -1. ATK HUB sends
// 4 random bytes; the reply echoes them scrambled, then CID, MID and the link type.
func (m *CompxMouse) LinkType() int {
	nonce := make([]byte, 4)
	rand.Read(nonce)
	r, err := m.xfer(cxMouseInfo, 0, 8, nonce)
	if err != nil {
		return -1
	}
	return int(r[11])
}

// Read returns up to 10 bytes of EEPROM.
func (m *CompxMouse) Read(addr, length int) ([]int, error) {
	r, err := m.xfer(cxGetEEPROM, addr, length, nil)
	if err != nil {
		return nil, err
	}
	out := make([]int, length)
	for i := range out {
		out[i] = int(r[5+i])
	}
	return out, nil
}

func (m *CompxMouse) readLong(addr, length int) ([]int, error) {
	var out []int
	for off := 0; off < length; off += 10 {
		part, err := m.Read(addr+off, min(10, length-off))
		if err != nil {
			return nil, err
		}
		out = append(out, part...)
	}
	return out, nil
}

// write stores bytes and reads them back; a write that didn't stick is an error.
func (m *CompxMouse) write(addr int, data []int) error {
	buf := make([]byte, len(data))
	for i, v := range data {
		buf[i] = byte(v)
	}
	if _, err := m.xfer(cxSetEEPROM, addr, len(buf), buf); err != nil {
		return err
	}
	back, err := m.Read(addr, len(buf))
	if err != nil {
		return err
	}
	for i := range buf {
		if back[i] != int(buf[i]) {
			return devErr("write to 0x%04x did not stick (wanted % x, got %v)", addr, buf, back)
		}
	}
	return nil
}

// patchRow reads a shared row fresh, changes one [value, ~value] pair, writes it back.
// It never writes when the read failed.
func (m *CompxMouse) patchRow(addr, length, index, value int) error {
	row, err := m.Read(addr, length)
	if err != nil {
		return err
	}
	p := pair(value)
	row[index], row[index+1] = int(p[0]), int(p[1])
	return m.write(addr, row)
}

func (m *CompxMouse) Wake(tries int) error {
	for i := 0; i < tries; i++ {
		if _, err := m.Read(addrRateRow, 2); err == nil {
			return nil
		} else if err != ErrAsleep {
			return err
		}
		time.Sleep(300 * time.Millisecond)
	}
	return ErrAsleep
}

func (m *CompxMouse) identify() Ident {
	key := ""
	if r, err := m.xfer(cxCidMid, 0, 0, nil); err == nil {
		key = fmt.Sprintf("%d,%d", r[5], r[6])
	}
	return m.Reg.Lookup("compx", key, m.VID, m.PID)
}

// CompetitiveMaxAllowed: ATK HUB's rule. CID 1 always; others from a listed firmware.
func CompetitiveMaxAllowed(key, version string, table map[string]string) bool {
	if key == "" {
		return false
	}
	if strings.SplitN(key, ",", 2)[0] == "1" {
		return true
	}
	need, ok := table[key]
	if !ok || need == "" {
		return false
	}
	v, err1 := strconv.ParseFloat(version, 64)
	n, err2 := strconv.ParseFloat(need, 64)
	return err1 == nil && err2 == nil && v >= n
}

func (m *CompxMouse) versions() map[string]string {
	out := map[string]string{}
	for key, cmd := range map[string]byte{"mouse": cxMouseVersion, "dongle": cxDongleVersion} {
		if r, err := m.xfer(cmd, 0, 0, nil); err == nil {
			out[key] = fmt.Sprintf("%x.%02x", r[5], r[6])
		}
	}
	return out
}

func (m *CompxMouse) readStages(count int) ([]int, error) {
	if m.codec == "M" {
		stages := make([]int, count)
		for i := range stages {
			row, err := m.Read(addrDPI3955+6*i, 6)
			if err != nil {
				return nil, err
			}
			stages[i] = DecM(row[0]|row[1]<<8, row[4]&1)
		}
		return stages, nil
	}
	c, ok := codecs[m.codec]
	if !ok {
		return nil, devErr("DPI format unknown for this mouse")
	}
	var flat []int
	for _, a := range addrDPIRows {
		row, err := m.Read(a, 8)
		if err != nil {
			return nil, err
		}
		flat = append(flat, row...)
	}
	stages := make([]int, count)
	for i := range stages {
		stages[i] = c.Dec(byte(flat[4*i]), flat[4*i+2]&0x0F)
	}
	return stages, nil
}

func (m *CompxMouse) ReadState() (*State, error) {
	if err := m.Wake(10); err != nil {
		return nil, err
	}
	s := &State{ID: m.identify(), Versions: m.versions()}
	if r, err := m.xfer(cxBattery, 0, 0, nil); err == nil {
		s.Battery = &Battery{Level: int(r[5]), Charging: r[6] != 0}
	}
	rate, err := m.Read(addrRateRow, 10)
	if err != nil {
		return nil, err
	}
	s.RateHz = cxRateHz(rate[0])
	sensorRow, err := m.Read(addrSensorRow, 6)
	if err != nil {
		return nil, err
	}
	s.SensorMode = sensorRow[4]
	// never hide a rate the mouse is already set to
	maxRate := max(MaxRate(m.LinkType()), s.RateHz)
	s.Caps = Capabilities("compx", s.ID.Sensor, s.ID.Flags, maxRate, CxRates,
		CompetitiveMaxAllowed(s.ID.Key, s.Versions["mouse"], CompetitiveMaxTable()),
		s.SensorMode, CxSensorModes)
	m.codec = s.Caps.DPICodec
	m.stageCount = 4
	if rate[2] >= 1 && rate[2] <= 8 {
		m.stageCount = rate[2]
	}
	s.StageCount, s.ActiveStage = m.stageCount, rate[4]
	if stages, err := m.readStages(8); err == nil {
		s.Stages = stages
	} else if err == ErrAsleep {
		return nil, err
	}
	lod, err := m.Read(addrLod, 2)
	if err != nil {
		return nil, err
	}
	s.LodCode = lod[0]
	f, err := m.Read(addrFuncRow, 10)
	if err != nil {
		return nil, err
	}
	s.DebounceMs, s.MotionSync, s.SleepS, s.AngleSnap, s.Ripple = f[0], f[2] != 0, f[4]*10, f[6] != 0, f[8] != 0
	s.LongRange = m.longRange(s.ID.Flags)
	if s.Light, s.StageColors, err = m.light(s.ID.Flags); err != nil {
		return nil, err
	}
	s.RxLight = m.receiverLight(s.ID.ReceiverFlags)
	keys, err := m.readLong(addrKey0, 24)
	if err != nil {
		return nil, err
	}
	for i := 0; i < 6; i++ {
		s.Buttons = append(s.Buttons, CxDecodeKey(keys[4*i], keys[4*i+1], keys[4*i+2]))
	}
	return s, nil
}

// Backup is a raw dump of every settings row, in the same format as the Linux app.
func (m *CompxMouse) Backup() (map[string]any, error) {
	dump := map[string]any{}
	for addr := 0; addr < 0xF0; addr += 10 {
		if row, err := m.Read(addr, 10); err == nil {
			dump[fmt.Sprintf("0x%04x", addr)] = row
		} else {
			dump[fmt.Sprintf("0x%04x", addr)] = err.Error()
		}
	}
	for i := 0; i < 6; i++ {
		a := addrShortcut0 + 32*i
		row, err := m.readLong(a, 30)
		if err != nil {
			return nil, err
		}
		dump[fmt.Sprintf("0x%04x", a)] = row
	}
	return dump, nil
}

// Restore writes every row back and keeps going past failures.
func (m *CompxMouse) Restore(dump map[string]any) ([]string, error) {
	var failed []string
	for key, v := range dump {
		addr, err := strconv.ParseInt(strings.TrimPrefix(key, "0x"), 16, 32)
		if err != nil {
			continue
		}
		list, ok := v.([]any)
		if !ok {
			continue
		}
		data := make([]int, len(list))
		for i, x := range list {
			f, _ := x.(float64)
			data[i] = int(f)
		}
		var err2 error
		if addr < 0xF0 {
			err2 = m.write(int(addr), data)
		} else { // 30-byte shortcut blocks
			for off := 0; off < len(data) && err2 == nil; off += 10 {
				err2 = m.write(int(addr)+off, data[off:min(len(data), off+10)])
			}
		}
		if err2 == ErrAsleep {
			return failed, err2
		} else if err2 != nil {
			failed = append(failed, key)
		}
	}
	return failed, nil
}

func (m *CompxMouse) SetRate(hz int) error         { return m.patchRow(addrRateRow, 10, 0, CxRates[hz]) }
func (m *CompxMouse) SetActiveStage(i int) error   { return m.patchRow(addrRateRow, 10, 4, i) }
func (m *CompxMouse) SetStageCount(n int) error    { return m.patchRow(addrRateRow, 10, 2, n) }
func (m *CompxMouse) SetLod(code int) error        { return m.write(addrLod, []int{code, int(inv(code))}) }
func (m *CompxMouse) SetDebounce(ms int) error     { return m.patchRow(addrFuncRow, 10, 0, ms) }
func (m *CompxMouse) SetMotionSync(on bool) error  { return m.patchRow(addrFuncRow, 10, 2, b2i(on)) }
func (m *CompxMouse) SetAngleSnap(on bool) error   { return m.patchRow(addrFuncRow, 10, 6, b2i(on)) }
func (m *CompxMouse) SetRipple(on bool) error      { return m.patchRow(addrFuncRow, 10, 8, b2i(on)) }
func (m *CompxMouse) SetSensorMode(mode int) error { return m.patchRow(addrSensorRow, 6, 4, mode) }

// light: the DPI light and the 8 stage colours, or nil without a light.
func (m *CompxMouse) light(flags []string) (*Light, []string, error) {
	if contains(flags, "noDpiRGB") {
		return nil, nil, nil
	}
	row, err := m.Read(addrLightRow, 8)
	if err != nil {
		return nil, nil, err
	}
	l := &Light{Mode: "steady", Brightness: nearestLevel(row[2], cxLightBrightness),
		Speed: nearestLevel(row[4], cxLightSpeed)}
	if row[0] == cxLightModes["breathing"] {
		l.Mode = "breathing"
	}
	if row[6] == 0 {
		l.Mode = "off"
	}
	var colors []string
	for _, a := range addrDPIColorRows {
		r, err := m.Read(a, 8)
		if err != nil {
			return nil, nil, err
		}
		colors = append(colors, rgbHex(r[0], r[1], r[2]), rgbHex(r[4], r[5], r[6]))
	}
	return l, colors, nil
}

func (m *CompxMouse) SetLight(l Light) error {
	row, err := m.Read(addrLightRow, 8)
	if err != nil {
		return err
	}
	set := func(i, v int) { p := pair(v); row[i], row[i+1] = int(p[0]), int(p[1]) }
	if l.Mode != "off" { // off keeps the last effect for next time
		set(0, cxLightModes[l.Mode])
	}
	set(2, cxLightBrightness[l.Brightness])
	set(4, cxLightSpeed[l.Speed])
	set(6, b2i(l.Mode != "off"))
	return m.write(addrLightRow, row)
}

func (m *CompxMouse) SetStageColor(i int, color string) error {
	addr := addrDPIColorRows[i>>1]
	row, err := m.Read(addr, 8)
	if err != nil {
		return err
	}
	c, o := hexRGB(color), 4*(i&1)
	row[o], row[o+1], row[o+2], row[o+3] = c[0], c[1], c[2], int(inv(c[0]+c[1]+c[2]))
	return m.write(addr, row)
}

// receiverLight: the receiver's light, or nil if it has none we can drive.
// A receiver without a light refuses the read, whatever ATK HUB's list says.
func (m *CompxMouse) receiverLight(flags []string) *ReceiverLight {
	kind := ""
	if m.Receiver {
		kind = receiverLightKind(flags)
	}
	switch kind {
	case "simple":
		if r, err := m.xfer(cxGetRxSimple, 0, 0, nil); err == nil {
			return &ReceiverLight{Kind: kind, Mode: int(r[5]), Sleep: -1}
		}
	case "full":
		if r, err := m.xfer(cxGetRxLight, 0, 0, nil); err == nil {
			return &ReceiverLight{Kind: kind, Mode: int(r[5]), Color: rgbHex(int(r[6]), int(r[7]), int(r[8])),
				Speed: nearestLevel(int(r[9]), cxRxSpeed), Brightness: nearestLevel(int(r[10]), cxRxBrightness),
				Sleep: nearestLevel(int(r[11]), cxRxSleep)}
		}
	}
	return nil
}

func (m *CompxMouse) SetReceiverLight(l ReceiverLight) error {
	var err error
	flags := []string{}
	if l.Kind == "simple" {
		flags = []string{"singleRgb"}
		_, err = m.xfer(cxSetRxSimple, 0, 10, []byte{byte(l.Mode)})
	} else {
		c := hexRGB(l.Color)
		_, err = m.xfer(cxSetRxLight, 0, 10, []byte{byte(l.Mode), byte(c[0]), byte(c[1]), byte(c[2]),
			byte(cxRxSpeed[l.Speed]), byte(cxRxBrightness[l.Brightness]), byte(cxRxSleep[l.Sleep])})
	}
	if err != nil {
		return err
	}
	if got := m.receiverLight(flags); got == nil || *got != l {
		return devErr("receiver light did not stick")
	}
	return nil
}

// longRange: long range mode on/off, or nil where ATK HUB doesn't offer it.
func (m *CompxMouse) longRange(flags []string) *bool {
	if contains(flags, "noFarDistance") {
		return nil
	}
	r, err := m.xfer(cxGetLongRange, 0, 0, nil)
	if err != nil {
		return nil
	}
	on := r[5] != 0
	return &on
}

func (m *CompxMouse) SetLongRange(on bool) error {
	if _, err := m.xfer(cxSetLongRange, 0, 10, []byte{byte(b2i(on))}); err != nil {
		return err
	}
	r, err := m.xfer(cxGetLongRange, 0, 0, nil)
	if err != nil {
		return err
	}
	if int(r[5]) != b2i(on) {
		return devErr("long range mode did not stick")
	}
	return nil
}

// SetSleep writes both timers: closeLedTime (0xA9 row) and the sensor's own (0xB5).
func (m *CompxMouse) SetSleep(seconds int) error {
	units := pyRound(float64(seconds) / 10)
	if err := m.patchRow(addrFuncRow, 10, 4, units); err != nil {
		return err
	}
	row, err := m.Read(addrSensorRow, 6)
	if err != nil {
		return err
	}
	row[0], row[1], row[2], row[3] = 1, int(inv(1)), units, int(inv(units))
	return m.write(addrSensorRow, row)
}

func (m *CompxMouse) SetButton(i int, a Action) error {
	k, err := CxEncodeKey(a)
	if err != nil {
		return err
	}
	return m.write(addrKey0+4*i, []int{k[0], k[1], k[2], int(inv(k[0] + k[1] + k[2]))})
}

// SetStageDPI writes one stage and returns the DPI the mouse stored.
func (m *CompxMouse) SetStageDPI(i, dpi int) (int, error) {
	if m.codec == "M" {
		value, doubled := EncM(dpi)
		lo, hi, flags := value&0xFF, value>>8, doubled|doubled<<4
		row := []int{lo, hi, lo, hi, flags}
		sum := 0
		for _, v := range row {
			sum += v
		}
		return DecM(value, doubled), m.write(addrDPI3955+6*i, append(row, int(inv(sum))))
	}
	c, ok := codecs[m.codec]
	if !ok {
		return 0, devErr("DPI format unknown for this mouse")
	}
	rowAddr := addrDPIRows[i>>1]
	row, err := m.Read(rowAddr, 8)
	if err != nil {
		return 0, err
	}
	xb, xn := c.Enc(dpi)
	mode := (xn&0x0F)<<4 | xn&0x0F
	o := 4 * (i & 1)
	row[o], row[o+1], row[o+2], row[o+3] = int(xb), int(xb), mode, int(inv(int(xb)+int(xb)+mode))
	if err := m.write(rowAddr, row); err != nil {
		return 0, err
	}
	return c.Dec(xb, xn), nil
}

func (m *CompxMouse) FactoryReset() error {
	_, err := m.xfer(cxFactoryReset, 0, 0, nil)
	return err
}

func b2i(b bool) int {
	if b {
		return 1
	}
	return 0
}
