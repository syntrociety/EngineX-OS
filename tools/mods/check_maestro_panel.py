"""Exercise the shipped Qt 5 panel and native helper against a loopback deck clock."""
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PyQt5.QtGui import QGuiApplication
from PyQt5.QtQml import QQmlEngine, QQmlExpression
from PyQt5.QtQuick import QQuickView
from PyQt5.QtCore import QUrl

class Clock(BaseHTTPRequestHandler):
    started = time.monotonic()

    def do_GET(self):
        elapsed = time.monotonic() - self.started
        deck = dict(deck=1, source="local", sourceId="local:1", master=True,
                    playing=True, bpm=240, beat=(elapsed * 4) % 4, positionMs=elapsed * 1000,
                    trackKey="test-usb-track", title="Testtrack", lightTag="warmup")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(dict(clockApi=True, decks=[deck])).encode())

    def log_message(self, *unused):
        pass

def check_panel(tmp):
    app = QGuiApplication(sys.argv)
    with tarfile.open("dmx-studio-maestro.tar.gz") as archive:
        for name in ("Panel.qml", "MaestroPane.qml", "bin/enginex-mod-helper-arm64"):
            archive.extract(name, tmp, filter="data")
    clock = ThreadingHTTPServer(("127.0.0.1", 0), Clock)
    threading.Thread(target=clock.serve_forever, daemon=True).start()
    binary = tmp / "bin/enginex-mod-helper-arm64"
    helper = subprocess.Popen([binary, "dmx-studio", "serve", tmp],
        env=dict(os.environ, ENGINEX_MOD_STATE=str(tmp), ENGINEX_WEBUI_URL="http://127.0.0.1:%d" % clock.server_port))
    try:
        subprocess.run([binary, "dmx-studio", "health", tmp], check=True)
        view = QQuickView()
        view.setSource(QUrl.fromLocalFile(str(tmp / "Panel.qml")))
        if view.status() != QQuickView.Ready:
            raise RuntimeError("\n".join(error.toString() for error in view.errors()))
        view.show()
        root = view.rootObject()
        def js(code):
            expr = QQmlExpression(QQmlEngine.contextForObject(root), root, code)
            result, unused = expr.evaluate()
            if expr.hasError():
                raise RuntimeError(expr.error().toString())
            return result
        def wait_for(code):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                app.processEvents()
                if js(code):
                    return
                time.sleep(.015)
            raise RuntimeError("Timeout: " + code + " / " + str(js("JSON.stringify([root.message,root.actionError,root.state.maestro])")))
        wait_for("root.helperOnline && root.state.maestro.valid")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
            rx.bind(("127.0.0.1", 0))
            rx.settimeout(1)
            js("maestroPane.openSettings(); maestroPane.configure(Object.assign({}, {host:'127.0.0.1',port:%d,phrase:16,shows:{warmup:'Amber'}})); maestroPane.settingUp=false" % rx.getsockname()[1])
            wait_for("root.state.maestro.config.shows.warmup === 'Amber'")
            js("maestroPane.command({action:'maestroStart'})")
            wait_for("root.state.maestro.sent === 'Amber'")
            assert rx.recv(256) == b"/show/name\0\0,s\0\0Amber\0\0\0"
            assert rx.recv(256) == b"/show/play\0\0,\0\0\0"
            for index, screen in enumerate(("showregie", "deckmix", "tags")):
                js("maestroPane.screen = %d" % index)
                app.processEvents()
                assert view.grabWindow().save("maestro-" + screen + ".png")
            js("maestroPane.command({action:'maestroStop'})")
            wait_for("!root.state.maestro.armed")
    finally:
        helper.terminate()
        helper.wait(timeout=5)
        clock.shutdown()
        clock.server_close()

with tempfile.TemporaryDirectory() as directory:
    check_panel(Path(directory))
