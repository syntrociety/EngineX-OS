"""Regression tests for #336 E4: settings must take effect on a single click/gesture.

Covers:
1. SwitchEntry.qml (5.0.4, 5.3.0, UIKit) keeps T.Switch.checked in sync with checkedExternally
   so touch slides/drags on default-ON or externally updated switches emit toggled() on the
   first gesture, while leaving stock checked:-only bindings untouched.
2. STREAM_SWITCHES uses checkedExternally (not checked) and scSetSource updates scSources
   optimistically so rapid multi-service toggles never re-enable the first service.
3. scCall serializes setting POSTs in FIFO order (with safe adjacent single-key coalescing),
   applies setting changes optimistically, updates scUiDigest/scUiAppliedAt on completion,
   resyncs on non-200 failure, and blocks stale scUiPoll replies started before a write.
4. scTunnelToggle (Remote access) and scBtLoad/scBtSet (Bluetooth Output Source) apply
   optimistically, serialize rapid updates, and ignore stale polls/loads.
"""
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import legacy_names as legacy

REPO = Path(__file__).resolve().parents[2]
BUILD_PACKS = REPO / "tools" / "ui-mod" / "build_packs.py"
LAB = Path(legacy.LAB)


def load_build_packs():
    spec = importlib.util.spec_from_file_location("build_packs", BUILD_PACKS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def extract_function(text, name, optional=False):
    m = re.search(rf"(?m)^\tfunction {re.escape(name)}\([^\n]*\) \{{", text)
    if not m:
        if optional:
            return ""
        raise AssertionError(f"missing function {name}")
    i = m.end() - 1
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[m.start():j + 1]
    raise AssertionError(f"unclosed function {name}")


def run_node(script):
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"node failed ({res.returncode}):\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
    return res.stdout


class SettingsSingleClickE4Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bp = load_build_packs()

    def test_switch_entry_syncs_checked_with_checked_externally_and_stream_switches_use_it(self):
        for version, skin in (("5.0.4", "EmbeddedCommon"), ("5.3.0", "Embedded")):
            qrc = LAB / version / "qrc"
            if not qrc.is_dir():
                self.skipTest(f"{qrc} not found (run on build host)")
            with tempfile.TemporaryDirectory(prefix=f"e4-sw-{version}-") as tmp:
                out = Path(tmp) / "packs"
                built = subprocess.run(
                    ["python3", str(BUILD_PACKS), "--qrc", str(qrc), "--out", str(out)],
                    capture_output=True, text=True,
                )
                self.assertEqual(built.returncode, 0, built.stderr)
                for lang in ("en", "zh-Hans", "zh-Hant", "es"):
                    with self.subTest(version=version, lang=lang):
                        sw = (out / lang / f"Skins/{skin}/Views/ControlCenter/Tabs/Preferences/Entries/Base/SwitchEntry.qml").read_text(encoding="utf-8")
                        self.assertIn("property alias checkedExternally: control.checkedExternally", sw)
                        self.assertIn("Component.onCompleted: if (checked !== checkedExternally) checked = checkedExternally", sw)
                        self.assertIn("onCheckedExternallyChanged: if (checked !== checkedExternally) checked = checkedExternally", sw)
                        self.assertIn("parent.clicked()", sw)
                        self.assertRegex(sw, r"onToggled:\s*\{[\s\S]*parent\.clicked\(\)[\s\S]*if \(checked !== checkedExternally\) checked = checkedExternally[\s\S]*\}")
                        streaming = (out / lang / f"Skins/{skin}/Views/ControlCenter/Tabs/Preferences/Entries/StreamingServices.qml").read_text(encoding="utf-8")
                        self.assertIn("checkedExternally: root.scSources.indexOf(modelData.id) >= 0", streaming)
                        self.assertNotIn("checked: root.scSources.indexOf(modelData.id) >= 0", streaming)
                        if version == "5.3.0":
                            uikit_sw = (out / lang / "Skins/UIKit/Elements/Settings/Entries/SwitchEntry.qml").read_text(encoding="utf-8")
                            self.assertIn("Component.onCompleted: if (checked !== checkedExternally) checked = checkedExternally", uikit_sw)
                            self.assertIn("onCheckedExternallyChanged: if (checked !== checkedExternally) checked = checkedExternally", uikit_sw)

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_inflight_poll_and_stale_digest_never_clobber_setting_toggle(self):
        main = self.bp.SERVICES_MAIN
        code = "\n".join(filter(None, [
            extract_function(main, "scSameItems"),
            extract_function(main, "scFxMsInteger"),
            extract_function(main, "scFxMsApply"),
            extract_function(main, "scApplyQuery", optional=True),
            extract_function(main, "scUiSyncPending", optional=True),
            extract_function(main, "scUiFlush", optional=True),
            extract_function(main, "scCall"),
            extract_function(main, "scUiPollReply"),
        ]))
        script = f"""
const assert = require('node:assert/strict');
const reqs = [];
class XMLHttpRequest {{
    constructor() {{ this.readyState = 0; this.status = 0; this.responseText = ''; this.headers = {{}}; this.aborted = false; }}
    open(m, u) {{ this.method = m; this.url = u; this.readyState = 1; }}
    send() {{ reqs.push(this); }}
    abort() {{ this.aborted = true; this.readyState = 0; }}
    getResponseHeader(k) {{ return this.headers[k] || ''; }}
    finish(status, body, digest = '') {{
        this.status = status;
        this.responseText = typeof body === 'string' ? body : JSON.stringify(body);
        this.headers = digest ? {{'X-UI-Digest': digest}} : {{}};
        this.readyState = 4;
        if (this.onreadystatechange) this.onreadystatechange();
    }}
}}
let scScope = '', scScopePending = 0, scUiPoll = null, scUiPollScope = '', scUiPollAt = 0, scUiDigest = '', scUiAppliedAt = 0;
let scUiWriteGen = 0, scUiPollGen = 0, scUiPending = 0, scUiQueue = [], scUiSending = false, scUiSendingAt = 0, scUiSendToken = 0, scUiSendXhr = null, scUiHadError = false;
let scTunnelPending = 0, scUiLoaded = true, scIsPlayerBoard = true, scConsoleLineBoard = false, scFourDeckOn = false, scHasMixer = false, scTransitionFxView = null;
let scMasterLimiter = false, scAutoGain = false, scLedBrightness = 100, scLedBrightnessSaved = 100, scSources = ['tidal', 'spotify'], scLoggedIn = [];
let scFxTimeMode = 'beats', scFxTimeMs = 500, scFxMsWanted = -1;
let scPadDelayRestarts = 0;
const scPadBrightnessDelay = {{ restart() {{ scPadDelayRestarts++; }} }};
let applyCount = 0;
function scEnsureTidal() {{}}
function scOpen(s) {{ scScope = s; }}
function scApply(v) {{
    applyCount++;
    if (v.loggedIn) scLoggedIn = v.loggedIn;
    if (v.masterLimiter !== undefined) scMasterLimiter = !!v.masterLimiter;
    if (v.autoGain !== undefined) scAutoGain = !!v.autoGain;
    if (v.ledBrightness !== undefined) {{
        const brt = parseInt(v.ledBrightness) || 100;
        scLedBrightness = brt;
        scLedBrightnessSaved = brt;
    }}
    if (v.sources) scSources = v.sources;
}}
{code}
// 1. Initial poll completes with d0.
scCall('');
reqs[0].finish(200, {{masterLimiter: false, autoGain: false}}, 'd0');
assert.equal(scUiDigest, 'd0');
// 2. User enables Auto-Gain -> immediate optimistic update and digest updated to d1 on reply.
scCall('autoGain=1');
assert.equal(scAutoGain, true, 'autoGain must flip optimistically');
reqs[1].finish(200, {{masterLimiter: false, autoGain: true}}, 'd1');
assert.equal(scUiDigest, 'd1', 'POST reply must advance scUiDigest');
// 3. 3s poll starts (captures pre-write state d1), then user taps Master Soft Clipper.
scCall('');
scCall('masterLimiter=1');
const mlPost = reqs[reqs.length - 1];
assert.ok(mlPost.url.endsWith('?masterLimiter=1'));
assert.equal(scMasterLimiter, true, 'masterLimiter must flip optimistically');
// Even if POST finishes FIRST and stale poll arrives AFTER POST, stale poll must not revert masterLimiter.
mlPost.finish(200, {{masterLimiter: true, autoGain: true}}, 'd2');
assert.equal(scMasterLimiter, true);
assert.equal(scUiDigest, 'd2');
// Stale poll (reused scUiPoll) now arrives with old d1 and masterLimiter=false:
scUiAppliedAt -= 31000; // even past the 30s safety window!
scUiPoll.finish(200, {{masterLimiter: false, autoGain: true}}, 'd1');
assert.equal(scMasterLimiter, true, 'stale poll started before POST must never revert masterLimiter');
assert.equal(scUiDigest, 'd2', 'stale poll must not overwrite scUiDigest');
// 4. A fresh poll started AFTER the POST still applies external changes cleanly.
scCall('');
scUiPoll.finish(200, {{masterLimiter: false, autoGain: true}}, 'd3');
assert.equal(scMasterLimiter, false, 'fresh post-write poll must still reflect external console changes');
"""
        run_node(script)

        for case, action in {
            "read_during_write": """
scCall('masterLimiter=1');
const write = reqs.at(-1), count = reqs.length;
scCall('');
const read = reqs.length > count ? reqs.at(-1) : null;
write.finish(200, {masterLimiter: true}, 'accepted');
if (read) read.finish(200, {masterLimiter: false}, 'old');
assert.equal(scMasterLimiter, true, 'read during write reverted acknowledged switch');
""",
            "older_callback_read": """
let callbackCount = 0;
scCall('', () => callbackCount++);
const read = reqs.at(-1);
scCall('masterLimiter=1');
reqs.at(-1).finish(200, {masterLimiter: true}, 'accepted');
read.finish(200, {masterLimiter: false}, 'old');
assert.equal(callbackCount, 1);
assert.equal(scMasterLimiter, true, 'callback GET reverted acknowledged switch');
""",
            "hung_first_post": """
const zero = Date.now();
Date.now = () => zero;
scCall('masterLimiter=1');
const hungFirst = reqs.at(-1);
scCall('autoGain=0');
for (let n = 1; n <= 8; n++) {
    Date.now = () => zero + n * 3000;
    scCall('');
    const x = reqs.at(-1);
    if (x.method === 'GET' && x.readyState !== 4) x.finish(200, {autoGain: true}, 'remote');
}
const secondPost = reqs.find(x => x.method === 'POST' && x.url.endsWith('?autoGain=0'));
assert.ok(secondPost, 'second tap still unsent after eight watchdog ticks');
secondPost.finish(200, {masterLimiter: false, autoGain: false}, 'after-second');
assert.equal(scAutoGain, false);
assert.equal(scMasterLimiter, false);
// Delayed reply from the timed-out first POST must never supersede newer state.
hungFirst.finish(200, {masterLimiter: true, autoGain: true}, 'late-hung');
assert.equal(scAutoGain, false, 'delayed reply from timed-out POST must be ignored');
assert.equal(scMasterLimiter, false, 'delayed reply from timed-out POST must be ignored');
""",
            "hung_single_post_reconciles_state": """
const zero = Date.now();
Date.now = () => zero;
scCall('masterLimiter=1');
const hungOnly = reqs.at(-1);
assert.equal(scMasterLimiter, true);
for (let n = 1; n <= 7; n++) {
    Date.now = () => zero + n * 3000;
    scCall('');
}
const reconGet = reqs.at(-1);
assert.equal(reconGet.method, 'GET', 'watchdog must issue a fresh GET when no queued POST remains');
reconGet.finish(200, {masterLimiter: false}, 'reconciled');
assert.equal(scMasterLimiter, false, 'reconciled GET restores confirmed server state');
hungOnly.finish(200, {masterLimiter: true}, 'late-hung');
assert.equal(scMasterLimiter, false, 'late timed-out POST must not override reconciled state');
""",
            "login_on_write_reply": """
globalThis.scLoginOpen = 'spotify';
globalThis.scLogin = 'spotify';
scCall('masterLimiter=1');
reqs.at(-1).finish(200, {masterLimiter: true, loggedIn: ['spotify']}, 'login');
scCall('');
reqs.at(-1).finish(200, {masterLimiter: true, loggedIn: ['spotify']}, 'login');
assert.equal(scScope, 'spotify', 'cached POST digest skipped login auto-open');
assert.equal(scLoginOpen, '');
assert.equal(scLogin, '');
""",
            "qr_callback_during_write": extract_function(main, "scLoginDone") + """
globalThis.root = {scLoginOpen: 'spotify'};
globalThis.scLogin = 'spotify';
globalThis.scEnterTries = 0;
globalThis.scEnterTimer = {start() {}};
globalThis.Planck = {librarianController: {streamingPlaylistsController: {invalidateUI() {}}}};
scCall('masterLimiter=1');
scLoginDone();
reqs.at(-1).finish(200, {masterLimiter: false, sources: ['spotify'], loggedIn: ['spotify']}, 'login');
assert.equal(scScope, 'spotify', 'login callback ignored successful GET');
assert.equal(scMasterLimiter, true, 'login callback GET during write must not revert pending setting');
""",
        }.items():
            with self.subTest(case=case):
                run_node(script + action)

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_rapid_setting_posts_serialize_coalesce_and_rollback_on_error(self):
        main = self.bp.SERVICES_MAIN
        code = "\n".join(filter(None, [
            extract_function(main, "scSameItems"),
            extract_function(main, "scFxMsInteger"),
            extract_function(main, "scFxMsApply"),
            extract_function(main, "scApplyQuery", optional=True),
            extract_function(main, "scSyncPadBrightness", optional=True),
            extract_function(main, "scUiSyncPending", optional=True),
            extract_function(main, "scUiFlush", optional=True),
            extract_function(main, "scCall"),
            extract_function(main, "scUiPollReply"),
        ]))
        script = f"""
const assert = require('node:assert/strict');
const reqs = [];
class XMLHttpRequest {{
    constructor() {{ this.readyState = 0; this.status = 0; this.responseText = ''; this.headers = {{}}; this.aborted = false; }}
    open(m, u) {{ this.method = m; this.url = u; this.readyState = 1; }}
    send() {{ reqs.push(this); }}
    abort() {{ this.aborted = true; this.readyState = 0; }}
    getResponseHeader(k) {{ return this.headers[k] || ''; }}
    finish(status, body, digest = '') {{
        this.status = status;
        this.responseText = typeof body === 'string' ? body : JSON.stringify(body);
        this.headers = digest ? {{'X-UI-Digest': digest}} : {{}};
        this.readyState = 4;
        if (this.onreadystatechange) this.onreadystatechange();
    }}
}}
let scScope = '', scScopePending = 0, scUiPoll = null, scUiPollScope = '', scUiPollAt = 0, scUiDigest = 'd0', scUiAppliedAt = Date.now();
let scUiWriteGen = 0, scUiPollGen = 0, scUiPending = 0, scUiQueue = [], scUiSending = false, scUiSendingAt = 0, scUiSendToken = 0, scUiSendXhr = null, scUiHadError = false;
let scTunnelPending = 0, scUiLoaded = true, scIsPlayerBoard = true, scConsoleLineBoard = false, scFourDeckOn = false, scHasMixer = true;
let scMasterLimiter = false, scAutoGain = false, scSdHighSpeed = false, scLedBrightness = 100, scLedBrightnessSaved = 100, scUpfaderEcho = false;
let scPadBrightnessSynced = false;
let publishedPadBrightness = 1.0;
const scPadBrightnessProp = {{ translator: {{ get value() {{ return publishedPadBrightness; }}, set value(v) {{ publishedPadBrightness = v; }} }} }};
globalThis.Planck = {{ getProperty() {{ return scPadBrightnessProp; }} }};
let scFxTimeMode = 'beats', scFxTimeMs = 500, scFxMsWanted = -1, scSources = [], scLoggedIn = [];
let transitionSets = 0;
const scTransitionFxView = {{
    _en: false,
    get enabled() {{ return this._en; }},
    set enabled(v) {{ this._en = !!v; transitionSets++; }},
}};
let scPadDelayRestarts = 0;
const scPadBrightnessDelay = {{ restart() {{ scPadDelayRestarts++; }} }};
function scEnsureTidal() {{}}
function scOpen(s) {{ scScope = s; }}
function scApply(v) {{
    if (v.masterLimiter !== undefined) scMasterLimiter = !!v.masterLimiter;
    if (v.autoGain !== undefined) scAutoGain = !!v.autoGain;
    if (v.sdHighSpeed !== undefined) scSdHighSpeed = !!v.sdHighSpeed;
    if (v.ledBrightness !== undefined) {{
        const brt = parseInt(v.ledBrightness) || 100;
        scLedBrightness = brt;
        scLedBrightnessSaved = brt;
    }}
    if (v.upfaderEcho !== undefined) scUpfaderEcho = !!v.upfaderEcho;
}}
{code}
// 1. Rapid ListEntry taps: 80 -> 60 -> 40. Only 80 is in flight; 60 coalesces into 40.
// Optimistic picker moves to 40, while hardware pad brightness waits for confirmed saves (#325).
scCall('ledBrightness=80');
scCall('ledBrightness=60');
scCall('ledBrightness=40');
assert.equal(scLedBrightness, 40, 'optimistic state tracks latest tap');
assert.equal(scLedBrightnessSaved, 100, 'confirmed hardware brightness stays at saved level while POST is in flight');
scSyncPadBrightness();
assert.equal(publishedPadBrightness, 1.0, 'slow POST must not publish unconfirmed brightness prematurely');
assert.equal(reqs.length, 1, 'only one setting POST in flight at once');
assert.ok(reqs[0].url.endsWith('?ledBrightness=80'));
// When 80 finishes (intermediate queued ACK), picker stays 40, confirmed hardware brightness advances to 80, and 40 sends next.
reqs[0].finish(200, {{ledBrightness: 80}}, 'd80');
assert.equal(scLedBrightness, 40, 'intermediate POST reply must not clobber queued tap');
assert.equal(scLedBrightnessSaved, 80, 'intermediate confirmed ACK updates hardware brightness');
scSyncPadBrightness();
assert.equal(publishedPadBrightness, 0.8);
assert.equal(reqs.length, 2);
assert.ok(reqs[1].url.endsWith('?ledBrightness=40'), '60 coalesced into 40');
reqs[1].finish(200, {{ledBrightness: 40}}, 'd40');
assert.equal(scLedBrightness, 40);
assert.equal(scLedBrightnessSaved, 40);
scSyncPadBrightness();
assert.equal(publishedPadBrightness, 0.4);
assert.equal(scUiPending, 0);
assert.ok(scPadDelayRestarts >= 2, '#325 pad brightness delay restarted on both intermediate and final confirmations');

// 2. Rapid distinct switch toggles: Master Soft Clipper ON then Auto-Gain ON.
scCall('masterLimiter=1');
scCall('autoGain=1');
assert.equal(scMasterLimiter, true);
assert.equal(scAutoGain, true);
assert.equal(reqs.length, 3);
assert.ok(reqs[2].url.endsWith('?masterLimiter=1'));
reqs[2].finish(200, {{masterLimiter: true, autoGain: false}}, 'dml');
assert.equal(scAutoGain, true, 'first switch reply must not flicker second switch off');
assert.equal(reqs.length, 4);
assert.ok(reqs[3].url.endsWith('?autoGain=1'));
reqs[3].finish(200, {{masterLimiter: true, autoGain: true}}, 'dag');
assert.equal(scMasterLimiter, true);
assert.equal(scAutoGain, true);
assert.equal(scUiPending, 0);

// 3. Fader Echo optimistic toggle also updates scTransitionFxView.enabled immediately.
scCall('upfaderEcho=1');
assert.equal(scUpfaderEcho, true);
assert.equal(scTransitionFxView.enabled, true);
assert.equal(transitionSets, 1);
reqs[4].finish(200, {{upfaderEcho: true}}, 'due');

// 4. Failed POST (e.g. sdHighSpeed 500) resyncs from server, aborting any stale in-flight poll.
scCall(''); // reqs[5] is scUiPoll (in flight with old gen)
scCall('sdHighSpeed=1'); // reqs[6]
assert.equal(scSdHighSpeed, true);
reqs[6].finish(500, {{error: 'script failed'}});
assert.equal(scUiPending, 0);
assert.equal(scUiDigest, '', 'error clears digest');
assert.equal(scUiPoll.aborted, true, 'stale in-flight poll aborted so resync runs immediately');
// Fresh GET resync runs on scUiPoll (reqs[7]) and restores server state sdHighSpeed=false.
scUiPoll.finish(200, {{sdHighSpeed: false}}, 'dsd0');
assert.equal(scSdHighSpeed, false, 'failed setting rolled back to server state');

// 5. Hung POST (>20s) recovers when the next click arrives.
scCall('masterLimiter=0'); // reqs[8]
const hungXhr = reqs[reqs.length - 1];
scUiSendingAt = Date.now() - 25000;
scCall('autoGain=0'); // aborts hungXhr and sends autoGain=0
assert.equal(hungXhr.aborted, true);
const freshXhr = reqs[reqs.length - 1];
assert.ok(freshXhr.url.endsWith('?autoGain=0'));
freshXhr.finish(200, {{masterLimiter: true, autoGain: false}}, 'drec');
assert.equal(scUiPending, 0);
"""
        run_node(script)

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_rapid_stream_source_toggles_keep_both_disabled(self):
        main = self.bp.SERVICES_MAIN
        code = "\n".join(filter(None, [
            extract_function(main, "scSameItems"),
            extract_function(main, "scFxMsInteger"),
            extract_function(main, "scFxMsApply"),
            extract_function(main, "scApplyQuery", optional=True),
            extract_function(main, "scUiSyncPending", optional=True),
            extract_function(main, "scUiFlush", optional=True),
            extract_function(main, "scCall"),
            extract_function(main, "scSetSource"),
        ]))
        script = f"""
const assert = require('node:assert/strict');
const reqs = [];
class XMLHttpRequest {{
    constructor() {{ this.readyState = 0; this.status = 0; this.responseText = ''; this.headers = {{}}; }}
    open(m, u) {{ this.method = m; this.url = u; this.readyState = 1; }}
    send() {{ reqs.push(this); }}
    abort() {{ this.readyState = 0; }}
    getResponseHeader(k) {{ return this.headers[k] || ''; }}
    finish(status, body, digest = '') {{
        this.status = status;
        this.responseText = JSON.stringify(body);
        this.headers = digest ? {{'X-UI-Digest': digest}} : {{}};
        this.readyState = 4;
        if (this.onreadystatechange) this.onreadystatechange();
    }}
}}
let scScope = '', scScopePending = 0, scUiPoll = null, scUiPollScope = '', scUiPollAt = 0, scUiDigest = '', scUiAppliedAt = 0;
let scUiWriteGen = 0, scUiPollGen = 0, scUiPending = 0, scUiQueue = [], scUiSending = false, scUiSendingAt = 0, scUiSendToken = 0, scUiSendXhr = null, scUiHadError = false;
let scTunnelPending = 0, scUiLoaded = true, scIsPlayerBoard = true, scConsoleLineBoard = false, scFourDeckOn = false, scHasMixer = false, scTransitionFxView = null;
let scSources = ['tidal', 'netease', 'spotify', 'deezer', 'soundcloud', 'beatport', 'applemusic', 'rekordbox'];
function scApply(v) {{ if (v.sources) scSources = v.sources; }}
{code}
// User disables SoundCloud, then immediately disables Deezer before POST 1 replies, while discovered rekordbox is present.
scSetSource('soundcloud', false);
scSetSource('deezer', false);
assert.equal(scSources.includes('soundcloud'), false, 'soundcloud disabled optimistically');
assert.equal(scSources.includes('deezer'), false, 'deezer disabled optimistically');
assert.equal(scSources.includes('rekordbox'), true, 'discovered rekordbox must stay visible optimistically');
assert.equal(reqs.length, 1, 'second sources POST waits behind first');
assert.equal(reqs[0].url, 'http://127.0.0.1:8080/v1/enginex/ui?sources=tidal,netease,spotify,deezer,beatport,applemusic');
reqs[0].finish(200, {{sources: ['tidal', 'netease', 'spotify', 'deezer', 'beatport', 'applemusic', 'rekordbox']}}, 'ds1');
assert.equal(reqs.length, 2);
assert.equal(reqs[1].url, 'http://127.0.0.1:8080/v1/enginex/ui?sources=tidal,netease,spotify,beatport,applemusic', 'second POST must NOT re-enable soundcloud or serialize rekordbox');
reqs[1].finish(200, {{sources: ['tidal', 'netease', 'spotify', 'beatport', 'applemusic', 'rekordbox']}}, 'ds2');
assert.deepEqual(scSources, ['tidal', 'netease', 'spotify', 'beatport', 'applemusic', 'rekordbox']);
"""
        run_node(script)

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_remote_access_tunnel_toggle_optimistic_serialized_and_poll_guarded(self):
        main = self.bp.SERVICES_MAIN
        code = "\n".join(filter(None, [
            extract_function(main, "scTunnelApply"),
            extract_function(main, "scTunnelSend", optional=True),
            extract_function(main, "scTunnelToggle"),
            extract_function(main, "scUiSyncPending", optional=True),
            extract_function(main, "scUiFlush", optional=True),
            extract_function(main, "scCall"),
            extract_function(main, "scUiPollReply"),
        ]))
        script = f"""
const assert = require('node:assert/strict');
const reqs = [];
class XMLHttpRequest {{
    constructor() {{ this.readyState = 0; this.status = 0; this.responseText = ''; this.headers = {{}}; this.aborted = false; }}
    open(m, u) {{ this.method = m; this.url = u; this.readyState = 1; }}
    setRequestHeader() {{}}
    send(body) {{ this.body = body; reqs.push(this); }}
    abort() {{ this.aborted = true; this.readyState = 0; }}
    getResponseHeader(k) {{ return this.headers[k] || ''; }}
    finish(status, body, digest = '') {{
        this.status = status;
        this.responseText = JSON.stringify(body);
        this.headers = digest ? {{'X-UI-Digest': digest}} : {{}};
        this.readyState = 4;
        if (this.onreadystatechange) this.onreadystatechange();
    }}
}}
let scScope = '', scScopePending = 0, scUiPoll = null, scUiPollScope = '', scUiPollAt = 0, scUiDigest = 'd0', scUiAppliedAt = Date.now();
let scUiWriteGen = 0, scUiPollGen = 0, scUiPending = 0, scUiQueue = [], scUiSending = false, scUiSendingAt = 0, scUiSendToken = 0, scUiSendXhr = null, scUiHadError = false;
let scRemoteAccess = false, scRemoteAccessStatus = 'stopped', scRemoteAccessUrl = '', scRemoteAccessError = '';
let scTunnelPending = 0, scTunnelSending = false, scTunnelQueued = null, scTunnelSendingAt = 0, scTunnelSendToken = 0, scTunnelSendXhr = null;
let scUiLoaded = true, scLoggedIn = [], scSources = [];
const root = {{
    get scRemoteAccess() {{ return scRemoteAccess; }}, set scRemoteAccess(v) {{ scRemoteAccess = v; }},
    get scRemoteAccessStatus() {{ return scRemoteAccessStatus; }}, set scRemoteAccessStatus(v) {{ scRemoteAccessStatus = v; }},
    get scRemoteAccessUrl() {{ return scRemoteAccessUrl; }}, set scRemoteAccessUrl(v) {{ scRemoteAccessUrl = v; }},
    get scRemoteAccessError() {{ return scRemoteAccessError; }}, set scRemoteAccessError(v) {{ scRemoteAccessError = v; }},
    scTunnelApply(v) {{ scTunnelApply(v); }},
    scCall(q, d) {{ scCall(q, d); }},
}};
function scEnsureTidal() {{}}
function scApply(v) {{ if (v.tunnel) scTunnelApply(v.tunnel); }}
{code}
// In-flight UI poll started while tunnel is off.
scCall('');
// User turns Remote access ON, then quickly toggles OFF while POST 1 is in flight.
scTunnelToggle(true);
assert.equal(scRemoteAccess, true, 'remote access flips on optimistically');
assert.equal(scUiPending, 1);
// Stale UI poll arrives saying tunnel.enabled=false -> must be ignored.
scUiPoll.finish(200, {{tunnel: {{enabled: false, status: 'stopped'}}}}, 'd0');
assert.equal(scRemoteAccess, true, 'stale UI poll must not revert in-flight tunnel toggle');
scTunnelToggle(false);
assert.equal(scRemoteAccess, false);
assert.equal(reqs.length, 2, 'second tunnel POST waits behind first');
assert.equal(reqs[1].body, JSON.stringify({{enabled: true}}));
reqs[1].finish(200, {{enabled: true, status: 'starting'}});
assert.equal(scRemoteAccess, false, 'first tunnel reply must not clobber queued toggle');
assert.equal(reqs.length, 3);
assert.equal(reqs[2].body, JSON.stringify({{enabled: false}}));
reqs[2].finish(200, {{enabled: false, status: 'stopped'}});
assert.equal(scUiPending, 0);
assert.equal(scRemoteAccess, false);
// Complete the post-tunnel resync poll triggered by scTunnelSend.
const postTunnelPoll = reqs.at(-1);
assert.equal(postTunnelPoll.method, 'GET');
postTunnelPoll.finish(200, {{tunnel: {{enabled: false, status: 'stopped'}}}}, 'dt1');
// Hung tunnel POST (>15s) recovers via 3s scCall('') watchdog without stranding pending count.
const t0 = Date.now();
Date.now = () => t0;
scTunnelToggle(true);
const hungTunnel = reqs.at(-1);
assert.equal(scUiPending, 1);
Date.now = () => t0 + 16000;
scCall('');
assert.equal(hungTunnel.aborted, true, 'hung tunnel POST aborted by 3s watchdog');
assert.equal(scTunnelPending, 0, 'hung tunnel clears pending count');
assert.equal(scUiPending, 0);
const reconPoll = reqs.at(-1);
assert.equal(reconPoll.method, 'GET');
reconPoll.finish(200, {{tunnel: {{enabled: false, status: 'stopped'}}}}, 'dt2');
assert.equal(scRemoteAccess, false, 'reconciled poll restores server tunnel state');
hungTunnel.finish(200, {{enabled: true, status: 'running'}});
assert.equal(scRemoteAccess, false, 'delayed reply from timed-out tunnel POST must be ignored');
"""
        run_node(script)

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_bluetooth_output_source_ignores_stale_load_and_serializes_sets(self):
        main = self.bp.SERVICES_MAIN
        code = "\n".join(filter(None, [
            extract_function(main, "scBtLabel"),
            extract_function(main, "scBtLabels"),
            extract_function(main, "scBtReply"),
            extract_function(main, "scBtLoad"),
            extract_function(main, "scBtSend", optional=True),
            extract_function(main, "scBtSet"),
        ]))
        script = f"""
const assert = require('node:assert/strict');
const reqs = [];
class XMLHttpRequest {{
    constructor() {{ this.readyState = 0; this.status = 0; this.responseText = ''; }}
    open(m, u) {{ this.method = m; this.url = u; this.readyState = 1; }}
    send() {{ reqs.push(this); }}
    finish(status, body) {{
        this.status = status;
        this.responseText = JSON.stringify(body);
        this.readyState = 4;
        if (this.onreadystatechange) this.onreadystatechange();
    }}
}}
let scBtLayers = ['Master', 'Booth', 'Headphones'], scBtLayer = 'Master';
const root = {{ scBtGen: 0, scBtSending: false, scBtQueued: '', scBtSendingAt: 0, scBtSendToken: 0, scBtReload: false }};
{code}
// Dialog opens -> GET in flight. User quickly taps Booth then Headphones.
scBtLoad();
scBtSet('Booth');
scBtSet('Headphones');
assert.equal(scBtLayer, 'Headphones', 'optimistic update shows latest choice');
assert.equal(reqs.length, 2, 'only GET + first POST in flight');
// Stale GET returns Master -> must be ignored.
reqs[0].finish(200, {{layers: ['Master', 'Booth', 'Headphones'], assignLayer: 'Master'}});
assert.equal(scBtLayer, 'Headphones', 'stale scBtLoad must not revert scBtSet');
// First POST (Booth) returns -> must not revert to Booth, and must flush queued Headphones.
reqs[1].finish(200, {{layers: ['Master', 'Booth', 'Headphones'], assignLayer: 'Booth'}});
assert.equal(scBtLayer, 'Headphones');
assert.equal(reqs.length, 3);
assert.ok(reqs[2].url.endsWith('assignLayer=Headphones'));
reqs[2].finish(200, {{layers: ['Master', 'Booth', 'Headphones'], assignLayer: 'Headphones'}});
assert.equal(scBtLayer, 'Headphones');
// Reopening Bluetooth dialog during an in-flight assignment defers GET until write completes.
scBtSet('Booth');
const write = reqs.at(-1), count = reqs.length;
scBtLoad();
const read = reqs.length > count ? reqs.at(-1) : null;
assert.equal(read, null, 'scBtLoad during scBtSet must defer instead of racing');
write.finish(200, {{layers: ['Master', 'Booth'], assignLayer: 'Booth'}});
assert.equal(scBtLayer, 'Booth', 'reopening during write must keep acknowledged assignment');
assert.equal(reqs.length, count + 1, 'deferred scBtLoad must run once the in-flight assignment finishes');
assert.equal(reqs.at(-1).method, 'GET');
reqs.at(-1).finish(200, {{layers: ['Master', 'Booth', 'Zone'], assignLayer: 'Booth'}});
assert.deepEqual(scBtLayers, ['Master', 'Booth', 'Zone']);
assert.equal(scBtLayer, 'Booth');
"""
        run_node(script)

    def _round2_js_harness(self):
        main = self.bp.SERVICES_MAIN
        names = (
            "scCall scUiPollReply scUiSyncPending scUiFlush scApplyQuery "
            "scSameItems scFxMsInteger scFxMsApply scLoginDone scBtLoad "
            "scBtSend scBtSet scBtReply scTunnelApply scTunnelSend scTunnelToggle"
        )
        main_js = "\n".join(filter(None, (extract_function(main, n, optional=True) for n in names.split())))
        fx = self.bp.FX_CONTROLLER.replace("    function ", "\tfunction ")
        knob_js = "\n".join(extract_function(fx, n) for n in ("scFxMsSet", "scFxMsFlush", "scFxMsExpire"))
        return """
const assert = require('node:assert/strict'), reqs = [];
let now = 100000;
Date.now = () => now;
globalThis.root = globalThis;
Object.assign(root, {
    scClientId: 'qml-r2', scUiWriteSeq: 0, scBtWriteSeq: 0, scTunnelWriteSeq: 0,
    scUiAppliedSeq: 0, scScopeNeedRefresh: false,
    scUiWriteGen: 0, scUiPollGen: 0, scUiPending: 0, scUiQueue: [], scUiSending: false,
    scUiSendingAt: 0, scUiSendToken: 0, scUiSendXhr: null, scUiHadError: false,
    scTunnelPending: 0, scTunnelSending: false, scTunnelQueued: null, scTunnelSendingAt: 0,
    scTunnelSendToken: 0, scTunnelSendXhr: null,
    scRemoteAccess: false, scRemoteAccessStatus: 'stopped', scRemoteAccessUrl: '', scRemoteAccessError: '',
    scScope: '', scScopePending: 0, scUiPoll: null, scUiPollScope: '', scUiPollAt: 0,
    scUiDigest: '', scUiAppliedAt: 0, scUiLoaded: true, scLoggedIn: [], scSources: ['spotify'],
    scLoginOpen: '', scLogin: '', scEnterTries: 0, scEnterTimer: {start() {}},
    Planck: {librarianController: {streamingPlaylistsController: {invalidateUI() {}}}},
    scBtSending: false, scBtSendingAt: 0, scBtQueued: '', scBtGen: 0, scBtSendToken: 0,
    scBtSendXhr: null, scBtReload: false, scBtLayers: ['Master', 'Booth', 'Headphones'], scBtLayer: 'Master',
    scSdHighSpeed: false, scMasterLimiter: false, scAutoGain: false, scLedBrightness: 100, scLedBrightnessSaved: 100,
    scFxTimeMode: 'ms', scFxTimeMs: 500, scFxMsSending: false, scFxMsRequest: 0,
    scFxMsWanted: -1, scFxMsSent: -1,
});
class XMLHttpRequest {
    constructor() { this.readyState = 0; this.reqHeaders = {}; this.aborted = false; }
    open(method, url) { Object.assign(this, {method, url, readyState: 1}); }
    setRequestHeader(k, v) { this.reqHeaders[k] = v; }
    send(body) { this.body = body; reqs.push(this); }
    abort() { this.aborted = true; this.readyState = 0; }
    getResponseHeader() { return this.digest || ''; }
    finish(data, digest = '', status = 200) {
        Object.assign(this, {readyState: 4, status, responseText: JSON.stringify(data), digest});
        if (this.onreadystatechange) this.onreadystatechange();
    }
}
function scEnsureTidal() {}
function scOpen(id) { scScope = id; }
function scApply(v) {
    if (v.scope !== undefined) scScope = v.scope;
    if (v.loggedIn) scLoggedIn = v.loggedIn;
    if (v.sources) scSources = v.sources;
    if (v.fxTimeMs !== undefined) scFxMsApply('ms', v.fxTimeMs);
    if (v.tunnel) scTunnelApply(v.tunnel);
}
""" + main_js + "\n" + knob_js + "\n"

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_qr_callback_keeps_newer_login_list(self):
        run_node(self._round2_js_harness() + """
// Case 1: newer settings ACK reports Spotify revoked before older QR GET returns.
scLoginOpen = 'spotify';
scLogin = 'spotify';
scLoginDone();
const older = reqs.at(-1);
scCall('masterLimiter=1');
const revoked = {sources: ['spotify'], loggedIn: []};
reqs.at(-1).finish(revoked, 'revoked');
older.finish({sources: ['spotify'], loggedIn: ['spotify']}, 'before-revocation');
scCall('');
reqs.at(-1).finish(revoked, 'revoked');
assert.deepEqual(scLoggedIn, [], 'older QR GET restored revoked Spotify despite current digest');
assert.notEqual(scScope, 'spotify', 'older QR GET opened a revoked provider');

// Case 2: newer periodic poll reports Spotify revoked (no settings write) before older QR GET returns.
scLoginOpen = 'spotify';
scLogin = 'spotify';
scLoginDone();
const older2 = reqs.at(-1);
scCall('');
reqs.at(-1).finish(revoked, 'revoked-poll');
older2.finish({sources: ['spotify'], loggedIn: ['spotify']}, 'before-poll-revocation');
assert.deepEqual(scLoggedIn, [], 'older QR GET must not override newer poll login list');
assert.notEqual(scScope, 'spotify', 'older QR GET must not open provider revoked by poll');
""")

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_older_poll_before_newer_qr_result(self):
        run_node(self._round2_js_harness() + """
scLoginOpen = 'spotify';
scLogin = 'spotify';
scCall('');
const oldPoll = reqs.at(-1);
scLoginDone();
const loginRead = reqs.at(-1);
oldPoll.finish({sources: ['spotify'], loggedIn: []}, 'pre-login');
loginRead.finish({sources: ['spotify'], loggedIn: ['spotify']}, 'signed-in');
scCall('');
reqs.at(-1).finish({sources: ['spotify'], loggedIn: ['spotify']}, 'signed-in');
assert.deepEqual(scLoggedIn, ['spotify']);
assert.equal(scScope, 'spotify', 'successful login never opened its selected source');
""")

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_newer_source_choice_wins_over_qr_callback(self):
        run_node(self._round2_js_harness() + """
scSources = ['spotify', 'deezer'];
scLoggedIn = ['deezer'];
scLoginOpen = 'spotify';
scLogin = 'spotify';
scLoginDone();
const oldLogin = reqs.at(-1);
scOpen('deezer');
scCall('scope=deezer', () => {});
reqs.at(-1).finish({scope: 'deezer', sources: ['spotify', 'deezer'], loggedIn: ['spotify', 'deezer']}, 'new-choice');
oldLogin.finish({sources: ['spotify', 'deezer'], loggedIn: ['spotify', 'deezer']}, 'old-login');
assert.equal(scScope, 'deezer', 'QR completion overwrote a newer selected source');
""")

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_stripped_ack_does_not_adopt_its_digest(self):
        run_node(self._round2_js_harness() + """
// 1. scUiFlush must not cache digest when reply.scope was stripped.
scScope = 'deezer';
scCall('masterLimiter=1');
const older = reqs.at(-1);
scOpen('spotify');
scCall('scope=spotify', () => {});
reqs.at(-1).finish({scope: 'spotify', masterLimiter: true}, 'new-scope');
older.finish({scope: 'deezer', masterLimiter: true}, 'stripped-scope');
assert.equal(scScope, 'spotify');
assert.notEqual(scUiDigest, 'stripped-scope', 'scUiFlush cached the digest of a partially applied snapshot');

// 2. scCall callback GET must not cache digest when reply.scope was stripped.
scScope = 'deezer';
scUiDigest = 'before-get';
scCall('', () => {});
const cbGet = reqs.at(-1);
scScope = 'spotify';
cbGet.finish({scope: 'deezer', masterLimiter: true}, 'stripped-get-digest');
assert.equal(scScope, 'spotify');
assert.notEqual(scUiDigest, 'stripped-get-digest', 'scCall cached the digest of a scope-stripped GET snapshot');
""")

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_rejected_scope_recovers_after_overlapping_setting(self):
        run_node(self._round2_js_harness() + """
scScope = 'deezer';
scCall('masterLimiter=1');
const setting = reqs.at(-1);
scScope = 'spotify';
scCall('scope=spotify', () => { throw Error('a rejected choice cannot succeed'); });
reqs.at(-1).finish({}, '', 409);
const state = {scope: 'deezer', masterLimiter: true};
reqs.at(-1).finish(state, 'server');
setting.finish(state, 'server');
for (let i = 0; i < 2; i++) {
    now += 3000;
    scCall('');
    reqs.at(-1).finish(state, 'server');
}
assert.equal(scScope, 'deezer', '409 recovery cached a digest without restoring server scope');
""")

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_bluetooth_queue_recovers_without_another_tap(self):
        run_node(self._round2_js_harness() + """
scBtSet('Booth');
const hungBooth = reqs.at(-1);
scBtSet('Headphones');
scBtLoad();
let hp = null;
for (let tick = 1; tick <= 8; tick++) {
    now += 3000;
    scCall('');
    const poll = reqs.at(-1);
    if (poll.method === 'GET' && poll.readyState !== 4) poll.finish({}, 'same');
    if (!hp) {
        hp = reqs.find(x => x.method === 'POST' && x.url.endsWith('assignLayer=Headphones'));
        if (hp) {
            assert.equal(hungBooth.aborted, true, 'hung Booth assignment must be aborted by 3s watchdog');
            const beforeCount = reqs.length;
            hp.finish({layers: ['Master', 'Booth', 'Headphones'], assignLayer: 'Headphones'});
            assert.equal(scBtLayer, 'Headphones');
            // Deferred scBtLoad runs after the recovered write finishes.
            assert.equal(reqs.length, beforeCount + 1);
            assert.equal(reqs.at(-1).method, 'GET');
            assert.ok(reqs.at(-1).url.endsWith('/v1/enginex/bluetooth'));
            reqs.at(-1).finish({layers: ['Master', 'Booth', 'Headphones'], assignLayer: 'Headphones'});
        }
    }
}
assert.ok(hp, 'latest Bluetooth choice was never sent after 24 seconds');
// Late reply from hungBooth must be ignored.
hungBooth.finish({layers: ['Master', 'Booth', 'Headphones'], assignLayer: 'Booth'});
assert.equal(scBtLayer, 'Headphones');
""")

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_knob_keeps_last_step_while_another_setting_is_slow(self):
        run_node(self._round2_js_harness() + """
let deadline = 0, persisted = 500;
const scFxMsTimeout = { restart() { deadline = now + 2000; }, stop() { deadline = 0; } };
function completeKnobWrites() {
    let x;
    while ((x = reqs.find(q => q.method === 'POST' && q.readyState !== 4 && q.url.includes('?fxTimeMs=')))) {
        persisted = Number(x.url.split('=').at(-1));
        x.finish({fxTimeMs: persisted}, String(persisted));
    }
}
scCall('sdHighSpeed=1');
const slow = reqs[0];
scFxMsSet(510);
scFxMsSet(520);
completeKnobWrites();
now += 2100;
if (deadline && now >= deadline) scFxMsExpire();
slow.finish({fxTimeMs: persisted}, 'sd-ready');
completeKnobWrites();
assert.equal(persisted, 520, 'the last knob step was discarded while waiting for the unrelated write');
""")

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_write_seq_headers_monotonic_across_endpoints(self):
        run_node(self._round2_js_harness() + """
scCall('ledBrightness=80');
const ui1 = reqs.at(-1);
assert.equal(ui1.reqHeaders['X-UI-Client-Id'], 'qml-r2');
assert.equal(ui1.reqHeaders['X-UI-Write-Seq'], '1');
ui1.finish({ledBrightness: 80}, 'u1');
scCall('ledBrightness=60');
const ui2 = reqs.at(-1);
assert.equal(ui2.reqHeaders['X-UI-Client-Id'], 'qml-r2');
assert.equal(ui2.reqHeaders['X-UI-Write-Seq'], '2');
ui2.finish({ledBrightness: 60}, 'u2');

scBtSet('Booth');
const bt1 = reqs.at(-1);
assert.equal(bt1.reqHeaders['X-UI-Client-Id'], 'qml-r2');
assert.equal(bt1.reqHeaders['X-UI-Write-Seq'], '1');
bt1.finish({layers: ['Master', 'Booth'], assignLayer: 'Booth'});
scBtSet('Headphones');
const bt2 = reqs.at(-1);
assert.equal(bt2.reqHeaders['X-UI-Write-Seq'], '2');
bt2.finish({layers: ['Master', 'Booth', 'Headphones'], assignLayer: 'Headphones'});

scTunnelToggle(true);
const tun1 = reqs.at(-1);
assert.equal(tun1.reqHeaders['X-UI-Client-Id'], 'qml-r2');
assert.equal(tun1.reqHeaders['X-UI-Write-Seq'], '1');
tun1.finish({enabled: true, status: 'running'});
scTunnelToggle(false);
const tun2 = reqs.at(-1);
assert.equal(tun2.reqHeaders['X-UI-Write-Seq'], '2');
""")

    def test_sandbox_dbus_cleanup_trap_enforces_stop_and_fails_on_lost_pid(self):
        if not Path("/proc/self/stat").exists():
            self.skipTest("Linux /proc required to verify sandbox D-Bus cleanup traps")
        launchers = [
            (REPO / "tools/az01-packer/test_settings_click_sandbox.sh", "cleanup"),
            (REPO / "tools/az01-packer/test_fx_selector_sandbox.sh", "fx_cleanup"),
        ]
        for path, fn_name in launchers:
            src = path.read_text(encoding="utf-8")
            m = re.search(rf"(?ms)^{fn_name}\(\) \{{.*?^\}}", src)
            self.assertIsNotNone(m, f"{path.name} missing {fn_name}()")
            trap_fn = m.group(0)
            self.assertIn("--print-pid=1", src)
            self.assertIn("dbus_started=1", src)
            self.assertIn('kill -0 "$dbus_pid"', src)
            for mode, want_rc in (("normal", 0), ("fail-exit", 7), ("lose-pid", 1)):
                with self.subTest(launcher=path.name, mode=mode):
                    with tempfile.TemporaryDirectory(prefix="e4-dbus-trap-") as tmp:
                        pidfile = Path(tmp) / "dummy.pid"
                        script = f"""
set -euo pipefail
R=""
board=JP14
PRODUCT=JP07
server=""
http_pid=""
dbus_started=1
dbus_pid=""
{trap_fn}
trap {fn_name} EXIT
sleep 30 >/dev/null 2>&1 &
dbus_pid=$!
printf '%s\\n' "$dbus_pid" > "{pidfile}"
if [ "{mode}" = "lose-pid" ]; then
    dbus_pid=""
fi
if [ "{mode}" = "fail-exit" ]; then
    exit 7
fi
exit 0
"""
                        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
                        dummy_pid = int(pidfile.read_text(encoding="utf-8").strip())
                        try:
                            self.assertEqual(res.returncode, want_rc, f"stderr={res.stderr}")
                            if mode in ("normal", "fail-exit"):
                                alive = subprocess.run(["kill", "-0", str(dummy_pid)], capture_output=True).returncode == 0
                                self.assertFalse(alive, f"{path.name} {fn_name}() left dummy daemon {dummy_pid} alive")
                            else:
                                self.assertIn("dbus_cleanup_pid_lost", res.stderr)
                        finally:
                            subprocess.run(["kill", "-9", str(dummy_pid)], capture_output=True)


if __name__ == "__main__":
    unittest.main()
