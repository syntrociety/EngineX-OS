package webui

import (
	"encoding/json"
	"os"
	"os/exec"
	"regexp"
	"slices"
	"strings"
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
