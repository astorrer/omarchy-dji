#!/usr/bin/env python3
"""Detect DJI Osmo cameras and cards, and import their media.

  dji.py status [--dest TEMPLATE] [--no-photos] [--proxies] [--json]
  dji.py import [--card MOUNT] [--dest TEMPLATE] [--no-photos] [--proxies]
                [--move] [--dry-run] [--json]
  dji.py clean  [--card MOUNT] [--yes] [--dry-run] [--json]
  dji.py eject  [--card MOUNT] [--json]
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
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
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
MAX_CARD_FILES = 20_000   # per scan; a real card holds a few thousand at most
MAX_MP4_BOXES = 10_000    # top-level boxes walked looking for moov
UDISKSCTL = "/usr/bin/udisksctl"
SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


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
        if dcim.is_symlink():
            return []
        return sorted(p for p in dcim.iterdir()
                      if p.name.upper().startswith("DJI_") and not p.is_symlink() and p.is_dir())[:999]
    except OSError:
        return []


def clean_label(text, limit=48):
    """Text a card or USB device supplied, reduced to something safe to display.

    Drops control characters and the markup characters <, >, &, quotes, so a
    crafted volume label or USB product string can't become rich text anywhere.
    """
    text = "".join(ch for ch in str(text or "") if ch.isprintable() and ch not in "<>&\"'`")
    return text.strip()[:limit]


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
            out.append({"product": clean_label(read_text(dev / "product")) or "DJI", "path": str(dev.resolve())})
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
            return clean_label(read_text(parent / "product")) if read_text(parent / "idVendor") == DJI_VENDOR else ""
    return ""


def mp4_moov(path, limit=32 * 1024 * 1024):
    """Bytes of the top-level moov box of an MP4/MOV, or b""."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            pos = 0
            for _ in range(MAX_MP4_BOXES):
                if pos + 8 > size:
                    break
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


def scan(card_folders, dest, photos=True, proxies=False, index=None):
    """Every importable file on the card with its target and status.

    status is "imported" when the same name and size already exist anywhere
    under dest, else "new". A new file whose name is taken in its date folder
    (another camera of the same model, say) gets a -2, -3, ... suffix instead
    of being skipped, so nothing is ever left behind on the card.
    """
    index = library_index(dest) if index is None else index
    claimed = set()
    seen = 0
    want = {"video"} | ({"photo"} if photos else set()) | ({"proxy"} if proxies else set())
    items = []
    for folder in card_folders:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for src in entries:
            seen += 1
            if seen > MAX_CARD_FILES:
                return items
            kind = KINDS.get(src.suffix.lower())
            try:
                if kind not in want or src.is_symlink() or not src.is_file():
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


def proxy_leftovers(card_folders, items, index, removing=()):
    """Card .LRF proxies that are safe to delete alongside their clips.

    A proxy qualifies when it is not itself being imported as media, a clip with
    its name is in the library, and that clip is no longer on the card (or is in
    `removing`, the clips being deleted now). Proxies are low-res copies the
    camera derives from each clip, so nothing unique is lost.
    """
    media = {i["src"] for i in items}
    removing = set(removing)
    library_clips = {Path(name).stem.lower() for name in index if KINDS.get(Path(name).suffix.lower()) == "video"}
    clips_on_card = set()
    proxies = []
    for folder in card_folders:
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for f in entries[:MAX_CARD_FILES]:
            if f.is_symlink():
                continue
            kind = KINDS.get(f.suffix.lower())
            if kind == "video" and f not in removing:
                clips_on_card.add((f.parent, f.stem.lower()))
            elif kind == "proxy" and f not in media:
                proxies.append(f)
    return sorted(f for f in proxies
                  if (f.parent, f.stem.lower()) not in clips_on_card and f.stem.lower() in library_clips)


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


