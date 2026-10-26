// SPDX-License-Identifier: CC0-1.0
package update

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func zipWith(name string, body []byte) []byte {
	var buf bytes.Buffer
	z := zip.NewWriter(&buf)
	w, _ := z.Create(name)
	w.Write(body)
	z.Close()
	return buf.Bytes()
}

func server(t *testing.T, tag string, zipData []byte) *httptest.Server {
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/repos/o/dpideck/releases/latest":
			assets := []map[string]string{}
			if zipData != nil {
				assets = append(assets, map[string]string{"name": Asset,
					"browser_download_url": srv.URL + "/dl.zip"})
			}
			json.NewEncoder(w).Encode(map[string]any{"tag_name": tag, "html_url": "x",
				"body": "notes", "assets": assets})
		case "/dl.zip":
			w.Write(zipData)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func setup(t *testing.T, version, tag string, zipData []byte) (*Updater, string) {
	dir := t.TempDir()
	exe := filepath.Join(dir, "DPIdeck.exe")
	os.WriteFile(exe, []byte("MZold"), 0o755)
	u := New("o/dpideck", version, exe, filepath.Join(dir, "cfg", "updates.json"))
	u.API = server(t, tag, zipData).URL
	return u, exe
}

func TestVersions(t *testing.T) {
	for _, c := range []struct {
		mine, theirs string
		want         bool
	}{{"2.1", "v2.2", true}, {"2.2", "v2.2", false}, {"2.2", "v2.1.9", false},
		{"2.2", "v2.10", true}, {"2.2", "nightly", false}, {"dev", "v2.2", true}} {
		u := &Updater{Version: c.mine}
		if got := u.newer(&Release{Tag: c.theirs}); got != c.want {
			t.Errorf("%s vs %s: %v", c.mine, c.theirs, got)
		}
	}
}

func TestCheckAndInstall(t *testing.T) {
	u, exe := setup(t, "2.1", "v2.2", zipWith("DPIdeck.exe", []byte("MZnew")))
	r, err := u.Check(false)
	if err != nil || r == nil || r.Tag != "v2.2" || r.Zip == "" {
		t.Fatalf("check: %+v %v", r, err)
	}
	if err := u.Install(r); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(exe); string(b) != "MZnew" {
		t.Fatalf("exe = %q", b)
	}
	if b, _ := os.ReadFile(exe + ".old"); string(b) != "MZold" {
		t.Fatalf("old = %q", b)
	}
	// state survives a restart: installed, not failed, auto kept
	u.SetAuto(true)
	v := New(u.Repo, "2.1", exe, u.StateFile)
	if !v.Installed(r) || v.Failed(r) || !v.Auto() {
		t.Fatalf("state %+v", v.st)
	}
	v.CleanOld()
	if _, err := os.Stat(exe + ".old"); !os.IsNotExist(err) {
		t.Fatal(".old not removed")
	}
	// the cached answer is used within a day, even with GitHub gone
	v.API = "http://127.0.0.1:1"
	if r2, err := v.Check(false); err != nil || r2 == nil {
		t.Fatalf("cached check: %v %v", r2, err)
	}
	if _, err := v.Check(true); err == nil {
		t.Fatal("forced check with GitHub gone should fail")
	}
}

func TestInstallFailures(t *testing.T) {
	for name, data := range map[string][]byte{
		"no asset":  nil,
		"not a zip": []byte("garbage"),
		"no exe":    zipWith("readme.txt", []byte("x")),
		"not exe":   zipWith("DPIdeck.exe", []byte("hello")),
	} {
		u, exe := setup(t, "2.1", "v2.2", data)
		r, err := u.Check(true)
		if err != nil || r == nil {
			t.Fatalf("%s: check %v", name, err)
		}
		if err := u.Install(r); err == nil {
			t.Errorf("%s: install should fail", name)
		}
		if !u.Failed(r) {
			t.Errorf("%s: not marked failed", name)
		}
		if b, _ := os.ReadFile(exe); string(b) != "MZold" {
			t.Errorf("%s: exe changed to %q", name, b)
		}
		if _, err := os.Stat(exe + ".new"); !os.IsNotExist(err) {
			t.Errorf("%s: .new left behind", name)
		}
	}
}

func TestDevBuildNeverInstalls(t *testing.T) {
	u, exe := setup(t, "dev", "v2.2", zipWith("DPIdeck.exe", []byte("MZnew")))
	r, _ := u.Check(true)
	if r == nil || !u.DevBuild() || u.Install(r) == nil {
		t.Fatal("dev build should report but not install")
	}
	if b, _ := os.ReadFile(exe); string(b) != "MZold" {
		t.Fatal("dev build exe replaced")
	}
}
