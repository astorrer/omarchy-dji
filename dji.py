#!/usr/bin/env python3
"""Detect DJI Osmo cameras and cards, and import their media.

  dji.py status [--dest TEMPLATE] [--no-photos] [--proxies] [--json]
  dji.py import [--card MOUNT] [--dest TEMPLATE] [--no-photos] [--proxies]
                [--move] [--dry-run] [--json]
  dji.py watch  [--interval SEC] [--dest TEMPLATE] [--no-photos] [--proxies]

Media lands in TEMPLATE/YYYY-MM-DD/, where TEMPLATE defaults to
~/Videos/{model} and {model} is the camera's short name (OsmoAction4 ->
Action4). Copies are written to a hidden .part file, fsynced, and renamed into
place; --move deletes a card file only after its copy is re-read from disk and
its hash matches. Files already imported (same name and size) are skipped.
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


def short_model(model):
    """Folder-friendly camera name: OsmoAction4 -> Action4, Osmo360 -> Osmo360."""
    model = re.sub(r"^DJI\s*", "", model or "", flags=re.I)
    model = re.sub(r"[^A-Za-z0-9._-]", "", model).lstrip(".")
    if re.match(r"^Osmo[A-Za-z]", model):
        model = model[4:]
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


def scan(card_folders, dest, photos=True, proxies=False):
    """Every importable file on the card with its target and status."""
    want = {"video"} | ({"photo"} if photos else set()) | ({"proxy"} if proxies else set())
    items = []
    for folder in card_folders:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for src in entries:
            kind = KINDS.get(src.suffix.lower())
            if kind not in want or not src.is_file():
                continue
            size = src.stat().st_size
            target = dest / day_for(src, src.name) / src.name
            if not target.exists():
                status = "new"
            elif target.stat().st_size == size:
                status = "imported"
            else:
                status = "conflict"
            items.append({"src": src, "name": src.name, "kind": kind, "size": size, "target": target, "status": status})
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
        "conflicts": sum(1 for i in items if i["status"] == "conflict"),
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
                if KINDS.get(f.suffix.lower()) == kind:
                    found = sniff_model(f)
                    if found:
                        return found
    return "DJI"


def status(dest_template=DEFAULT_DEST, photos=True, proxies=False, mounts_text=None, sysroot="/sys", home=None):
    cards = []
    error = None
    mounted_usb = set()
    for card in find_cards(mounts_text, sysroot):
        model = card_model(card)
        folder = short_model(model)
        entry = {"mount": card["mount"], "label": os.path.basename(card["mount"]), "device": card["device"],
                 "model": model, "folder": folder, "connection": "usb" if card["usbProduct"] else "card"}
        try:
            dest = expand_dest(dest_template, folder, home)
            entry["dest"] = str(dest)
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


def hash_file(path, drop_cache=False):
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        if drop_cache and hasattr(os, "posix_fadvise"):
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        while chunk := f.read(CHUNK):
            h.update(chunk)
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
        os.replace(part, target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return h.digest()


def free_space(path):
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def run_import(card_mount=None, dest_template=DEFAULT_DEST, photos=True, proxies=False, move=False,
               dry_run=False, emit=print, mounts_text=None, sysroot="/sys", home=None):
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
        dest = expand_dest(dest_template, short_model(model), home)
    except ValueError as exc:
        emit({"event": "error", "message": str(exc)})
        return 1

    items = scan(card["folders"], dest, photos, proxies)
    todo = [i for i in items if i["status"] == "new" or (move and i["status"] == "imported")]
    copy_bytes = sum(i["size"] for i in todo if i["status"] == "new")
    emit({"event": "start", "model": model, "mount": card["mount"], "dest": str(dest), "files": len(todo),
          "bytes": copy_bytes, "move": move, "dryRun": dry_run})

    if not dry_run and copy_bytes and free_space(dest) < copy_bytes:
        emit({"event": "error", "message": f"Not enough free space in {dest}", "needed": copy_bytes,
              "free": free_space(dest)})
        return 1

    copied = skipped = deleted = 0
    errors = [f"{i['name']}: a different file with this name already exists" for i in items
              if i["status"] == "conflict"]
    done_bytes = 0
    last = 0.0

    def on_bytes(n):
        nonlocal done_bytes, last
        done_bytes += n
        now = time.monotonic()
        if now - last >= PROGRESS_EVERY:
            last = now
            emit({"event": "progress", "bytes": done_bytes, "total": copy_bytes})

    for index, item in enumerate(todo, 1):
        emit({"event": "file", "index": index, "files": len(todo), "name": item["name"], "size": item["size"],
              "day": item["target"].parent.name, "action": "copy" if item["status"] == "new" else "verify"})
        if dry_run:
            continue
        try:
            if item["status"] == "new":
                digest = copy_file(item["src"], item["target"], on_bytes, verify=move)
                copied += 1
            else:
                digest = hash_file(item["target"], drop_cache=True)
                skipped += 1
            if move:
                if hash_file(item["src"]) != digest:
                    raise OSError(f"{item['name']} changed on the card during import; left in place")
                item["src"].unlink()
                deleted += 1
        except OSError as exc:
            errors.append(f"{item['name']}: {exc.strerror or exc}")
    skipped += sum(1 for i in items if i["status"] == "imported") if not move else 0
    emit({"event": "progress", "bytes": done_bytes, "total": copy_bytes})
    emit({"event": "done", "dest": str(dest), "copied": copied, "skipped": skipped, "deleted": deleted,
          "errors": errors, "dryRun": dry_run})
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
        print(f"  import to: {c['dest']}")
        for kind in ("video", "photo", "proxy"):
            k = counts.get(kind)
            if k and k["total"]:
                print(f"  {kind}s: {k['total']} on card, {k['new']} new")
        print(f"  to import: {c.get('newFiles', 0)} files, {human_bytes(c.get('newBytes', 0))}")
    for product in state["unmounted"]:
        print(f"{product} connected over USB but no card is mounted. Choose \"Transfer\" mode on the camera.")
    if state["error"]:
        print(f"error: {state['error']}", file=sys.stderr)


class HumanEmitter:
    def __init__(self):
        self.tty = sys.stdout.isatty()

    def __call__(self, ev):
        kind = ev["event"]
        if kind == "start":
            mode = " (dry run)" if ev["dryRun"] else (" (move)" if ev["move"] else "")
            print(f"{ev['model']}: {ev['files']} file(s), {human_bytes(ev['bytes'])} -> {ev['dest']}{mode}")
        elif kind == "file":
            verb = "verify" if ev["action"] == "verify" else "copy"
            print(f"[{ev['index']}/{ev['files']}] {verb} {ev['day']}/{ev['name']} ({human_bytes(ev['size'])})")
        elif kind == "progress" and self.tty and ev["total"]:
            pct = 100 * ev["bytes"] / ev["total"]
            print(f"\r  {pct:5.1f}%  {human_bytes(ev['bytes'])} / {human_bytes(ev['total'])}", end="", flush=True)
            if ev["bytes"] >= ev["total"]:
                print()
        elif kind == "done" and ev["dryRun"]:
            print("Dry run: nothing was changed.")
        elif kind == "done":
            print(f"Done: {ev['copied']} copied, {ev['skipped']} already there, {ev['deleted']} removed from card")
            for err in ev["errors"]:
                print(f"  error: {err}", file=sys.stderr)
        elif kind == "error":
            print(f"error: {ev['message']}", file=sys.stderr)
        elif kind == "cancelled":
            print("\nCancelled; partial file removed.", file=sys.stderr)


def json_emit(ev):
    sys.stdout.write(json.dumps(ev, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def watch(args):
    last_sig = None
    last_out = None
    while True:
        sig = (read_text("/proc/self/mounts"), tuple(u["product"] for u in usb_cameras()))
        if sig != last_sig:
            last_sig = sig
            time.sleep(0.5 if last_out is not None else 0)  # let a fresh mount settle
            out = json.dumps(status(args.dest, args.photos, args.proxies), separators=(",", ":"))
            if out != last_out:
                last_out = out
                sys.stdout.write(out + "\n")
                sys.stdout.flush()
        time.sleep(args.interval)


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
    w = sub.add_parser("watch", parents=[common], help="stream status as NDJSON when devices change")
    w.add_argument("--interval", type=float, default=2.0)
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
    try:
        return run_import(args.card, args.dest, args.photos, args.proxies, args.move, args.dry_run, emit)
    except (Cancelled, KeyboardInterrupt):
        emit({"event": "cancelled"})
        return 130


if __name__ == "__main__":
    sys.exit(main())
