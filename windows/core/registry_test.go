package core

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
)

// Runs only with ATKHUB_SNAPSHOT set to a folder of ATK HUB scripts and
// ATKHUB_EXPECTED to atk.py's parse_device_tables() output for the same
// files in sorted order. ATK's files are not part of this repo.
func TestParseDeviceTablesMatchesPython(t *testing.T) {
	dir, want := os.Getenv("ATKHUB_SNAPSHOT"), os.Getenv("ATKHUB_EXPECTED")
	if dir == "" || want == "" {
		t.Skip("no ATK HUB snapshot")
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*.js"))
	sort.Strings(files)
	var scripts []string
	for _, f := range files {
		b, _ := os.ReadFile(f)
		scripts = append(scripts, string(b))
	}
	got, _ := json.Marshal(ParseDeviceTables(scripts))
	exp, _ := os.ReadFile(want)
	var a, b any
	json.Unmarshal(got, &a)
	json.Unmarshal(exp, &b)
	if !reflect.DeepEqual(a, b) {
		t.Fatal("Go and Python device tables differ")
	}
	reg := ParseDeviceTables(scripts)
	t.Logf("%d COMPX, %d BITMOUSE, %d receivers, %d recovery", len(reg.Compx.Cid), len(reg.Bitmouse.Cid),
		len(reg.Receivers), len(reg.Recovery))
}

// Runs only with ATK_LIVE_COMPARE set to the Linux app's devices.json cache.
func TestFetchRegistryLive(t *testing.T) {
	path := os.Getenv("ATK_LIVE_COMPARE")
	if path == "" {
		t.Skip("live download not requested")
	}
	reg, err := FetchRegistry()
	if err != nil {
		t.Fatal(err)
	}
	var cache struct {
		Registry json.RawMessage `json:"registry"`
	}
	b, _ := os.ReadFile(path)
	json.Unmarshal(b, &cache)
	got, _ := json.Marshal(reg)
	var a, c any
	json.Unmarshal(got, &a)
	json.Unmarshal(cache.Registry, &c)
	if !reflect.DeepEqual(a, c) {
		t.Fatal("Go download differs from the Linux app's list")
	}
}
