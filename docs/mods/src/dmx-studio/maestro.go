// Maestro is an optional control output. The controller continues to render DMX
// and analyse audio. Only a change of selected show produces a network command.
package dmxstudio

import (
	"encoding/json"
	"math"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/Cd1s/EngineX-OS/docs/mods/src/modkit"
)

type maestroSettings struct {
	Host    string            `json:"host"`
	Port    int               `json:"port"`
	Phrase  int               `json:"phrase"`
	Shows   map[string]string `json:"shows"`  // comment tag -> unique saved show name
	Device  string            `json:"device"` // optional Linux raw MIDI input
	Channel int               `json:"channel"`
	LocalCC int               `json:"localCC"`
	PeerCC  int               `json:"peerCC"`
	Cross   string            `json:"cross"` // off (empty), local-X or local-Y
	CrossCC int               `json:"crossCC"`
}

var tagKey = regexp.MustCompile("^[a-z0-9][a-z0-9_-]{0,31}$")
var showIDTag = regexp.MustCompile("^[0-9a-f]{32}$")
var midiDevice = regexp.MustCompile("^/dev/snd/midiC[0-9]+D[0-9]+$")

type maestroShow struct {
	address string
	target  string
}

func (c maestroSettings) showForTag(tag string) maestroShow {
	if name := c.Shows[tag]; name != "" {
		return maestroShow{"/show/name", name}
	}
	if showIDTag.MatchString(tag) {
		id := tag[:8] + "-" + tag[8:12] + "-" + tag[12:16] + "-" + tag[16:20] + "-" + tag[20:]
		return maestroShow{"/show", id}
	}
	return maestroShow{}
}

func (c maestroSettings) validate() error {
	ip := net.ParseIP(c.Host)
	if c.Host != "" && (ip.To4() == nil || strings.Contains(c.Host, ":") || !(ip.IsGlobalUnicast() || ip.IsLinkLocalUnicast() || ip.IsLoopback())) {
		return modkit.Bad("Gebruik het unicast IPv4-adres van Maestro")
	}
	if c.Port < 1 || c.Port > 65535 || (c.Phrase != 16 && c.Phrase != 32) {
		return modkit.Bad("Ongeldige poort of fraselengte")
	}
	if c.Channel < 1 || c.Channel > 16 || c.LocalCC < 0 || c.LocalCC > 127 || c.PeerCC < 0 || c.PeerCC > 127 || c.LocalCC == c.PeerCC || (c.Device != "" && !midiDevice.MatchString(c.Device)) {
		return modkit.Bad("Controleer de MIDI-ingang, het kanaal en de twee verschillende CC-nummers")
	}
	if c.Cross != "" && ((c.Cross != "local-X" && c.Cross != "local-Y") || c.CrossCC < 0 || c.CrossCC > 127 || c.CrossCC == c.LocalCC || c.CrossCC == c.PeerCC) {
		return modkit.Bad("Ongeldige crossfadertoewijzing")
	}
	if c.Shows == nil || len(c.Shows) > 64 {
		return modkit.Bad("Maximaal 64 tagkoppelingen")
	}
	for k, v := range c.Shows {
		if !tagKey.MatchString(k) || strings.TrimSpace(v) != v || len(v) == 0 || len(v) > 128 || strings.ContainsRune(v, 0) {
			return modkit.Bad("Ongeldige tag of shownaam")
		}
	}
	return nil
}

type maestro struct {
	mu                               sync.Mutex
	sendMu                           sync.Mutex // serializes sends with stop/reconfigure, independent of fixture output
	cfg                              maestroSettings
	socket                           *net.UDPConn
	decks                            []deckSnapshot
	seen                             time.Time
	source, track, fault             string
	next, last                       maestroShow
	bpm, beats, position             float64
	boundary                         int
	midi                             mixerInput
	done                             chan struct{}
}

