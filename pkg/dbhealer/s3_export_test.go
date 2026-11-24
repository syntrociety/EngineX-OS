package dbhealer

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestS3PlaylistEntityDeduplicatesNullAndEmptyUUIDs(t *testing.T) {
	for _, dataOnly := range []bool{false, true} {
		name := "full"
		if dataOnly {
			name = "data-only"
		}
		t.Run(name, func(t *testing.T) {
			db, _ := newLibrary(t)
			track(t, db, 1)
			track(t, db, 2)
			track(t, db, 3)
			mustExec(t, db, "INSERT INTO Playlist(id,title,parentListId,nextListId,isPersisted) VALUES(1,'Club Set',0,0,1)")

			// SQLite UNIQUE(listId, databaseUuid, trackId) permits multiple NULL UUID rows
			// and allows '' alongside the library UUID. Stock Engine export joins on
			// (trackId, databaseUuid), which fails to match NULL and collides on export.
			mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(10,1,1,NULL,20,0)")
			mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(20,1,1,NULL,30,1)")
			mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(30,1,2,'',40,1)")
			mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(40,1,2,'test-library',50,0)")
			mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(50,1,3,'remote-library',60,0)")
			mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(60,1,3,'test-library',0,0)")

			if _, err := heal(db, dataOnly); err != nil {
				t.Fatalf("heal(dataOnly=%v): %v", dataOnly, err)
			}

			var count int
			if err := db.QueryRow("SELECT count(*) FROM PlaylistEntity WHERE listId=1").Scan(&count); err != nil {
				t.Fatal(err)
			}
			if count != 4 {
				t.Fatalf("expected 4 deduplicated PlaylistEntity rows, got %d", count)
			}

			var missingUUID int
			if err := db.QueryRow("SELECT count(*) FROM PlaylistEntity WHERE listId=1 AND trackId>0 AND (databaseUuid IS NULL OR databaseUuid='')").Scan(&missingUUID); err != nil {
				t.Fatal(err)
			}
			if missingUUID != 0 {
				t.Fatalf("expected 0 NULL/empty databaseUuid rows for local tracks, got %d", missingUUID)
			}

			// Verify surviving chain order preserves track 1, track 2, remote track 3, local track 3.
			ids := chainIDs(t, db, "PlaylistEntity", "listId", "nextEntityId", 1)
			if len(ids) != 4 {
				t.Fatalf("expected 4 linked entities, got %v", ids)
			}

			// Track 2 must keep direct membershipReference=0.
			var mr int
			if err := db.QueryRow("SELECT membershipReference FROM PlaylistEntity WHERE listId=1 AND trackId=2 AND databaseUuid='test-library'").Scan(&mr); err != nil {
				t.Fatal(err)
			}
			if mr != 0 {
				t.Fatalf("expected track 2 membershipReference=0, got %d", mr)
			}

			// Stock Engine 5.3.0 pack/export query must succeed against a fresh target DB.
			assertStockPackExportSucceeds(t, db, 1, 4)
		})
	}
}

func TestS3OrphanPlaylistEntitiesRemoved(t *testing.T) {
	db, _ := newLibrary(t)
	track(t, db, 1)
	track(t, db, 2)
	mustExec(t, db, "INSERT INTO Playlist(id,title,parentListId,nextListId,isPersisted) VALUES(1,'Live',0,0,1)")
	mustExec(t, db, "PRAGMA foreign_keys=OFF")
	mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(10,1,1,'test-library',0,0)")
	mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(20,99,2,'test-library',0,0)")
	mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(30,NULL,2,'test-library',0,0)")
	mustExec(t, db, "PRAGMA foreign_keys=ON")

	if _, err := heal(db, true); err != nil {
		t.Fatal(err)
	}

	var orphans int
	if err := db.QueryRow("SELECT count(*) FROM PlaylistEntity WHERE listId IS NULL OR listId NOT IN (SELECT id FROM Playlist)").Scan(&orphans); err != nil {
		t.Fatal(err)
	}
	if orphans != 0 {
		t.Fatalf("expected 0 orphan PlaylistEntity rows, got %d", orphans)
	}

	// Re-creating Playlist 99 and exporting track 2 into it must not collide.
	mustExec(t, db, "INSERT INTO Playlist(id,title,parentListId,nextListId,isPersisted) VALUES(99,'Recreated',0,0,1)")
	mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(20,99,2,'test-library',0,0)")
}

