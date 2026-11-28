package dmxstudio

import (
	"bytes"
	"net"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestMaestroShowIDTag(t *testing.T) {
	const tag = "3c8ff84c026e4e44a9e1869fa9ae35d7"
	cfg := maestroSettings{Shows: map[string]string{}}
	want := maestroShow{"/show", "3c8ff84c-026e-4e44-a9e1-869fa9ae35d7"}
	if got := cfg.showForTag(tag); got != want {
		t.Fatalf("show = %#v, want %#v", got, want)
	}
	cfg.Shows[tag] = "Explicit"
	if got := cfg.showForTag(tag); got != (maestroShow{"/show/name", "Explicit"}) {
		t.Fatalf("mapping was ignored: %#v", got)
	}
	if got := cfg.showForTag("unmapped"); got != (maestroShow{}) {
		t.Fatalf("unexpected show: %#v", got)
	}
}

func TestMaestroLiveClockToOSC(t *testing.T) {
	rx, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer rx.Close()
	state := t.TempDir()
	m, err := newMaestro(state)
	if err != nil {
		t.Fatal(err)
	}
	m.cfg.Host = "127.0.0.1"
	m.cfg.Port = rx.LocalAddr().(*net.UDPAddr).Port
	m.cfg.Shows["peak"] = "Peak"
	m.cfg.Phrase = 16
	if err = m.action("maestroStart", nil, state); err != nil {
		t.Fatal(err)
	}
	defer m.stop()
	start := time.Now()
	d := deckSnapshot{Deck: 1, Source: "peer", SourceID: "peer:B:1", TrackKey: "song", LightTag: "peak", BPM: 120, Playing: true, Master: true}
	for i := 0; i <= 160; i++ {
		at := start.Add(time.Duration(i) * 100 * time.Millisecond)
		d.Beat = float64(i%20) / 5
		d.PositionMS = float64(100 * i)
		m.observe([]deckSnapshot{d}, at)
		m.tick(at)
	}
	for _, want := range [][]byte{[]byte("/show/name\x00\x00,s\x00\x00Peak\x00\x00\x00\x00"), []byte("/show/play\x00\x00,\x00\x00\x00")} {
		rx.SetReadDeadline(time.Now().Add(time.Second))
		var buf [256]byte
		n, _, err := rx.ReadFromUDP(buf[:])
		if err != nil || !bytes.Equal(buf[:n], want) {
			t.Fatalf("OSC = %q, %v, want %q", buf[:n], err, want)
		}
	}
	rx.SetReadDeadline(time.Now().Add(30 * time.Millisecond))
	if n, _, _ := rx.ReadFromUDP(make([]byte, 256)); n > 0 {
		t.Fatal("same show restarted")
	}
}
func TestMaestroXoneSelection(t *testing.T) {
	m, err := newMaestro(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	a := deckSnapshot{Deck: 1, Source: "local", SourceID: "local:1", BPM: 128, Playing: true}
	b := a
	b.Source = "peer"
	b.SourceID = "peer:B:1"
	pair := []deckSnapshot{a, b}
	if m.selectDeck(pair) != nil {
		t.Fatal("ambiguous lead")
	}
	pair[0].Master = true
	if m.selectDeck(pair) != &pair[0] {
		t.Fatal("expected master")
	}
	m.cfg.Device = "/dev/snd/midiC1D0"
	m.midi.open = true
	m.midi.local = 0
	m.midi.peer = 100
	if m.selectDeck(pair) != &pair[1] {
		t.Fatal("peer fader open")
	}
	pair[1].Cue = true
	if m.selectDeck(pair) != nil {
		t.Fatal("preview and closed fader")
	}
	pair[1].Cue = false
	m.cfg.Cross = "local-X"
	m.midi.local = 127
	m.midi.peer = 127
	m.midi.cross = 127
	if m.selectDeck(pair) != &pair[1] {
		t.Fatal("crossfader Y")
	}
	m.midi.open = false
	if m.selectDeck(pair) != &pair[0] {
		t.Fatal("MIDI disconnect did not restore deck clock")
	}
}
func TestMixerRunningStatus(t *testing.T) {
	var parser ccParser
	var values [][3]int
	for _, b := range []byte{0xbf, 0, 64, 1, 0xf8, 127, 0x90, 0, 1, 5, 6, 0xbf, 5, 12} {
		parser.feed(b, func(ch, cc, v int) { values = append(values, [3]int{ch, cc, v}) })
	}
	if len(values) != 3 || values[0] != [3]int{16, 0, 64} || values[1] != [3]int{16, 1, 127} || values[2] != [3]int{16, 5, 12} {
		t.Fatalf("CCs: %v", values)
	}
}

func TestMixerReadsChannelFadersUntilDisconnect(t *testing.T) {
	path := filepath.Join(t.TempDir(), "midi-in")
	if err := syscall.Mkfifo(path, 0600); err != nil {
		t.Fatal(err)
	}
	writer, err := syscall.Open(path, syscall.O_RDWR|syscall.O_NONBLOCK, 0)
	if err != nil {
		t.Fatal(err)
	}
	cfg := maestroSettings{Device: path, Channel: 16, LocalCC: 0, PeerCC: 1, Cross: "local-X", CrossCC: 5}
	var input mixerInput
	input.reset()
	defer input.close()
	now := time.Now()
	input.poll(cfg, now)
	if _, err = syscall.Write(writer, []byte{0xb0, 0, 42, 0xbf, 0, 127, 1, 64, 5, 0}); err != nil {
		syscall.Close(writer)
		t.Fatal(err)
	}
	input.poll(cfg, now)
	syscall.Close(writer)
	if !input.ready(cfg) || input.local != 127 || input.peer != 64 || input.cross != 0 || input.last != "CH 16 · CC 5 · 0" {
		t.Fatalf("received %+v", input)
	}
	input.poll(cfg, now)
	if input.ready(cfg) || input.open || input.peer != -1 {
		t.Fatal("stale fader positions after disconnect")
	}
}
