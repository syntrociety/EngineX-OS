package webui

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestLightingTagOnSourceAndPeerClock(t *testing.T) {
	root := t.TempDir()
	audio := filepath.Join(root, "USB", "Music", "track.wav")
	dir := filepath.Join(root, "USB", "Engine Library", "Database2")
	if err := os.MkdirAll(dir, 0755); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", filepath.Join(dir, "m.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err = db.Exec("CREATE TABLE Track (id INTEGER, path TEXT, comment TEXT); INSERT INTO Track VALUES (1,'../Music/track.wav','notes [licht:peak]')"); err != nil {
		t.Fatal(err)
	}
	sh := shadowState{mediaRoot: root, dataRoot: t.TempDir()}
	tag := sh.lightTag(1, audio, "track")
	if tag != "peak" {
		t.Fatalf("library tag %q", tag)
	}
	d := deckState{Deck: 1, Key: "track", LightTag: tag}
	now := time.Now()
	clock := carriedClockDeck(d, "peer", "peer:B:1", now, now)
	if clock.TrackKey != "track" || clock.LightTag != "peak" {
		t.Fatalf("peer: %+v", clock)
	}
	if oneLightTag("[licht:one] [licht:two]") != "" || oneLightTag("[licht:one] [licht:one]") != "one" {
		t.Fatal("ambiguous or repeated tags")
	}
}
