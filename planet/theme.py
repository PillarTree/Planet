"""Visual theme, fonts and layout constants for Planet.

The palette is the same family as Cadet/Pilot but tuned with a warmer accent
for the "app center" feel -- green for installed, accent blue for actions.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Palette (dark, low-glare)
# ---------------------------------------------------------------------------
BG          = (28, 32, 38)        # main background
PANEL       = (40, 45, 52)        # toolbar / status bar / cards
PANEL_LINE  = (18, 20, 24)        # separator lines
CARD_BG     = (44, 49, 58)        # app card backgrounds

TEXT        = (216, 222, 233)     # primary text
TEXT_DIM    = (139, 147, 163)     # secondary text
TEXT_FAINT  = (100, 108, 122)     # hints / placeholders
ACCENT      = (94, 129, 172)      # accent blue
ACCENT_HOT  = (129, 161, 193)     # accent, hover
SELECT      = (76, 86, 106)       # selection row
SELECT_FOC  = (59, 70, 89)       # selection row (focused) - slightly deeper
ROW_HOVER   = (52, 59, 71)       # mouse hover row
ROW_ALT     = (33, 37, 43)       # zebra striping

INSTALLED   = (163, 190, 140)     # installed indicator (green)
UPGRADE     = (235, 203, 139)     # upgrade available (amber)
NOT_INST    = (139, 147, 163)     # not installed (dim)
ERR_RED     = (191, 97, 106)     # errors
OK_GREEN    = (163, 190, 140)    # success
WARN_YELLOW = (235, 203, 139)

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
FONT_DIRS = [
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/liberation"),
    Path("/usr/share/fonts/truetype/noto"),
    Path("/usr/share/fonts/truetype/freefont"),
    Path("/usr/share/fonts/truetype/ubuntu"),
]

_SANS = ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "NotoSans-Regular.ttf",
         "FreeSans.ttf", "Ubuntu-Regular.ttf"]
_MONO = ["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "FreeMono.ttf"]
_BOLD = ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"]


def _find(names):
    for fd in FONT_DIRS:
        if fd.is_dir():
            for name in names:
                p = fd / name
                if p.is_file():
                    return str(p)
    return None


FONT_UI     = _find(_SANS)
FONT_UI_B   = _find(_BOLD) or FONT_UI
FONT_MONO   = _find(_MONO)

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
SIZE_W       = 1000
SIZE_H       = 640
TOOLBAR_H    = 46
STATUSBAR_H  = 26
ROW_H        = 24
CARD_H       = 110            # height of app card in grid view
PAD          = 6
BTN_H        = 28
BTN_W_MIN    = 80
ICON_PAD_X   = 10
SCROLL_W     = 12
MENU_W       = 240
MENU_ITEM_H  = 26
SEARCH_H     = 30
TITLEBAR_H   = 0              # no titlebar -- we draw our own chrome
GRID_GAP     = 8
MAX_DIALOG_W = 460

# Key repeat throttle (seconds)
KEY_REPEAT_DELAY = 0.45
KEY_REPEAT_RATE  = 0.06

# Background operation poll interval
POLL_INTERVAL = 0.05

# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
VIEW_LIST   = "list"
VIEW_GRID   = "grid"