def is_removable(device, sysroot="/sys"):
    """True for SD cards, USB card readers, and cameras; False for fixed disks."""
    disk = os.path.basename(parent_disk(device, sysroot))
    node = Path(sysroot) / "class/block" / disk
    if read_text(node / "removable") == "1":
        return True
    try:
        path = str(node.resolve())
    except OSError:
        return False
    return "/usb" in path or "/mmc" in path or disk.startswith("mmcblk")


def find_cards(mounts_text=None, sysroot="/sys"):
    if mounts_text is None:
        mounts_text = read_text("/proc/self/mounts")
    cards = []
    for device, mount, fstype in parse_mounts(mounts_text):
        if fstype not in CARD_FSTYPES or not device.startswith("/dev/") or not is_removable(device, sysroot):
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
        entry = {"mount": card["mount"], "label": clean_label(os.path.basename(card["mount"])), "device": card["device"],
                 "model": model, "folder": folder, "connection": "usb" if card["usbProduct"] else "card"}
        try:
            dest = expand_dest(dest_template, folder, home)
            entry["dest"] = str(dest)
            entry["destExists"] = dest.is_dir()
            entry["destError"] = check_dest(dest, card["mount"])
            index = library_index(dest)
            items = scan(card["folders"], dest, photos, proxies, index)
            entry.update(summarize(items))
            imported = {i["src"] for i in items if i["status"] == "imported"}
            entry["cleanProxies"] = len(proxy_leftovers(card["folders"], items, index, imported))
        except ValueError as exc:
            error = str(exc)
            entry["dest"] = ""
        if card["usbProduct"]:
            mounted_usb.add(card["usbProduct"])
        cards.append(entry)
    unmounted = [u["product"] for u in usb_cameras(sysroot) if u["product"] not in mounted_usb]
    return {"cards": cards, "unmounted": unmounted, "error": error}


# ---------------------------------------------------------------- import


def open_nofollow(path, dir_fd=None):
    """Open a file for reading, refusing a symlink."""
    return os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd), "rb")


def hash_file(path, drop_cache=False, on_bytes=None, dir_fd=None):
    h = hashlib.blake2b(digest_size=32)
    with open_nofollow(path, dir_fd) as f:
        if drop_cache and hasattr(os, "posix_fadvise"):
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        while chunk := f.read(CHUNK):
            h.update(chunk)
            if on_bytes:
                on_bytes(len(chunk))
    return h.digest()


