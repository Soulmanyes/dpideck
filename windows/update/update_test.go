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
	"strings"
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

func TestWhatsNew(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[{"tag_name":"v2.6","body":"- six\r\n\r\n**Full Changelog**: x"},
			{"tag_name":"v2.5","body":"five"},{"tag_name":"v2.4","body":"four"},
			{"tag_name":"v2.5.5","body":""},{"tag_name":"v2.7","body":"later","draft":true}]`))
	}))
	defer srv.Close()
	cases := []struct {
		name, state, version string
		want                 []string
		seen                 string
	}{
		{"fresh", `{}`, "2.6", nil, "2.6"},
		{"in-app update before this", `{"installed":"v2.6"}`, "2.6", []string{"v2.6"}, ""},
		{"skipped one", `{"seen":"2.4"}`, "2.6", []string{"v2.6", "v2.5"}, "2.4"},
		{"same", `{"seen":"2.6"}`, "2.6", nil, "2.6"},
		{"downgrade", `{"seen":"2.7"}`, "2.6", nil, "2.6"},
		{"no notes", `{"seen":"2.5"}`, "2.5.5", nil, "2.5.5"},
		{"dev build", `{}`, "dev", nil, ""},
	}
	for _, c := range cases {
		f := filepath.Join(t.TempDir(), "updates.json")
		os.WriteFile(f, []byte(c.state), 0o644)
		u := New("O/r", c.version, "x.exe", f)
		u.API = srv.URL
		got, err := u.WhatsNew()
		if err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
		var tags []string
		for _, r := range got {
			tags = append(tags, r.Tag)
		}
		if strings.Join(tags, ",") != strings.Join(c.want, ",") || u.st.Seen != c.seen {
			t.Errorf("%s: got %v seen %q, want %v seen %q", c.name, tags, u.st.Seen, c.want, c.seen)
		}
		if len(got) > 0 && got[0].Notes != "- six" {
			t.Errorf("%s: notes %q", c.name, got[0].Notes)
		}
	}
	if p := PlainNotes("## Hi\n- **Bold** `c` [l](http://a)"); p != "Hi\r\n• Bold c l" {
		t.Errorf("PlainNotes: %q", p)
	}
}
