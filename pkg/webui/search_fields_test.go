package webui

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"testing"
)

func uiGet(t *testing.T, base string) (int, uiSettings) {
	t.Helper()
	resp, err := http.Get(base + "/v1/enginex/ui")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var v uiSettings
	if err := json.NewDecoder(resp.Body).Decode(&v); err != nil {
		t.Fatal(err)
	}
	return resp.StatusCode, v
}

func TestUISearchFieldsDefaultAndPersistence(t *testing.T) {
	dir := t.TempDir()
	cfg := Config{AccessToken: "code", UIDir: dir, StateDir: t.TempDir()}
	srv := httptest.NewServer(New(cfg, nil))
	defer srv.Close()

	wantDefault := []string{"song", "artist"}
	if code, v := uiGet(t, srv.URL); code != 200 || !slices.Equal(v.SearchFields, wantDefault) {
		t.Fatalf("default: status %d, searchFields %v, want %v", code, v.SearchFields, wantDefault)
	}
	if _, err := os.Stat(filepath.Join(dir, "settings.conf")); err == nil {
		t.Fatalf("default GET unexpectedly created settings.conf")
	}

	// Out-of-order + duplicate roles canonicalize into Search.qml menuRoles order.
	wantCustom := []string{"song", "key", "comment", "bpm"}
	if code, v := uiPost(t, srv.URL, "searchFields=bpm,key,song,comment,song"); code != 200 || !slices.Equal(v.SearchFields, wantCustom) {
		t.Fatalf("custom save: status %d, searchFields %v, want %v", code, v.SearchFields, wantCustom)
	}
	data, err := os.ReadFile(filepath.Join(dir, "settings.conf"))
	if err != nil || !slices.Contains(splitLines(data), "search_fields=song,key,comment,bpm") {
		t.Fatalf("settings.conf missing search_fields: %q %v", data, err)
	}

	// Survives server restart and unrelated saves.
	again := httptest.NewServer(New(cfg, nil))
	defer again.Close()
	if code, v := uiGet(t, again.URL); code != 200 || !slices.Equal(v.SearchFields, wantCustom) {
		t.Fatalf("after restart: status %d, searchFields %v, want %v", code, v.SearchFields, wantCustom)
	}
	if code, v := uiPost(t, again.URL, "waveform=rgb"); code != 200 || !slices.Equal(v.SearchFields, wantCustom) {
		t.Fatalf("after unrelated save: status %d, searchFields %v, want %v", code, v.SearchFields, wantCustom)
	}
	data, err = os.ReadFile(filepath.Join(dir, "settings.conf"))
	if err != nil || !slices.Contains(splitLines(data), "search_fields=song,key,comment,bpm") {
		t.Fatalf("unrelated save dropped search_fields: %q %v", data, err)
	}

	// Snake-case parameter and all 12 roles round-trip.
	all12 := "filename,dateImported,year,label,genre,bpm,comment,key,length,album,artist,song"
	wantAll12 := []string{"song", "artist", "album", "length", "key", "comment", "bpm", "genre", "label", "year", "dateImported", "filename"}
	if code, v := uiPost(t, again.URL, "search_fields="+all12); code != 200 || !slices.Equal(v.SearchFields, wantAll12) {
		t.Fatalf("all 12 roles: status %d, searchFields %v, want %v", code, v.SearchFields, wantAll12)
	}

	// JSON POST body works too.
	req, err := http.NewRequest("POST", again.URL+"/v1/enginex/ui", bytes.NewBufferString(`{"searchFields":"genre,label"}`))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var jsonOut uiSettings
	if err := json.NewDecoder(resp.Body).Decode(&jsonOut); err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 200 || !slices.Equal(jsonOut.SearchFields, []string{"genre", "label"}) {
		t.Fatalf("JSON update: status %d, searchFields %v", resp.StatusCode, jsonOut.SearchFields)
	}

	// Returning to stock embedded defaults omits search_fields= from settings.conf.
	if code, v := uiPost(t, again.URL, "searchFields=artist,song"); code != 200 || !slices.Equal(v.SearchFields, wantDefault) {
		t.Fatalf("reset to stock default: status %d, searchFields %v", code, v.SearchFields)
	}
	data, _ = os.ReadFile(filepath.Join(dir, "settings.conf"))
	for _, line := range splitLines(data) {
		if len(line) >= len("search_fields=") && line[:len("search_fields=")] == "search_fields=" {
			t.Fatalf("default search_fields should be omitted from settings.conf: %q", data)
		}
	}

	// Empty query value resets to stock defaults too.
	if code, _ := uiPost(t, again.URL, "searchFields=bpm"); code != 200 {
		t.Fatalf("set bpm failed: %d", code)
	}
	if code, v := uiPost(t, again.URL, "searchFields="); code != 200 || !slices.Equal(v.SearchFields, wantDefault) {
		t.Fatalf("empty reset: status %d, searchFields %v", code, v.SearchFields)
	}
}

func TestUISearchFieldsRejectsInvalidAndRecoversCorruptFile(t *testing.T) {
	dir := t.TempDir()
	cfg := Config{AccessToken: "code", UIDir: dir, StateDir: t.TempDir()}
	srv := httptest.NewServer(New(cfg, nil))
	defer srv.Close()

	for _, q := range []string{"searchFields=bogus", "searchFields=song,evil", "searchFields=,", "searchFields=,,", "search_fields=../etc"} {
		if code, _ := uiPost(t, srv.URL, q); code != 400 {
			t.Errorf("%s: status %d, want 400", q, code)
		}
	}
	if _, err := os.Stat(filepath.Join(dir, "settings.conf")); err == nil {
		t.Fatalf("invalid searchFields wrote settings.conf")
	}

	// Corrupt settings.conf keeps valid roles and ignores bogus tokens.
	if err := os.WriteFile(filepath.Join(dir, "settings.conf"), []byte("search_fields=bogus,key,bad,bpm,key\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code, v := uiGet(t, srv.URL); code != 200 || !slices.Equal(v.SearchFields, []string{"key", "bpm"}) {
		t.Fatalf("partial corrupt recovery: status %d, searchFields %v", code, v.SearchFields)
	}

	// Totally invalid settings.conf falls back to stock defaults.
	if err := os.WriteFile(filepath.Join(dir, "settings.conf"), []byte("search_fields=totally_invalid\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code, v := uiGet(t, srv.URL); code != 200 || !slices.Equal(v.SearchFields, []string{"song", "artist"}) {
		t.Fatalf("full corrupt fallback: status %d, searchFields %v", code, v.SearchFields)
	}
}
