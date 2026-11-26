package webui

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestE4ConnectedStreamingToggle verifies that toggling streaming services in QML while a
// runtime-discovered rekordbox player is present in uiSettings.Sources never sends the
// non-configurable "rekordbox" ID to POST /v1/enginex/ui (which returns HTTP 400), persists
// the toggled configurable services, and keeps "rekordbox" visible in both QML and server state.
func TestE4ConnectedStreamingToggle(t *testing.T) {
	if _, err := exec.LookPath("node"); err != nil {
		t.Skip("node is required to execute extracted QML scSetSource helper")
	}
	e := newRbxEnv(t, nil)
	var ui uiSettings
	for i := 0; i < 100; i++ {
		tidalGet(t, e.base, "/v1/enginex/ui", 200, &ui)
		if slices.Contains(ui.Sources, "rekordbox") {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !slices.Contains(ui.Sources, "rekordbox") {
		t.Fatal("fixture did not discover the rekordbox player")
	}
	if !slices.Contains(ui.Sources, "deezer") || !slices.Contains(ui.Sources, "soundcloud") {
		t.Fatalf("fixture needs deezer and soundcloud initially enabled, got %v", ui.Sources)
	}
	src, err := os.ReadFile("../../tools/ui-mod/build_packs.py")
	if err != nil {
		t.Fatal(err)
	}
	fn := regexp.MustCompile(`(?ms)^\tfunction scSetSource\(.*?^\t}`).Find(src)
	if len(fn) == 0 {
		t.Fatal("missing scSetSource in build_packs.py")
	}
	ids, _ := json.Marshal(ui.Sources)
	script := `
let scUiLoaded = true, scSources = ` + string(ids) + `;
const queries = [];
function scCall(q) { queries.push(q); }
` + string(fn) + `
scSetSource('soundcloud', false);
const afterFirst = scSources.slice();
scSetSource('deezer', false);
console.log(JSON.stringify({queries, afterFirst, afterSecond: scSources}));
`
	out, err := exec.Command("node", "-e", script).CombinedOutput()
	if err != nil {
		t.Fatalf("node failed: %v\n%s", err, out)
	}
	var res struct {
		Queries     []string `json:"queries"`
		AfterFirst  []string `json:"afterFirst"`
		AfterSecond []string `json:"afterSecond"`
	}
	if err := json.Unmarshal(out, &res); err != nil {
		t.Fatalf("decode node output %q: %v", out, err)
	}
	if len(res.Queries) != 2 {
		t.Fatalf("expected 2 queries, got %v", res.Queries)
	}
	if !slices.Contains(res.AfterFirst, "rekordbox") || slices.Contains(res.AfterFirst, "soundcloud") {
		t.Fatalf("optimistic state after first toggle wrong: %v", res.AfterFirst)
	}
	if !slices.Contains(res.AfterSecond, "rekordbox") || slices.Contains(res.AfterSecond, "soundcloud") || slices.Contains(res.AfterSecond, "deezer") {
		t.Fatalf("optimistic state after second toggle wrong: %v", res.AfterSecond)
	}
	for i, q := range res.Queries {
		if strings.Contains(q, "rekordbox") {
			t.Fatalf("query[%d] leaked runtime-only rekordbox source: %q", i, q)
		}
		code, after := uiPost(t, e.base, q)
		if code != 200 {
			t.Fatalf("query[%d] %q returned HTTP %d", i, q, code)
		}
		if !slices.Contains(after.Sources, "rekordbox") {
			t.Fatalf("query[%d] lost discovered rekordbox visibility: %v", i, after.Sources)
		}
		if i == 1 && (slices.Contains(after.Sources, "soundcloud") || slices.Contains(after.Sources, "deezer")) {
			t.Fatalf("final persisted sources still contain toggled-off service: %v", after.Sources)
		}
	}
}

// TestE4LateCancelledSave verifies that a timed-out/cancelled settings POST held before handler
// entry cannot overwrite a newer acknowledged setting once released.
func TestE4LateCancelledSave(t *testing.T) {
	s := New(Config{UIDir: t.TempDir(), StateDir: t.TempDir()}, nil)
	defer s.Close()
	entered, release, applied := make(chan struct{}), make(chan struct{}), make(chan struct{})
	endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost && r.URL.Query().Get("ledBrightness") == "80" {
			close(entered)
			<-release
			defer close(applied)
		}
		s.ServeHTTP(w, r)
	}))
	defer endpoint.Close()
	released := false
	defer func() {
		if !released {
			close(release)
		}
	}()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	old, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint.URL+"/v1/enginex/ui?ledBrightness=80", nil)
	if err != nil {
		t.Fatal(err)
	}
	oldDone := make(chan error, 1)
	go func() {
		res, err := endpoint.Client().Do(old)
		if res != nil {
			res.Body.Close()
		}
		oldDone <- err
	}()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("first POST did not arrive")
	}
	cancel()
	if err := <-oldDone; err == nil {
		t.Fatal("first POST was not cancelled")
	}
	code, saved := uiPost(t, endpoint.URL, "ledBrightness=60")
	if code != http.StatusOK || saved.LedBrightness != 60 {
		t.Fatalf("replacement: %d, %d", code, saved.LedBrightness)
	}
	close(release)
	released = true
	select {
	case <-applied:
	case <-time.After(3 * time.Second):
		t.Fatal("old handler did not finish")
	}
	var final uiSettings
	tidalGet(t, endpoint.URL, "/v1/enginex/ui", http.StatusOK, &final)
	if final.LedBrightness != 60 {
		t.Fatalf("new value 60 was acknowledged; GET after the expired handler returned %d", final.LedBrightness)
	}
}

