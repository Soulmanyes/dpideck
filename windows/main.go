// Mouse Settings for Windows: the native counterpart of app.py (Linux).
// Keep the two in step; see PARITY.md.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/lxn/walk"
	. "github.com/lxn/walk/declarative"
	"github.com/lxn/win"

	"vxeatk/core"
)

type opt struct{ key, label string }

func intOpts(pairs ...any) []opt {
	var out []opt
	for i := 0; i < len(pairs); i += 2 {
		out = append(out, opt{strconv.Itoa(pairs[i].(int)), pairs[i+1].(string)})
	}
	return out
}

var (
	debounceOpts = intOpts(0, "Off", 1, "1 ms", 2, "2 ms", 4, "4 ms", 8, "8 ms", 15, "15 ms", 20, "20 ms")
	sleepOpts    = intOpts(30, "30 s", 60, "1 min", 120, "2 min", 180, "3 min", 300, "5 min",
		1200, "20 min", 1500, "25 min", 1800, "30 min")
	// fixed lift-off steps; the codes are not in ascending order
	lodLevels = map[string][]opt{"levels": intOpts(1, "1 mm", 2, "2 mm"),
		"levels07": intOpts(3, "0.7 mm", 1, "1 mm", 2, "2 mm")}
	actions = []struct {
		a     core.Action
		label string
	}{
		{core.Action{Kind: "mouse", Btn: 0}, "Left click"}, {core.Action{Kind: "mouse", Btn: 1}, "Right click"},
		{core.Action{Kind: "mouse", Btn: 2}, "Middle click"}, {core.Action{Kind: "mouse", Btn: 3}, "Back"},
		{core.Action{Kind: "mouse", Btn: 4}, "Forward"}, {core.Action{Kind: "dpi", Op: "cycle"}, "DPI cycle"},
		{core.Action{Kind: "dpi", Op: "up"}, "DPI up"}, {core.Action{Kind: "dpi", Op: "down"}, "DPI down"},
		{core.Action{Kind: "scroll", Dir: "up"}, "Scroll up"}, {core.Action{Kind: "scroll", Dir: "down"}, "Scroll down"},
		{core.Action{Kind: "scroll", Dir: "left"}, "Scroll left"}, {core.Action{Kind: "scroll", Dir: "right"}, "Scroll right"},
		{core.Action{Kind: "disable"}, "Disabled"},
	}
)

func actionKey(a core.Action) string { return fmt.Sprintf("%s/%d/%s/%s", a.Kind, a.Btn, a.Op, a.Dir) }

func continuousLod() []opt {
	var out []opt
	for code := 1; code <= 11; code++ {
		out = append(out, opt{strconv.Itoa(code), fmt.Sprintf("%.1f mm", core.LodCodeToMM(code))})
	}
	return out
}

// wheelSafeCombo: a Win32 combo box changes its selection on the mouse wheel,
// which would silently write a setting. Pass the wheel to the page instead.
type wheelSafeCombo struct {
	*walk.ComboBox
	scroll **walk.ScrollView
}

func (c *wheelSafeCombo) WndProc(hwnd win.HWND, msg uint32, wParam, lParam uintptr) uintptr {
	if msg == win.WM_MOUSEWHEEL {
		if sv := *c.scroll; sv != nil {
			win.SendMessage(sv.Handle(), msg, wParam, lParam)
		}
		return 0
	}
	return c.ComboBox.WndProc(hwnd, msg, wParam, lParam)
}

// choice is a combo box over (key, label) options that can show unknown values.
type choice struct {
	app    *app
	cb     *walk.ComboBox
	base   []opt
	opts   []opt
	onPick func(o opt)
}

func (c *choice) setOptions(opts []opt) {
	c.base = opts
	c.opts = append([]opt(nil), opts...)
	c.apply()
}

func (c *choice) apply() {
	labels := make([]string, len(c.opts))
	for i, o := range c.opts {
		labels[i] = o.label
	}
	c.cb.SetModel(labels)
}