def open_day_folder(day):
    """Create the date folder if needed and return an fd for it.

    Every later step works relative to this fd, never by pathname, so swapping
    the folder for a symlink after this point (possible in a library other
    people can write to) can't redirect a write. A date folder that's already
    a symlink is refused. The library root above it is the user's choice and
    may itself be a symlink, e.g. to a NAS mount.
    """
    day.parent.mkdir(parents=True, exist_ok=True)
    parent = os.open(day.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            os.mkdir(day.name, 0o755, dir_fd=parent)
        except FileExistsError:
            pass
        try:
            return os.open(day.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise OSError(f"{day.name} is a symlink or not a folder in the library; skipped") from None
            raise
    finally:
        os.close(parent)


def copy_file(src, target, on_bytes, verify):
    """Copy src to target via a hidden .part file; return the source hash.

    The date folder is opened once (open_day_folder), and the .part file is
    created in it with O_EXCL | O_NOFOLLOW and kept open. Verification reads
    the copy back through that same open file, never by name. After placing
    it, the final name must point at that exact file (same device and inode),
    otherwise the import fails and the card original is kept. So neither a
    planted symlink, a folder swapped mid-copy, nor a .part file replaced
    after verification can put unverified data in the library or cost the
    card original.
    """
    day = open_day_folder(target.parent)
    part = f".{target.name}.part"
    try:
        try:
            os.unlink(part, dir_fd=day)  # a leftover from an interrupted run (unlink never follows a symlink)
        except FileNotFoundError:
            pass
        h = hashlib.blake2b(digest_size=32)
        fd = os.open(part, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=day)
        mine = os.fstat(fd)
        try:
            with os.fdopen(fd, "r+b") as fo:
                with open_nofollow(src) as fi:
                    while chunk := fi.read(CHUNK):
                        h.update(chunk)
                        fo.write(chunk)
                        on_bytes(len(chunk))
                    fo.flush()
                    os.fsync(fo.fileno())
                    st = os.fstat(fi.fileno())
                os.utime(fo.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
                if verify and hash_open_file(fo, drop_cache=True) != h.digest():
                    raise OSError(f"verification failed for {target.name}")
                place_without_clobber(part, target.name, dir_fd=day, expect=mine)
        except BaseException:
            unlink_if_same(part, day, mine)
            raise
    finally:
        os.close(day)
    return h.digest()


def hash_open_file(f, drop_cache=False):
    """Hash an already-open file from the start, without reopening it by name."""
    f.seek(0)
    if drop_cache and hasattr(os, "posix_fadvise"):
        os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    h = hashlib.blake2b(digest_size=32)
    while chunk := f.read(CHUNK):
        h.update(chunk)
    return h.digest()


def same_file(a, b):
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def unlink_if_same(name, dir_fd, expect):
    """Remove name only if it's still the file we created."""
    try:
        if expect is None or same_file(os.stat(name, dir_fd=dir_fd, follow_symlinks=False), expect):
            os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


RENAME_NOREPLACE = 1
AT_FDCWD = -100
_renameat2 = None


def rename_noreplace(src, dst, dir_fd=None):
    """renameat2(RENAME_NOREPLACE): an atomic rename that never replaces dst."""
    global _renameat2
    if _renameat2 is None:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        _renameat2 = getattr(libc, "renameat2", False)
        if _renameat2:
            _renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if not _renameat2:
        raise OSError(errno.ENOSYS, "renameat2 is not available")
    fd = AT_FDCWD if dir_fd is None else dir_fd
    if _renameat2(fd, os.fsencode(src), fd, os.fsencode(dst), RENAME_NOREPLACE) != 0:
        import ctypes

        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def place_without_clobber(part, target, dir_fd=None, expect=None):
    """Move part to target, failing instead of replacing anything there.

    With dir_fd, both names are relative to that folder fd. With expect (the
    fstat of the file we wrote), the placed name must be that exact file.
    """
    name = Path(target).name
    clash = f"{name} appeared in the library during import; left on the card"
    try:
        os.link(part, target, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)  # atomic, no clobber
        linked = True
    except FileExistsError:
        raise OSError(clash) from None
    except OSError:  # filesystem without hard links (exFAT, FAT): atomic no-replace rename instead
        try:
            rename_noreplace(part, target, dir_fd)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise OSError(clash) from None
            raise OSError(f"this filesystem can't place {name} without risking an overwrite; "
                          "left on the card") from None
        linked = False
    if expect is not None and not same_file(os.stat(target, dir_fd=dir_fd, follow_symlinks=False), expect):
        raise OSError(f"{name} was replaced in the library during import; left on the card")
    if linked:
        unlink_if_same(part, dir_fd, expect)


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
    removed = set()
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
                    raise OSError("doesn't match its copy in the library; left on the card")
                item["src"].unlink()
                deleted += 1
                removed.add(item["src"])
        except OSError as exc:
            errors.append(f"{item['name']}: {exc.strerror or exc}")

    proxies_deleted = 0
    if move:
        gone = {i["src"] for i in todo} if dry_run else removed
        for proxy in proxy_leftovers(card["folders"], items, library_index(dest), gone):
            if not dry_run:
                try:
                    proxy.unlink()
                except OSError as exc:
                    errors.append(f"{proxy.name}: {exc.strerror or exc}")
                    continue
            proxies_deleted += 1
    skipped += sum(1 for i in items if i["status"] == "imported") if not move else 0
    kept = sum(1 for i in items if i["status"] == "new") if clean else 0
    emit({"event": "progress", "bytes": done_bytes, "total": work_bytes})
    emit({"event": "done", "mode": mode, "dest": str(dest), "copied": copied, "skipped": 0 if clean else skipped,
          "deleted": deleted, "proxiesDeleted": proxies_deleted, "renamed": renamed, "kept": kept,
          "errors": errors, "dryRun": dry_run})
    return 1 if errors else 0


# ---------------------------------------------------------------- eject


def parent_disk(device, sysroot="/sys"):
    """/dev/sda1 -> /dev/sda; a whole disk is returned as is."""
    name = os.path.basename(device)
    node = Path(sysroot) / "class/block" / name
    if (node / "partition").exists():
        try:
            return "/dev/" + node.resolve().parent.name
        except OSError:
            pass
    return device


def udisks(*args, run=subprocess.run):
    """Run udisksctl without prompting; return (ok, message)."""
    try:
        proc = run([UDISKSCTL, *args, "--no-user-interaction"], capture_output=True, text=True, timeout=60,
                   env=SAFE_ENV)
    except FileNotFoundError:
        return False, "udisksctl is not installed"
    except subprocess.TimeoutExpired:
        return False, "udisksctl timed out"
    text = (proc.stderr or proc.stdout or "").strip()
    text = re.sub(r"^Error \w+ [^:]*: ", "", text.splitlines()[-1] if text else "")
    return proc.returncode == 0, clean_label(text, 300)


def eject(card_mount=None, emit=print, mounts_text=None, sysroot="/sys", run=subprocess.run, sync=os.sync):
    """Flush, unmount, and power off the DJI card (or camera) so it is safe to unplug."""
    cards = find_cards(mounts_text, sysroot)
    if card_mount:
        cards = [c for c in cards if os.path.realpath(c["mount"]) == os.path.realpath(card_mount)]
    if len(cards) != 1:
        emit({"event": "error", "message": "No DJI card found" if not cards
              else "Several DJI cards are mounted; pick one with --card"})
        return 1
    card = cards[0]
    sync()
    ok, message = udisks("unmount", "-b", card["device"], run=run)
    if not ok:
        busy = "busy" in message.lower()
        emit({"event": "error", "message": "The card is in use; close any file manager, terminal, or app "
              "using it and try again" if busy else f"Could not unmount: {message or 'unknown error'}"})
        return 1
    powered, why = udisks("power-off", "-b", parent_disk(card["device"], sysroot), run=run)
    emit({"event": "ejected", "mount": card["mount"], "poweredOff": powered, "note": "" if powered else why})
    return 0


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
            if ev.get("proxiesDeleted"):
                self.line(f"  ...and {ev['proxiesDeleted']} LRF proxies of those clips")
            self.line("Dry run: nothing was changed.")
        elif kind == "done" and ev.get("mode") == "clean":
            self.draw()
            if self.bar_shown:
                self.out.write("\n")
                self.bar_shown = False
            proxies = f" (+{ev['proxiesDeleted']} LRF proxies)" if ev.get("proxiesDeleted") else ""
            self.line(f"Done: {ev['deleted']} removed from card{proxies}, {ev['kept']} not yet imported kept")
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
        elif kind == "ejected":
            self.line("Ejected: safe to unplug." if ev["poweredOff"]
                      else f"Unmounted: safe to unplug (could not power off: {ev['note'] or 'unsupported'}).")
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
    if not n and not c.get("cleanProxies"):
        return True  # nothing to delete; run_import says so
    print(f"Delete {n} imported file(s) ({human_bytes(c.get('importedBytes', 0))}) from the card at {c['mount']}?")
    print(f"Each one is hashed and compared with its copy in {c['dest']} first; "
          f"{c.get('newFiles', 0)} file(s) not yet imported stay on the card.")
    if c.get("cleanProxies"):
        print(f"Their {c['cleanProxies']} .LRF proxies (low-res copies the camera makes) are removed too.")
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
    ej = sub.add_parser("eject", parents=[common], help="flush, unmount, and power off the card so it is safe to unplug")
    ej.add_argument("--card", help="card mount point (default: the one mounted DJI card)")
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
    if args.command == "eject":
        return eject(args.card, emit)

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