// TestE4WriteSeqRejectsStaleWrites verifies out-of-order X-UI-Client-Id / X-UI-Write-Seq POSTs
// are rejected with 409 Conflict across /v1/enginex/ui, /v1/enginex/bluetooth, and /v1/enginex/tunnel
// without overwriting newer committed values, while unheadered callers remain unaffected and
// client tracking stays bounded at 64 entries.
func TestE4WriteSeqRejectsStaleWrites(t *testing.T) {
	asDeck(t, "inmusic,jc11")

	s := New(Config{UIDir: t.TempDir(), StateDir: t.TempDir()}, nil)
	defer s.Close()
	srv := httptest.NewServer(s)
	defer srv.Close()

	postWithSeq := func(path, body, clientID string, seq int) int {
		t.Helper()
		req, err := http.NewRequest(http.MethodPost, srv.URL+path, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		if body != "" {
			req.Header.Set("Content-Type", "application/json")
		}
		if clientID != "" {
			req.Header.Set("X-UI-Client-Id", clientID)
			req.Header.Set("X-UI-Write-Seq", strconv.Itoa(seq))
		}
		res, err := srv.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		return res.StatusCode
	}

	// 1. /v1/enginex/ui: seq=2 commits ledBrightness=60; delayed seq=1 (ledBrightness=80) returns 409.
	if code := postWithSeq("/v1/enginex/ui?ledBrightness=60", "", "qml-a", 2); code != http.StatusOK {
		t.Fatalf("ui seq=2 status=%d, want 200", code)
	}
	if code := postWithSeq("/v1/enginex/ui?ledBrightness=80", "", "qml-a", 1); code != http.StatusConflict {
		t.Fatalf("ui stale seq=1 status=%d, want 409", code)
	}
	if code := postWithSeq("/v1/enginex/ui?ledBrightness=80", "", "qml-a", 2); code != http.StatusConflict {
		t.Fatalf("ui duplicate seq=2 status=%d, want 409", code)
	}
	var ui uiSettings
	tidalGet(t, srv.URL, "/v1/enginex/ui", http.StatusOK, &ui)
	if ui.LedBrightness != 60 {
		t.Fatalf("ui ledBrightness=%d after stale seq, want 60", ui.LedBrightness)
	}
	// Unheadered caller still succeeds (backwards compatibility).
	if code := postWithSeq("/v1/enginex/ui?ledBrightness=40", "", "", 0); code != http.StatusOK {
		t.Fatalf("ui unheadered status=%d, want 200", code)
	}
	// Bounded client map eviction (64 entries): 64 newer clients evict qml-a so qml-a seq=1 succeeds again.
	for i := 0; i < 64; i++ {
		if code := postWithSeq("/v1/enginex/ui?autoGain=0", "", "client-"+strconv.Itoa(i), 1); code != http.StatusOK {
			t.Fatalf("eviction write %d status=%d, want 200", i, code)
		}
	}
	if code := postWithSeq("/v1/enginex/ui?autoGain=0", "", "qml-a", 1); code != http.StatusOK {
		t.Fatalf("evicted client qml-a seq=1 status=%d, want 200 (proves 64-entry bound)", code)
	}

	// 2. /v1/enginex/bluetooth: seq=2 commits Headphones; stale seq=1 (Booth) returns 409.
	if code := postWithSeq("/v1/enginex/bluetooth?assignLayer=Headphones", "", "qml-bt", 2); code != http.StatusOK {
		t.Fatalf("bt seq=2 status=%d, want 200", code)
	}
	if code := postWithSeq("/v1/enginex/bluetooth?assignLayer=Booth", "", "qml-bt", 1); code != http.StatusConflict {
		t.Fatalf("bt stale seq=1 status=%d, want 409", code)
	}
	var bt bluetoothStatus
	tidalGet(t, srv.URL, "/v1/enginex/bluetooth", http.StatusOK, &bt)
	if bt.AssignLayer != "Headphones" {
		t.Fatalf("bt assignLayer=%q after stale seq, want Headphones", bt.AssignLayer)
	}

	// 3. /v1/enginex/tunnel: seq=2 commits enabled=false; stale seq=1 (enabled=true) returns 409.
	if code := postWithSeq("/v1/enginex/tunnel", `{"enabled":false}`, "qml-tun", 2); code != http.StatusOK {
		t.Fatalf("tunnel seq=2 status=%d, want 200", code)
	}
	if code := postWithSeq("/v1/enginex/tunnel", `{"enabled":true}`, "qml-tun", 1); code != http.StatusConflict {
		t.Fatalf("tunnel stale seq=1 status=%d, want 409", code)
	}
	var tun TunnelStatus
	tidalGet(t, srv.URL, "/v1/enginex/tunnel", http.StatusOK, &tun)
	if tun.Enabled {
		t.Fatalf("tunnel enabled=true after stale seq, want false")
	}
}

// TestR3SDCompletionAfterNewerSave verifies that an older SD high-speed helper failing or
// completing after a newer acknowledged write never rolls back the newer setting or overrides
// a newer broadcast preference.
func TestR3SDCompletionAfterNewerSave(t *testing.T) {
	asDeck(t, "inmusic,jp07")
	for _, mode := range []string{"failed helper rollback", "successful helper then broadcast"} {
		t.Run(mode, func(t *testing.T) {
			entered, release := make(chan struct{}), make(chan struct{})
			var once sync.Once
			resume := func() { once.Do(func() { close(release) }) }
			cfg := Config{
				UIDir: t.TempDir(), StateDir: t.TempDir(), SDHighSpeedRunDir: t.TempDir(),
				ApplySDHighSpeed: func(bool, bool) (bool, error) {
					close(entered)
					<-release
					if mode == "failed helper rollback" {
						return false, errors.New("fixture SD failure")
					}
					return false, nil
				},
			}
			srv := New(cfg, nil)
			defer srv.Close()
			handled := make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("X-UI-Write-Seq") == "1" {
					defer close(handled)
				}
				srv.ServeHTTP(w, r)
			}))
			defer server.Close()
			defer resume()
			post := func(ctx context.Context, query, seq string) (int, error) {
				req, err := http.NewRequestWithContext(ctx, http.MethodPost, server.URL+"/v1/enginex/ui?"+query, nil)
				if err != nil {
					return 0, err
				}
				req.Header.Set("X-UI-Client-Id", "review-r3")
				req.Header.Set("X-UI-Write-Seq", seq)
				resp, err := server.Client().Do(req)
				if err != nil {
					return 0, err
				}
				defer resp.Body.Close()
				return resp.StatusCode, nil
			}
			first := "sdHighSpeed=1"
			next := "ledBrightness=60"
			if mode == "successful helper then broadcast" {
				first += "&broadcast=1"
				next = "broadcast=0"
			}
			old, cancelOld := context.WithCancel(context.Background())
			defer cancelOld()
			closed := make(chan int, 1)
			go func() { code, _ := post(old, first, "1"); closed <- code }()
			select {
			case <-entered:
			case <-time.After(3 * time.Second):
				t.Fatal("SD helper never started")
			}
			cancelOld()
			if code := <-closed; code != 0 {
				t.Fatalf("expired client received response %d", code)
			}
			if code, err := post(context.Background(), next, "2"); code != 200 || err != nil {
				t.Fatalf("new write: status %d, %v", code, err)
			}
			var ack uiSettings
			tidalGet(t, server.URL, "/v1/enginex/ui", 200, &ack)
			if mode == "failed helper rollback" && ack.LedBrightness != 60 {
				t.Fatal("missing brightness ACK")
			}
			if mode == "successful helper then broadcast" && ack.BroadcastEnabled {
				t.Fatal("missing off ACK")
			}
			resume()
			select {
			case <-handled:
			case <-time.After(3 * time.Second):
				t.Fatal("expired handler never finished")
			}
			var final uiSettings
			tidalGet(t, server.URL, "/v1/enginex/ui", 200, &final)
			if mode == "failed helper rollback" && final.LedBrightness != 60 {
				t.Errorf("acknowledged 60; after rollback GET returned %d", final.LedBrightness)
			}
			if mode == "successful helper then broadcast" && final.BroadcastEnabled {
				t.Error("acknowledged broadcast off; after old helper GET returned on")
			}
		})
	}
}