func (c *choice) set(key string) {
	for i, o := range c.opts {
		if o.key == key {
			c.cb.SetCurrentIndex(i)
			return
		}
	}
	c.opts = append(append([]opt(nil), c.base...), opt{key, "Other (" + key + ")"})
	c.apply()
	c.cb.SetCurrentIndex(len(c.opts) - 1)
}

func (c *choice) changed() {
	if c.app.loading {
		return
	}
	if i := c.cb.CurrentIndex(); i >= 0 && i < len(c.opts) {
		c.onPick(c.opts[i])
	}
}

type app struct {
	mw          *walk.MainWindow
	title, sub  *walk.Label
	banner      *walk.Label
	scroll      *walk.ScrollView
	page        *walk.Composite
	statusItem  *walk.StatusBarItem
	dpiNote     *walk.Label
	stageRows   [8]*walk.Composite
	stageEdits  [8]*walk.LineEdit
	switches    map[string]*walk.CheckBox
	buttonRows  [6]*walk.Composite
	backupAct   *walk.Action
	restoreAct  *walk.Action
	lodLabel    *walk.Label
	lodRow      *walk.Composite
	combos      map[string]*choice
	buttons     [6]*choice
	stageCount  *choice
	activeStage *choice

	mouse      core.Mouse
	transport  *hidTransport
	state      *core.State
	loading    bool
	connecting bool
	demo       bool
	nodes      string
	jobs       chan func()
}

func (a *app) newChoice(onPick func(o opt)) *choice {
	return &choice{app: a, onPick: onPick}
}

// run executes device I/O off the UI thread, then reports back on it.
func (a *app) run(work func() error, done func(error)) {
	a.jobs <- func() {
		err := work()
		a.mw.Synchronize(func() { done(err) })
	}
}

func (a *app) toast(msg string) {
	if a.statusItem != nil {
		a.statusItem.SetText(msg)
	}
}

func (a *app) showError(err error) {
	switch {
	case errors.Is(err, core.ErrAsleep):
		a.banner.SetText("Mouse is asleep. Move it, then press Reload.")
	default:
		a.banner.SetText(err.Error())
	}
	a.banner.SetVisible(true)
	if a.mouse == nil {
		a.page.SetEnabled(false)
		a.sub.SetText("Not connected")
	}
}

// apply writes one setting; on failure the UI reloads what the mouse really has.
func (a *app) apply(write func(m core.Mouse) error, msg string, after func()) {
	if a.loading || a.mouse == nil {
		return
	}
	m := a.mouse
	a.run(func() error { return write(m) }, func(err error) {
		if err != nil {
			a.showError(err)
			a.toast("Not saved")
			a.reload()
			return
		}
		a.toast(msg)
		if after != nil {
			after()
		}
	})
}

func (a *app) connect() {
	if a.connecting {
		return
	}
	if a.demo {
		if a.mouse == nil {
			a.mouse = newDemoMouse()
		}
		a.reload()
		return
	}
	a.connecting = true
	if a.mouse == nil {
		a.sub.SetText("Looking for the mouse…")
	}
	var st *core.State
	a.run(func() error {
		if a.transport != nil {
			a.transport.Close()
		}
		a.mouse, a.transport = nil, nil
		reg := core.GetRegistry(false)
		a.nodes = nodesKey(findNodes(reg))
		m, t, err := openMouse(reg)
		if err != nil {
			return err
		}
		a.mouse, a.transport = m, t
		st, err = m.ReadState()
		return err
	}, func(err error) {
		a.connecting = false
		if err != nil {
			a.showError(err)
			return
		}
		a.show(st)
	})
}

func (a *app) reload() {
	if a.mouse == nil {
		a.connect()
		return
	}
	m := a.mouse
	var st *core.State
	a.run(func() (err error) { st, err = m.ReadState(); return }, func(err error) {
		if err != nil {
			a.showError(err)
			return
		}
		a.show(st)
	})
}

