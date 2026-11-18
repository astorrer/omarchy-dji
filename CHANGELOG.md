# Changelog

## 0.1.3 — 2026-11-18

Follow-up fixes from HANCORE-linux's marketplace review of 0.1.2:

- The copy is verified by reading it back through the file it was written
  to, never by reopening its name. After it's placed, the final name must
  point at that exact file (same device and inode). Otherwise the import
  fails and the card original is kept, even with "Remove from card" on. A
  `.part` file swapped after verification in a shared library can no longer
  get the card original deleted.
- On filesystems without hard links (exFAT, FAT), the copy is placed with an
  atomic no-replace rename (`renameat2(RENAME_NOREPLACE)`) instead of
  check-then-`os.replace`, so a file that appears at the last moment is never
  overwritten. If the filesystem supports neither, the file is left on the
  card with an error instead of risking an overwrite.

## 0.1.2 — 2026-11-16

- Imports no longer look up the date folder by path after checking it. The
  folder is opened once, and the temp file is created, verified, and moved
  into place relative to that open folder. Someone who can write to a shared
  library can't swap the date folder for a symlink mid-import to redirect
  your files elsewhere. (Reported by HANCORE-linux in marketplace review.)

## 0.1.1 — 2026-11-15

Security hardening from a pre-marketplace audit:

- Helpers run as `/usr/bin/python3 -I` with a cleared environment (`HOME`,
  `LANG`, fixed `PATH`); `udisksctl`, `notify-send`, `xdg-open`, and the folder
  picker are called by absolute path, and opening a folder no longer goes
  through `sh -c`. A planted `python3` earlier on `PATH` can't run.
- Only removable devices (SD, USB card readers, cameras) count as cards, so
  clean-up and eject can never act on a fixed disk.
- Card-supplied text (volume label, USB product name) is stripped of control
  and markup characters and capped; notification text is neutralized too.
- Symlinks on the card are skipped; `.part` files are created with
  `O_EXCL | O_NOFOLLOW`; symlinked date folders are refused.
- MP4 box walking and card scans are bounded.
- The clean-up confirmation names the volume it will remove files from.

## 0.1.0 — 2026-11-15

- First release: bar widget and `dji.py` CLI that detect DJI Osmo cameras
  (USB or card reader), show new videos/photos, and import them into
  `~/Videos/{model}/YYYY-MM-DD/` with atomic, optionally verified copies.
- Options for photos, LRF proxies, removal from the card after a verified
  import, connect notifications, and a custom import path template.
- CLI: live progress bar with speed and time left when run in a terminal.
- `{model}` is the name the camera reports (`OsmoAction4`), consistent for
  every DJI model.
- Safe with existing folders: files already anywhere under the import path
  count as imported; refuses a destination on the card, through a file, or
  not writable; the panel says whether the folder exists or will be created.
- Bar count stays current: rescans every 5s while a card is present and when
  the panel opens, so CLI imports and library changes show up without a
  replug; a card pulled mid-scan no longer stops detection.
- The bar shows only the DJI logo; the new-file count and import progress
  are in its tooltip, and the icon takes the active color while importing.
- A clip whose name is already taken in its date folder (e.g. by a second
  camera of the same model) is imported as NAME-2.EXT instead of being left
  on the card; the final rename never replaces an existing file.
- Picking an import folder in the panel keeps one folder per camera by
  appending `/{model}` (or swapping in `{model}` if the pick already is the
  camera's folder).
- Option toggles dim while an import runs, not just lock.
- Clean up card (panel button with a confirmation, or `dji.py clean`): removes
  only files already imported, each hash-verified against its library copy.
- Rescan button in the panel header.
- Cleaning up (or importing with removal) also deletes each removed clip's
  `.LRF` proxy, plus leftover proxies whose clip is already in the library.
  Proxies that are being imported, or whose clip isn't imported, are kept.
- Safe eject (⏏ in the panel, `e`, or `dji.py eject`): sync, unmount, power
  off via udisksctl; a busy card is reported and left mounted.
