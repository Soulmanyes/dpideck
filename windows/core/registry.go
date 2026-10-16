// SPDX-License-Identifier: MIT
// Ported from atk.py (device list and Competitive Max table); keep the two in step.

package core

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Which sensor each mouse has comes from ATK HUB's own device tables. They are
// not shipped: the app downloads ATK HUB's public web app and reads the tables
// out of it as text (the JavaScript is never run), cached for 30 days.
const (
	HubURL        = "https://hub.atk.pro/"
	hubCDN        = "https://bpcdn.atkgear.com/"
	devicesMaxAge = 30 * 24 * time.Hour
	downloadLimit = 64 << 20
	UpgradeURL    = "https://bpcdn.atkgear.com/device/constant/mouse-upgrade.json"
	upgradeMaxAge = 7 * 24 * time.Hour

	recoveryUsagePage = 0xFF02 // ATK HUB lists mice in firmware-recovery mode under it
)

var familyByUsagePage = map[int]string{0xFF04: "compx", 0xFF05: "bitmouse"}
var keepFlags = map[string]bool{"noBottomButton": true, "noZeroKeyDebounce": true, "noAthleticsMax": true}

// Entry is [name, sensor, flags], stored as a JSON array like the Linux cache.
type Entry struct {
	Name, Sensor string
	Flags        []string
}

func (e Entry) MarshalJSON() ([]byte, error) {
	flags := e.Flags
	if flags == nil {
		flags = []string{}
	}
	return json.Marshal([]any{e.Name, e.Sensor, flags})
}

func (e *Entry) UnmarshalJSON(b []byte) error {
	var raw []json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil || len(raw) != 3 {
		return errors.New("bad registry entry")
	}
	if err := json.Unmarshal(raw[0], &e.Name); err != nil {
		return err
	}
	if err := json.Unmarshal(raw[1], &e.Sensor); err != nil {
		return err
	}
	return json.Unmarshal(raw[2], &e.Flags)
}

type FamilyTable struct {
	Cid map[string]Entry `json:"cid"`
	Pid map[string]Entry `json:"pid"`
}

type Registry struct {
	Compx     FamilyTable       `json:"compx"`
	Bitmouse  FamilyTable       `json:"bitmouse"`
	Receivers map[string]string `json:"receivers"`
	Recovery  []string          `json:"recovery"`
}

func emptyRegistry() *Registry {
	return &Registry{
		Compx:     FamilyTable{map[string]Entry{}, map[string]Entry{}},
		Bitmouse:  FamilyTable{map[string]Entry{}, map[string]Entry{}},
		Receivers: map[string]string{}, Recovery: []string{},
	}
}

func (r *Registry) family(f string) *FamilyTable {
	if f == "compx" {
		return &r.Compx
	}
	return &r.Bitmouse
}

func usbKey(vid, pid uint16) string { return fmt.Sprintf("%04x:%04x", vid, pid) }

// Lookup: by CID/MID first, then by USB ID (cable).
func (r *Registry) Lookup(family, key string, vid, pid uint16) Ident {
	fam := r.family(family)
	usb := usbKey(vid, pid)
	receiver := r.Receivers[usb]
	e, ok := fam.Cid[key]
	if !ok || key == "" {
		e, ok = fam.Pid[usb]
	}
	if ok {
		return Ident{Key: key, Name: e.Name, Sensor: e.Sensor, Flags: e.Flags, Receiver: receiver}
	}
	k := key
	if k == "" {
		k = "no ID"
	}
	return Ident{Key: key, Name: fmt.Sprintf("Unknown ATK mouse (%s)", k), Receiver: receiver}
}

func (r *Registry) IsReceiver(vid, pid uint16) bool {
	_, ok := r.Receivers[usbKey(vid, pid)]
	return ok
}

func (r *Registry) IsRecovery(vid, pid uint16) bool {
	usb := usbKey(vid, pid)
	for _, u := range r.Recovery {
		if u == usb {
			return true
		}
	}
	return false
}

var (
	reVendor  = regexp.MustCompile(`^\{vendorId:(\d+)`)
	reProduct = regexp.MustCompile(`productId:(\d+)`)
	reUsage   = regexp.MustCompile(`usagePage:(\d+)`)
	reName    = regexp.MustCompile(`name:"([^"]*)"`)
	reSensor  = regexp.MustCompile(`sensor:[A-Za-z_$]+\.(\w+)`)
	reFlag    = regexp.MustCompile(`(\w+):!0`)
	reCidMid  = regexp.MustCompile(`mouseCidMid:"(\d+,\d+)"`)
	reEntry   = regexp.MustCompile(`src="(https://bpcdn\.atkgear\.com/[^"]+/static/index-[^"]+\.js)"`)
	reDep     = regexp.MustCompile(`"(?:\./|static/)([\w.-]+\.js)"`)
	reRateK   = regexp.MustCompile(`(?i)(\d)\s*k\b`)
)

