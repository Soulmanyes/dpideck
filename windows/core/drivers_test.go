package core

import (
	"testing"
	"time"
)

// fakeTransport answers like a mouse would; handle() returns the reply report or nil.
type fakeTransport struct {
	handle  func(frame []byte) []byte
	pending [][]byte
	sent    [][]byte
}

func (f *fakeTransport) Write(report []byte) error {
	f.sent = append(f.sent, report)
	if r := f.handle(report[1:]); r != nil {
		f.pending = append(f.pending, append([]byte{ReportID}, r...))
	}
	return nil
}

func (f *fakeTransport) Read(timeout time.Duration) ([]byte, error) {
	if len(f.pending) == 0 {
		return nil, nil
	}
	r := f.pending[0]
	f.pending = f.pending[1:]
	return r, nil
}

func (f *fakeTransport) Flush() { f.pending = nil }

// testRegistry knows one mouse of each family, like ATK HUB's tables do.
func testRegistry() *Registry {
	r := emptyRegistry()
	r.Compx.Cid["2,49"] = Entry{"MAD R", "PAW3395", []string{"noBottomButton"}}
	r.Compx.Cid["1,55"] = Entry{"ATK PHANTOM ULTIMATE", "PAW3955MASTER", []string{"noBottomButton"}}
	r.Bitmouse.Cid["1,10"] = Entry{"ATK PHANTOM ULTIMATE", "PAW3955MASTER", []string{"noBottomButton"}}
	r.Receivers["373b:104d"] = "MAD 1K Dongle"
	r.Receivers["373b:12cf"] = "Mouse 8K Dongle"
	return r
}

// ---- COMPX: an EEPROM image behind the 16-byte protocol ----

func fakeCompx(cid, mid byte) (*fakeTransport, map[int]byte) {
	mem := map[int]byte{}
	set := func(addr int, vals ...int) {
		for i, v := range vals {
			mem[addr+i] = byte(v)
		}
	}
	set(0x00, 1, 84, 1, 84, 0, 85, 0, 85, 0, 85) // 1000 Hz, 1 stage, stage 1 active
	set(0x0A, 2, 83)                             // lift-off 2 mm
	set(0x0C, 31, 31, 0, 23, 63, 63, 0, 215)     // B codec: 1600, 3200
	set(0xA9, 0, 85, 0, 85, 6, 79, 0, 85, 0, 85) // sleep 60 s
	set(0xB5, 0, 85, 6, 79, 2, 83)               // Competitive Max
	set(0x60, 1, 1, 0, 83, 1, 2, 0, 82, 1, 4, 0, 80, 1, 8, 0, 76, 1, 16, 0, 68, 2, 1, 0, 82)
	set(0x1B00, 0x3F, 0x06, 0x3F, 0x06, 0, 0) // PAW3955 stage 1: 1600
	t := &fakeTransport{}
	t.handle = func(f []byte) []byte {
		cmd, addr, n := f[0], int(f[2])<<8|int(f[3]), int(f[4])
		r := make([]byte, 16)
		copy(r, f[:5])
		switch cmd {
		case cxGetEEPROM:
			for i := 0; i < n; i++ {
				r[5+i] = mem[addr+i]
			}
		case cxSetEEPROM:
			for i := 0; i < n; i++ {
				mem[addr+i] = f[5+i]
			}
		case cxCidMid:
			r[5], r[6] = cid, mid
		case cxMouseVersion:
			r[5], r[6] = 3, 0
		case cxBattery:
			r[5] = 77
		case cxMouseInfo:
			r[9], r[10], r[11] = cid, mid, mem[-1] // link type
		case cxGetLongRange:
			r[5] = mem[-2]
		case cxGetRxLight, cxGetRxSimple: // mem[-3] = 1: a receiver without a light
			if mem[-3] == 1 {
				r[1] = 1
			}
			base := map[byte]int{cxGetRxLight: -20, cxGetRxSimple: -30}[cmd]
			for i := 0; i < 7; i++ {
				r[5+i] = mem[base+i]
			}
		case cxSetRxLight, cxSetRxSimple:
			if mem[-3] == 1 {
				r[1] = 1
			}
			base := map[byte]int{cxSetRxLight: -20, cxSetRxSimple: -30}[cmd]
			for i := 0; i < 7; i++ {
				mem[base+i] = f[5+i]
			}
		case cxSetLongRange:
			mem[-2] = f[5]
		}
		sum := 8
		for _, v := range r[:15] {
			sum += int(v)
		}
		r[15] = byte((85 - (sum & 0xFF)) & 0xFF)
		return r
	}
	return t, mem
}