// poll reconnects when nothing is open, or a cable or dongle was plugged or pulled.
func (a *app) poll() {
	for range time.Tick(3 * time.Second) {
		if a.demo {
			continue
		}
		check := func() {
			key := nodesKey(findNodes(core.GetRegistry(false)))
			a.mw.Synchronize(func() {
				if !a.connecting && (a.mouse == nil || key != a.nodes) {
					a.connect()
				}
			})
		}
		select {
		case a.jobs <- check:
		default: // the worker is busy; try again next tick
		}
	}
}

func itoa(v int) string { return strconv.Itoa(v) }

func (a *app) show(s *core.State) {
	a.state = s
	c := s.Caps
	a.loading = true
	defer func() { a.loading = false }()
	a.banner.SetVisible(false)
	a.page.SetEnabled(true)
	link := "cable"
	if a.mouse.IsReceiver() {
		link = "wireless"
	}
	sub := link
	if s.Battery != nil {
		sub += fmt.Sprintf(" · battery %d%%", s.Battery.Level)
		if s.Battery.Charging {
			sub += " (charging)"
		}
	}
	a.title.SetText(s.ID.Name)
	a.sub.SetText(sub)

	// DPI
	a.buttonRows[5].SetVisible(c.DPIButton)
	if s.Stages != nil {
		a.dpiNote.SetText("To switch stages with the mouse, set a button to DPI cycle below. " +
			fmt.Sprintf("Type a value (%d–%d) and press Enter.", c.MinDPI, c.MaxDPI))
	} else {
		a.dpiNote.SetText("This mouse stores DPI in a format the app can't read yet.")
	}
	var counts []opt
	for n := 1; n <= 8; n++ {
		counts = append(counts, opt{itoa(n), itoa(n)})
	}
	a.stageCount.setOptions(counts)
	a.stageCount.set(itoa(s.StageCount))
	a.stageCount.cb.SetEnabled(c.StageCountEditable && s.Stages != nil)
	a.showStages(s.StageCount)
	for i, e := range a.stageEdits {
		e.SetEnabled(s.Stages != nil)
		if s.Stages != nil {
			e.SetText(itoa(min(s.Stages[i], c.MaxDPI)))
		}
	}
	a.activeStage.cb.SetEnabled(s.Stages != nil)
	a.setActiveOptions(s.StageCount)
	a.activeStage.set(itoa(s.ActiveStage))

	// sensor
	var rates []opt
	for _, hz := range c.Rates {
		rates = append(rates, opt{itoa(hz), fmt.Sprintf("%d Hz", hz)})
	}
	a.combos["rate"].setOptions(rates)
	a.combos["rate"].set(itoa(s.RateHz))
	var modes []opt
	for _, m := range c.SensorModes {
		modes = append(modes, opt{itoa(m.Value), m.Label})
	}
	a.combos["mode"].setOptions(modes)
	a.combos["mode"].set(itoa(s.SensorMode))
	a.lodRow.SetVisible(c.Lod != "") // ATK HUB shows no lift-off control otherwise
	switch c.Lod {
	case "levels", "levels07":
		a.combos["lod"].setOptions(lodLevels[c.Lod])
	case "continuous":
		a.combos["lod"].setOptions(continuousLod())
	}
	a.combos["lod"].set(itoa(s.LodCode))
	if c.DebounceOff {
		a.combos["debounce"].setOptions(debounceOpts)
	} else {
		a.combos["debounce"].setOptions(debounceOpts[1:])
	}
	a.combos["debounce"].set(itoa(s.DebounceMs))
	a.combos["sleep"].setOptions(sleepOpts)
	a.combos["sleep"].set(itoa(s.SleepS))
	a.switches["motion_sync"].SetChecked(s.MotionSync)
	a.switches["angle_snap"].SetChecked(s.AngleSnap)
	a.switches["ripple"].SetChecked(s.Ripple)
	for i, b := range a.buttons {
		b.set(actionKey(s.Buttons[i]))
	}
	// backups are raw EEPROM dumps, which only COMPX mice have
	a.backupAct.SetEnabled(c.Backup)
	a.restoreAct.SetEnabled(c.Backup)
}

