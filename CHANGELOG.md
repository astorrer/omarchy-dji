# Changelog

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