func TestCompxMadR(t *testing.T) {
	ft, mem := fakeCompx(2, 49)
	m := &CompxMouse{T: ft, VID: 0x373B, PID: 0x103F, Reg: testRegistry()}
	upgrade = map[string]string{"2,49": "3.00"} // no network in tests
	s, err := m.ReadState()
	if err != nil {
		t.Fatal(err)
	}
	if s.ID.Name != "MAD R" || s.RateHz != 1000 || s.Stages[0] != 1600 || s.Stages[1] != 3200 ||
		s.LodCode != 2 || s.SensorMode != 2 || s.SleepS != 60 || s.Caps.MaxDPI != 30000 ||
		s.Caps.DPIButton || len(s.Caps.SensorModes) != 3 || s.Buttons[5].Kind != "dpi" {
		t.Fatalf("unexpected state %+v", s)
	}
	if got, err := m.SetStageDPI(1, 30000); err != nil || got != 30000 {
		t.Fatal(got, err)
	}
	s, _ = m.ReadState()
	if s.Stages[1] != 30000 || s.Stages[0] != 1600 {
		t.Fatal("stage write touched the wrong slot", s.Stages)
	}
	if err := m.SetSensorMode(0); err != nil || mem[0xB9] != 0 || mem[0xBA] != 85 {
		t.Fatal("sensor mode", err)
	}
	if err := m.SetSleep(300); err != nil || mem[0xAD] != 30 || mem[0xB7] != 30 || mem[0xB5] != 1 {
		t.Fatal("sleep", err)
	}
	if err := m.SetButton(4, Action{Kind: "dpi", Op: "cycle"}); err != nil || mem[0x70] != 2 {
		t.Fatal("button", err)
	}
	// a write that doesn't stick is an error
	ft2, _ := fakeCompx(2, 49)
	base := ft2.handle
	ft2.handle = func(f []byte) []byte {
		if f[0] == cxSetEEPROM {
			f = append([]byte(nil), f...)
			f[5]++ // the mouse stores something else
		}
		return base(f)
	}
	m2 := &CompxMouse{T: ft2, VID: 0x373B, PID: 0x103F, Reg: testRegistry()}
	if err := m2.SetSensorMode(1); err == nil {
		t.Fatal("expected a did-not-stick error")
	}
	// refused commands never reach the mouse
	if _, err := m.xfer(0x0D, 0, 0, nil); err == nil {
		t.Fatal("upgrade command was allowed")
	}
	if _, err := m.xfer(cxMouseInfo, 0, 10, []byte{1, 2, 3, 4}); err == nil {
		t.Fatal("0x01 was allowed in a form ATK HUB never sends")
	}
}

func TestCompxRatesFollowLinkType(t *testing.T) {
	upgrade = map[string]string{"2,49": "3.00"}
	for _, c := range []struct{ link, top int }{{0, 1000}, {3, 8000}, {1, 4000}, {4, 2000}, {99, 8000}} {
		ft, mem := fakeCompx(2, 49)
		mem[-1] = byte(c.link)
		m := &CompxMouse{T: ft, VID: 0x373B, PID: 0x103F, Reg: testRegistry()}
		s, err := m.ReadState()
		if err != nil {
			t.Fatal(err)
		}
		if top := s.Caps.Rates[len(s.Caps.Rates)-1]; top != c.top {
			t.Fatalf("link %d: top rate %d, want %d", c.link, top, c.top)
		}
	}
	// an 8K mouse already at 2000 Hz still offers 8000 Hz
	ft, mem := fakeCompx(2, 49)
	mem[-1], mem[0x00], mem[0x01] = 3, 0x10, 85-0x10
	s, err := (&CompxMouse{T: ft, VID: 0x373B, PID: 0x103F, Reg: testRegistry()}).ReadState()
	if err != nil || s.RateHz != 2000 || s.Caps.Rates[len(s.Caps.Rates)-1] != 8000 {
		t.Fatalf("rates %v at %d Hz, %v", s.Caps.Rates, s.RateHz, err)
	}
}