func (a *app) showStages(n int) {
	for i, r := range a.stageRows {
		r.SetVisible(i < n)
	}
}

func (a *app) setActiveOptions(n int) {
	var opts []opt
	for i := 0; i < n; i++ {
		opts = append(opts, opt{itoa(i), fmt.Sprintf("Stage %d", i+1)})
	}
	cur := -1
	if a.activeStage.cb.CurrentIndex() >= 0 && a.activeStage.cb.CurrentIndex() < len(a.activeStage.opts) {
		cur, _ = strconv.Atoi(a.activeStage.opts[a.activeStage.cb.CurrentIndex()].key)
	}
	a.activeStage.setOptions(opts)
	if cur >= 0 {
		was := a.loading
		a.loading = true
		a.activeStage.set(itoa(cur))
		a.loading = was
	}
}

func (a *app) onStageDPI(i int) {
	if a.loading || a.state == nil || a.mouse == nil {
		return
	}
	v, err := strconv.Atoi(strings.TrimSpace(a.stageEdits[i].Text()))
	if err != nil {
		a.toast("Type a number")
		return
	}
	dpi := core.SnapDPI(v, &a.state.Caps)
	if a.state.Stages != nil && a.state.Stages[i] == dpi {
		a.loading = true
		a.stageEdits[i].SetText(itoa(dpi)) // only snapped, nothing to write
		a.loading = false
		return
	}
	var stored int
	a.apply(func(m core.Mouse) (err error) { stored, err = m.SetStageDPI(i, dpi); return },
		fmt.Sprintf("Stage %d: %d DPI", i+1, dpi), func() {
			a.loading = true
			a.stageEdits[i].SetText(itoa(stored)) // what the mouse really stored
			a.loading = false
			a.state.Stages[i] = stored
		})
}

func (a *app) intChoice(write func(m core.Mouse, v int) error, msgFormat string) *choice {
	return a.newChoice(func(o opt) {
		v, _ := strconv.Atoi(o.key)
		a.apply(func(m core.Mouse) error { return write(m, v) }, fmt.Sprintf(msgFormat, o.label), nil)
	})
}

func (a *app) comboWidget(c *choice) Widget {
	return ComboBox{AssignTo: &c.cb, OnCurrentIndexChanged: c.changed, MinSize: Size{Width: 160}}
}

func (a *app) row(label string, w Widget) Widget {
	return Composite{Layout: HBox{MarginsZero: true}, Children: []Widget{
		Label{Text: label, MinSize: Size{Width: 170}}, HSpacer{}, w}}
}

