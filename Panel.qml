import QtQuick
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// DJI camera import for the Omarchy bar. The icon shows up while a DJI camera
// or card is mounted; the panel imports its media into a dated folder tree.
Panel {
  id: root
  moduleName: "io.github.astorrer.omarchy-dji"
  ipcTarget: "io.github.astorrer.omarchy-dji"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color accent: Commons.Color.accent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property var card: dji.card
  readonly property string cameraName: card ? Model.displayName(card.folder) : ""
  readonly property int pending: Model.newFiles(card)
  readonly property int importedCount: Model.importedFiles(card)
  readonly property int proxyCount: Model.cleanProxies(card)
  readonly property bool canClean: card !== null && (importedCount > 0 || proxyCount > 0)
  readonly property string cleanMessage: {
    if (!card) return ""
    var where = Model.homeRelative(card.dest, dji.home)
    if (importedCount === 0)
      return strings.tn("clean.confirmOnlyProxies", proxyCount).arg(proxyCount).arg(where)
    var text = strings.tn("clean.confirm", importedCount).arg(importedCount)
      .arg(bytes(card.importedBytes)).arg(where)
    if (proxyCount > 0) text += "\n\n" + strings.tn("clean.confirmProxies", proxyCount).arg(proxyCount)
    return text
  }
  property bool cleanConfirmOpen: false
  readonly property string destProblem: card && typeof card.destError === "string" ? card.destError.slice(0, 300) : ""
  readonly property real fraction: Model.percent(dji.progress.bytes, dji.progress.total)
  readonly property string tooltip: {
    if (dji.importing)
      return strings.t(dji.job === "clean" ? "tooltip.cleaning" : "tooltip.importing").arg(Math.round(fraction * 100))
        .arg(dji.progress.index).arg(dji.progress.files)
    if (card) return strings.t("tooltip.card").arg(cameraName).arg(pending)
    if (dji.state.unmounted.length) return strings.t("tooltip.unmounted").arg(dji.state.unmounted[0])
    return strings.t("title")
  }

  function writeSetting(key, value) {
    var entry = { id: moduleName }
    for (var k in root.settings) if (k !== "id") entry[k] = root.settings[k]
    entry[key] = value
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(moduleName, entry)
  }

  function bytes(n) { return Model.formatBytes(n, Commons.I18n.formatNumber) }

  function countText(kind) {
    return strings.t("card.countNew")
      .arg(Commons.I18n.formatNumber(Model.count(card, kind, "total"), 0))
      .arg(Commons.I18n.formatNumber(Model.count(card, kind, "new"), 0))
  }

  function resultText() {
    var r = dji.result
    if (!r) return ""
    if (r.kind === "cancelled") return strings.t("result.cancelled")
    if (r.kind === "error") return strings.t("result.failed").arg(r.message)
    if (r.mode === "clean") {
      var cleaned = [strings.tn("result.removed", r.deleted).arg(r.deleted)]
      if (r.proxiesDeleted > 0) cleaned.push(strings.tn("result.proxies", r.proxiesDeleted).arg(r.proxiesDeleted))
      if (r.kept > 0) cleaned.push(strings.tn("result.kept", r.kept).arg(r.kept))
      return cleaned.join(" · ")
    }
    var parts = [strings.tn("result.copied", r.copied).arg(r.copied)]
    if (r.renamed > 0) parts.push(strings.tn("result.renamed", r.renamed).arg(r.renamed))
    if (r.skipped > 0) parts.push(strings.tn("result.skipped", r.skipped).arg(r.skipped))
    if (r.deleted > 0) parts.push(strings.tn("result.deleted", r.deleted).arg(r.deleted))
    return parts.join(" · ")
  }

  visible: dji.connected || dji.importing || dji.alwaysShowIcon || opened
  implicitWidth: visible ? button.implicitWidth : 0
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (!opened) cleanConfirmOpen = false
  else {
    if (!dji.importing) dji.restartWatch()
    Qt.callLater(function() { if (panel.loadedContent) panel.loadedContent.forceActiveFocus() })
  }

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  Service {
    id: dji
    settings: root.settings
    i18n: strings
    onFolderPicked: function(path) { root.writeSetting("importPath", path) }
  }

  ShellIpc {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { dji.restartWatch(); return "ok" }
    function startImport(): string { dji.startImport(); return dji.importing ? "importing" : "no card" }
    function cancel(): string { dji.cancelImport(); return "ok" }
    // Opens the confirmation only; nothing is deleted until it is accepted.
    function cleanUp(): string {
      if (!root.canClean || dji.importing) return "nothing to clean"
      root.open()
      root.cleanConfirmOpen = true
      return "confirm in panel"
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // Icon only, like the rest of the bar: the active color marks an import
    // in progress, and the tooltip carries counts and progress.
    active: dji.importing
    dimmed: !root.card && !dji.importing
    tooltipText: root.tooltip
    iconComponent: Component {
      DjiIcon {
        color: button.active && button.useActiveColor ? button.activeColor : button.foreground
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) dji.restartWatch()
      else if (buttonCode === Qt.MiddleButton) dji.openFolder()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(620))

    content: PanelKeyCatcher {
      implicitHeight: column.implicitHeight
      anchors.fill: parent
      onActivateRequested: {
        if (root.cleanConfirmOpen) {
          if (cleanConfirm.selectedIndex === 1) cleanConfirm.confirmed()
          else cleanConfirm.canceled()
        } else if (!dji.importing) dji.startImport()
      }
      onCloseRequested: root.cleanConfirmOpen ? cleanConfirm.canceled() : root.close()
      onMoveRequested: function(dx, dy) {
        if (root.cleanConfirmOpen && dx !== 0) cleanConfirm.selectedIndex = cleanConfirm.selectedIndex === 0 ? 1 : 0
      }
      onTabRequested: function(direction) {
        if (root.cleanConfirmOpen) cleanConfirm.selectedIndex = cleanConfirm.selectedIndex === 0 ? 1 : 0
        else root.switchPanel(direction)
      }
      onTextKey: function(t) {
        if (root.cleanConfirmOpen) return
        if (t === "o" || t === "O") dji.openFolder()
        else if (t === "r" || t === "R") dji.restartWatch()
        else if (t === "i" || t === "I") dji.startImport()
      }

      Column {
        id: column
        width: parent.width
        spacing: Style.space(12)

        PanelHero {
          id: hero
          width: parent.width
          trailingControl: Component {
            PanelActionButton {
              visible: dji.connected
              iconText: "󰑐"
              tooltipText: strings.t("refresh")
              foreground: hero.foreground
              fontFamily: hero.fontFamily
              enabled: !dji.importing
              onClicked: dji.restartWatch()
            }
          }
          title: root.card ? root.cameraName : strings.t("title")
          meta: root.card
            ? strings.t(root.card.connection === "usb" ? "card.viaUsb" : "card.viaReader").arg(root.card.label)
            : strings.t("empty.title")
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconOpacity: root.card ? 1.0 : 0.5
          iconComponent: Component {
            DjiIcon {
              iconSize: Style.font.display * 0.75
              color: root.foreground
            }
          }
        }

        BodyText {
          visible: !root.card
          text: dji.state.unmounted.length
            ? strings.t("empty.unmounted").arg(dji.state.unmounted[0])
            : strings.t("empty.help")
        }

        BodyText {
          visible: dji.state.error !== ""
          text: dji.state.error
          color: root.urgent
        }

        Column {
          visible: root.card !== null
          width: parent.width
          spacing: Style.spacing.labelGap

          InfoPair { label: strings.t("card.videos"); value: root.countText("video") }
          InfoPair { visible: dji.includePhotos; label: strings.t("card.photos"); value: root.countText("photo") }
          InfoPair { visible: dji.includeProxies; label: strings.t("card.proxies"); value: root.countText("proxy") }
          InfoPair { label: strings.t("card.toImport"); value: root.bytes(root.card ? root.card.newBytes : 0) }
        }

        PanelSeparator { foreground: root.foreground }

        Column {
          width: parent.width
          spacing: Style.space(6)

          PanelSectionHeader {
            text: strings.t("dest.header")
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          RowLayout {
            width: parent.width
            spacing: Style.space(6)

            ColumnLayout {
              Layout.fillWidth: true
              spacing: Style.space(1)
              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: dji.importPath
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideMiddle
              }
              Text {
                Layout.fillWidth: true
                visible: root.card !== null && root.card.dest !== ""
                textFormat: Text.PlainText
                text: root.card ? Model.homeRelative(root.card.dest, dji.home) + "/YYYY-MM-DD/" : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideMiddle
              }
              Text {
                Layout.fillWidth: true
                visible: root.card !== null && root.card.dest !== ""
                textFormat: Text.PlainText
                text: root.destProblem !== ""
                  ? strings.t("dest.error").arg(root.destProblem)
                  : strings.t(root.card && root.card.destExists ? "dest.exists" : "dest.willCreate")
                color: root.destProblem !== "" ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            PanelActionButton {
              iconText: "󰉋"
              tooltipText: strings.t("dest.pick")
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !dji.importing
              onClicked: dji.pickFolder()
            }
            PanelActionButton {
              visible: dji.importPath !== Model.DEFAULT_DEST
              iconText: "󰑓"
              tooltipText: strings.t("dest.reset")
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !dji.importing
              onClicked: root.writeSetting("importPath", Model.DEFAULT_DEST)
            }
            PanelActionButton {
              iconText: "󰏌"
              tooltipText: strings.t("dest.open")
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: root.card !== null
              onClicked: dji.openFolder()
            }
          }
        }

        Column {
          width: parent.width
          spacing: Style.space(6)

          OptionToggle {
            label: strings.t("opt.photos")
            description: strings.t("opt.photosHelp")
            checked: dji.includePhotos
            onClicked: root.writeSetting("includePhotos", !dji.includePhotos)
          }
          OptionToggle {
            label: strings.t("opt.proxies")
            description: strings.t("opt.proxiesHelp")
            checked: dji.includeProxies
            onClicked: root.writeSetting("includeProxies", !dji.includeProxies)
          }
          OptionToggle {
            label: strings.t("opt.move")
            description: strings.t("opt.moveHelp")
            checked: dji.deleteAfterImport
            onClicked: root.writeSetting("deleteAfterImport", !dji.deleteAfterImport)
          }
        }

        Column {
          visible: dji.importing
          width: parent.width
          spacing: Style.space(6)

          Rectangle {
            width: parent.width
            height: Style.space(6)
            radius: Style.cornerRadius > 0 ? height / 2 : 0
            color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.15)
            Rectangle {
              width: parent.width * root.fraction
              height: parent.height
              radius: parent.radius
              color: root.accent
              Behavior on width { NumberAnimation { duration: Style.duration(200); easing.type: Easing.OutCubic } }
            }
          }
          BodyText {
            text: strings.t(dji.job === "clean" ? "clean.progress" : "import.progress")
              .arg(dji.progress.index).arg(dji.progress.files)
              .arg(root.bytes(dji.progress.bytes)).arg(root.bytes(dji.progress.total))
          }
          BodyText {
            text: dji.progress.name
            color: root.dim
            elide: Text.ElideMiddle
            wrapMode: Text.NoWrap
          }
        }

        BodyText {
          visible: !dji.importing && dji.result !== null
          text: root.resultText()
          color: dji.result && (dji.result.kind === "error" || (dji.result.errors && dji.result.errors.length)) ? root.urgent : root.foreground
        }

        Repeater {
          model: !dji.importing && dji.result && dji.result.errors ? dji.result.errors : []
          BodyText {
            required property var modelData
            text: modelData
            color: root.urgent
            font.pixelSize: Style.font.caption
          }
        }

        Button {
          width: parent.width
          visible: root.card !== null || dji.importing
          enabled: dji.importing ? !dji.cancelling : (root.pending > 0 && root.destProblem === "")
          iconText: dji.importing ? "󰜺" : "󰇚"
          text: dji.importing
            ? strings.t(dji.cancelling ? "import.cancelling" : (dji.job === "clean" ? "clean.cancel" : "import.cancel"))
            : (root.pending > 0
              ? strings.tn("import.button", root.pending).arg(root.pending).arg(root.bytes(root.card.newBytes))
              : strings.t("import.upToDate"))
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          bordered: true
          onClicked: dji.importing ? dji.cancelImport() : dji.startImport()
        }

        Button {
          width: parent.width
          visible: root.canClean && !dji.importing
          iconText: "󰃢"
          text: root.importedCount > 0
            ? strings.tn("clean.button", root.importedCount).arg(root.importedCount)
              .arg(root.bytes(root.card ? root.card.importedBytes : 0))
            : strings.tn("clean.buttonProxies", root.proxyCount).arg(root.proxyCount)
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          bordered: true
          onClicked: root.cleanConfirmOpen = true
        }

        Text {
          width: parent.width
          textFormat: Text.PlainText
          horizontalAlignment: Text.AlignRight
          text: "DJI Import " + Model.PLUGIN_VERSION
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: Qt.openUrlExternally(Model.PROJECT_URL)
          }
        }
      }

      // Deleting from the card always goes through this; Cancel is preselected.
      ConfirmDialog {
        id: cleanConfirm
        anchors.fill: parent
        z: 10
        opened: root.cleanConfirmOpen
        selectedIndex: 0
        message: root.cleanMessage
        confirmText: strings.t("clean.confirmButton")
        foreground: root.foreground
        fontFamily: root.fontFamily
        onOpenedChanged: if (opened) selectedIndex = 0
        onCanceled: root.cleanConfirmOpen = false
        onConfirmed: {
          root.cleanConfirmOpen = false
          dji.startClean()
        }
      }
    }
  }

  // Locked while an import runs, and dimmed so that reads at a glance.
  component OptionToggle: Toggle {
    width: parent ? parent.width : 0
    foreground: root.foreground
    accent: root.accent
    fontFamily: root.fontFamily
    enabled: !dji.importing
    opacity: enabled ? 1.0 : 0.45
    Behavior on opacity { NumberAnimation { duration: Style.duration(180); easing.type: Easing.OutCubic } }
  }

  component BodyText: Text {
    width: parent ? parent.width : 0
    textFormat: Text.PlainText
    color: root.dim
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
    wrapMode: Text.WordWrap
  }

  component InfoPair: RowLayout {
    property string label: ""
    property string value: ""
    width: parent ? parent.width : 0
    spacing: Style.space(8)
    Text {
      textFormat: Text.PlainText
      text: label
      color: root.foreground
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
    Item { Layout.fillWidth: true }
    Text {
      textFormat: Text.PlainText
      text: value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }
}
