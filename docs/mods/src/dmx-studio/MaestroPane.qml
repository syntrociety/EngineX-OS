import QtQuick 2.12
import QtQuick.Controls 2.5
import QtQuick.Layouts 1.12

Rectangle {
    id: desk
    color: "#101410"
    property var status: ({})
    property bool available: false
    signal command(var body)
    readonly property var cfg: status.config || ({})
    readonly property var midi: status.midi || ({})
    readonly property var decks: status.decks || []
    readonly property color accent: "#d2f47c"
    property string editingTag: ""
    property bool settingUp: false

    function configure(changes) {
        var value = Object.assign({}, cfg, changes)
        command({action:"maestroConfigure", config:value})
    }
    function editRoute(remove) {
        var shows = Object.assign({}, cfg.shows || {})
        var key = tagInput.text.trim()
        if (!remove && (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(key) || !showInput.text.trim())) {
            formError.text = "Vul een geldige tag en een unieke opgeslagen shownaam in."
            return
        }
        if (!remove && key !== editingTag && Object.prototype.hasOwnProperty.call(shows, key)) {
            formError.text = "Deze tag heeft al een koppeling."
            return
        }
        if (editingTag) delete shows[editingTag]
        if (!remove) Object.defineProperty(shows, key, {enumerable:true, configurable:true, value:showInput.text.trim()})
        formError.text = ""
        configure({shows:shows})
        editingTag = ""; tagInput.text = ""; showInput.text = ""
    }
    function openSettings() {
        host.text = cfg.host || ""
        port.value = cfg.port || 7672
        phrase.currentIndex = cfg.phrase === 16 ? 0 : 1
        var options = [""].concat(midi.devices || [])
        if (cfg.device && options.indexOf(cfg.device) < 0) options.push(cfg.device)
        device.model = options
        device.currentIndex = Math.max(0, options.indexOf(cfg.device || ""))
        crossMode.currentIndex = cfg.cross === "local-X" ? 1 : cfg.cross === "local-Y" ? 2 : 0
        crossCC.value = cfg.crossCC === undefined ? 5 : cfg.crossCC
        channel.value = cfg.channel || 16
        localCC.value = cfg.localCC || 0
        peerCC.value = cfg.peerCC === undefined ? 1 : cfg.peerCC
        settingUp = true
    }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 10; spacing: 8
        RowLayout {
            Layout.fillWidth: true
            Label { text: "MaestroDMX"; color: desk.accent; font.pixelSize: 24; font.bold: true }
            Item { Layout.fillWidth: true }
            Label { text: !desk.available ? "Helper offline" : desk.status.armed ? "● OSC actief" : "○ OSC uit"; color: "white" }
            Button { text: desk.settingUp ? "Terug" : "Uitgang & MIDI"; enabled: desk.available; onClicked: { if (desk.settingUp) desk.settingUp=false; else desk.openSettings() } }
        }
        TabBar {
            id: views; visible: !desk.settingUp; Layout.fillWidth: true
            TabButton { text: "Showregie" }
            TabButton { text: "Deckmix" }
            TabButton { text: "Tags & shows" }
        }
        StackLayout {
            Layout.fillWidth: true; Layout.fillHeight: true
            currentIndex: desk.settingUp ? 3 : views.currentIndex
            ColumnLayout {
                RowLayout {
                    Layout.fillWidth: true
                    Rectangle {
                        color: "#262d21"; radius: 8; Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.fillHeight: true
                        ColumnLayout {
                            anchors.fill: parent; anchors.margins: 18
                            Label { text: "LAATST AANGEVRAAGDE SHOW"; color: "#bbcab0" }
                            Label { text: desk.status.sent || "Maestro autonoom"; color: "white"; font.pixelSize: 32; font.bold: true; Layout.fillWidth: true; elide: Text.ElideRight }
                            Label { text: "Audioanalyse op Maestro"; color: "#c0cabe" }
                        }
                    }
                    Rectangle {
                        color: "#1b201b"; radius: 8; Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.fillHeight: true
                        ColumnLayout {
                            anchors.fill: parent; anchors.margins: 18
                            Label { text: "VOLGENDE FRASEGRENS"; color: "#bbcab0" }
                            Label { text: desk.status.next || "Geen wissel"; color: desk.accent; font.pixelSize: 30; Layout.fillWidth: true; elide: Text.ElideRight }
                            Label { text: desk.status.source || "Wacht op eenduidige lichtbron"; color: "white"; Layout.fillWidth: true; elide: Text.ElideMiddle }
                        }
                    }
                }
                Label { color: "white"; text: desk.status.valid ? Number(desk.status.bpm).toFixed(1)+" BPM   ·   Beat "+desk.status.beat+" / "+desk.cfg.phrase : "Wacht op deckklok" }
                ProgressBar { Layout.fillWidth: true; from: 0; to: desk.cfg.phrase || 32; value: desk.status.valid ? desk.status.beat : 0 }
                RowLayout {
                    Button { text: desk.status.armed ? "Stop OSC" : "Schakel OSC in"; enabled: desk.available && (!!desk.cfg.host || desk.status.armed); onClicked: desk.command({action:desk.status.armed ? "maestroStop" : "maestroStart"}) }
                    Label { color: "#c0cabe"; text: desk.status.error || (desk.cfg.host ? desk.cfg.host+" · UDP "+desk.cfg.port : "Stel het Maestro-adres in bij Uitgang & MIDI."); wrapMode: Text.Wrap; Layout.fillWidth: true }
                }
            }
            ColumnLayout {
                Label { text: "Automatische lichtbron"; font.pixelSize: 20; color: desk.accent }
                Label { visible: !desk.decks.length; text: "Nog geen verse decks van de lokale klok."; color: "white" }
                GridView {
                    id: deckGrid; clip: true; Layout.fillWidth: true; Layout.fillHeight: true
                    cellWidth: Math.max(1,Math.floor(width/2)); cellHeight: 115
                    model: desk.decks
                    delegate: Rectangle {
                        width: deckGrid.cellWidth-6; height: deckGrid.cellHeight-6; radius: 6
                        color: modelData.sourceId === desk.status.source ? "#2b3822" : "#202520"
                        Column {
                            anchors.fill: parent; anchors.margins: 9; spacing: 4
                            Label { color: "white"; font.bold: true; text: "Deck "+modelData.deck+" · "+modelData.source+(modelData.sourceId===desk.status.source ? " · LICHTBRON" : "") }
                            Label { width: parent.width; elide: Text.ElideRight; color: "white"; text: modelData.title || "Track onbekend" }
                            Label { color: "#ccdbc5"; text: (modelData.cue ? "Voorbeluisteren" : modelData.playing ? "Speelt" : "Pauze")+" · "+(modelData.lightTag ? "[licht:"+modelData.lightTag+"]" : "Geen lichttag") }
                            Label { color: desk.accent; text: { var val=modelData.source==="local"?desk.midi.local:modelData.source==="peer"?desk.midi.peer:-1; return val>=0 ? "Kanaalfader "+Math.round(val*100/127)+"%" : "Kanaalfader onbekend" } }
                        }
                    }
                }
                Label { color: "white"; text: desk.midi.ready ? "Bronkeuze via Xone-kanaalfaders" : "Bronkeuze via deckklok" }
                Label { color: "#c0cabe"; text: desk.midi.last || desk.midi.error || "Nog geen MIDI CC ontvangen"; Layout.fillWidth: true; elide: Text.ElideRight }
            }
            RowLayout {
                ListView {
                    Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.fillHeight: true; clip: true
                    model: Object.keys(desk.cfg.shows || {}).sort()
                    delegate: ItemDelegate {
                        width: parent ? parent.width : 0
                        text: "[licht:"+modelData+"]   →   "+desk.cfg.shows[modelData]
                        onClicked: { desk.editingTag=modelData; tagInput.text=modelData; showInput.text=desk.cfg.shows[modelData]; formError.text="" }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true; Layout.preferredWidth: 1
                    Label { color: desk.accent; font.pixelSize: 20; text: desk.editingTag ? "Koppeling bewerken" : "Nieuwe koppeling" }
                    TextField { id: tagInput; Layout.fillWidth: true; placeholderText: "Tracktag, bijvoorbeeld warmup"; maximumLength: 32 }
                    TextField { id: showInput; Layout.fillWidth: true; placeholderText: "Opgeslagen Maestro-shownaam"; maximumLength: 128 }
                    RowLayout {
                        Button { text: "Opslaan"; enabled: desk.available && !desk.status.armed; onClicked: desk.editRoute(false) }
                        Button { text: "Verwijder"; enabled: !!desk.editingTag && desk.available && !desk.status.armed; onClicked: desk.editRoute(true) }
                        Button { text: "Nieuw"; onClicked: { desk.editingTag=""; tagInput.text=""; showInput.text="" } }
                    }
                    Label { id: formError; color: "#ffc498"; Layout.fillWidth: true; wrapMode: Text.Wrap }
                    Label { color: "#c0cabe"; Layout.fillWidth: true; wrapMode: Text.Wrap; text: desk.status.armed ? "Stop OSC om koppelingen te wijzigen." : "Zet [licht:warmup] in het trackcommentaar." }
                }
            }
            ColumnLayout {
                GridLayout {
                    columns: 4; Layout.fillWidth: true
                    Label { color: "white"; text: "Maestro IPv4" }
                    TextField { id: host; Layout.fillWidth: true; placeholderText: "10.0.0.50" }
                    Label { color: "white"; text: "UDP-poort" }
                    SpinBox { id: port; from: 1; to: 65535; editable: true }
                    Label { color: "white"; text: "Frase" }
                    ComboBox { id: phrase; model: ["16 beats","32 beats"] }
                    Label { color: "white"; text: "MIDI-kanaal" }
                    SpinBox { id: channel; from: 1; to: 16 }
                    Label { color: "white"; text: "MIDI-ingang" }
                    ComboBox { id: device; Layout.columnSpan: 3; Layout.fillWidth: true; displayText: currentText || "Uit · volg deckklok" }
                    Label { color: "white"; text: "CC lokaal" }
                    SpinBox { id: localCC; from: 0; to: 127 }
                    Label { color: "white"; text: "CC peer" }
                    SpinBox { id: peerCC; from: 0; to: 127 }
                    Label { color: "white"; text: "Crossfader" }
                    ComboBox { id: crossMode; model: ["Bypass","Lokaal X / peer Y","Lokaal Y / peer X"] }
                    Label { color: "white"; text: "Crossfader CC" }
                    SpinBox { id: crossCC; from: 0; to: 127 }
                }
                Label { color: "#c0cabe"; text: desk.midi.error || desk.midi.last || "Beweeg beide kanaalfaders na het instellen."; Layout.fillWidth: true; elide: Text.ElideRight }
                Button { text: "Instellingen opslaan"; enabled: desk.available && !desk.status.armed
                    onClicked: desk.configure({host:host.text.trim(),port:port.value,phrase:phrase.currentIndex===0?16:32,device:device.currentText,channel:channel.value,localCC:localCC.value,peerCC:peerCC.value,cross:["","local-X","local-Y"][crossMode.currentIndex],crossCC:crossCC.value})
                }
                Item { Layout.fillHeight: true }
            }
        }
    }
}
