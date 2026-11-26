"""Load the actual Qt 5 deck panel and capture its disconnected startup state."""
import sys
from pathlib import Path

from PyQt5.QtCore import QTimer, QUrl
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtQuick import QQuickView

app = QGuiApplication(sys.argv)
view = QQuickView()
source = Path("docs/mods/src/dmx-studio/Panel.qml").resolve()
view.setSource(QUrl.fromLocalFile(str(source)))
if view.status() != QQuickView.Ready:
    sys.exit("\n".join(error.toString() for error in view.errors()))
view.show()

def capture():
    app.exit(0 if view.grabWindow().save("maestro-panel.png") else 1)

QTimer.singleShot(250, capture)
sys.exit(app.exec_())
