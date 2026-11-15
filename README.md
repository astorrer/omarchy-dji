# omarchy-dji

Import footage from DJI Osmo cameras (Action 4/5, Pocket 3, Osmo 360, …) on
[Omarchy](https://omarchy.org). A bar icon appears when a camera or its card
is connected. The panel shows what's new on the card and copies it into
dated folders:

```
~/Videos/Action4/
├── 2026-11-14/
│   ├── DJI_20261114093012_0001_D.MP4
│   └── DJI_20261114093544_0002_D.JPG
└── 2026-11-15/
    └── DJI_20261115115741_0001_D.MP4
```

It isn't affiliated with DJI. Plugins run unsandboxed inside `omarchy-shell`.

## Install

```sh
omarchy plugin add https://github.com/astorrer/omarchy-dji.git --enable
```

You need `python3`, which comes with Omarchy (it uses only the standard
library). Nothing runs as root, and the plugin makes no network calls.

## Usage

1. Plug in the camera and pick **Transfer** mode on its screen, or put the
   card in a card reader. Omarchy mounts it, the camera icon shows up on the
   bar with the number of new files, and you get a notification.
2. Click the icon, check the destination, and press **Import**. You can
   cancel at any point. A cancelled copy never leaves a half-written file.

| Setting | Default | |
|---|---|---|
| Import path | `~/Videos/{model}` | `{model}` is the camera's short name, e.g. `Action4`, `Pocket3`. Pick a folder with the 󰉋 button, or type a template in the widget settings. |
| Include photos | on | JPG/DNG stills go next to the clips. |
| Include LRF proxies | off | The camera's low-res proxy copies. |
| Remove from card after import | off | Deletes each card file only after its copy has been fsynced, re-read from disk, and its hash compared with the original. |
| Notify on connect | on | |
| Always show icon | off | By default the icon only shows while a DJI camera or card is present. |

On the bar icon, a right-click rescans and a middle-click opens the import
folder. With the panel open, <kbd>Enter</kbd> imports, <kbd>o</kbd> opens the
folder, and <kbd>r</kbd> rescans.

## How it works

`dji.py` does all the work, and you can also run it as a CLI on its own. In a
terminal, `import` shows a live progress bar with speed and time left:

```sh
./dji.py status                 # camera, card, and what's new
./dji.py import --dry-run       # what would be copied where
./dji.py import                 # copy new media
./dji.py import --move          # copy, verify, then remove from the card
./dji.py import --dest '/mnt/nas/{model}' --no-photos --proxies
```

Detection:

- **Card:** any mounted FAT/exFAT/NTFS volume that has a `DCIM/DJI_*` folder.
- **Camera model:** read from the USB device (vendor `2ca3`) when the camera
  itself is connected. With a card reader, it's taken from the clip's MP4
  metadata or the photo's EXIF.
- **Folder name:** `OsmoAction4` → `Action4`.
- **Already imported:** a file counts as imported when one with the same name
  and size is in the destination. Later imports pick up only new files.
- **Wrong USB mode:** if the camera is plugged in but no card is mounted
  (webcam mode), the panel tells you to switch it to Transfer mode.

Each date folder comes from the timestamp in the DJI filename, which is the
camera's local time. Copies keep the original modification time.

## Development

```sh
ln -s "$PWD" ~/.config/omarchy/plugins/io.github.astorrer.omarchy-dji
omarchy-shell shell rescanPlugins
omarchy plugin enable io.github.astorrer.omarchy-dji
./tests/run     # ruff, unit tests, manifest checks, plugin validate, qmllint
```

## License

MIT