type r3ObservedContext struct {
	context.Context
	observe func(error) error
}

func (c r3ObservedContext) Err() error { return c.observe(c.Context.Err()) }

// TestR3CancelledTunnelWhileWaitingForManager verifies that a tunnel POST cancelled while
// waiting on TunnelManager.mu does not mutate or persist tunnel state once the lock is released.
func TestR3CancelledTunnelWhileWaitingForManager(t *testing.T) {
	s := New(Config{UIDir: t.TempDir(), StateDir: t.TempDir()}, nil)
	defer s.Close()
	manager := s.TunnelManager()
	manager.mu.Lock()
	manager.enabled = true
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	checked := make(chan struct{})
	var once sync.Once
	observed := r3ObservedContext{ctx, func(err error) error { once.Do(func() { close(checked) }); return err }}
	req := httptest.NewRequest(http.MethodPost, "/v1/enginex/tunnel", strings.NewReader(`{"enabled":false}`)).WithContext(observed)
	done := make(chan struct{})
	go func() { defer close(done); s.tunnelPost(httptest.NewRecorder(), req) }()
	select {
	case <-checked:
	case <-time.After(3 * time.Second):
		manager.mu.Unlock()
		<-done
		t.Fatal("did not reach first context check")
	}
	cancel()
	manager.mu.Unlock()
	<-done
	if !manager.Status("").Enabled {
		t.Fatal("cancelled request disabled tunnel after manager unlock")
	}
}

// TestR3CancelledVerboseWriteRemainsConsistent verifies that cancellation around verboseLog
// never leaves logging.ini enabled while settings.conf keeps VerboseLog false.
func TestR3CancelledVerboseWriteRemainsConsistent(t *testing.T) {
	path := verboseLogPath
	verboseLogPath = filepath.Join(t.TempDir(), "logging.ini")
	defer func() { verboseLogPath = path }()
	s := New(Config{UIDir: t.TempDir(), StateDir: t.TempDir()}, nil)
	defer s.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	live := r3ObservedContext{ctx, func(err error) error {
		if _, statErr := os.Stat(verboseLogPath); statErr == nil {
			cancel()
		}
		return ctx.Err()
	}}
	req := httptest.NewRequest(http.MethodPost, "/v1/enginex/ui?verboseLog=1", nil).WithContext(live)
	s.uiUpdate(httptest.NewRecorder(), req)
	_, err := os.Stat(verboseLogPath)
	if s.loadUI().VerboseLog != (err == nil) {
		t.Fatal("logging.ini is enabled while the persisted UI setting is off")
	}
}
