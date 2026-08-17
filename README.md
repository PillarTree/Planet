# Planet

An application installer/manager for X11, written in pure Python using only
[`python-xlib`](https://github.com/python-xlib/python-xlib), `python-apt`, and
Pillow. No GTK, no Qt, no Tk.

Like Cadet (the file manager that shares the same rendering architecture),
Planet draws its entire interface into a software framebuffer with Pillow.
The finished frame is pushed to the X window in a single `XPutImage`, with
text cached as raster tiles so even large package listings scroll smoothly.
APT operations run on a background thread, so the UI never freezes while
installing, removing, or upgrading packages.

## Features

* **Browse** all packages from your APT repositories
* **Search** by name or description (Ctrl+F, live as you type)
* **Install / Remove / Purge** individual or multiple packages
* **Upgrade all** packages at once
* **List and grid views** — switch with `Tab`
* **Sortable columns** — click a header to sort (name, size, version)
* **Filters** — show all, installed, available, or upgradable only (Ctrl+1/2/3/4)
* **Background operations** — the UI stays responsive during installs
* **Keyboard-first** navigation with typeahead search, context menu, and
  toolbar for mouse users
* **Dark theme** with status colour coding:
  green = installed, amber = upgrade available, dim = not installed

## Running

```sh
python3 -m planet                  # open the package manager
```

Or build/install the `.deb`:

```sh
./build_deb.sh
sudo apt install ./planet_0.1.0-1.deb
```

## Controls

| Key(s) | Action |
| --- | --- |
| `↑` `↓` `PgUp` `PgDn` `Home` `End` | move selection |
| `Enter` | open package details |
| `Backspace` / `Escape` | go back / clear search |
| `Tab` | switch list / grid view |
| `Ctrl+F` | focus the search bar |
| `Ctrl+I` | install selected packages |
| `Delete` | remove selected packages |
| `Ctrl+U` | upgrade all packages |
| `F5` / `Ctrl+R` | refresh package list |
| `Ctrl+H` | toggle hidden packages |
| `Ctrl+1` `Ctrl+2` `Ctrl+3` `Ctrl+4` | filter all / installed / available / upgrades |
| `F1` | key help |
| `Alt+←` | go back |

Mouse: single-click selects, double-click opens details, wheel scrolls,
right-click opens the context menu. Click a column header to sort.

## Layout

```
planet/__init__.py     entry points
planet/app.py          X11 window, event loop, navigation, search, dialogs
planet/widgets.py      buttons, text box, list view, grid view, menus, prompts
planet/apt.py          package model + background APT operations
planet/render.py       framebuffer + raster text cache + XPutImage pump
planet/theme.py        colours, fonts, layout constants
```

## Dependencies

* `python3-xlib` (required)
* `python3-pil` (required)
* `python3-apt` (required — reading the APT cache)
* `python3-numpy` (recommended — fast pixel packing; falls back to pure Python)
* `xdg-utils` (recommended — opening URLs / package homepages)
* `xclip` (optional — clipboard for the search field)
* `apt` (required at runtime — for install / remove / upgrade)

## Notes

* Installing, removing, and upgrading packages requires root. Run `planet`
  with `sudo` if your user is not configured with polkit/passwordless apt.
* The `apt` cache is kept open for fast browsing. Press `F5` to refresh it
  after running `apt update`.
* Hidden packages (those whose names start with a dot) are filtered out by
  default; toggle with `Ctrl+H`.
* Package icons are loaded from the freedesktop icon theme; if no icon is
  found, a coloured placeholder card is drawn instead.

## License

MIT
