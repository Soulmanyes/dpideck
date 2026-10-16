package main

import "dpideck/core"

// demoMouse is a simulated VXE MAD R for `DPIdeck.exe -demo`, so the
// window can be checked without a mouse. Writes only change this copy.
type demoMouse struct{ s core.State }

func newDemoMouse() *demoMouse {
	caps := core.Capabilities("compx", "PAW3395", []string{"noBottomButton"}, 1000, core.CxRates,
		true, 2, core.CxSensorModes)
	return &demoMouse{core.State{
		ID:       core.Ident{Key: "2,49", Name: "MAD R (demo)", Sensor: "PAW3395", Flags: []string{"noBottomButton"}},
		Versions: map[string]string{"mouse": "3.00"}, Battery: &core.Battery{Level: 80},
		RateHz: 1000, SensorMode: 2, Caps: caps, StageCount: 2, ActiveStage: 0,
		Stages: []int{1600, 3200, 3200, 3200, 3200, 3200, 3200, 3200}, LodCode: 2, SleepS: 60,
		Buttons: []core.Action{{Kind: "mouse", Btn: 0}, {Kind: "mouse", Btn: 1}, {Kind: "mouse", Btn: 2},
			{Kind: "mouse", Btn: 3}, {Kind: "mouse", Btn: 4}, {Kind: "dpi", Op: "cycle"}},
	}}
}

func (d *demoMouse) Family() string                           { return "compx" }
func (d *demoMouse) IsReceiver() bool                         { return true }
func (d *demoMouse) Wake(int) error                           { return nil }
func (d *demoMouse) ReadState() (*core.State, error)          { s := d.s; return &s, nil }
func (d *demoMouse) SetRate(hz int) error                     { d.s.RateHz = hz; return nil }
func (d *demoMouse) SetLod(code int) error                    { d.s.LodCode = code; return nil }
func (d *demoMouse) SetDebounce(ms int) error                 { d.s.DebounceMs = ms; return nil }
func (d *demoMouse) SetMotionSync(on bool) error              { d.s.MotionSync = on; return nil }
func (d *demoMouse) SetAngleSnap(on bool) error               { d.s.AngleSnap = on; return nil }
func (d *demoMouse) SetRipple(on bool) error                  { d.s.Ripple = on; return nil }
func (d *demoMouse) SetSleep(sec int) error                   { d.s.SleepS = sec; return nil }
func (d *demoMouse) SetSensorMode(mode int) error             { d.s.SensorMode = mode; return nil }
func (d *demoMouse) SetButton(i int, a core.Action) error     { d.s.Buttons[i] = a; return nil }
func (d *demoMouse) SetStageDPI(i, dpi int) (int, error)      { d.s.Stages[i] = dpi; return dpi, nil }
func (d *demoMouse) SetActiveStage(i int) error               { d.s.ActiveStage = i; return nil }
func (d *demoMouse) SetStageCount(n int) error                { d.s.StageCount = n; return nil }
func (d *demoMouse) FactoryReset() error                      { return nil }
func (d *demoMouse) Backup() (map[string]any, error)          { return map[string]any{}, nil }
func (d *demoMouse) Restore(map[string]any) ([]string, error) { return nil, nil }