// filterObjects returns the text of every {vendorId:...} device filter in a script.
func filterObjects(src string) []string {
	var out []string
	for _, loc := range regexp.MustCompile(`\{vendorId:`).FindAllStringIndex(src, -1) {
		depth := 0
		end := min(len(src), loc[0]+20000)
		for j := loc[0]; j < end; j++ {
			switch src[j] {
			case '{':
				depth++
			case '}':
				depth--
			}
			if depth == 0 {
				out = append(out, src[loc[0]:j+1])
				break
			}
		}
	}
	return out
}

// ParseDeviceTables builds the registry from ATK HUB's script texts.
func ParseDeviceTables(scripts []string) *Registry {
	reg := emptyRegistry()
	type famUSB struct{ fam, usb string }
	seenPid := map[famUSB][]Entry{}
	var order []famUSB
	for _, src := range scripts {
		for _, o := range filterObjects(src) {
			v, p, page := reVendor.FindStringSubmatch(o), reProduct.FindStringSubmatch(o), reUsage.FindStringSubmatch(o)
			if v == nil || p == nil || page == nil {
				continue
			}
			vid, _ := strconv.Atoi(v[1])
			pid, _ := strconv.Atoi(p[1])
			pg, _ := strconv.Atoi(page[1])
			usb := fmt.Sprintf("%04x:%04x", vid, pid)
			if pg == recoveryUsagePage && !strings.Contains(o, "sensor:") {
				reg.Recovery = append(reg.Recovery, usb)
				continue
			}
			family, ok := familyByUsagePage[pg]
			if !ok {
				continue
			}
			name := ""
			if n := reName.FindStringSubmatch(o); n != nil {
				name = strings.TrimSpace(n[1])
			}
			if strings.Contains(o, "receiver:!0") {
				reg.Receivers[usb] = name
				continue
			}
			sensor := reSensor.FindStringSubmatch(o)
			if sensor == nil {
				continue // other ATK products (docks, monitors...) share the interface
			}
			flagSet := map[string]bool{}
			for _, f := range reFlag.FindAllStringSubmatch(o, -1) {
				if keepFlags[f[1]] {
					flagSet[f[1]] = true
				}
			}
			flags := []string{}
			for f := range flagSet {
				flags = append(flags, f)
			}
			sort.Strings(flags)
			e := Entry{name, sensor[1], flags}
			if cm := reCidMid.FindStringSubmatch(o); cm != nil {
				if _, exists := reg.family(family).Cid[cm[1]]; !exists {
					reg.family(family).Cid[cm[1]] = e
				}
			}
			k := famUSB{family, usb}
			if _, seen := seenPid[k]; !seen {
				order = append(order, k)
			}
			seenPid[k] = append(seenPid[k], e)
		}
	}
	// a USB ID shared by models with different sensors can't identify the sensor
	for _, k := range order {
		entries := seenPid[k]
		same := true
		for _, e := range entries {
			same = same && e.Sensor == entries[0].Sensor
		}
		if same {
			reg.family(k.fam).Pid[k.usb] = entries[0]
		}
	}
	sort.Strings(reg.Recovery)
	reg.Recovery = uniq(reg.Recovery)
	return reg
}

func uniq(s []string) []string {
	out := s[:0]
	for i, v := range s {
		if i == 0 || v != s[i-1] {
			out = append(out, v)
		}
	}
	return out
}

var httpClient = &http.Client{Timeout: 10 * time.Second}

func get(url string, budget *int) (string, error) {
	if !strings.HasPrefix(url, hubCDN) && !strings.HasPrefix(url, HubURL) {
		return "", fmt.Errorf("refusing to download %s", url)
	}
	resp, err := httpClient.Get(url)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("%s: %s", url, resp.Status)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, int64(*budget)+1))
	if err != nil {
		return "", err
	}
	*budget -= len(data)
	if *budget < 0 {
		return "", errors.New("ATK HUB download larger than expected")
	}
	return string(data), nil
}