func TestCompxPaw3955(t *testing.T) {
	ft, mem := fakeCompx(1, 55)
	m := &CompxMouse{T: ft, VID: 0x373B, PID: 0x1296, Reg: testRegistry()}
	s, err := m.ReadState()
	if err != nil {
		t.Fatal(err)
	}
	if s.Caps.MaxDPI != 50000 || s.Caps.Lod != "continuous" || s.Stages[0] != 1600 {
		t.Fatalf("unexpected 3955 state %+v", s.Caps)
	}
	if got, _ := m.SetStageDPI(0, 45001); got != 45000 || mem[0x1B04] != 0x11 {
		t.Fatal("3955 doubling", got, mem[0x1B04])
	}
}

func TestCompxAsleepNeverWrites(t *testing.T) {
	ft, mem := fakeCompx(2, 49)
	ft.handle = func([]byte) []byte { return nil } // asleep
	m := &CompxMouse{T: ft, VID: 0x373B, PID: 0x103F, Reg: testRegistry()}
	before := mem[0xB9]
	if err := m.SetSensorMode(0); err != ErrAsleep {
		t.Fatal("expected asleep", err)
	}
	for _, r := range ft.sent {
		if r[1] == cxSetEEPROM {
			t.Fatal("wrote while the read failed")
		}
	}
	if mem[0xB9] != before {
		t.Fatal("memory changed")
	}
}

// ---- BITMOUSE: the same simulated mouse as the Linux tests ----

func fakeBitmouse(answersTarget byte) (*fakeTransport, []byte, map[string]int) {
	mem := make([]byte, 4096)
	mem[1], mem[2] = 0, 2
	for i, dpi := range []int{800, 1600} {
		o := 3 + 8*i
		mem[o], mem[o+1], mem[o+2], mem[o+3] = byte(dpi), byte(dpi>>8), byte(dpi), byte(dpi>>8)
		mem[o+6] = 255
	}
	mem[74] = 4
	for i, k := range [][]byte{{1, 0, 0, 0, 0}, {1, 0, 1, 0, 0}, {1, 0, 2, 0, 0}, {1, 0, 3, 0, 0}, {1, 0, 4, 0, 0}, {10, 0, 0, 0, 0}} {
		copy(mem[82+5*i:], k)
	}
	cfg := map[string]int{"rate": 0, "lodoff": 1, "ms": 0, "lc": 0, "rip": 0, "sleep": 60, "deb": 2}
	t := &fakeTransport{}
	t.handle = func(f []byte) []byte {
		sum := 0
		for _, v := range f[1:] {
			sum += int(v)
		}
		if len(f) != 63 || f[0] != byte(sum) {
			panic("bad BITMOUSE frame")
		}
		cmd, p := f[5], f[7:]
		if cmd == bmGetRxLight || cmd == bmSetRxLight {
			if f[4] != 0 { // the receiver answers, on target 0
				return nil
			}
			if cmd == bmSetRxLight {
				copy(mem[4000:4009], p[:9])
			}
			return append([]byte{0x72, 0, 0, 0, cmd, 9}, mem[4000:4009]...)
		}
		if cmd == bmGetLinkType {
			if link, ok := cfg["link"]; ok && f[4] == 0 { // the receiver answers, on target 0
				return []byte{0x72, 0, 0, 0, cmd, 1, byte(link)}
			}
			return nil
		}
		if f[4] != answersTarget && cmd != bmDongleVersion {
			return nil // wrong target: silence
		}
		reply := func(payload ...byte) []byte {
			r := make([]byte, 63)
			r[0], r[3], r[4], r[5] = 0x72, f[4], cmd, byte(len(payload))
			copy(r[6:], payload)
			return r
		}
		switch cmd {
		case bmGetConfig:
			return reply(0, 1, byte(cfg["rate"]), 0, 0, byte(cfg["lodoff"]), byte(cfg["ms"]), byte(cfg["lc"]),
				byte(cfg["rip"]), byte(cfg["sleep"]), byte(cfg["sleep"]>>8), byte(cfg["deb"]), 0, 0, 0, 0, 0, 0)
		case bmCidMid:
			return reply(0, 1, 10, 0, 0, 0)
		case bmGetAddress:
			a, n := int(p[0])|int(p[1])<<8, int(p[2])
			return reply(append([]byte{p[0], p[1], p[2]}, mem[a:a+n]...)...)
		case bmSetRate:
			cfg["rate"] = int(p[0])
		case bmSetLongRange:
			mem[bmAddrLongRange] = p[0]
		case bmSetLight:
			copy(mem[bmAddrLight:], p[:3])
		case bmSetLod:
			cfg["lodoff"] = int(p[1])
		case bmSetSleep:
			cfg["sleep"] = int(p[0]) | int(p[1])<<8
		case bmSetSensorMode:
			mem[74] = p[0]
		case bmSetButton:
			copy(mem[82+5*int(p[0]):], p[1:5])
		case bmSetDPI:
			i, o := int(p[0]), 3+8*int(p[0])
			copy(mem[o:o+4], p[1:5])
			copy(mem[o+4:o+7], p[5:8])
			if p[9] != 0 {
				mem[1] = byte(i)
			}
		case bmMouseVersion, bmDongleVersion:
			return reply(1, 2, 3)
		case bmBattery:
			return reply(77)
		case bmCharging:
			return reply(0)
		}
		return reply()
	}
	return t, mem, cfg
}

