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
let scMasterLimiter = false, scAutoGain = false, scLedBrightness = 100, scSources = ['tidal', 'spotify'], scLoggedIn = [];
let scFxTimeMode = 'beats', scFxTimeMs = 500, scFxMsWanted = -1;
let scPadDelayRestarts = 0;
const scPadBrightnessDelay = {{ restart() {{ scPadDelayRestarts++; }} }};
let applyCount = 0;
function scEnsureTidal() {{}}
function scOpen(s) {{ scScope = s; }}
function scApply(v) {{
    applyCount++;
    if (v.masterLimiter !== undefined) scMasterLimiter = !!v.masterLimiter;
    if (v.autoGain !== undefined) scAutoGain = !!v.autoGain;
    if (v.ledBrightness !== undefined) scLedBrightness = parseInt(v.ledBrightness) || 100;
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

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_rapid_setting_posts_serialize_coalesce_and_rollback_on_error(self):
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
let scScope = '', scScopePending = 0, scUiPoll = null, scUiPollScope = '', scUiPollAt = 0, scUiDigest = 'd0', scUiAppliedAt = Date.now();
let scUiWriteGen = 0, scUiPollGen = 0, scUiPending = 0, scUiQueue = [], scUiSending = false, scUiSendingAt = 0, scUiSendToken = 0, scUiSendXhr = null, scUiHadError = false;
let scTunnelPending = 0, scUiLoaded = true, scIsPlayerBoard = true, scConsoleLineBoard = false, scFourDeckOn = false, scHasMixer = true;
let scMasterLimiter = false, scAutoGain = false, scSdHighSpeed = false, scLedBrightness = 100, scUpfaderEcho = false;
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
    if (v.ledBrightness !== undefined) scLedBrightness = parseInt(v.ledBrightness) || 100;
    if (v.upfaderEcho !== undefined) scUpfaderEcho = !!v.upfaderEcho;
}}
{code}
// 1. Rapid ListEntry taps: 80 -> 60 -> 40. Only 80 is in flight; 60 coalesces into 40.
scCall('ledBrightness=80');
scCall('ledBrightness=60');
scCall('ledBrightness=40');
assert.equal(scLedBrightness, 40, 'optimistic state tracks latest tap');
assert.equal(reqs.length, 1, 'only one setting POST in flight at once');
assert.ok(reqs[0].url.endsWith('?ledBrightness=80'));
// When 80 finishes, QML must NOT revert scLedBrightness to 80 and must immediately send 40.
reqs[0].finish(200, {{ledBrightness: 80}}, 'd80');
assert.equal(scLedBrightness, 40, 'intermediate POST reply must not clobber queued tap');
assert.equal(reqs.length, 2);
assert.ok(reqs[1].url.endsWith('?ledBrightness=40'), '60 coalesced into 40');
reqs[1].finish(200, {{ledBrightness: 40}}, 'd40');
assert.equal(scLedBrightness, 40);
assert.equal(scUiPending, 0);
assert.ok(scPadDelayRestarts >= 1, '#325 pad brightness delay restarted after POST confirmation');

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
let scSources = ['tidal', 'netease', 'spotify', 'deezer', 'soundcloud', 'beatport', 'applemusic'];
function scApply(v) {{ if (v.sources) scSources = v.sources; }}
{code}
// User disables SoundCloud, then immediately disables Deezer before POST 1 replies.
scSetSource('soundcloud', false);
scSetSource('deezer', false);
assert.equal(scSources.includes('soundcloud'), false, 'soundcloud disabled optimistically');
assert.equal(scSources.includes('deezer'), false, 'deezer disabled optimistically');
assert.equal(reqs.length, 1, 'second sources POST waits behind first');
assert.equal(reqs[0].url, 'http://127.0.0.1:8080/v1/enginex/ui?sources=tidal,netease,spotify,deezer,beatport,applemusic');
reqs[0].finish(200, {{sources: ['tidal', 'netease', 'spotify', 'deezer', 'beatport', 'applemusic']}}, 'ds1');
assert.equal(reqs.length, 2);
assert.equal(reqs[1].url, 'http://127.0.0.1:8080/v1/enginex/ui?sources=tidal,netease,spotify,beatport,applemusic', 'second POST must NOT re-enable soundcloud');
reqs[1].finish(200, {{sources: ['tidal', 'netease', 'spotify', 'beatport', 'applemusic']}}, 'ds2');
assert.deepEqual(scSources, ['tidal', 'netease', 'spotify', 'beatport', 'applemusic']);
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
let scTunnelPending = 0, scTunnelSending = false, scTunnelQueued = null, scTunnelSendingAt = 0, scTunnelSendToken = 0;
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
const root = {{ scBtGen: 0, scBtSending: false, scBtQueued: '', scBtSendingAt: 0, scBtSendToken: 0 }};
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
"""
        run_node(script)


if __name__ == "__main__":
    unittest.main()