// FetchRegistry downloads ATK HUB and extracts its device tables.
func FetchRegistry() (*Registry, error) {
	budget := downloadLimit
	page, err := get(HubURL, &budget)
	if err != nil {
		return nil, err
	}
	m := reEntry.FindStringSubmatch(page)
	if m == nil {
		return nil, errors.New("ATK HUB page layout not recognised")
	}
	base := m[1][:strings.LastIndex(m[1], "/static/")] + "/"
	todo, done, scripts := []string{m[1]}, map[string]bool{}, []string(nil)
	for len(todo) > 0 {
		url := todo[len(todo)-1]
		todo = todo[:len(todo)-1]
		if done[url] {
			continue
		}
		done[url] = true
		src, err := get(url, &budget)
		if err != nil {
			if budget < 0 {
				return nil, err
			}
			continue // one missing chunk shouldn't stop the rest
		}
		scripts = append(scripts, src)
		for _, d := range reDep.FindAllStringSubmatch(src, -1) {
			todo = append(todo, base+"static/"+d[1])
		}
	}
	reg := ParseDeviceTables(scripts)
	if len(reg.Compx.Cid)+len(reg.Bitmouse.Cid) < 20 {
		return nil, errors.New("ATK HUB device tables not found")
	}
	return reg, nil
}

// ConfigDir: %AppData%\atk-mouse on Windows, ~/.config/atk-mouse on Linux.
func ConfigDir() string {
	d, err := os.UserConfigDir()
	if err != nil {
		d = "."
	}
	return filepath.Join(d, "atk-mouse")
}

type cacheFile[T any] struct {
	Fetched  float64 `json:"fetched"`
	Registry *T      `json:"registry,omitempty"`
	Table    *T      `json:"table,omitempty"`
}

var (
	regMu     sync.Mutex
	registry  *Registry
	upgradeMu sync.Mutex
	upgrade   map[string]string
)

// GetRegistry: cached copy, else a fresh download, else an empty list.
func GetRegistry(refresh bool) *Registry {
	regMu.Lock()
	defer regMu.Unlock()
	if registry != nil && !refresh {
		return registry
	}
	path := filepath.Join(ConfigDir(), "devices.json")
	var cached cacheFile[Registry]
	haveCache := false
	if b, err := os.ReadFile(path); err == nil && json.Unmarshal(b, &cached) == nil && cached.Registry != nil {
		haveCache = true
		age := time.Since(time.Unix(int64(cached.Fetched), 0))
		if !refresh && age < devicesMaxAge {
			registry = cached.Registry
			return registry
		}
	}
	if reg, err := FetchRegistry(); err == nil {
		os.MkdirAll(ConfigDir(), 0o755)
		if b, err := json.Marshal(cacheFile[Registry]{Fetched: float64(time.Now().Unix()), Registry: reg}); err == nil {
			os.WriteFile(path, b, 0o644)
		}
		registry = reg
	} else if haveCache {
		registry = cached.Registry // offline or ATK changed its site: keep the old copy
	} else {
		registry = emptyRegistry()
	}
	return registry
}

// CompetitiveMaxTable: {"cid,mid": "min firmware"} for COMPX mice, from ATK's
// public config; cached for a week, empty if never fetched and offline.
func CompetitiveMaxTable() map[string]string {
	upgradeMu.Lock()
	defer upgradeMu.Unlock()
	if upgrade != nil {
		return upgrade
	}
	path := filepath.Join(ConfigDir(), "competitive-max.json")
	var cached cacheFile[map[string]string]
	haveCache := false
	if b, err := os.ReadFile(path); err == nil && json.Unmarshal(b, &cached) == nil && cached.Table != nil {
		haveCache = true
		if time.Since(time.Unix(int64(cached.Fetched), 0)) < upgradeMaxAge {
			upgrade = *cached.Table
			return upgrade
		}
	}
	table, err := fetchUpgrade()
	if err == nil {
		os.MkdirAll(ConfigDir(), 0o755)
		if b, err := json.Marshal(cacheFile[map[string]string]{Fetched: float64(time.Now().Unix()), Table: &table}); err == nil {
			os.WriteFile(path, b, 0o644)
		}
		upgrade = table
	} else if haveCache {
		upgrade = *cached.Table
	} else {
		upgrade = map[string]string{}
	}
	return upgrade
}

func fetchUpgrade() (map[string]string, error) {
	c := &http.Client{Timeout: 3 * time.Second}
	resp, err := c.Get(UpgradeURL)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var all map[string]map[string]any
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&all); err != nil {
		return nil, err
	}
	compx, ok := all["3"]
	if !ok {
		return nil, errors.New("no COMPX section")
	}
	table := map[string]string{}
	for k, v := range compx {
		if obj, ok := v.(map[string]any); ok {
			if s, ok := obj["cordedMode"].(string); ok {
				table[k] = s
			}
		}
	}
	return table, nil
}

// UsbMaxRate: the highest polling rate to offer. The Linux app also reads the
// USB link speed; Windows has no cheap way, so a receiver named "4K"/"8K" sets
// the cap and anything else gets 1000 Hz (or the mouse's current rate if higher).
func UsbMaxRate(receiverName string) int {
	if m := reRateK.FindStringSubmatch(receiverName); m != nil {
		n, _ := strconv.Atoi(m[1])
		return n * 1000
	}
	return 1000
}