func newMaestro(state string) (*maestro, error) {
	m := &maestro{cfg: maestroSettings{Port: 7672, Phrase: 32, Channel: 16, PeerCC: 1, CrossCC: 5, Shows: map[string]string{}}}
	m.midi.reset()
	saved := m.cfg
	saved.Shows = map[string]string{}
	err := modkit.LoadJSON(filepath.Join(state, "maestro.json"), &saved)
	if err == nil {
		err = saved.validate()
	}
	if err != nil && !os.IsNotExist(err) {
		m.fault = "Maestro-configuratie kon niet worden geladen: " + err.Error()
	} else if err == nil {
		m.cfg = saved
	}
	return m, nil
}
func (m *maestro) stop() {
	m.sendMu.Lock()
	defer m.sendMu.Unlock()
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.socket != nil {
		m.socket.Close()
		m.socket = nil
	}
	m.clearClock()
	m.last = maestroShow{}
}
func (m *maestro) clearClock() {
	m.source = ""
	m.track = ""
	m.next = maestroShow{}
	m.beats = 0
}
func (m *maestro) action(action string, req map[string]any, state string) error {
	if action == "maestroStop" {
		m.stop()
		return nil
	}
	m.sendMu.Lock()
	defer m.sendMu.Unlock()
	m.mu.Lock()
	defer m.mu.Unlock()
	switch action {
	case "maestroConfigure":
		if m.socket != nil {
			return modkit.Bad("Stop OSC voor het wijzigen van de configuratie")
		}
		b, err := json.Marshal(req["config"])
		if err != nil {
			return err
		}
		var c maestroSettings
		if err = json.Unmarshal(b, &c); err != nil {
			return modkit.Bad("Ongeldige Maestro-instellingen")
		}
		if err = c.validate(); err != nil {
			return err
		}
		if err = modkit.SaveJSON(filepath.Join(state, "maestro.json"), c); err != nil {
			return err
		}
		if c.Device != m.cfg.Device || c.Channel != m.cfg.Channel || c.LocalCC != m.cfg.LocalCC || c.PeerCC != m.cfg.PeerCC || c.Cross != m.cfg.Cross || c.CrossCC != m.cfg.CrossCC {
			m.midi.close()
		}
		m.cfg = c
		m.clearClock()
		m.fault = ""
	case "maestroStart":
		if m.socket != nil {
			return nil
		}
		if m.cfg.Host == "" {
			return modkit.Bad("Stel eerst het IP-adres van Maestro in")
		}
		conn, err := net.DialUDP("udp4", nil, &net.UDPAddr{IP: net.ParseIP(m.cfg.Host), Port: m.cfg.Port})
		if err != nil {
			return err
		}
		m.socket = conn
		m.clearClock()
		m.last = maestroShow{}
		m.fault = ""
	default:
		return modkit.Bad("Onbekende Maestro-actie")
	}
	return nil
}