func TestBitmousePhantom(t *testing.T) {
	ft, mem, cfg := fakeBitmouse(0)
	reg := testRegistry()
	reg.Receivers["373b:12ce"] = "unknown receiver" // forces target 1, which the mouse ignores
	m := NewBitMouse(ft, 0x373B, 0x9999, reg)       // unknown ID: probe both targets
	s, err := m.ReadState()
	if err != nil {
		t.Fatal(err)
	}
	if m.target != 0 || s.ID.Name != "ATK PHANTOM ULTIMATE" || s.Caps.MaxDPI != 50000 ||
		s.Caps.Lod != "continuous" || s.SensorMode != 4 || s.StageCount != 2 || s.Stages[1] != 1600 ||
		s.LodCode != 2 || s.Caps.Backup || s.Caps.StageCountEditable {
		t.Fatalf("unexpected state %+v %+v", s, s.Caps)
	}
	if err := m.SetRate(500); err != nil || cfg["rate"] != 1 {
		t.Fatal("rate", err)
	}
	if err := m.SetLod(4); err != nil || cfg["lodoff"] != 3 {
		t.Fatal("lod", err)
	}
	if err := m.SetSensorMode(5); err != nil || mem[74] != 5 {
		t.Fatal("mode", err)
	}
	if got, err := m.SetStageDPI(1, 45000); err != nil || got != 45000 {
		t.Fatal("dpi", err)
	}
	if err := m.SetActiveStage(1); err != nil || mem[1] != 1 {
		t.Fatal("active", err)
	}
	if err := m.SetButton(4, Action{Kind: "dpi", Op: "cycle"}); err != nil || mem[82+20] != 10 {
		t.Fatal("button", err)
	}
	if m.SetStageCount(3) == nil {
		t.Fatal("stage count should be refused")
	}
	if _, err := m.xfer(bmCidMid, 7, 5, 1); err == nil {
		t.Fatal("changing the mouse ID was allowed")
	}
	if _, err := m.xfer(0x18, 3, 1, 0); err == nil {
		t.Fatal("address write was allowed")
	}
	if _, err := m.xfer(0x82, 3, 0); err == nil {
		t.Fatal("pairing was allowed")
	}
}