func TestS3AdvancesLaggingSqliteSequence(t *testing.T) {
	db, _ := newLibrary(t)
	track(t, db, 25)
	mustExec(t, db, "INSERT INTO Playlist(id,title,parentListId,nextListId,isPersisted) VALUES(10,'Main',0,0,1)")
	mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(50,10,25,'test-library',0,0)")
	mustExec(t, db, "INSERT INTO AlbumArt(id,hash,albumArt) VALUES(5,'h',X'00')")
	mustExec(t, db, "DELETE FROM AlbumArt WHERE id=5")

	// Simulate corrupt/lagging sqlite_sequence entries (below MAX(id)) while AlbumArt stays ahead.
	mustExec(t, db, "UPDATE sqlite_sequence SET seq=10 WHERE name='PlaylistEntity'")
	mustExec(t, db, "UPDATE sqlite_sequence SET seq=2 WHERE name='Playlist'")
	mustExec(t, db, "UPDATE sqlite_sequence SET seq=5 WHERE name='Track'")
	mustExec(t, db, "UPDATE sqlite_sequence SET seq=99 WHERE name='AlbumArt'")

	if _, err := heal(db, true); err != nil {
		t.Fatal(err)
	}

	checkSeq := func(table string, want int64) {
		t.Helper()
		var got int64
		if err := db.QueryRow("SELECT seq FROM sqlite_sequence WHERE name=?", table).Scan(&got); err != nil {
			t.Fatalf("sqlite_sequence[%s]: %v", table, err)
		}
		if got != want {
			t.Fatalf("sqlite_sequence[%s] = %d, want %d", table, got, want)
		}
	}
	checkSeq("PlaylistEntity", 50)
	checkSeq("Playlist", 10)
	checkSeq("Track", 25)
	checkSeq("AlbumArt", 99) // Never lower seq.

	// Sequence-allocated next PlaylistEntity.id and Track.id must succeed without PK or trigger failure.
	var nextEntityID int64
	if err := db.QueryRow("SELECT seq+1 FROM sqlite_sequence WHERE name='PlaylistEntity'").Scan(&nextEntityID); err != nil {
		t.Fatal(err)
	}
	mustExec(t, db, "INSERT INTO Track(id,path,isAnalyzed) VALUES(26,'Music/26.mp3',1)")
	mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(?,10,26,'test-library',0,0)", nextEntityID)
}

func TestS3CrossListAndNegativeNextEntityIdExportSucceeds(t *testing.T) {
	db, _ := newLibrary(t)
	track(t, db, 1)
	track(t, db, 2)
	track(t, db, 3)
	mustExec(t, db, "INSERT INTO Playlist(id,title,parentListId,nextListId,isPersisted) VALUES(1,'One',0,2,1),(2,'Two',0,0,1)")
	// Row 10 points across lists to row 30 (in list 2); row 20 carries a negative nextEntityId
	// left by an interrupted stock pack/export transaction.
	mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(10,1,1,'test-library',30,0)")
	mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(20,1,2,'test-library',-11,0)")
	mustExec(t, db, "INSERT INTO PlaylistEntity(id,listId,trackId,databaseUuid,nextEntityId,membershipReference) VALUES(30,2,3,'test-library',0,0)")

	if _, err := heal(db, true); err != nil {
		t.Fatal(err)
	}

	// Stock Engine PlaylistOrder CTE (which joins without listId filter on recursion)
	// must only traverse list 1's two tracks, never pulling list 2's row 30.
	rows, err := db.Query(`
WITH PlaylistOrder AS (
	SELECT id AS entityId, nextEntityId, 1 AS reverseOrder, trackId
	FROM PlaylistEntity WHERE nextEntityId = 0 AND listId = 1
	UNION ALL
	SELECT c.id, c.nextEntityId, l.reverseOrder + 1, c.trackId
	FROM PlaylistEntity c
	JOIN PlaylistOrder l ON c.nextEntityId = l.entityId
)
SELECT entityId FROM PlaylistOrder ORDER BY reverseOrder DESC`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var got []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			t.Fatal(err)
		}
		got = append(got, id)
	}
	if fmt.Sprint(got) != "[10 20]" {
		t.Fatalf("PlaylistOrder list 1 = %v, want [10 20]", got)
	}

	assertStockPackExportSucceeds(t, db, 1, 2)
	assertStockPackExportSucceeds(t, db, 2, 1)
}