func (m *maestro) selectDeck(decks []deckSnapshot) *deckSnapshot {
	var best *deckSnapshot
	bestRank := 0
	mapped := m.cfg.Device != "" && m.midi.ready(m.cfg)
	for i := range decks {
		d := &decks[i]
		if d.Deck < 1 || d.Deck > 4 || d.Stale || !d.Playing || d.Cue || d.effectiveBPM() <= 0 || d.Beat < 0 || d.Beat >= 4 || d.SourceID == "" {
			continue
		}
		rank := 1
		if d.Master || d.Lead {
			rank = 2
		}
		if mapped {
			// CH faders represent the local and paired players, routed outside X/Y.
			level, other := m.midi.weights(m.cfg)
			switch d.Source {
			case "local":
			case "peer":
				level, other = other, level
			default:
				continue
			}
			if level < 8 {
				continue
			}
			if level > other+12 {
				rank += 4
			}
		}
		if rank > bestRank {
			best = d
			bestRank = rank
		} else if rank == bestRank {
			best = nil
		}
	}
	return best
}
func (m *maestro) observe(decks []deckSnapshot, now time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.decks = decks
	d := m.selectDeck(decks)
	if d == nil {
		m.clearClock()
		m.seen = now
		return
	}
	dt := now.Sub(m.seen).Seconds()
	fresh := d.SourceID == m.source && d.TrackKey == m.track && dt >= 0 && dt <= 0.5
	if fresh && d.PositionMS > 0 && m.position > 0 {
		speed := 1 + d.Pitch/100
		if d.BPM > 0 {
			speed = d.effectiveBPM() / d.BPM
		}
		fresh = math.Abs(d.PositionMS-m.position-dt*1000*speed) < 220
	}
	if fresh {
		predicted := m.beats + dt*m.bpm/60
		delta := wrapBar(d.Beat - predicted)
		if math.Abs(delta) > 0.75 {
			fresh = false
		} else {
			m.beats = predicted + delta
		}
	}
	if !fresh {
		m.beats = d.Beat
		m.boundary = 0
	}
	m.source = d.SourceID
	m.track = d.TrackKey
	m.bpm = d.effectiveBPM()
	m.position = d.PositionMS
	m.seen = now
	m.next = maestroShow{}
	if d.TrackKey != "" {
		m.next = m.cfg.showForTag(d.LightTag)
	}
}
func oscString(packet []byte, s string) []byte {
	packet = append(packet, s...)
	packet = append(packet, 0)
	for len(packet)%4 != 0 {
		packet = append(packet, 0)
	}
	return packet
}
func oscMessage(address string, args ...string) []byte {
	packet := oscString(nil, address)
	packet = oscString(packet, ","+strings.Repeat("s", len(args)))
	for _, arg := range args {
		packet = oscString(packet, arg)
	}
	return packet
}
func (m *maestro) phase(now time.Time) (float64, bool) {
	age := now.Sub(m.seen)
	if m.source == "" || age < 0 || age > 500*time.Millisecond {
		return 0, false
	}
	return m.beats + age.Seconds()*m.bpm/60, true
}
func (m *maestro) tick(now time.Time) {
	m.sendMu.Lock()
	defer m.sendMu.Unlock()
	m.mu.Lock()
	beat, valid := m.phase(now)
	if valid && m.midi.ready(m.cfg) {
		current := m.selectDeck(m.decks)
		if current == nil || current.SourceID != m.source {
			m.clearClock()
			m.mu.Unlock()
			return
		}
	}
	idx := int(math.Floor(beat / float64(m.cfg.Phrase)))
	if !valid || m.socket == nil || idx <= m.boundary {
		m.mu.Unlock()
		return
	}
	previous := m.boundary
	m.boundary = idx
	show, socket := m.next, m.socket
	if idx != previous+1 || show.target == "" || show == m.last {
		m.mu.Unlock()
		return
	}
	m.mu.Unlock()
	// A bounded pair of writes, never retried. UDP cannot confirm playback.
	var err error
	for _, packet := range [][]byte{oscMessage(show.address, show.target), oscMessage("/show/play")} {
		if err = socket.SetWriteDeadline(time.Now().Add(50 * time.Millisecond)); err != nil {
			break
		}
		if _, err = socket.Write(packet); err != nil {
			break
		}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if err != nil {
		m.fault = err.Error()
		socket.Close()
		m.socket = nil
		return
	}
	m.last = show
	m.fault = ""
}
func (m *maestro) run(stop <-chan struct{}) {
	defer close(m.done)
	ticker := time.NewTicker(time.Second / 40)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			m.stop()
			m.mu.Lock()
			m.midi.close()
			m.mu.Unlock()
			return
		case now := <-ticker.C:
			m.mu.Lock()
			m.midi.poll(m.cfg, now)
			m.mu.Unlock()
			m.tick(now)
		}
	}
}
func (m *maestro) status() map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := time.Now()
	beat, valid := m.phase(now)
	decks := m.decks
	if now.Sub(m.seen) > 500*time.Millisecond {
		decks = nil
	}
	var source, next string
	if valid {
		source = m.source
		if m.socket != nil && m.next != m.last {
			next = m.next.target
		}
	}
	return map[string]any{"config": m.cfg, "armed": m.socket != nil, "decks": decks, "source": source, "bpm": m.bpm,
		"beat": int(math.Floor(math.Mod(beat, float64(m.cfg.Phrase)))) + 1, "valid": valid, "next": next, "sent": m.last.target, "error": m.fault, "midi": m.midi.status(m.cfg)}
}