func TestBitmouseRatesFollowLinkType(t *testing.T) {
	// an 8K receiver (target 1 for the mouse, 0 for the link type), mouse at 2000 Hz
	ft, _, cfg := fakeBitmouse(1)
	cfg["link"], cfg["rate"] = 5, BmRates[2000]
	m := NewBitMouse(ft, 0x373B, 0x12CF, testRegistry())
	s, err := m.ReadState()
	if err != nil || s.RateHz != 2000 || s.Caps.Rates[len(s.Caps.Rates)-1] != 8000 {
		t.Fatalf("rates %v at %d Hz, %v", s.Caps.Rates, s.RateHz, err)
	}
	// a 4K receiver caps it at 4000 Hz although the name says 8K
	ft, _, cfg = fakeBitmouse(1)
	cfg["link"] = 1
	s, err = NewBitMouse(ft, 0x373B, 0x12CF, testRegistry()).ReadState()
	if err != nil || s.Caps.Rates[len(s.Caps.Rates)-1] != 4000 {
		t.Fatalf("rates %v, %v", s.Caps.Rates, err)
	}
}

func TestLongRange(t *testing.T) {
	upgrade = map[string]string{"2,49": "3.00"}
	ft, mem := fakeCompx(2, 49)
	cx := &CompxMouse{T: ft, VID: 0x373B, PID: 0x103F, Reg: testRegistry()}
	if s, err := cx.ReadState(); err != nil || s.LongRange == nil || *s.LongRange {
		t.Fatal("COMPX long range should read as off", err)
	}
	if err := cx.SetLongRange(true); err != nil || mem[-2] != 1 {
		t.Fatal("COMPX long range write", err)
	}
	reg := testRegistry()
	reg.Compx.Cid["2,49"] = Entry{"MAD R", "PAW3395", []string{"noFarDistance"}}
	cx = &CompxMouse{T: ft, VID: 0x373B, PID: 0x103F, Reg: reg}
	if s, err := cx.ReadState(); err != nil || s.LongRange != nil {
		t.Fatal("long range offered on a noFarDistance mouse", err)
	}

	fb, bmem, _ := fakeBitmouse(1)
	bm := NewBitMouse(fb, 0x373B, 0x12CF, testRegistry())
	bmem[bmAddrLongRange] = 1
	if s, err := bm.ReadState(); err != nil || s.LongRange == nil || !*s.LongRange {
		t.Fatal("BITMOUSE long range should read as on", err)
	}
	if err := bm.SetLongRange(false); err != nil || bmem[bmAddrLongRange] != 0 {
		t.Fatal("BITMOUSE long range write", err)
	}
}

func TestCompxLights(t *testing.T) {
	upgrade = map[string]string{"2,49": "3.00"}
	ft, mem := fakeCompx(2, 49)
	for i, v := range []int{2, 83, 128, 213, 3, 82, 0, 85} { // MAD R as read: breathing, off
		mem[0x4C+i] = byte(v)
	}
	for i, v := range []int{0, 0, 255, 86, 255, 0, 255, 87} {
		mem[0x2C+i] = byte(v)
	}
	for i, v := range []int{3, 255, 0, 0, 5, 9, 10} { // receiver: steady red, medium, fast, 10 min
		mem[-20+i] = byte(v)
	}
	m := &CompxMouse{T: ft, VID: 0x373B, PID: 0x104D, Receiver: true, Reg: testRegistry()}
	s, err := m.ReadState()
	if err != nil || *s.Light != (Light{"off", 1, 1}) || s.StageColors[0] != "#0000ff" || s.StageColors[1] != "#ff00ff" {
		t.Fatalf("light %+v %v %v", s.Light, s.StageColors, err)
	}
	if *s.RxLight != (ReceiverLight{Kind: "full", Mode: 3, Color: "#ff0000", Brightness: 2, Speed: 1, Sleep: 2}) {
		t.Fatalf("receiver light %+v", s.RxLight)
	}
	if err := m.SetLight(Light{"steady", 2, 0}); err != nil || mem[0x4C] != 1 || mem[0x4E] != 255 || mem[0x50] != 1 || mem[0x52] != 1 {
		t.Fatal("light write", err)
	}
	if err := m.SetLight(Light{"off", 2, 0}); err != nil || mem[0x4C] != 1 || mem[0x52] != 0 {
		t.Fatal("light off should keep the effect", err)
	}
	if err := m.SetStageColor(1, "#00ff00"); err != nil || mem[0x30] != 0 || mem[0x31] != 255 || mem[0x33] != 86 || mem[0x2E] != 255 {
		t.Fatal("stage colour", err)
	}
	if err := m.SetReceiverLight(ReceiverLight{Kind: "full", Mode: 2, Color: "#123456", Brightness: 0, Speed: 2, Sleep: 0}); err != nil ||
		mem[-20] != 2 || mem[-19] != 0x12 || mem[-16] != 9 || mem[-15] != 0 || mem[-14] != 1 {
		t.Fatal("receiver light write", err)
	}
	// a receiver that refuses the read has no light, whatever the list says
	mem[-3] = 1
	if s, _ := m.ReadState(); s.RxLight != nil {
		t.Fatal("receiver light shown for a receiver without one")
	}
	// simple receivers, and lights hidden by flags
	mem[-3], mem[-30] = 0, 2
	reg := testRegistry()
	reg.RxFlags["373b:104d"] = []string{"singleRgb"}
	reg.Compx.Cid["2,49"] = Entry{"MAD R", "PAW3395", []string{"noDpiRGB"}}
	m = &CompxMouse{T: ft, VID: 0x373B, PID: 0x104D, Receiver: true, Reg: reg}
	if s, _ := m.ReadState(); s.Light != nil || s.StageColors != nil || *s.RxLight != (ReceiverLight{Kind: "simple", Mode: 2, Sleep: -1}) {
		t.Fatalf("simple receiver %+v, light %+v", s.RxLight, s.Light)
	}
	if err := m.SetReceiverLight(ReceiverLight{Kind: "simple", Mode: 1, Sleep: -1}); err != nil || mem[-30] != 1 {
		t.Fatal("simple receiver write", err)
	}
	reg.RxFlags["373b:104d"] = []string{"noDongleRGB"}
	if s, _ := m.ReadState(); s.RxLight != nil {
		t.Fatal("receiver light shown on a noDongleRGB receiver")
	}
}