func (a *app) buildUI() error {
	a.combos = map[string]*choice{
		"rate": a.intChoice(func(m core.Mouse, v int) error { return m.SetRate(v) }, "Polling rate %s"),
		"mode": a.intChoice(func(m core.Mouse, v int) error { return m.SetSensorMode(v) }, "Sensor mode: %s"),
		"lod":  a.intChoice(func(m core.Mouse, v int) error { return m.SetLod(v) }, "Lift-off distance %s"),
		"debounce": a.intChoice(func(m core.Mouse, v int) error { return m.SetDebounce(v) },
			"Click debounce %s"),
		"sleep": a.intChoice(func(m core.Mouse, v int) error { return m.SetSleep(v) }, "Sleeps after %s"),
	}
	a.stageCount = a.newChoice(func(o opt) {
		n, _ := strconv.Atoi(o.key)
		a.showStages(n)
		a.setActiveOptions(n)
		a.apply(func(m core.Mouse) error { return m.SetStageCount(n) }, fmt.Sprintf("%s DPI stage(s)", o.key), nil)
	})
	a.activeStage = a.newChoice(func(o opt) {
		i, _ := strconv.Atoi(o.key)
		a.apply(func(m core.Mouse) error { return m.SetActiveStage(i) }, "Active: "+o.label, nil)
	})
	var buttonOpts []opt
	for _, x := range actions {
		buttonOpts = append(buttonOpts, opt{actionKey(x.a), x.label})
	}
	for i := range a.buttons {
		i := i
		a.buttons[i] = a.newChoice(func(o opt) {
			for _, x := range actions {
				if actionKey(x.a) == o.key {
					act := x.a
					a.apply(func(m core.Mouse) error { return m.SetButton(i, act) },
						fmt.Sprintf("%s: %s", core.ButtonNames[i], o.label), nil)
				}
			}
		})
	}

	var stageWidgets []Widget
	for i := 0; i < 8; i++ {
		i := i
		stageWidgets = append(stageWidgets, Composite{AssignTo: &a.stageRows[i], Layout: HBox{MarginsZero: true},
			Children: []Widget{
				Label{Text: fmt.Sprintf("Stage %d", i+1), MinSize: Size{Width: 170}}, HSpacer{},
				LineEdit{AssignTo: &a.stageEdits[i], MinSize: Size{Width: 160}, MaxSize: Size{Width: 160},
					OnEditingFinished: func() { a.onStageDPI(i) }},
			}})
	}
	var buttonWidgets []Widget
	for i := range a.buttons {
		buttonWidgets = append(buttonWidgets, Composite{AssignTo: &a.buttonRows[i], Layout: HBox{MarginsZero: true},
			Children: []Widget{Label{Text: core.ButtonNames[i], MinSize: Size{Width: 170}}, HSpacer{},
				a.comboWidget(a.buttons[i])}})
	}
	a.switches = map[string]*walk.CheckBox{}
	var ms, as, rp *walk.CheckBox
	toggle := func(cb **walk.CheckBox, title string, write func(m core.Mouse, on bool) error) func() {
		return func() {
			if a.loading {
				return
			}
			on := (*cb).Checked()
			state := "off"
			if on {
				state = "on"
			}
			a.apply(func(m core.Mouse) error { return write(m, on) }, title+" "+state, nil)
		}
	}

	err := MainWindow{
		AssignTo: &a.mw,
		Title:    "Mouse Settings",
		MinSize:  Size{Width: 520, Height: 500},
		Size:     Size{Width: 560, Height: 760},
		Layout:   VBox{},
		MenuItems: []MenuItem{
			Menu{Text: "&Mouse", Items: []MenuItem{
				Action{Text: "&Reload", OnTriggered: func() { a.reload() }},
				Separator{},
				Action{AssignTo: &a.backupAct, Text: "Save &backup…", OnTriggered: a.onBackup},
				Action{AssignTo: &a.restoreAct, Text: "&Restore backup…", OnTriggered: a.onRestore},
				Separator{},
				Action{Text: "&Factory reset…", OnTriggered: a.onReset},
			}},
		},
		StatusBarItems: []StatusBarItem{{AssignTo: &a.statusItem, Width: 500}},
		Children: []Widget{
			Composite{Layout: HBox{MarginsZero: true}, Children: []Widget{
				Composite{Layout: VBox{MarginsZero: true, SpacingZero: true}, Children: []Widget{
					Label{AssignTo: &a.title, Text: "Mouse Settings", Font: Font{PointSize: 12, Bold: true}},
					Label{AssignTo: &a.sub, Text: "Looking for the mouse…"},
				}},
				HSpacer{},
				PushButton{Text: "Reload", OnClicked: func() { a.reload() }},
			}},
			Label{AssignTo: &a.banner, Visible: false, TextColor: walk.RGB(0xC0, 0x1C, 0x28)},
			ScrollView{AssignTo: &a.scroll, HorizontalFixed: true, Layout: VBox{}, Children: []Widget{
				Composite{AssignTo: &a.page, Layout: VBox{MarginsZero: true}, Enabled: false, Children: []Widget{
					GroupBox{Title: "Sensitivity (DPI)", Layout: VBox{}, Children: append([]Widget{
						Label{AssignTo: &a.dpiNote, Text: "To switch stages with the mouse, set a button to DPI cycle below."},
						a.row("Number of stages", a.comboWidget(a.stageCount)),
						a.row("Active stage", a.comboWidget(a.activeStage)),
					}, stageWidgets...)},
					GroupBox{Title: "Sensor", Layout: VBox{}, Children: []Widget{
						a.row("Polling rate", a.comboWidget(a.combos["rate"])),
						a.row("Sensor mode (competitive modes use more battery)", a.comboWidget(a.combos["mode"])),
						Composite{AssignTo: &a.lodRow, Layout: HBox{MarginsZero: true}, Children: []Widget{
							Label{AssignTo: &a.lodLabel, Text: "Lift-off distance", MinSize: Size{Width: 170}},
							HSpacer{}, a.comboWidget(a.combos["lod"])}},
						CheckBox{AssignTo: &ms, Alignment: AlignHNearVCenter, Text: "Motion sync (smoother tracking, about 1 ms more latency)",
							OnClicked: toggle(&ms, "Motion sync", func(m core.Mouse, on bool) error { return m.SetMotionSync(on) })},
						CheckBox{AssignTo: &as, Alignment: AlignHNearVCenter, Text: "Angle snapping (straightens lines you draw)",
							OnClicked: toggle(&as, "Angle snapping", func(m core.Mouse, on bool) error { return m.SetAngleSnap(on) })},
						CheckBox{AssignTo: &rp, Alignment: AlignHNearVCenter, Text: "Ripple control (smooths jitter at high DPI)",
							OnClicked: toggle(&rp, "Ripple control", func(m core.Mouse, on bool) error { return m.SetRipple(on) })},
					}},
					GroupBox{Title: "Clicks and power", Layout: VBox{}, Children: []Widget{
						a.row("Click debounce", a.comboWidget(a.combos["debounce"])),
						a.row("Sleep after", a.comboWidget(a.combos["sleep"])),
					}},
					GroupBox{Title: "Buttons", Layout: VBox{}, Children: buttonWidgets},
					VSpacer{},
				}},
			}},
		},
	}.Create()
	if err != nil {
		return err
	}
	a.switches["motion_sync"], a.switches["angle_snap"], a.switches["ripple"] = ms, as, rp
	all := []*choice{a.stageCount, a.activeStage}
	for _, c := range a.combos {
		all = append(all, c)
	}
	all = append(all, a.buttons[:]...)
	for _, c := range all {
		if err := walk.InitWrapperWindow(&wheelSafeCombo{ComboBox: c.cb, scroll: &a.scroll}); err != nil {
			return err
		}
	}
	for _, b := range a.buttons {
		b.setOptions(buttonOpts)
	}
	return nil
}

