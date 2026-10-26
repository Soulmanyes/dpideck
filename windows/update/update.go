// SPDX-License-Identifier: CC0-1.0
// Package update checks DPIdeck's GitHub releases and swaps in a newer DPIdeck.exe.
// It is the Windows twin of updater.py: the same state file keys, the same daily
// check and the same "installed"/"failed" tags that stop reinstall loops. No GUI code,
// so its tests run on Linux.
package update

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	CheckEvery = 24 * time.Hour // between automatic checks
	Asset      = "DPIdeck-windows.zip"
	exeName    = "DPIdeck.exe"
)

var client = &http.Client{Timeout: 60 * time.Second}

// Release is the latest release, as remembered in the state file.
type Release struct {
	Tag   string `json:"tag"`
	URL   string `json:"url"`
	Notes string `json:"notes"`
	Zip   string `json:"zip,omitempty"` // download URL of DPIdeck-windows.zip; empty if missing
}

type state struct {
	Auto      bool     `json:"auto"`
	Checked   float64  `json:"checked,omitempty"` // Unix seconds, as updater.py writes it
	Latest    *Release `json:"latest,omitempty"`
	Installed string   `json:"installed,omitempty"`
	Failed    string   `json:"failed,omitempty"`
}

type Updater struct {
	Repo      string // "Owner/name" on GitHub
	Version   string // the running version, "2.2"; "dev" for a build without one
	Exe       string // the running DPIdeck.exe
	StateFile string
	API       string // GitHub API base; tests point it at a local server
	st        state
}

func New(repo, version, exe, stateFile string) *Updater {
	u := &Updater{Repo: repo, Version: version, Exe: exe, StateFile: stateFile,
		API: "https://api.github.com"}
	if b, err := os.ReadFile(stateFile); err == nil {
		json.Unmarshal(b, &u.st)
	}
	return u
}

func (u *Updater) save() error {
	if err := os.MkdirAll(filepath.Dir(u.StateFile), 0o755); err != nil {
		return err
	}
	b, _ := json.MarshalIndent(u.st, "", "  ")
	tmp := u.StateFile + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, u.StateFile)
}

func (u *Updater) Auto() bool { return u.st.Auto }

func (u *Updater) SetAuto(on bool) {
	u.st.Auto = on
	u.save()
}

// DevBuild: built without a version (go run, a plain go build). It only reports updates.
func (u *Updater) DevBuild() bool { return ParseVersion(u.Version) == nil }

// Installed: already swapped in by an earlier update, waiting for a restart.
func (u *Updater) Installed(r *Release) bool { return u.st.Installed == r.Tag }

// Failed: its install failed before. Auto mode doesn't retry it on every start.
func (u *Updater) Failed(r *Release) bool { return u.st.Failed == r.Tag }

// ParseVersion: "v2.1" or "2.1.0" -> [2 1 0]; nil if it isn't a plain version number.
func ParseVersion(s string) []int {
	parts := strings.Split(strings.TrimLeft(strings.TrimSpace(s), "vV"), ".")
	var nums []int
	for _, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 {
			return nil
		}
		nums = append(nums, n)
	}
	for len(nums) < 3 {
		nums = append(nums, 0)
	}
	return nums
}

func newer(mine, theirs []int) bool {
	for i := 0; i < len(mine) || i < len(theirs); i++ {
		a, b := 0, 0
		if i < len(mine) {
			a = mine[i]
		}
		if i < len(theirs) {
			b = theirs[i]
		}
		if a != b {
			return b > a
		}
	}
	return false
}

func (u *Updater) newer(r *Release) bool {
	mine, theirs := ParseVersion(u.Version), ParseVersion(r.Tag)
	if theirs == nil {
		return false
	}
	return mine == nil || newer(mine, theirs) // a dev build hears about every release
}

func get(url string) (*http.Response, error) {
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "dpideck")
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("%s: %s", url, resp.Status)
	}
	return resp, nil
}

// Check returns the latest release if it is newer than this version, else nil.
// It asks GitHub only when forced or a day after the last check. An error means
// GitHub couldn't be reached or answered oddly, so callers can stay quiet.
func (u *Updater) Check(force bool) (*Release, error) {
	r := u.st.Latest
	checked := time.Unix(int64(u.st.Checked), 0)
	if force || r == nil || time.Since(checked) > CheckEvery {
		resp, err := get(u.API + "/repos/" + u.Repo + "/releases/latest")
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		var data struct {
			Tag    string `json:"tag_name"`
			URL    string `json:"html_url"`
			Body   string `json:"body"`
			Assets []struct {
				Name string `json:"name"`
				URL  string `json:"browser_download_url"`
			} `json:"assets"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
			return nil, err
		}
		if data.Tag == "" {
			return nil, errors.New("GitHub's answer has no release tag")
		}
		r = &Release{Tag: data.Tag, URL: data.URL, Notes: data.Body}
		for _, a := range data.Assets {
			if a.Name == Asset {
				r.Zip = a.URL
			}
		}
		u.st.Latest, u.st.Checked = r, float64(time.Now().Unix())
		u.save()
	}
	if u.newer(r) {
		return r, nil
	}
	return nil, nil
}

// Install downloads the release's zip and puts its DPIdeck.exe in place of the
// running one, which Windows lets you rename but not overwrite. The old file
// stays as DPIdeck.exe.old until CleanOld on the next start. The error is a
// message for the user.
func (u *Updater) Install(r *Release) error {
	if err := u.install(r); err != nil {
		u.st.Failed = r.Tag
		u.save()
		return err
	}
	u.st.Installed, u.st.Failed = r.Tag, ""
	u.save()
	return nil
}

func (u *Updater) install(r *Release) error {
	if u.DevBuild() {
		return errors.New("this is a development build. Build the new version yourself")
	}
	if r.Zip == "" {
		return fmt.Errorf("release %s has no %s", r.Tag, Asset)
	}
	resp, err := get(r.Zip)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 200<<20))
	if err != nil {
		return err
	}
	exe, err := exeFromZip(data)
	if err != nil {
		return err
	}
	dir := filepath.Dir(u.Exe)
	next := u.Exe + ".new"
	if err := os.WriteFile(next, exe, 0o755); err != nil {
		return fmt.Errorf("can't write to %s. Download the new version by hand", dir)
	}
	old := u.Exe + ".old"
	os.Remove(old)
	if err := os.Rename(u.Exe, old); err != nil {
		os.Remove(next)
		return fmt.Errorf("can't replace %s: %v", u.Exe, err)
	}
	if err := os.Rename(next, u.Exe); err != nil {
		os.Rename(old, u.Exe) // put the running version back
		os.Remove(next)
		return fmt.Errorf("can't replace %s: %v", u.Exe, err)
	}
	return nil
}

func exeFromZip(data []byte) ([]byte, error) {
	z, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, fmt.Errorf("couldn't unpack the release: %v", err)
	}
	for _, f := range z.File {
		if f.Name != exeName {
			continue
		}
		rc, err := f.Open()
		if err != nil {
			return nil, err
		}
		defer rc.Close()
		b, err := io.ReadAll(rc)
		if err != nil {
			return nil, fmt.Errorf("couldn't unpack the release: %v", err)
		}
		if len(b) < 2 || b[0] != 'M' || b[1] != 'Z' {
			return nil, errors.New("the release's DPIdeck.exe isn't a program")
		}
		return b, nil
	}
	return nil, fmt.Errorf("the release download has no %s", exeName)
}

// CleanOld removes the DPIdeck.exe.old an update left behind.
func (u *Updater) CleanOld() { os.Remove(u.Exe + ".old") }
