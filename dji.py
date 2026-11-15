#!/usr/bin/env python3
"""Detect DJI Osmo cameras and cards, and import their media.

  dji.py status [--dest TEMPLATE] [--no-photos] [--proxies] [--json]
  dji.py import [--card MOUNT] [--dest TEMPLATE] [--no-photos] [--proxies]
                [--move] [--dry-run] [--json]
  dji.py watch  [--interval SEC] [--dest TEMPLATE] [--no-photos] [--proxies]

Media lands in TEMPLATE/YYYY-MM-DD/, where TEMPLATE defaults to
~/Videos/{model} and {model} is the name the camera reports (OsmoAction4).
Missing folders are created; nothing in the destination is ever overwritten
or deleted. A card file counts as imported when a file with the same name and
size exists anywhere under the destination, so a reorganized library is not
re-copied. Copies are written to a hidden .part file, fsynced, and renamed into
place; --move deletes a card file only after its copy is re-read from disk and
its hash matches.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

DJI_VENDOR = "2ca3"
DEFAULT_DEST = "~/Videos/{model}"
CARD_FSTYPES = {"vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk"}
KINDS = {
    ".mp4": "video", ".mov": "video", ".osv": "video",
    ".jpg": "photo", ".jpeg": "photo", ".dng": "photo",
    ".lrf": "proxy",
}
NAME_DATE = re.compile(r"^DJI_(\d{4})(\d{2})(\d{2})")
SUFFIXED = re.compile(r"^(.+)-(\d+)(\.[^.]+)$")
MODEL_BYTES = re.compile(rb"(Osmo[A-Za-z0-9]{1,24})")
CHUNK = 4 * 1024 * 1024
PROGRESS_EVERY = 0.25


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- detection


def parse_mounts(text):
    """Return (device, mountpoint, fstype) for each line of /proc/self/mounts."""
    unescape = lambda s: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)  # noqa: E731
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            out.append((unescape(parts[0]), unescape(parts[1]), parts[2]))
    return out


def dji_folders(mount):
    dcim = Path(mount) / "DCIM"
    try:
        return sorted(p for p in dcim.iterdir() if p.is_dir() and p.name.upper().startswith("DJI_"))
    except OSError:
        return []


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def usb_cameras(sysroot="/sys"):
    """DJI USB devices currently attached: [{product, path}]."""
    out = []
    base = Path(sysroot) / "bus/usb/devices"
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return out
    for dev in entries:
        if read_text(dev / "idVendor") == DJI_VENDOR:
            out.append({"product": read_text(dev / "product") or "DJI", "path": str(dev.resolve())})
    return out


def usb_product_for_device(device, sysroot="/sys"):
    """The DJI USB product name behind a block device such as /dev/sda1, or ""."""
    name = os.path.basename(device)
    try:
        node = (Path(sysroot) / "class/block" / name).resolve()
    except OSError:
        return ""
    for parent in [node, *node.parents]:
        if (parent / "idVendor").exists():
            return read_text(parent / "product") if read_text(parent / "idVendor") == DJI_VENDOR else ""
    return ""


def mp4_moov(path, limit=32 * 1024 * 1024):
    """Bytes of the top-level moov box of an MP4/MOV, or b""."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            pos = 0
            while pos + 8 <= size:
                f.seek(pos)
                header = f.read(16)
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                head_len = 8
                if box_size == 1:
                    box_size = int.from_bytes(header[8:16], "big")
                    head_len = 16
                elif box_size == 0:
                    box_size = size - pos
                if box_size < head_len:
                    return b""
                if box_type == b"moov":
                    f.seek(pos + head_len)
                    return f.read(min(box_size - head_len, limit))
                pos += box_size
    except OSError:
        pass
    return b""


def sniff_model(path):
    """Camera model string embedded in a DJI file (e.g. OsmoAction4), or ""."""
    suffix = Path(path).suffix.lower()
    if KINDS.get(suffix) == "video":
        data = mp4_moov(path)
    else:
        try:
            with open(path, "rb") as f:
                data = f.read(128 * 1024)
        except OSError:
            data = b""
    m = MODEL_BYTES.search(data)
    return m.group(1).decode() if m else ""