func (a *app) onBackup() {
	if a.mouse == nil {
		return
	}
	os.MkdirAll(core.ConfigDir(), 0o755)
	dlg := walk.FileDialog{Title: "Save backup", Filter: "Backup (*.json)|*.json",
		InitialDirPath: core.ConfigDir(), FilePath: "backup-" + time.Now().Format("20060102-150405") + ".json"}
	if ok, _ := dlg.ShowSave(a.mw); !ok {
		return
	}
	path := dlg.FilePath
	if !strings.HasSuffix(strings.ToLower(path), ".json") {
		path += ".json"
	}
	m := a.mouse
	a.run(func() error {
		dump, err := m.Backup()
		if err != nil {
			return err
		}
		b, _ := json.MarshalIndent(dump, "", " ")
		return os.WriteFile(path, b, 0o644)
	}, func(err error) {
		if err != nil {
			a.showError(err)
			return
		}
		a.toast("Backup saved")
	})
}

func (a *app) onRestore() {
	if a.mouse == nil {
		return
	}
	dlg := walk.FileDialog{Title: "Restore backup", Filter: "Backup (*.json)|*.json", InitialDirPath: core.ConfigDir()}
	if ok, _ := dlg.ShowOpen(a.mw); !ok {
		return
	}
	m := a.mouse
	var failed []string
	a.run(func() error {
		b, err := os.ReadFile(dlg.FilePath)
		if err != nil {
			return err
		}
		var dump map[string]any
		if err := json.Unmarshal(b, &dump); err != nil {
			return err
		}
		failed, err = m.Restore(dump)
		return err
	}, func(err error) {
		if err != nil {
			a.showError(err)
			return
		}
		if len(failed) == 0 {
			a.toast("Backup restored")
		} else {
			a.toast("Restored, except " + strings.Join(failed, ", "))
		}
		a.reload()
	})
}

