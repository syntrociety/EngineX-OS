package webui

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"regexp"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Cd1s/EngineX-OS/internal/stems"
)

// The deck shows the renderer a track goes to (#238 review). With a relay that
// only offers hpss and a home computer that has the neural model, tracks go to
// the computer (stemsproc: TestOnlyANeuralRendererGetsTheTrack), so the status
// must name the computer, and the relay's later answers must not put the relay
// back. A home computer that only has hpss changes nothing, and when the
// neural one goes away the relay is reported again.
func TestStemsStatusShowsTheRendererInUse(t *testing.T) {
	prevProbe, prevEvery := lanStemsProbe, lanProbeEvery
	lanProbeEvery = 5 * time.Millisecond
	t.Cleanup(func() { lanStemsProbe, lanProbeEvery = prevProbe, prevEvery })

	relay := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/healthz/stems" {
			http.NotFound(w, r)
			return
		}
		w.Write([]byte(`{"ok":true,"device":"relay-box","backend":"hpss"}`))
	}))
	defer relay.Close()
	var lan atomic.Pointer[stems.LANState] // what answers on the LAN, if anything
	lanStemsProbe = func(context.Context, *stems.LANState, time.Duration) (stems.LANState, bool) {
		if st := lan.Load(); st != nil {
			return *st, true
		}
		return stems.LANState{}, false
	}

	state := t.TempDir()
	s := New(Config{UIDir: t.TempDir(), StateDir: state}, nil)
	defer s.Close()
	relayURL, password := relay.URL, "pw"
	if err := s.updateRelayConfig(&relayURL, &password); err != nil {
		t.Fatal(err)
	}
	shown := func() string {
		if st := s.relayStatusView().LANStems; st != nil && st.Online {
			return st.Hostname + " " + st.Backend
		}
		return ""
	}
	// relayAnswers is the relay's next 30-second answer, now.
	relayAnswers := func() {
		relayProbeMu.Lock()
		delete(relayProbed, state)
		relayProbeMu.Unlock()
		s.refreshRelayStatus()
	}
	settles := func(want string) {
		t.Helper()
		deadline := time.Now().Add(3 * time.Second)
		for shown() != want {
			if time.Now().After(deadline) {
				t.Fatalf("the deck shows %q, want %q", shown(), want)
			}
			time.Sleep(10 * time.Millisecond)
			relayAnswers()
		}
		for i := 0; i < 5; i++ { // and it stays, through LAN probes and relay answers
			relayAnswers()
			time.Sleep(10 * time.Millisecond)
			if got := shown(); got != want {
				t.Fatalf("the deck shows %q after settling on %q", got, want)
			}
		}
	}

	settles("relay-box hpss")
	lan.Store(&stems.LANState{Endpoint: "http://192.168.1.50:17890", Hostname: "old-pc", Backend: "hpss"})
	settles("relay-box hpss")
	lan.Store(&stems.LANState{Endpoint: "http://192.168.1.51:17890", Hostname: "studio-pc", Backend: stems.Neural})
	settles("studio-pc " + stems.Neural)
	lan.Store(nil)
	settles("relay-box hpss")
}

// #238 / #336 E3: the web console's Stems status line must agree with
// stems-processor and the on-deck status line across all four languages:
// only htdemucs is reported as high-quality remote separation; hpss or an
// unnamed backend is reported as unused with on-deck basic quality; a
// standalone deck states on-deck basic quality and recommends LAN htdemucs;
// AZ04 units (capabilities.remoteStems === false) omit the suffix.
func TestConsoleStemsStatusMatchesIssue238(t *testing.T) {
	b, err := assets.ReadFile("index.html")
	if err != nil {
		t.Fatal(err)
	}
	page := string(b)
	for _, want := range []string{
		"function stemsLine(st,remote)",
		"st.backend==='htdemucs'",
		"capabilities.remoteStems",
		"stemsHq:[",
		"stemsHpss:[",
		"stemsDeckOff:[",
		"stemsDeck:[",
	} {
		if !strings.Contains(page, want) {
			t.Fatalf("pkg/webui/index.html missing %q", want)
		}
	}
	if strings.Contains(page, "lanOn:[") || strings.Contains(page, "lanOff:[") {
		t.Fatal("pkg/webui/index.html still contains pre-#238 lanOn/lanOff strings")
	}

	node, err := exec.LookPath("node")
	if err != nil {
		return
	}
	tStart := strings.Index(page, "const T={")
	tEnd := strings.Index(page[tStart:], "\n};\n")
	if tStart < 0 || tEnd < 0 {
		t.Fatal("missing translation table in index.html")
	}
	tTable := page[tStart : tStart+tEnd+4]
	tFn := regexp.MustCompile(`function t\(k,\.\.\.a\)\{[^\n]+\}`).FindString(page)
	stemsFn := regexp.MustCompile(`function stemsLine\([^\n]+\}`).FindString(page)
	if tFn == "" || stemsFn == "" {
		t.Fatalf("could not extract t/stemsLine from index.html")
	}
	script := `
const assert = require('assert');
` + tTable + `
const LANGS = ['en', 'zh-Hans', 'zh-Hant', 'es'];
let lang = 'en';
` + tFn + `
` + stemsFn + `
for (const l of LANGS) {
  lang = l;
  assert.ok(stemsLine({online: true, hostname: 'studio-pc', backend: 'htdemucs'}, true).includes('studio-pc'));
  assert.ok(stemsLine({online: true, hostname: 'studio-pc', backend: 'htdemucs'}, true).includes('htdemucs'));
  const hpss = stemsLine({online: true, hostname: 'relay-box', backend: 'hpss', relay: true}, true);
  assert.ok(hpss.includes('relay-box') && hpss.includes('hpss') && hpss.includes('htdemucs'), l + ': ' + hpss);
  assert.ok(!/online|已连上|已連上|en línea/i.test(hpss), l + ' falsely marks hpss online: ' + hpss);
  assert.equal(stemsLine({online: true, hostname: 'studio-pc', backend: 'htdemucs'}, false), '');
  assert.ok(stemsLine(null, true).includes('htdemucs'), l + ' standalone missing htdemucs hint');
  assert.ok(stemsLine({online: true, endpoint: 'http://192.168.1.9:17890'}, true).includes('http://192.168.1.9:17890'));
}
`
	if out, err := exec.Command(node, "-e", script).CombinedOutput(); err != nil {
		t.Fatalf("node stemsLine check failed: %v\n%s", err, out)
	}
}
