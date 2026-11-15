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
