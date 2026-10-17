// SPDX-License-Identifier: MIT
// Ported from atk.py (lights); keep the two in step.

package core

import "fmt"

// Light is the mouse's DPI light: Mode "off"/"steady"/"breathing", Brightness
// and Speed as 0 (low/slow), 1 or 2 (high/fast). Each family stores its own raw values.
type Light struct {
	Mode              string
	Brightness, Speed int
}

var LightModes = []string{"off", "steady", "breathing"}

// ReceiverLight is a receiver's own light. Kind "full" takes an effect, colour,
// brightness and speed (Sleep is -1 where ATK HUB has no timer); "simple" only
// shows a status, so just Mode applies.
type ReceiverLight struct {
	Kind                     string
	Mode                     int
	Color                    string
	Brightness, Speed, Sleep int
}

type LightMode struct {
	Value int
	Label string
}

// Receiver light modes as ATK HUB offers them.
var RxFullModes = []LightMode{{0, "Off"}, {1, "Colour flow"}, {2, "Breathing, one colour"},
	{3, "Steady"}, {4, "Neon"}, {5, "Breathing, mixed colours"}, {6, "Flashing"}}
var RxSimpleModes = []LightMode{{0, "Off"}, {1, "Show polling rate"}, {2, "Show battery level"},
	{3, "Low battery warning"}}

// Modes where ATK HUB greys out each control.
var RxNoSpeed = map[int]bool{0: true, 3: true, 6: true}
var RxNoBrightness = map[int]bool{0: true}
var RxColor = map[int]bool{2: true, 3: true}

// receiverLightKind: "full", "simple" or "" from the receiver's ATK HUB flags.
// gatherRgb receivers use another mode list that isn't supported yet.
func receiverLightKind(flags []string) string {
	if contains(flags, "noDongleRGB") || contains(flags, "gatherRgb") {
		return ""
	}
	if contains(flags, "singleRgb") {
		return "simple"
	}
	return "full"
}

// nearestLevel: the level whose raw value is closest to what the device stores.
func nearestLevel(raw int, table [3]int) int {
	best := 0
	for i, v := range table {
		if abs(v-raw) < abs(table[best]-raw) {
			best = i
		}
	}
	return best
}

func abs(v int) int {
	if v < 0 {
		return -v
	}
	return v
}

func rgbHex(r, g, b int) string { return fmt.Sprintf("#%02x%02x%02x", r, g, b) }

func hexRGB(c string) [3]int {
	var r, g, b int
	fmt.Sscanf(c, "#%02x%02x%02x", &r, &g, &b)
	return [3]int{r, g, b}
}