def folder_name(model):
    """The camera's own name, made safe for a path: "DJI OsmoAction4" -> OsmoAction4."""
    model = re.sub(r"^DJI\s*", "", model or "", flags=re.I)
    model = re.sub(r"[^A-Za-z0-9._-]", "", model).lstrip(".")
    return model or "DJI"


def expand_dest(template, folder, home=None):
    home = home or os.path.expanduser("~")
    text = (template or DEFAULT_DEST).strip().replace("{model}", folder)
    if text == "~" or text.startswith("~/"):
        text = home + text[1:]
    path = Path(text)
    if not path.is_absolute():
        raise ValueError(f"import path must be absolute or start with ~/: {template}")
    return path


def day_for(path, name):
    m = NAME_DATE.match(name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    try:
        return datetime.fromtimestamp(os.stat(path).st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return "unsorted"


def library_index(dest, limit=500_000):
    """name -> [(size, path)] for every file already under dest, at any depth.

    Lets a library the user has reorganized (or one that predates this tool)
    count as imported, so nothing is copied twice. A copy renamed to dodge a
    name clash (NAME-2.MP4) is also listed under its original name. Hidden
    entries are skipped.
    """
    index = {}
    seen = 0
    for root, dirs, files in os.walk(dest):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            path = Path(root) / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            index.setdefault(name, []).append((size, path))
            renamed = SUFFIXED.match(name)
            if renamed:
                index.setdefault(renamed.group(1) + renamed.group(3), []).append((size, path))
            seen += 1
            if seen >= limit:
                return index
    return index


def check_dest(dest, card_mount=None):
    """Why dest cannot be imported into, or None when it is safe to use."""
    if card_mount:
        card = os.path.realpath(card_mount)
        real = os.path.realpath(dest)
        if real == card or real.startswith(card.rstrip("/") + "/"):
            return f"{dest} is on the card itself"
    for probe in [dest, *dest.parents]:
        if os.path.lexists(probe):
            if not probe.is_dir():
                return f"{probe} exists but is not a folder"
            if not os.access(probe, os.W_OK | os.X_OK):
                return f"{probe} is not writable"
            return None
    return None


def free_target(folder, name, claimed):
    """folder/name, or folder/NAME-2.EXT, -3, ... if that name is taken."""
    target = folder / name
    stem, ext = os.path.splitext(name)
    n = 1
    while os.path.lexists(target) or target in claimed:
        n += 1
        target = folder / f"{stem}-{n}{ext}"
    return target


def scan(card_folders, dest, photos=True, proxies=False):
    """Every importable file on the card with its target and status.

    status is "imported" when the same name and size already exist anywhere
    under dest, else "new". A new file whose name is taken in its date folder
    (another camera of the same model, say) gets a -2, -3, ... suffix instead
    of being skipped, so nothing is ever left behind on the card.
    """
    index = library_index(dest)
    claimed = set()
    want = {"video"} | ({"photo"} if photos else set()) | ({"proxy"} if proxies else set())
    items = []
    for folder in card_folders:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for src in entries:
            kind = KINDS.get(src.suffix.lower())
            try:
                if kind not in want or not src.is_file():
                    continue
                size = src.stat().st_size
            except OSError:  # card pulled mid-scan
                continue
            day = dest / day_for(src, src.name)
            found = next((path for s, path in index.get(src.name, []) if s == size), None)
            target = day / src.name if found else free_target(day, src.name, claimed)
            claimed.add(target)
            items.append({"src": src, "name": src.name, "kind": kind, "size": size, "target": target,
                          "found": found, "status": "imported" if found else "new",
                          "renamed": not found and target.name != src.name})
    return items


def summarize(items):
    counts = {k: {"total": 0, "new": 0} for k in ("video", "photo", "proxy")}
    new_bytes = 0
    for item in items:
        counts[item["kind"]]["total"] += 1
        if item["status"] == "new":
            counts[item["kind"]]["new"] += 1
            new_bytes += item["size"]
    return {
        "counts": counts,
        "newFiles": sum(c["new"] for c in counts.values()),
        "newBytes": new_bytes,
        "renamed": sum(1 for i in items if i["renamed"]),
        "importedFiles": sum(1 for i in items if i["status"] == "imported"),
        "importedBytes": sum(i["size"] for i in items if i["status"] == "imported"),
    }


def find_cards(mounts_text=None, sysroot="/sys"):
    if mounts_text is None:
        mounts_text = read_text("/proc/self/mounts")
    cards = []
    for device, mount, fstype in parse_mounts(mounts_text):
        if fstype not in CARD_FSTYPES:
            continue
        folders = dji_folders(mount)
        if folders:
            cards.append({"device": device, "mount": mount, "folders": folders,
                          "usbProduct": usb_product_for_device(device, sysroot)})
    return cards


_MODEL_CACHE = {}


def card_model(card):
    if card["usbProduct"]:
        return card["usbProduct"]
    for folder in card["folders"]:
        try:
            files = sorted(folder.iterdir())
        except OSError:
            continue
        for kind in ("video", "photo"):
            for f in files:
                if KINDS.get(f.suffix.lower()) != kind:
                    continue
                try:
                    key = (str(f), f.stat().st_size)
                except OSError:
                    continue
                if key not in _MODEL_CACHE:
                    if len(_MODEL_CACHE) > 256:
                        _MODEL_CACHE.clear()
                    _MODEL_CACHE[key] = sniff_model(f)
                if _MODEL_CACHE[key]:
                    return _MODEL_CACHE[key]
    return "DJI"


def status(dest_template=DEFAULT_DEST, photos=True, proxies=False, mounts_text=None, sysroot="/sys", home=None):
    cards = []
    error = None
    mounted_usb = set()
    for card in find_cards(mounts_text, sysroot):
        model = card_model(card)
        folder = folder_name(model)
        entry = {"mount": card["mount"], "label": os.path.basename(card["mount"]), "device": card["device"],
                 "model": model, "folder": folder, "connection": "usb" if card["usbProduct"] else "card"}
        try:
            dest = expand_dest(dest_template, folder, home)
            entry["dest"] = str(dest)
            entry["destExists"] = dest.is_dir()
            entry["destError"] = check_dest(dest, card["mount"])
            entry.update(summarize(scan(card["folders"], dest, photos, proxies)))
        except ValueError as exc:
            error = str(exc)
            entry["dest"] = ""
        if card["usbProduct"]:
            mounted_usb.add(card["usbProduct"])
        cards.append(entry)
    unmounted = [u["product"] for u in usb_cameras(sysroot) if u["product"] not in mounted_usb]
    return {"cards": cards, "unmounted": unmounted, "error": error}


# ---------------------------------------------------------------- import


def hash_file(path, drop_cache=False, on_bytes=None):
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        if drop_cache and hasattr(os, "posix_fadvise"):
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        while chunk := f.read(CHUNK):
            h.update(chunk)
            if on_bytes:
                on_bytes(len(chunk))
    return h.digest()


def copy_file(src, target, on_bytes, verify):
    """Copy src to target via a hidden .part file; return the source hash."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.parent / f".{target.name}.part"
    h = hashlib.blake2b(digest_size=32)
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while chunk := fi.read(CHUNK):
                h.update(chunk)
                fo.write(chunk)
                on_bytes(len(chunk))
            fo.flush()
            os.fsync(fo.fileno())
        st = os.stat(src)
        os.utime(part, ns=(st.st_atime_ns, st.st_mtime_ns))
        if verify and hash_file(part, drop_cache=True) != h.digest():
            raise OSError(f"verification failed for {target.name}")
        place_without_clobber(part, target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return h.digest()


def place_without_clobber(part, target):
    """Move part to target, failing instead of replacing anything there."""
    try:
        os.link(part, target)  # atomic, and refuses an existing target
    except FileExistsError:
        raise OSError(f"{target.name} appeared in the library during import; left on the card") from None
    except OSError:  # filesystem without hard links
        if os.path.lexists(target):
            raise OSError(f"{target.name} appeared in the library during import; left on the card") from None
        os.replace(part, target)
        return
    os.unlink(part)


def free_space(path):
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def run_import(card_mount=None, dest_template=DEFAULT_DEST, photos=True, proxies=False, move=False,
               dry_run=False, emit=print, mounts_text=None, sysroot="/sys", home=None, clean=False):
    """Copy new media off the card (clean=False), or with clean=True copy
    nothing and delete only card files whose imported copy hashes the same.
    """
    cards = find_cards(mounts_text, sysroot)
    if card_mount:
        cards = [c for c in cards if os.path.realpath(c["mount"]) == os.path.realpath(card_mount)]
    if not cards:
        emit({"event": "error", "message": "No DJI card found" + (f" at {card_mount}" if card_mount else "")})
        return 1
    if len(cards) > 1:
        emit({"event": "error", "message": "Several DJI cards are mounted; pick one with --card",
              "cards": [c["mount"] for c in cards]})
        return 1
    card = cards[0]
    model = card_model(card)
    try:
        dest = expand_dest(dest_template, folder_name(model), home)
    except ValueError as exc:
        emit({"event": "error", "message": str(exc)})
        return 1
    problem = check_dest(dest, card["mount"])
    if problem:
        emit({"event": "error", "message": problem})
        return 1

    items = scan(card["folders"], dest, photos, proxies)
    if clean:
        move = True
        todo = [i for i in items if i["status"] == "imported"]
        work_bytes = sum(i["size"] for i in todo)  # progress tracks reading the card copies
        copy_bytes = 0
    else:
        todo = [i for i in items if i["status"] == "new" or (move and i["status"] == "imported")]
        work_bytes = copy_bytes = sum(i["size"] for i in todo if i["status"] == "new")
    mode = "clean" if clean else "import"
    emit({"event": "start", "mode": mode, "model": model, "mount": card["mount"], "dest": str(dest),
          "files": len(todo), "bytes": work_bytes, "move": move, "dryRun": dry_run})

    if not dry_run and copy_bytes and free_space(dest) < copy_bytes:
        emit({"event": "error", "message": f"Not enough free space in {dest}", "needed": copy_bytes,
              "free": free_space(dest)})
        return 1

    copied = skipped = deleted = renamed = 0
    errors = []
    done_bytes = 0
    last = 0.0

    def on_bytes(n):
        nonlocal done_bytes, last
        done_bytes += n
        now = time.monotonic()
        if now - last >= PROGRESS_EVERY:
            last = now
            emit({"event": "progress", "bytes": done_bytes, "total": work_bytes})

    for index, item in enumerate(todo, 1):
        emit({"event": "file", "index": index, "files": len(todo), "name": item["name"], "size": item["size"],
              "day": item["target"].parent.name, "target": item["target"].name, "renamed": item["renamed"],
              "action": "copy" if item["status"] == "new" else "verify"})
        if dry_run:
            continue
        try:
            if item["status"] == "new":
                digest = copy_file(item["src"], item["target"], on_bytes, verify=move)
                copied += 1
                renamed += item["renamed"]
            else:
                digest = hash_file(item["found"], drop_cache=True)
                skipped += 1
            if move:
                if hash_file(item["src"], on_bytes=on_bytes if clean else None) != digest:
                    raise OSError(f"{item['name']} changed on the card during import; left in place")
                item["src"].unlink()
                deleted += 1
        except OSError as exc:
            errors.append(f"{item['name']}: {exc.strerror or exc}")
    skipped += sum(1 for i in items if i["status"] == "imported") if not move else 0
    kept = sum(1 for i in items if i["status"] == "new") if clean else 0
    emit({"event": "progress", "bytes": done_bytes, "total": work_bytes})
    emit({"event": "done", "mode": mode, "dest": str(dest), "copied": copied, "skipped": 0 if clean else skipped,
          "deleted": deleted, "renamed": renamed, "kept": kept, "errors": errors, "dryRun": dry_run})
    return 1 if errors else 0


# ---------------------------------------------------------------- output


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def print_status(state):
    if not state["cards"] and not state["unmounted"]:
        print("No DJI camera or card found.")
    for c in state["cards"]:
        counts = c.get("counts", {})
        print(f"{c['model']} ({c['connection']}) at {c['mount']}")
        note = "existing folder" if c.get("destExists") else "will be created"
        print(f"  import to: {c['dest']} ({note})")
        if c.get("destError"):
            print(f"  cannot import: {c['destError']}", file=sys.stderr)
        for kind in ("video", "photo", "proxy"):
            k = counts.get(kind)
            if k and k["total"]:
                print(f"  {kind}s: {k['total']} on card, {k['new']} new")
        print(f"  to import: {c.get('newFiles', 0)} files, {human_bytes(c.get('newBytes', 0))}")
    for product in state["unmounted"]:
        print(f"{product} connected over USB but no card is mounted. Choose \"Transfer\" mode on the camera.")
    if state["error"]:
        print(f"error: {state['error']}", file=sys.stderr)


def human_duration(seconds):
    seconds = int(max(0, seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def render_bar(done, total, rate, width, prefix=""):
    """One terminal line: prefix, bar, percent, bytes, speed, and time left."""
    frac = min(1.0, done / total) if total else 1.0
    stats = f" {frac * 100:3.0f}%  {human_bytes(done)} / {human_bytes(total)}"
    if rate > 0:
        stats += f"  {human_bytes(rate)}/s  {human_duration((total - done) / rate)} left"
    bar_width = max(10, width - len(prefix) - len(stats) - 1)
    filled = frac * bar_width
    full = int(filled)
    bar = "━" * full
    if full < bar_width:
        bar += ("╸" if filled - full >= 0.5 else "─") + "─" * (bar_width - full - 1)
    return f"{prefix}{bar}{stats}"[: max(width, 20)]


class HumanEmitter:
    RATE_WINDOW = 5.0

    def __init__(self, stream=None, tty=None, width=None, clock=time.monotonic):
        self.out = stream or sys.stdout
        self.tty = self.out.isatty() if tty is None else tty
        self.width = width
        self.clock = clock
        self.samples = []
        self.done = self.total = 0
        self.prefix = ""
        self.dry = False
        self.bar_shown = False

    def line(self, text, err=False):
        self.clear_bar()
        print(text, file=sys.stderr if err else self.out, flush=True)

    def clear_bar(self):
        if self.bar_shown:
            self.out.write("\r\033[K")
            self.bar_shown = False

    def rate(self):
        now = self.clock()
        self.samples.append((now, self.done))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.RATE_WINDOW:
            self.samples.pop(0)
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 - t0 >= 1.0 else 0.0

    def draw(self):
        if not self.tty or not self.total:
            return
        width = self.width or shutil.get_terminal_size((80, 24)).columns
        self.out.write("\r\033[K" + render_bar(self.done, self.total, self.rate(), width - 1, self.prefix))
        self.out.flush()
        self.bar_shown = True

    def __call__(self, ev):
        kind = ev["event"]
        if kind == "start":
            mode = " (dry run)" if ev["dryRun"] else (" (move)" if ev["move"] else "")
            if ev.get("mode") == "clean":
                mode = " to check and remove from the card" + (" (dry run)" if ev["dryRun"] else "")
            self.total = ev["bytes"]
            self.dry = ev["dryRun"]
            self.line(f"{ev['model']}: {ev['files']} file(s), {human_bytes(ev['bytes'])} -> {ev['dest']}{mode}")
        elif kind == "file":
            verb = "check + remove" if ev["action"] == "verify" else "copy"
            self.prefix = f"{ev['index']}/{ev['files']} "
            note = f", as {ev['target']} because the name is taken" if ev.get("renamed") else ""
            self.line(f"  {verb} {ev['day']}/{ev['name']} ({human_bytes(ev['size'])}{note})")
            if not self.dry:
                self.draw()
        elif kind == "progress":
            self.done = ev["bytes"]
            self.draw()
        elif kind == "done" and ev["dryRun"]:
            self.line("Dry run: nothing was changed.")
        elif kind == "done" and ev.get("mode") == "clean":
            self.draw()
            if self.bar_shown:
                self.out.write("\n")
                self.bar_shown = False
            self.line(f"Done: {ev['deleted']} removed from card, {ev['kept']} not yet imported kept")
            for err in ev["errors"]:
                self.line(f"  kept on card: {err}", err=True)
        elif kind == "done":
            self.draw()
            if self.bar_shown:
                self.out.write("\n")
                self.bar_shown = False
            extra = f" ({ev['renamed']} renamed to avoid a name clash)" if ev.get("renamed") else ""
            self.line(f"Done: {ev['copied']} copied{extra}, {ev['skipped']} already there, "
                      f"{ev['deleted']} removed from card")
            for err in ev["errors"]:
                self.line(f"  error: {err}", err=True)
        elif kind == "error":
            self.line(f"error: {ev['message']}", err=True)
        elif kind == "cancelled":
            self.line("Cancelled; partial file removed.", err=True)


def json_emit(ev):
    sys.stdout.write(json.dumps(ev, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def device_signature():
    return read_text("/proc/self/mounts"), tuple(u["product"] for u in usb_cameras())


def watch(args, get_status=None, signature=device_signature, sleep=time.sleep, clock=time.monotonic,
          out=None, loops=None):
    """Print status whenever it changes.

    Mounts and DJI USB devices are checked every --interval seconds. While a
    card is present, status is also recomputed every --refresh seconds, so
    imports from the CLI or a renamed library show up without a replug.
    """
    get_status = get_status or (lambda: status(args.dest, args.photos, args.proxies))
    out = out or sys.stdout
    last_sig = None
    last_out = None
    last_scan = 0.0
    has_card = False
    while loops is None or loops > 0:
        if loops is not None:
            loops -= 1
        sig = signature()
        due = has_card and clock() - last_scan >= args.refresh
        if sig != last_sig or due:
            if sig != last_sig and last_out is not None:
                sleep(0.5)  # let a fresh mount settle
            last_sig = sig
            last_scan = clock()
            try:
                state = get_status()
            except OSError:  # device vanished mid-scan; try again next tick
                last_sig = None
                sleep(args.interval)
                continue
            has_card = bool(state["cards"])
            line = json.dumps(state, separators=(",", ":"))
            if line != last_out:
                last_out = line
                out.write(line + "\n")
                out.flush()
        sleep(args.interval)


def confirm_clean(args, ask=input):
    state = status(args.dest, args.photos, args.proxies)
    cards = [c for c in state["cards"]
             if not args.card or os.path.realpath(c["mount"]) == os.path.realpath(args.card)]
    if len(cards) != 1:
        return True  # run_import reports the problem
    c = cards[0]
    n = c.get("importedFiles", 0)
    if not n:
        return True  # nothing to delete; run_import says so
    print(f"Delete {n} imported file(s) ({human_bytes(c.get('importedBytes', 0))}) from the card at {c['mount']}?")
    print(f"Each one is hashed and compared with its copy in {c['dest']} first; "
          f"{c.get('newFiles', 0)} file(s) not yet imported stay on the card.")
    try:
        return ask("Type 'yes' to continue: ").strip().lower() == "yes"
    except EOFError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dji.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dest", default=DEFAULT_DEST, help="import path template (default: %(default)s)")
    common.add_argument("--no-photos", dest="photos", action="store_false", help="skip photos")
    common.add_argument("--proxies", action="store_true", help="also import .LRF low-res proxies")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("status", parents=[common], help="show cameras, cards, and what is new")
    imp = sub.add_parser("import", parents=[common], help="import new media from the card")
    imp.add_argument("--card", help="card mount point (default: the one mounted DJI card)")
    imp.add_argument("--move", action="store_true", help="delete card files after a verified copy")
    imp.add_argument("-n", "--dry-run", action="store_true", help="show what would happen")
    cln = sub.add_parser("clean", parents=[common],
                         help="delete card files that are already imported (each verified by hash first)")
    cln.add_argument("--card", help="card mount point (default: the one mounted DJI card)")
    cln.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    cln.add_argument("-n", "--dry-run", action="store_true", help="show what would be deleted")
    w = sub.add_parser("watch", parents=[common], help="stream status as NDJSON when devices change")
    w.add_argument("--interval", type=float, default=2.0, help="device check period (seconds)")
    w.add_argument("--refresh", type=float, default=5.0, help="rescan period while a card is present")
    args = parser.parse_args(argv)

    if args.command == "status":
        state = status(args.dest, args.photos, args.proxies)
        if args.json:
            json_emit(state)
        else:
            print_status(state)
        return 0
    if args.command == "watch":
        try:
            watch(args)
        except (KeyboardInterrupt, BrokenPipeError):
            return 0
    emit = json_emit if args.json else HumanEmitter()

    def cancel(signum, frame):
        raise Cancelled()

    signal.signal(signal.SIGTERM, cancel)
    if args.command == "clean" and not args.dry_run and not args.yes:
        if args.json or not sys.stdin.isatty():
            emit({"event": "error", "message": "clean deletes files from the card; pass --yes to confirm"})
            return 2
        if not confirm_clean(args):
            print("Nothing deleted.")
            return 1
    try:
        return run_import(args.card, args.dest, args.photos, args.proxies, getattr(args, "move", False),
                          args.dry_run, emit, clean=args.command == "clean")
    except (Cancelled, KeyboardInterrupt):
        emit({"event": "cancelled"})
        return 130


if __name__ == "__main__":
    sys.exit(main())
