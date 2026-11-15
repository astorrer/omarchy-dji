import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// Owns the `dji.py watch` stream (cards, models, what is new) and the
// `dji.py import` run, so both keep going while the panel is closed.
Item {
  id: root

  property var settings: ({})
  property var i18n: null

  readonly property string importPath: Model.validDest(setting("importPath", Model.DEFAULT_DEST))
  readonly property bool includePhotos: setting("includePhotos", true) !== false
  readonly property bool includeProxies: setting("includeProxies", false) === true
  readonly property bool deleteAfterImport: setting("deleteAfterImport", false) === true
  readonly property bool notifyOnConnect: setting("notifyOnConnect", true) !== false
  readonly property bool alwaysShowIcon: setting("alwaysShowIcon", false) === true
  readonly property string home: Quickshell.env("HOME")
  readonly property string helperPath: decodeURIComponent(Qt.resolvedUrl("dji.py").toString().replace(/^file:\/\//, ""))
  readonly property var watchArgs: Model.helperArgs(helperPath, "watch", {
    dest: importPath, photos: includePhotos, proxies: includeProxies
  })

  property var state: ({ cards: [], unmounted: [], error: "" })
  readonly property var card: state.cards.length > 0 ? state.cards[0] : null
  readonly property bool connected: card !== null || state.unmounted.length > 0
  property bool importing: false
  property bool cancelling: false
  property var progress: ({ bytes: 0, total: 0, index: 0, files: 0, name: "" })
  property var result: null
  property bool _restarting: false
  property bool _seeded: false
  property var _seen: ({})

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function t(key) { return i18n ? i18n.t(key) : key }
  function tn(key, n) { return i18n ? i18n.tn(key, n) : key }

  function restartWatch() {
    _restarting = true
    if (watchProc.running) watchProc.running = false
    watchRestart.interval = 300
    watchRestart.restart()
  }

  function applyState(line) {
    var parsed = Model.parseJson(line)
    if (!parsed) return
    var next = Model.normalizeState(parsed)
    if (_seeded && notifyOnConnect && !importing) {
      var fresh = Model.arrivals(_seen, next.cards)
      for (var i = 0; i < fresh.length; i++) {
        notify(t("notify.connected").arg(Model.displayName(fresh[i].folder)),
          tn("notify.ready", Model.newFiles(fresh[i])).arg(Model.newFiles(fresh[i]))
            .arg(Model.formatBytes(fresh[i].newBytes)))
      }
    }
    _seen = Model.mountSet(next.cards)
    _seeded = true
    state = next
  }

  function notify(title, body) {
    Quickshell.execDetached(["notify-send", "-a", "DJI Import", "-i", "camera-video", String(title), String(body)])
  }

  function startImport() {
    if (importing || !card) return
    result = null
    progress = { bytes: 0, total: 0, index: 0, files: 0, name: "" }
    importing = true
    importProc.command = Model.helperArgs(helperPath, "import", {
      dest: importPath, photos: includePhotos, proxies: includeProxies,
      move: deleteAfterImport, card: card.mount
    })
    importProc.running = true
  }

  function cancelImport() {
    if (!importProc.running || cancelling) return
    cancelling = true
    importProc.signal(15)
  }

  function handleEvent(line) {
    var ev = Model.parseJson(line)
    if (!ev) return
    if (ev.event === "start") {
      progress = { bytes: 0, total: Number(ev.bytes) || 0, index: 0, files: Number(ev.files) || 0, name: "" }
    } else if (ev.event === "file") {
      progress = { bytes: progress.bytes, total: progress.total, index: Number(ev.index) || 0,
        files: Number(ev.files) || 0, name: String(ev.name || "").slice(0, 200) }
    } else if (ev.event === "progress") {
      progress = { bytes: Number(ev.bytes) || 0, total: Number(ev.total) || progress.total,
        index: progress.index, files: progress.files, name: progress.name }
    } else if (ev.event === "done") {
      result = {
        kind: "done", dest: String(ev.dest || ""),
        copied: Number(ev.copied) || 0, skipped: Number(ev.skipped) || 0, deleted: Number(ev.deleted) || 0,
        errors: Array.isArray(ev.errors) ? ev.errors.slice(0, 20).map(function(e) { return String(e).slice(0, 300) }) : []
      }
    } else if (ev.event === "error") {
      result = { kind: "error", message: String(ev.message || "").slice(0, 400) }
    } else if (ev.event === "cancelled") {
      result = { kind: "cancelled" }
    }
  }

  function pickFolder() {
    if (!pickProc.running) pickProc.running = true
  }

  function openFolder() {
    var dest = card && card.dest ? card.dest : (result && result.dest ? result.dest : "")
    if (dest === "") return
    Quickshell.execDetached(["sh", "-c", "mkdir -p -- \"$1\" && exec xdg-open \"$1\"", "sh", dest])
  }

  signal folderPicked(string path)

  onWatchArgsChanged: restartWatch()
  Component.onCompleted: restartWatch()

  Timer {
    id: watchRestart
    repeat: false
    onTriggered: {
      root._restarting = false
      watchProc.command = root.watchArgs
      watchProc.running = true
    }
  }

  Process {
    id: watchProc
    running: false
    stdout: SplitParser { onRead: function(line) { root.applyState(line) } }
    onExited: {
      if (root._restarting) return
      watchRestart.interval = 5000
      watchRestart.restart()
    }
  }

  Process {
    id: importProc
    running: false
    stdout: SplitParser { onRead: function(line) { root.handleEvent(line) } }
    onExited: function(exitCode) {
      if (!root.result) root.result = { kind: "error", message: root.t("import.stopped") }
      if (root.result.kind === "done") {
        root.notify(root.t("notify.doneTitle"),
          root.tn("result.copied", root.result.copied).arg(root.result.copied) +
          (root.result.errors.length ? " · " + root.tn("result.errors", root.result.errors.length).arg(root.result.errors.length) : ""))
      }
      root.importing = false
      root.cancelling = false
      root.restartWatch()
    }
  }

  Process {
    id: pickProc
    running: false
    command: ["omarchy-file-select", "--directory", "--title", root.t("dest.pickTitle")]
    stdout: StdioCollector { id: pickOut; waitForEnd: true }
    onExited: function(exitCode) {
      var path = String(pickOut.text || "").split("\n")[0].trim()
      if (exitCode === 0 && path.charAt(0) === "/") root.folderPicked(Model.homeRelative(path, root.home))
    }
  }
}
