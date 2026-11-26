package webui

import (
	"context"
	"database/sql"
	"net/url"
	"path/filepath"
	"regexp"
	"time"
)

// Only the normalized light tag is shared with paired players.
var lightMarker = regexp.MustCompile(`\[licht:([a-z0-9][a-z0-9_-]{0,31})\]`)

func oneLightTag(comment string) string {
	var tag string
	for _, match := range lightMarker.FindAllStringSubmatch(comment, -1) {
		if tag != "" && tag != match[1] {
			return ""
		}
		tag = match[1]
	}
	return tag
}

type lightLookup struct {
	path, key, tag string
	retry          time.Time
}

// Resolve changes on the source player. The read-only query runs without the
// shadow mutex; at most one lookup per deck is in flight.
func (sh *shadowState) lightTag(deck int, path, key string) string {
	if deck < 1 || deck > 4 {
		return ""
	}
	sh.lightMu.Lock()
	prev := sh.lightCache[deck]
	if path == "" {
		sh.lightCache[deck] = lightLookup{}
		sh.lightMu.Unlock()
		return ""
	}
	if prev.path == path && prev.key == key && (prev.retry.IsZero() || time.Now().Before(prev.retry)) {
		sh.lightMu.Unlock()
		return prev.tag
	}
	entry := lightLookup{path: path, key: key, retry: time.Now().Add(5 * time.Second)}
	sh.lightCache[deck] = entry
	sh.lightMu.Unlock()
	lib := engineLibrary(path, sh.dataRoot, sh.mediaRoot)
	rel, err := filepath.Rel(lib, path)
	if err != nil {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	for _, name := range []string{"m.db", "stm.db"} {
		db, e := sql.Open("sqlite", (&url.URL{Scheme: "file", Path: filepath.Join(lib, "Database2", name)}).String()+"?mode=ro&_pragma=query_only(1)&_pragma=busy_timeout(100)")
		if e != nil {
			continue
		}
		var comment string
		e = db.QueryRowContext(ctx, `SELECT coalesce(substr(comment,1,4096),'') FROM Track WHERE path=? ORDER BY id DESC LIMIT 1`, filepath.ToSlash(rel)).Scan(&comment)
		db.Close()
		if e == nil {
			tag := oneLightTag(comment)
			sh.lightMu.Lock()
			if sh.lightCache[deck] == entry {
				sh.lightCache[deck] = lightLookup{path: path, key: key, tag: tag}
			}
			sh.lightMu.Unlock()
			return tag
		}
	}
	return ""
}