func (a *app) onReset() {
	if a.mouse == nil {
		return
	}
	if walk.MsgBox(a.mw, "Factory reset?", "All settings on the mouse go back to factory defaults.",
		walk.MsgBoxYesNo|walk.MsgBoxIconWarning) != walk.DlgCmdYes {
		return
	}
	m := a.mouse
	a.run(func() error {
		err := m.FactoryReset()
		time.Sleep(time.Second)
		return err
	}, func(err error) {
		if err != nil {
			a.showError(err)
			return
		}
		a.toast("Factory reset done")
		a.reload()
	})
}

// dump: hidden check mode, `vxe-atk.exe -dump file.json`, writes what the app reads.
func dump(path string) {
	out := map[string]any{}
	reg := core.GetRegistry(false)
	m, t, err := openMouse(reg)
	if err == nil {
		defer t.Close()
		out["family"] = m.Family()
		out["receiver"] = m.IsReceiver()
		if st, err2 := m.ReadState(); err2 == nil {
			out["state"] = st
		} else {
			err = err2
		}
	}
	if err != nil {
		out["error"] = err.Error()
	}
	b, _ := json.MarshalIndent(out, "", " ")
	os.WriteFile(path, b, 0o644)
}

func main() {
	if len(os.Args) == 3 && os.Args[1] == "-dump" {
		dump(os.Args[2])
		return
	}
	a := &app{jobs: make(chan func(), 64), demo: len(os.Args) >= 2 && (os.Args[1] == "-demo" || os.Args[1] == "-wheeltest")}
	go func() {
		for job := range a.jobs {
			job()
		}
	}()
	if err := a.buildUI(); err != nil {
		walk.MsgBox(nil, "Mouse Settings", err.Error(), walk.MsgBoxIconError)
		return
	}
	a.connect()
	go a.poll()
	if len(os.Args) == 3 && os.Args[1] == "-wheeltest" {
		go a.wheelTest(os.Args[2])
	}
	a.mw.Run()
	if a.transport != nil {
		a.transport.Close()
	}
}

// wheelTest (hidden, demo data): sends mouse-wheel messages to every drop-down
// and records whether any selection changed. Writes the result to a file.
func (a *app) wheelTest(path string) {
	time.Sleep(3 * time.Second)
	a.mw.Synchronize(func() {
		all := []*choice{a.stageCount, a.activeStage, a.combos["rate"], a.combos["mode"], a.combos["lod"],
			a.combos["debounce"], a.combos["sleep"]}
		all = append(all, a.buttons[:]...)
		var report []string
		changed := 0
		for _, c := range all {
			before := c.cb.CurrentIndex()
			c.cb.SetFocus()
			for _, delta := range []int16{-120, -120, 120, -120} {
				w := uintptr(uint32(uint16(delta)) << 16)
				win.SendMessage(c.cb.Handle(), win.WM_MOUSEWHEEL, w, 0)
			}
			if c.cb.CurrentIndex() != before {
				changed++
				report = append(report, fmt.Sprintf("CHANGED: %d -> %d", before, c.cb.CurrentIndex()))
			}
		}
		report = append(report, fmt.Sprintf("drop-downs tested: %d, changed by the wheel: %d", len(all), changed))
		os.WriteFile(path, []byte(strings.Join(report, "\n")+"\n"), 0o644)
		a.mw.Close()
	})
}
