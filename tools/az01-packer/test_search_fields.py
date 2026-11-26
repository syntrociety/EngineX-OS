"""Regression checks for S23 (#336): persisting SearchMenuDialog search-filter fields across restarts."""
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from legacy_names import LAB as LAB_STR

REPO = Path(__file__).resolve().parents[2]
LAB = Path(LAB_STR)
BUILDER = REPO / "tools" / "ui-mod" / "build_packs.py"

spec = importlib.util.spec_from_file_location("build_packs_s23", BUILDER)
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)

EXPECTED_ROLES = [
    "song", "artist", "album", "length", "key", "comment",
    "bpm", "genre", "label", "year", "dateImported", "filename",
]


def _extract_fn(source: str, name: str) -> str:
    m = re.search(rf"(?m)^\tfunction {re.escape(name)}\([^\n]*\) \{{", source)
    if not m:
        raise AssertionError(f"function {name} not found")
    line_end = source.index("\n", m.start())
    if source[m.start():line_end].rstrip().endswith("}"):
        return source[m.start():line_end]
    end = source.index("\n\t}", m.end()) + 3
    return source[m.start():end]


class SearchFieldsPersistenceTest(unittest.TestCase):
    def _build_pack(self, version: str, out: Path) -> Path:
        qrc = LAB / version / "qrc"
        if not qrc.is_dir():
            self.skipTest(f"firmware QRC fixture missing: {qrc}")
        res = subprocess.run(
            ["python3", str(BUILDER), "--qrc", str(qrc), "--out", str(out)],
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0, f"{version} build failed:\n{res.stderr}\n{res.stdout}")
        return out

    def test_search_qml_and_embedded_main_patched_for_530_and_504_all_languages(self):
        for version, skin in (("5.3.0", "Skins/Embedded"), ("5.0.4", "Skins/EmbeddedCommon")):
            with self.subTest(version=version):
                with tempfile.TemporaryDirectory() as td:
                    out = self._build_pack(version, Path(td) / "packs")
                    for lang in ("en", "zh-Hans", "zh-Hant", "es"):
                        with self.subTest(lang=lang):
                            search_path = out / lang / skin / "Views/BrowseView/Components/Search.qml"
                            main_path = out / lang / skin / "EmbeddedMain.qml"
                            self.assertTrue(search_path.is_file(), f"missing {search_path}")
                            self.assertTrue(main_path.is_file(), f"missing {main_path}")
                            search_qml = search_path.read_text(encoding="utf-8")
                            main_qml = main_path.read_text(encoding="utf-8")

                            # All 12 stock role identifiers remain intact in every language pack.
                            for role in EXPECTED_ROLES:
                                self.assertIn(role, search_qml, f"{version}/{lang} lost role {role}")

                            # Search.qml syncs from Engine without saving on mount, suppresses dialog echo during sync,
                            # saves on user dialog edit, and re-syncs when EmbeddedMain restores or resets search fields.
                            self.assertIn("property bool scSyncingSearchFields: false", search_qml)
                            self.assertIn("if (search.scSyncingSearchFields) return", search_qml)
                            self.assertIn("function scSyncFromEngine()", search_qml)
                            self.assertIn("function onScSearchFieldsVersionChanged()", search_qml)
                            self.assertIn("root.scSaveSearchFields(", search_qml)
                            self.assertIn("search.scSyncFromEngine()", search_qml)
                            comp_tail = search_qml.split("Component.onCompleted:", 1)[1]
                            self.assertNotIn("scSaveSearchFields", comp_tail, "Component.onCompleted must not overwrite saved settings.conf")

                            # EmbeddedMain.qml wires restore, single-flight save, passive reset, and offline settings.conf parsing.
                            for needle in (
                                "scSearchFieldOrder",
                                "scSearchFields",
                                "function scNormalizeSearchFields(",
                                "function scFlushSearchFields(",
                                "function scSyncSearchFields(",
                                "function scSaveSearchFields(",
                                "function scResetSearchFields(",
                                'key === "search_fields"',
                                '"/Client/Preferences/ResetAll"',
                            ):
                                self.assertIn(needle, main_qml, f"{version}/{lang} EmbeddedMain missing {needle}")
                            self.assertNotIn(
                                'ActionTranslatorStateChangeNotifier {\n\t\tprop: Planck.getProperty("/Client/Preferences/ResetAll")',
                                main_qml,
                                f"{version}/{lang} EmbeddedMain must not clear /Client/Preferences/ResetAll state before Search.qml",
                            )

    @unittest.skipUnless(shutil.which("node"), "node required for QML JS behavior checks")
    def test_search_fields_state_machine_js(self):
        fns = "\n".join(
            _extract_fn(bp.SERVICES_MAIN, name)
            for name in (
                "scSameItems",
                "scNormalizeSearchFields",
                "scFlushSearchFields",
                "scSyncSearchFields",
                "scSaveSearchFields",
                "scResetSearchFields",
                "scParseSettingsConf",
                "scCall",
                "scUiPollReply",
            )
        )
        script = r"""
const assert = require('node:assert/strict');

let nowMs = 10000;
Date.now = () => nowMs;

let engineRoles = ["song", "artist"];
let searchAll = false;
const setCalls = [];
let resetCalls = 0;
const posted = [];
const inflight = [];

const Planck = {
    getProperty(path) {
        if (path === "/Client/Librarian/SearchAll") {
            return { translator: { get state() { return searchAll; }, set state(v) { searchAll = v; } } };
        }
        return null;
    },
    librarianController: {
        searchConstraintRolesList() { return engineRoles.slice(); },
        setSearchConstraint(roles) {
            setCalls.push(roles.slice());
            engineRoles = roles.slice();
        },
        resetSearchConstraintsToDefaults() {
            resetCalls++;
            engineRoles = ["song", "artist"];
        },
    },
};

const scSearchFieldOrder = ["song", "artist", "album", "length", "key", "comment", "bpm", "genre", "label", "year", "dateImported", "filename"];
let scSearchFields = ["song", "artist"];
let scSearchFieldsRestored = false;
let scSearchFieldsDirty = false;
let scSearchFieldsSending = false;
let scSearchFieldsQueued = null;
let scSearchFieldsSeq = 0;
let scSearchFieldsReqId = 0;
let scUiPollSearchSeq = 0;
let scSearchFieldsSentAt = 0;
let scSearchFieldsVersion = 0;

// Stubs needed by scCall / scUiPollReply / scParseSettingsConf
let scUiPoll = null, scScopePending = 0, scScope = "", scUiPollScope = "", scUiPollAt = 0, scUiDigest = "", scUiAppliedAt = 0, scUiLoaded = true;
let scLoggedIn = [], scSources = [], scLoginOpen = "", scLogin = "";
let scCueCountdown = true, scBpmDecimals = 2, scTracklistWaveform = true;
function scFxMsInteger() { return 500; }
function scFxMsApply() {}
function scEnsureTidal() {}
function scOpen() {}
function scApply(v) {
    if (v && v.searchFields !== undefined) scSyncSearchFields(v.searchFields);
}
const root = {
    scIsPlayerBoard: false,
    scReverseJogLeft: false, scReverseJogRight: false,
    scSdHighSpeed: false, scHifiOutput: false, scBeatJumpPads: false,
    scRestoreBpmOnUnsync: false, scDualColumn: false, scTracklistWaveform: true,
    scCueCountdown: true, scBpmDecimals: 2, scSyncGuardPct: 0, scSwapLoopOrder: false,
    scLedBrightness: 100, scUIScale: 100, scStemsEqMode: false, scPitchBendBeatJump: false,
    scMixstreamFx: [], scPioneerFx: "off", scSearchFields: ["song", "artist"],
};

let autoReply = false;
class XMLHttpRequest {
    constructor() { this.readyState = 0; this.status = 0; this.responseText = ""; this.headers = {}; }
    open(method, url) { this.method = method; this.url = url; this.readyState = 1; }
    abort() { this.readyState = 0; }
    getResponseHeader(k) { return this.headers[k] || ""; }
    send() {
        const q = this.url.indexOf("?") >= 0 ? this.url.split("?")[1] : "";
        if (this.method === "POST") posted.push(q);
        const req = {
            xhr: this,
            query: q,
            reply(obj, status = 200, digest = "") {
                this.xhr.status = status;
                this.xhr.responseText = JSON.stringify(obj);
                this.xhr.headers = digest ? { "X-UI-Digest": digest } : {};
                this.xhr.readyState = 4;
                if (this.xhr.onreadystatechange) this.xhr.onreadystatechange();
            },
        };
        if (autoReply && q.startsWith("searchFields=")) {
            const parsed = decodeURIComponent(q.split("=")[1] || "").split(",").filter(Boolean);
            req.reply({ searchFields: parsed });
        } else {
            inflight.push(req);
        }
    }
}

""" + fns + r"""

// 1. Default boot: saved=["song","artist"], Engine already ["song","artist"] -> zero setSearchConstraint calls.
scSyncSearchFields(["song", "artist"]);
assert.equal(scSearchFieldsRestored, true);
assert.equal(setCalls.length, 0, "default boot should not call setSearchConstraint");
assert.equal(scSearchFieldsVersion, 0);
assert.equal(searchAll, false);

// 2. Fresh reboot with saved custom fields: applies once, bumps version, never touches SearchAll.
scSearchFieldsRestored = false;
scSearchFields = ["song", "artist"];
searchAll = true;
scSyncSearchFields(["bpm", "comment", "song", "key"]);
assert.deepEqual(engineRoles, ["song", "key", "comment", "bpm"]);
assert.equal(setCalls.length, 1);
assert.equal(scSearchFieldsVersion, 1);
assert.equal(searchAll, true, "SearchAll belongs to stock PlanckClientState.json and must not be overwritten");

// 3. Repeated unchanged poll is a no-op.
scSyncSearchFields(["song", "key", "comment", "bpm"]);
assert.equal(setCalls.length, 1);
assert.equal(scSearchFieldsVersion, 1);

// 4. Rapid burst of 3 checkbox taps + concurrent unrelated POST (?playhead=...) returning late
// (exact race caught live on JP07): single-flight serializes searchFields POSTs AND scSearchFieldsSeq
// strips stale searchFields from the late playhead reply after dirty clears.
autoReply = false;
engineRoles = ["song", "artist", "bpm"];
scSaveSearchFields(["song", "artist", "bpm"]);
const firstSfReq = inflight.shift();
assert.equal(firstSfReq.query, "searchFields=song%2Cartist%2Cbpm");

// Unrelated POST ?playhead=... starts while searchFields=song,artist,bpm is on disk.
scCall("playhead=0.33", function() {});
const playheadReq = inflight.shift();
assert.equal(playheadReq.query, "playhead=0.33");

// User rapidly taps key + comment before first POST returns.
engineRoles = ["song", "artist", "key", "bpm"];
scSaveSearchFields(["song", "artist", "key", "bpm"]);
engineRoles = ["song", "artist", "key", "comment", "bpm"];
scSaveSearchFields(["song", "artist", "key", "comment", "bpm"]);
assert.equal(inflight.length, 0, "must keep at most one searchFields POST in flight during rapid burst");
assert.equal(scSearchFieldsDirty, true);
assert.equal(scSearchFieldsSending, true);

// First searchFields reply completes -> must NOT revert Engine and must flush coalesced final set.
// Also simulate scKeepPlayhead firing an unrelated POST ?playhead=... WHILE scSearchFieldsDirty is still true
// (at the exact same scSearchFieldsSeq as the queued final searchFields POST — caught live on JP11/JP07!).
firstSfReq.reply({ searchFields: ["song", "artist", "bpm"] });
assert.deepEqual(engineRoles, ["song", "artist", "key", "comment", "bpm"], "intermediate reply must not clobber newer tap");
assert.equal(inflight.length, 1, "coalesced final selection must flush right after first POST");
const finalSfReq = inflight.shift();
assert.equal(finalSfReq.query, "searchFields=song%2Cartist%2Ckey%2Ccomment%2Cbpm");
assert.equal(scSearchFieldsDirty, true);
scCall("playhead=0.42", function() {});
const midDirtyPlayheadReq = inflight.shift();
assert.equal(midDirtyPlayheadReq.query, "playhead=0.42");

// Final searchFields reply completes -> dirty clears.
finalSfReq.reply({ searchFields: ["song", "artist", "key", "comment", "bpm"] });
assert.equal(scSearchFieldsDirty, false);
assert.equal(scSearchFieldsSending, false);

// Now BOTH earlier unrelated POSTs (?playhead=0.33 sent before seq++ AND ?playhead=0.42 sent mid-dirty
// at the same seq!) return with STALE searchFields=["song","artist","bpm"]: both MUST be stripped!
playheadReq.reply({ playhead: 0.33, searchFields: ["song", "artist", "bpm"] });
midDirtyPlayheadReq.reply({ playhead: 0.42, searchFields: ["song", "artist", "bpm"] });
assert.deepEqual(engineRoles, ["song", "artist", "key", "comment", "bpm"], "stale unrelated POST replies must not revert Engine");
assert.deepEqual(scSearchFields, ["song", "artist", "key", "comment", "bpm"]);

// 5. Stale GET /v1/enginex/ui poll started before a user tap must not revert Engine after save completes.
scCall(); // starts scUiPoll with current scSearchFieldsSeq
const stalePoll = inflight.shift();
engineRoles = ["genre", "label"];
scSaveSearchFields(["genre", "label"]);
const genreReq = inflight.shift();
genreReq.reply({ searchFields: ["genre", "label"] });
assert.equal(scSearchFieldsDirty, false);
stalePoll.reply({ searchFields: ["song", "artist", "key", "comment", "bpm"] }, 200, "old-digest");
assert.deepEqual(engineRoles, ["genre", "label"], "stale GET poll must not revert Engine");
assert.equal(scUiDigest, "", "stale poll must not poison digest cache");

// 6. Failed POST retry: non-200 POST leaves dirty=true; poll after >3 s retries POST without reverting Engine.
engineRoles = ["album", "year"];
scSaveSearchFields(["album", "year"]);
const failedReq = inflight.shift();
failedReq.reply({}, 500);
assert.equal(scSearchFieldsDirty, true);
nowMs += 500; // within retry window: stale poll ignored
scSyncSearchFields(["genre", "label"]);
assert.deepEqual(engineRoles, ["album", "year"]);
assert.equal(inflight.length, 0);
nowMs += 3500; // past retry window: next poll triggers retry
scCall();
const retryPoll = inflight.shift();
retryPoll.reply({ searchFields: ["genre", "label"] }, 200, "d-retry");
assert.equal(inflight.length, 1, "should retry unacknowledged user edit");
const retryReq = inflight.shift();
assert.equal(retryReq.query, "searchFields=album%2Cyear");
retryReq.reply({ searchFields: ["album", "year"] });
assert.equal(scSearchFieldsDirty, false);
assert.deepEqual(engineRoles, ["album", "year"]);

// 7. External API / Settings update while clean applies to Engine and bumps version.
const verBefore = scSearchFieldsVersion;
scSyncSearchFields(["year", "filename"]);
assert.deepEqual(engineRoles, ["year", "filename"]);
assert.equal(scSearchFieldsVersion, verBefore + 1);

// 8. Reset All restores stock defaults and persists default clear.
autoReply = true;
scResetSearchFields();
assert.equal(resetCalls, 1);
assert.deepEqual(engineRoles, ["song", "artist"]);
assert.equal(posted[posted.length - 1], "searchFields=song%2Cartist");
assert.equal(scSearchFieldsDirty, false);

// 9. Offline settings.conf parsing applies search_fields= and ignores empty/default.
scSearchFieldsRestored = false;
engineRoles = ["song", "artist"];
scParseSettingsConf("lang=en\nsearch_fields=filename,bpm,song\n");
assert.deepEqual(engineRoles, ["song", "bpm", "filename"]);
assert.deepEqual(scSearchFields, ["song", "bpm", "filename"]);
"""
        res = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"node failed:\n{res.stderr}\n{res.stdout}")


if __name__ == "__main__":
    unittest.main()
