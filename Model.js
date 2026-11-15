.pragma library

var PLUGIN_VERSION = "0.1.0"
var PROJECT_URL = "https://github.com/astorrer/omarchy-dji"
var DEFAULT_DEST = "~/Videos/{model}"
var HELPER_INTERPRETER = "python3"

function validDest(value) {
  var text = String(value === undefined || value === null ? "" : value).trim()
  return text === "" ? DEFAULT_DEST : text
}

// "OsmoAction5Pro" -> "Osmo Action 5 Pro", "Osmo360" -> "Osmo 360".
function displayName(folder) {
  var text = String(folder || "DJI")
  return text.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/([a-z])(\d)/g, "$1 $2").replace(/(\d)([A-Z])/g, "$1 $2")
}

function formatBytes(bytes, formatNumber) {
  var n = Number(bytes)
  if (!isFinite(n) || n < 0) n = 0
  var units = ["B", "KB", "MB", "GB", "TB"]
  var i = 0
  while (n >= 1000 && i < units.length - 1) {
    n /= 1000
    i++
  }
  var decimals = i === 0 ? 0 : (n < 10 ? 1 : 0)
  var text = formatNumber ? formatNumber(n, decimals) : n.toFixed(decimals)
  return text + " " + units[i]
}

function percent(bytes, total) {
  var b = Number(bytes), t = Number(total)
  if (!isFinite(b) || !isFinite(t) || t <= 0) return 0
  return Math.max(0, Math.min(1, b / t))
}

function homeRelative(path, home) {
  var p = String(path || "").trim().replace(/\/+$/, "")
  var h = String(home || "").replace(/\/+$/, "")
  if (h !== "" && p === h) return "~"
  if (h !== "" && p.indexOf(h + "/") === 0) return "~" + p.slice(h.length)
  return p
}

// Import path template for a folder chosen in the picker: keep one folder per
// camera by appending {model}, unless the pick already is that camera's folder.
function pickedTemplate(path, home, folder) {
  var raw = String(path || "").trim()
  if (raw === "") return DEFAULT_DEST
  var dir = homeRelative(raw, home) || "/"
  var parts = dir.split("/")
  if (folder && parts.length > 1 && parts[parts.length - 1] === folder) {
    parts[parts.length - 1] = "{model}"
    return parts.join("/")
  }
  return (dir === "/" ? "" : dir) + "/{model}"
}

function helperArgs(helper, command, opts) {
  var o = opts || {}
  var args = [HELPER_INTERPRETER, helper, command]
  if (command !== "watch") args.push("--json")
  args.push("--dest", validDest(o.dest))
  if (o.photos === false) args.push("--no-photos")
  if (o.proxies === true) args.push("--proxies")
  if (command === "import") {
    if (o.move === true) args.push("--move")
    if (o.card) args.push("--card", String(o.card))
  }
  return args
}

function parseJson(line) {
  var text = String(line || "").trim()
  if (text === "" || text.length > 1000000) return null
  try {
    var value = JSON.parse(text)
    return value && typeof value === "object" && !Array.isArray(value) ? value : null
  } catch (e) {
    return null
  }
}

// Coerce a `dji.py status` line into the shape the UI binds to.
function normalizeState(raw) {
  var state = raw || {}
  var cards = Array.isArray(state.cards) ? state.cards.slice(0, 8) : []
  var unmounted = Array.isArray(state.unmounted) ? state.unmounted.slice(0, 8).map(String) : []
  return {
    cards: cards.filter(function(c) { return c && typeof c === "object" && typeof c.mount === "string" }),
    unmounted: unmounted,
    error: typeof state.error === "string" ? state.error.slice(0, 400) : ""
  }
}

function count(card, kind, field) {
  var c = card && card.counts && card.counts[kind]
  var n = c ? Number(c[field]) : 0
  return isFinite(n) && n > 0 ? Math.floor(n) : 0
}

function newFiles(card) {
  var n = card ? Number(card.newFiles) : 0
  return isFinite(n) && n > 0 ? Math.floor(n) : 0
}

// Mounts that appeared since `seen` and have something new to import.
function arrivals(seen, cards) {
  var out = []
  for (var i = 0; i < cards.length; i++) {
    if (!seen[cards[i].mount] && newFiles(cards[i]) > 0) out.push(cards[i])
  }
  return out
}

function mountSet(cards) {
  var set = {}
  for (var i = 0; i < cards.length; i++) set[cards[i].mount] = true
  return set
}
