// Unit tests for Model.js (plain node, no deps).
const fs = require("fs")
const path = require("path")
const assert = require("assert")

const src = fs.readFileSync(path.join(__dirname, "..", "Model.js"), "utf8").replace(/^\.pragma library\s*/, "")
const M = new Function(src + "\nreturn { PLUGIN_VERSION, PROJECT_URL, DEFAULT_DEST, validDest, displayName, formatBytes, percent, homeRelative, helperArgs, parseJson, normalizeState, count, newFiles, arrivals, mountSet }")()

let passed = 0
function test(name, fn) { fn(); passed++ }

test("validDest", () => {
  assert.strictEqual(M.validDest(""), "~/Videos/{model}")
  assert.strictEqual(M.validDest(null), "~/Videos/{model}")
  assert.strictEqual(M.validDest("  /mnt/nas/{model} "), "/mnt/nas/{model}")
})

test("displayName", () => {
  assert.strictEqual(M.displayName("OsmoAction4"), "Osmo Action 4")
  assert.strictEqual(M.displayName("OsmoAction5Pro"), "Osmo Action 5 Pro")
  assert.strictEqual(M.displayName("OsmoPocket3"), "Osmo Pocket 3")
  assert.strictEqual(M.displayName("Osmo360"), "Osmo 360")
  assert.strictEqual(M.displayName(""), "DJI")
})

test("formatBytes", () => {
  assert.strictEqual(M.formatBytes(0), "0 B")
  assert.strictEqual(M.formatBytes(999), "999 B")
  assert.strictEqual(M.formatBytes(1500), "1.5 KB")
  assert.strictEqual(M.formatBytes(45721664756), "46 GB")
  assert.strictEqual(M.formatBytes(-5), "0 B")
  assert.strictEqual(M.formatBytes(2500000, (n, d) => n.toFixed(d).replace(".", ",")), "2,5 MB")
})

test("percent", () => {
  assert.strictEqual(M.percent(50, 200), 0.25)
  assert.strictEqual(M.percent(5, 0), 0)
  assert.strictEqual(M.percent(500, 200), 1)
})

test("homeRelative", () => {
  assert.strictEqual(M.homeRelative("/home/a/Videos/X", "/home/a"), "~/Videos/X")
  assert.strictEqual(M.homeRelative("/home/a/", "/home/a"), "~")
  assert.strictEqual(M.homeRelative("/home/ab/x", "/home/a"), "/home/ab/x")
  assert.strictEqual(M.homeRelative("/mnt/nas", "/home/a"), "/mnt/nas")
})

test("helperArgs", () => {
  assert.deepStrictEqual(M.helperArgs("/p/dji.py", "watch", { dest: "", photos: true, proxies: false }),
    ["python3", "/p/dji.py", "watch", "--dest", "~/Videos/{model}"])
  assert.deepStrictEqual(M.helperArgs("/p/dji.py", "import", { dest: "/x", photos: false, proxies: true, move: true, card: "/run/media/u/SD" }),
    ["python3", "/p/dji.py", "import", "--json", "--dest", "/x", "--no-photos", "--proxies", "--move", "--card", "/run/media/u/SD"])
  assert.ok(!M.helperArgs("/p", "watch", { move: true, card: "/c" }).includes("--move"))
})

test("parseJson", () => {
  assert.deepStrictEqual(M.parseJson('{"a":1}'), { a: 1 })
  assert.strictEqual(M.parseJson("[1]"), null)
  assert.strictEqual(M.parseJson("nope"), null)
  assert.strictEqual(M.parseJson(""), null)
})

test("normalizeState", () => {
  const s = M.normalizeState({ cards: [{ mount: "/m", newFiles: 3 }, null, { nope: 1 }], unmounted: ["OsmoAction4"], error: null })
  assert.strictEqual(s.cards.length, 1)
  assert.deepStrictEqual(s.unmounted, ["OsmoAction4"])
  assert.strictEqual(s.error, "")
  assert.deepStrictEqual(M.normalizeState(null), { cards: [], unmounted: [], error: "" })
})

test("counts", () => {
  const card = { newFiles: 4, counts: { video: { total: 7, new: 5 } } }
  assert.strictEqual(M.count(card, "video", "total"), 7)
  assert.strictEqual(M.count(card, "photo", "new"), 0)
  assert.strictEqual(M.newFiles(card), 4)
  assert.strictEqual(M.newFiles(null), 0)
})

test("arrivals only reports new mounts with new media", () => {
  const cards = [{ mount: "/a", newFiles: 2 }, { mount: "/b", newFiles: 0 }, { mount: "/c", newFiles: 1 }]
  assert.deepStrictEqual(M.arrivals({ "/a": true }, cards).map(c => c.mount), ["/c"])
  assert.deepStrictEqual(M.mountSet(cards), { "/a": true, "/b": true, "/c": true })
})

test("version tracks manifest", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"))
  assert.strictEqual(M.PLUGIN_VERSION, manifest.version)
  assert.strictEqual(M.PROJECT_URL, "https://github.com/astorrer/omarchy-dji")
  assert.strictEqual(M.DEFAULT_DEST, manifest.barWidget.defaults.importPath)
})

console.log(`model: ${passed} tests ok`)