// assertStockPackExportSucceeds replays Engine 5.3.0's CraylistEntitiesToPack export
// query from src into a fresh Engine 5.0.4/5.3.0 target database.
func assertStockPackExportSucceeds(t *testing.T, src *sql.DB, listID int64, wantRows int) {
	t.Helper()
	dstPath := filepath.Join(t.TempDir(), "dst.db")
	dst, err := sql.Open("sqlite", dstPath)
	if err != nil {
		t.Fatal(err)
	}
	schema, err := os.ReadFile("testdata/engine-5.0.4.sql")
	if err != nil {
		dst.Close()
		t.Fatal(err)
	}
	mustExec(t, dst, strings.ReplaceAll(string(schema), "CREATE TABLE sqlite_sequence(name,seq);", ""))
	mustExec(t, dst, "INSERT INTO Information(uuid,schemaVersionMajor,schemaVersionMinor,schemaVersionPatch) VALUES('target-library',3,0,2)")
	mustExec(t, dst, "INSERT INTO Playlist(id,title,parentListId,nextListId,isPersisted) VALUES(1,'Exported',0,0,1)")
	dst.Close()

	mustExec(t, src, fmt.Sprintf("ATTACH DATABASE '%s' AS dst", strings.ReplaceAll(dstPath, "'", "''")))
	defer mustExec(t, src, "DETACH DATABASE dst")

	mustExec(t, src, "CREATE TEMP TABLE IF NOT EXISTS CraylistsToPack(id INTEGER PRIMARY KEY, sourceId INTEGER)")
	mustExec(t, src, "DELETE FROM CraylistsToPack")
	mustExec(t, src, "INSERT INTO CraylistsToPack(id, sourceId) VALUES(1, ?)", listID)

	mustExec(t, src, `
CREATE TEMP TABLE IF NOT EXISTS CraylistEntitiesToPack (
	id INTEGER,
	listId INTEGER,
	trackId INTEGER,
	databaseUuid TEXT,
	nextEntityId INTEGER,
	membershipReference INTEGER
)`)
	mustExec(t, src, "DELETE FROM CraylistEntitiesToPack")
	mustExec(t, src, `
WITH targetEntities AS (
	SELECT
		craylistEntities.id AS id,
		CraylistsToPack.id AS newListId,
		sourceEntities.trackId,
		sourceEntities.databaseUuid,
		sourceEntities.id AS sourceId,
		sourceEntities.nextEntityId AS sourceNextId,
		sourceEntities.membershipReference AS sourceMR,
		craylistEntities.membershipReference AS targetMR
	FROM (
		SELECT * FROM main.PlaylistEntity
		WHERE listId IN (SELECT sourceId FROM CraylistsToPack)
	) AS sourceEntities
	LEFT JOIN CraylistsToPack
		ON sourceEntities.listId = CraylistsToPack.sourceId
	LEFT JOIN dst.PlaylistEntity craylistEntities
		ON craylistEntities.listId = newListId
		AND (sourceEntities.trackId, sourceEntities.databaseUuid)
			= (craylistEntities.trackId, craylistEntities.databaseUuid)
),
targetEntitiesWithPos AS (
	SELECT *, 1 AS pos FROM targetEntities WHERE sourceNextId = 0
	UNION ALL
	SELECT targetEntities.*, pos+1 AS pos
	FROM targetEntities
	INNER JOIN targetEntitiesWithPos
		ON targetEntitiesWithPos.sourceId = targetEntities.sourceNextId
),
newEntities AS (
	SELECT
		IFNULL(id, newId + COALESCE((SELECT max(id) FROM dst.PlaylistEntity), 0)) AS id,
		newListId, trackId, databaseUuid, sourceMR, targetMR, pos
	FROM (
		SELECT
			row_number() OVER (PARTITION BY targetEntitiesWithPos.id) AS newId,
			targetEntitiesWithPos.*
		FROM targetEntitiesWithPos
	)
)
INSERT INTO CraylistEntitiesToPack(id, listId, trackId, databaseUuid, nextEntityId, membershipReference)
SELECT
	id, newListId AS listId, trackId, databaseUuid,
	LEAD(id, 1, 0) OVER (PARTITION BY newListId ORDER BY pos DESC) AS nextEntityId,
	CASE
		WHEN targetMR IS NULL THEN sourceMR
		WHEN targetMR = 0 THEN 0
		ELSE sourceMR
	END AS membershipReference
FROM newEntities`)

	// Export twice: second pass exercises ON CONFLICT(id) matching existing target rows.
	for pass := 1; pass <= 2; pass++ {
		if _, err := src.Exec(`
INSERT INTO dst.PlaylistEntity (id, listId, trackId, databaseUuid, nextEntityId, membershipReference)
SELECT id, listId, trackId, databaseUuid, nextEntityId, membershipReference
FROM CraylistEntitiesToPack
WHERE TRUE
ON CONFLICT(id) DO UPDATE SET
	listId = excluded.listId,
	trackId = excluded.trackId,
	databaseUuid = excluded.databaseUuid,
	nextEntityId = excluded.nextEntityId,
	membershipReference = excluded.membershipReference`); err != nil {
			t.Fatalf("stock export pass %d failed: %v", pass, err)
		}
	}

	var exported int
	if err := src.QueryRow("SELECT count(*) FROM dst.PlaylistEntity WHERE listId=1").Scan(&exported); err != nil {
		t.Fatal(err)
	}
	if exported != wantRows {
		t.Fatalf("exported %d rows, want %d", exported, wantRows)
	}
}
