#!/usr/bin/env bash
# Build the Planet .deb package.
# Usage: ./build_deb.sh
set -euo pipefail

cd "$(dirname "$0")"

VER="0.1.0"
REL="1"
PKG="planet_${VER}-${REL}_all"
STAGE="debian/${PKG}"
OUT="planet_${VER}-${REL}.deb"

rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN"
mkdir -p "$STAGE/usr/bin"
mkdir -p "$STAGE/usr/lib/python3/dist-packages"
mkdir -p "$STAGE/usr/share/applications"
mkdir -p "$STAGE/usr/share/doc/planet"
mkdir -p "$STAGE/usr/share/man/man1"
mkdir -p "$STAGE/usr/share/icons/hicolor/48x48/apps"
mkdir -p "$STAGE/usr/share/icons/hicolor/128x128/apps"
mkdir -p "$STAGE/usr/share/pixmaps"

# --- Python package ------------------------------------------------------
cp -r planet "$STAGE/usr/lib/python3/dist-packages/planet"
find "$STAGE/usr/lib/python3/dist-packages/planet" -name '__pycache__' -type d \
    -exec rm -rf {} + 2>/dev/null || true

# --- launcher ------------------------------------------------------------
cat > "$STAGE/usr/bin/planet" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 -m planet "$@"
EOF
chmod 755 "$STAGE/usr/bin/planet"

# --- desktop entry -------------------------------------------------------
cat > "$STAGE/usr/share/applications/planet.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Version=1.0
Name=Planet
GenericName=Application Manager
Comment=Browse and manage software packages on X11
Exec=planet
Icon=planet
Terminal=false
Categories=System;PackageManager;
Keywords=packages;apt;install;remove;manager;
StartupNotify=false
EOF

# --- icons ---------------------------------------------------------------
STAGE="$STAGE" python3 - <<'PYEOF'
import os
from PIL import Image, ImageDraw

STAGE = os.environ["STAGE"]

def make_icon(size):
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = max(6, int(s * 0.12))
    pad = int(s * 0.08)
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=r,
                        fill=(44, 49, 58, 255))
    def fx(v):
        return int(v * s)
    # planet ring
    cx, cy, rad = s // 2, s // 2, int(s * 0.32)
    d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad],
              outline=(94, 129, 172, 255), width=max(2, int(s * 0.05)))
    # orbits
    d.arc([pad*2, pad*2, s-pad*2, s-pad*2], start=20, end=160,
          fill=(129, 161, 193, 255), width=max(1, int(s*0.025)))
    d.arc([pad*2, pad*2, s-pad*2, s-pad*2], start=200, end=340,
          fill=(129, 161, 193, 255), width=max(1, int(s*0.025)))
    return img

for s in (48, 128):
    p = os.path.join(STAGE, f"usr/share/icons/hicolor/{s}x{s}/apps/planet.png")
    make_icon(s).save(p)
make_icon(48).save(os.path.join(STAGE, "usr/share/pixmaps/planet.png"))
PYEOF

# --- man page ------------------------------------------------------------
cat > "$STAGE/usr/share/man/man1/planet.1" <<'EOF'
.TH PLANET 1 "August 2026" "Planet 0.1.0" "User Commands"
.SH NAME
planet \- application installer/manager for X11
.SH SYNOPSIS
.B planet
.SH DESCRIPTION
.B planet
is a graphical application installer and package manager for X11, written in
pure Python on top of python\-xlib and python\-apt.  It renders its interface
with a software framebuffer and supports browsing, searching, installing,
removing and upgrading packages without any desktop toolkit.
.SH KEYS
.nf
Up/Down/PgUp/PgDn/Home/End    move selection
Enter                         open package details
Tab                           switch list/grid view
Ctrl+F                        focus search
Ctrl+I                        install selected
Ctrl+U                        upgrade all
Delete                        remove selected
F5                            refresh package list
Ctrl+H                        toggle hidden packages
Ctrl+1/2/3/4                  filter all/installed/available/upgrades
F1                            show key help
Escape                        back / clear search
.fi
.SH FILES
.TP
.I /usr/lib/python3/dist-packages/planet/
The application sources.
.SH AUTHOR
Planet Developers.
EOF
gzip -9 -n -f "$STAGE/usr/share/man/man1/planet.1"

# --- docs ----------------------------------------------------------------
cp README.md "$STAGE/usr/share/doc/planet/README" 2>/dev/null || true
cp debian/copyright "$STAGE/usr/share/doc/planet/copyright"
cp debian/changelog "$STAGE/usr/share/doc/planet/changelog.Debian"
gzip -9 -n -f "$STAGE/usr/share/doc/planet/changelog.Debian"

# --- control -------------------------------------------------------------
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/planet"

INSTALLED=$(du -sk "$STAGE" | awk '{print $1}')
cat > "$STAGE/DEBIAN/control" <<EOF
Package: planet
Version: ${VER}-${REL}
Section: utils
Priority: optional
Architecture: all
Maintainer: Planet Developers <planet@users.noreply.github.com>
Installed-Size: ${INSTALLED}
Depends: python3 (>= 3.9), python3-pil, python3-xlib, python3-apt
Recommends: python3-numpy, xdg-utils, xclip
Description: application installer/manager for X11 in pure Python
 Planet is an application installer and manager for X11, written from scratch
 in Python using only python-xlib and python-apt. It renders its entire UI with
 a software framebuffer (Pillow + XPutImage), so it works without any desktop
 toolkit (no GTK, no Qt, no Tk).
 .
 Features:
  * browse, search, install, remove and purge software packages
  * list and grid views with scrollable, sortable columns
  * background-thread APT operations -- the UI never blocks
  * keyboard-first navigation (typeahead search, Ctrl+I install, etc.)
  * context menu and toolbar for mouse users
  * dark theme with package status colour coding
EOF

# --- build ---------------------------------------------------------------
dpkg-deb --build --root-owner-group "$STAGE" "$OUT"
rm -rf "$STAGE"
echo "Built: $OUT"