func TestBitmouseLights(t *testing.T) {
	ft, mem, _ := fakeBitmouse(1)
	copy(mem[bmAddrLight:], []byte{1, 170, 20})               // breathing, medium, fast
	copy(mem[4000:], []byte{1, 2, 100, 0, 7, 0, 128, 255, 3}) // receiver: one-colour breathing
	m := NewBitMouse(ft, 0x373B, 0x12CF, testRegistry())
	s, err := m.ReadState()
	if err != nil || *s.Light != (Light{"breathing", 1, 2}) || s.StageColors[1] != "#ff0000" || s.StageColors[2] != "#000000" {
		t.Fatalf("light %+v %v %v", s.Light, s.StageColors, err)
	}
	if *s.RxLight != (ReceiverLight{Kind: "full", Mode: 2, Color: "#0080ff", Brightness: 2, Speed: 0, Sleep: -1}) {
		t.Fatalf("receiver light %+v", s.RxLight)
	}
	if err := m.SetLight(Light{"off", 0, 1}); err != nil || mem[bmAddrLight] != 0 || mem[bmAddrLight+1] != 85 || mem[bmAddrLight+2] != 15 {
		t.Fatal("light write", err)
	}
	if err := m.SetStageColor(0, "#ff0000"); err != nil || mem[3+6] != 255 || mem[3+4] != 0 {
		t.Fatal("stage colour", err)
	}
	if err := m.SetReceiverLight(ReceiverLight{Kind: "full", Mode: 3, Color: "#ffffff", Brightness: 0, Speed: 1, Sleep: -1}); err != nil ||
		mem[4001] != 3 || mem[4002] != 10 || mem[4003] != 1 || mem[4004] != 7 || mem[4005] != 255 || mem[4008] != 3 {
		t.Fatal("receiver light write keeps unknown bytes", err, mem[4000:4009])
	}
}

func TestBitmouseKnownReceiverUsesTarget1(t *testing.T) {
	ft, _, _ := fakeBitmouse(1)
	m := NewBitMouse(ft, 0x373B, 0x12CF, testRegistry())
	if !m.Receiver || m.target != 1 || !m.probed {
		t.Fatal("known receiver should use target 1 without probing")
	}
	if _, err := m.ReadState(); err != nil {
		t.Fatal(err)
	}
}
