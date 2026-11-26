package dmxstudio

import (
	"fmt"
	"path/filepath"
	"syscall"
	"time"
)

// A raw ALSA MIDI stream preserves CC running status across reads and real-time bytes.
type ccParser struct {
	status byte
	first  byte
	count  int
}

func (p *ccParser) feed(b byte, emit func(int, int, int)) {
	if b >= 0xf8 {
		return
	}
	if b&0x80 != 0 {
		p.status = b
		p.count = 0
		return
	}
	if p.status&0xf0 != 0xb0 {
		return
	}
	if p.count == 0 {
		p.first = b
		p.count = 1
		return
	}
	emit(int(p.status&15)+1, int(p.first), int(b))
	p.count = 0
}

type mixerInput struct {
	fd                 int
	open               bool
	local, peer, cross int
	last, err          string
	retry              time.Time
	devices            []string
	scan               time.Time
	parser             ccParser
}

func (m *mixerInput) reset() {
	m.local = -1
	m.peer = -1
	m.cross = -1
	m.last = ""
	m.parser = ccParser{}
}
func (m *mixerInput) ready(c maestroSettings) bool {
	return m.open && m.local >= 0 && m.peer >= 0 && (c.Cross == "" || m.cross >= 0)
}
func (m *mixerInput) weights(c maestroSettings) (int, int) {
	if c.Cross == "" {
		return m.local, m.peer
	}
	x := m.cross
	if c.Cross == "local-Y" {
		x = 127 - x
	}
	return m.local * (127 - x) / 127, m.peer * x / 127
}
func (m *mixerInput) close() {
	if m.open {
		syscall.Close(m.fd)
	}
	m.open = false
	m.err = ""
	m.retry = time.Time{}
	m.reset()
}
func (m *mixerInput) poll(c maestroSettings, now time.Time) {
	if !now.Before(m.scan) {
		m.devices, _ = filepath.Glob("/dev/snd/midiC*D*")
		m.scan = now.Add(3 * time.Second)
	}
	if c.Device == "" || now.Before(m.retry) {
		return
	}
	if !m.open {
		fd, err := syscall.Open(c.Device, syscall.O_RDONLY|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
		if err != nil {
			m.err = err.Error()
			m.retry = now.Add(2 * time.Second)
			return
		}
		m.fd = fd
		m.open = true
		m.err = ""
	}
	var buf [256]byte
	n, err := syscall.Read(m.fd, buf[:])
	if err == syscall.EAGAIN || err == syscall.EINTR {
		return
	}
	if err != nil || n == 0 {
		m.close()
		m.err = "MIDI-ingang losgekoppeld"
		m.retry = now.Add(2 * time.Second)
		return
	}
	for _, b := range buf[:n] {
		m.parser.feed(b, func(ch, cc, val int) {
			m.last = fmt.Sprintf("CH %d · CC %d · %d", ch, cc, val)
			if ch == c.Channel {
				if cc == c.CrossCC {
					m.cross = val
				}
				switch cc {
				case c.LocalCC:
					m.local = val
				case c.PeerCC:
					m.peer = val
				}
			}
		})
	}
}
func (m *mixerInput) status(c maestroSettings) map[string]any {
	return map[string]any{"devices": m.devices, "connected": m.open, "ready": m.ready(c), "cross": m.cross, "local": m.local, "peer": m.peer, "last": m.last, "error": m.err}
}
