"""Planet -- an application installer/manager for X11 in pure Python.

Rendering: a Pillow framebuffer is pushed to an X pixmap once per frame via
XPutImage (numpy-accelerated pixel packing), then copied to the window.  Text
is cached as raster tiles, so even 100k-package listings scroll smoothly.
APT operations run on a background thread and report through a queue.

The architecture and rendering pipeline mirror Cadet (the file manager by the
same author): the only difference is ``apt`` replaces ``fs`` as the data model.
"""

from __future__ import annotations

import os
import queue
import select
import sys
import threading
import time
import traceback
from typing import Optional

from PIL import Image, ImageFont

from Xlib import X, XK
from Xlib import display as xdisplay
from Xlib.protocol.request import PutImage

from . import apt, theme
from .render import Renderer
from .widgets import (Button, GridView, ListView, Menu, MenuItem, Prompt,
                      TextBox)

NAME = "Planet"
KEYSYM_REV = {v: k[3:] for k, v in vars(XK).items()
              if isinstance(v, int) and k.startswith("XK_")}
WM_DELETE_WINDOW = None

# Package listing filters
FILTER_ALL        = "all"
FILTER_INSTALLED  = "installed"
FILTER_AVAILABLE  = "available"
FILTER_UPGRADABLE = "upgradable"
FILTERS = [
    (FILTER_ALL, "All"),
    (FILTER_INSTALLED, "Installed"),
    (FILTER_AVAILABLE, "Available"),
    (FILTER_UPGRADABLE, "Upgrades"),
]


class ConfirmDialog:
    """Modal yes/no confirmation that names the action and package(s)."""

    def __init__(self, title: str, action: str, packages: list, font=None):
        self.title = title
        self.action = action            # e.g. "install", "remove", "purge"
        self.packages = packages
        self.ok_btn = Button("Confirm", "confirm")
        self.cancel_btn = Button("Cancel", "cancel_prompt")
        self.font = font
        self.box = (0, 0, 0, 0)

    def layout(self, r: Renderer, win_w: int, win_h: int):
        w = min(theme.MAX_DIALOG_W, win_w - 80)
        h = 140
        x = (win_w - w) // 2
        y = (win_h - h) // 2
        self.box = (x, y, w, h)
        self.ok_btn.set_bounds(x + w - 180, y + h - 38, 78, 28)
        self.cancel_btn.set_bounds(x + w - 94, y + h - 38, 78, 28)

    def render(self, r: Renderer, win_w: int, win_h: int):
        self.layout(r, win_w, win_h)
        x, y, w, h = self.box
        dim = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 150))
        r.blit(dim, 0, 0)
        r.rect(x, y, w, h, theme.PANEL)
        r.outline(x, y, w, h, theme.ACCENT_HOT, 1)
        th = r.text_h()
        r.text(x + 16, y + 14, self.title, theme.TEXT, r.font_ui_b)
        names = ", ".join(p[:30] for p in self.packages[:10])
        if len(self.packages) > 10:
            names += f" (...+{len(self.packages) - 10} more)"
        r.text(x + 16, y + 44, names, theme.TEXT_DIM)
        r.text(x + 16, y + 64,
               f"This will {self.action.replace('_', ' ')} "
               f"{len(self.packages)} package(s).", theme.TEXT_FAINT)
        self.ok_btn.render(r)
        self.cancel_btn.render(r)


class Planet:
    def __init__(self):
        self.d = xdisplay.Display()
        self.screen = self.d.screen()
        self.root = self.screen.root
        self.depth = self.screen.root_depth
        self.win_w = theme.SIZE_W
        self.win_h = theme.SIZE_H
        self._init_window()
        self._init_graphics()
        self._init_state()
        self._init_widgets()
        self._load_packages()
        self.running = True

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _init_window(self):
        w = self.root.create_window(
            0, 0, self.win_w, self.win_h, 0,
            self.depth, X.InputOutput, X.CopyFromParent,
            event_mask=(X.ExposureMask | X.KeyPressMask | X.KeyReleaseMask |
                        X.ButtonPressMask | X.ButtonReleaseMask |
                        X.PointerMotionMask | X.StructureNotifyMask))
        self.win = w
        w.set_wm_name(NAME)
        w.set_wm_class(NAME, NAME)
        global WM_DELETE_WINDOW
        WM_DELETE_WINDOW = self.d.intern_atom("WM_DELETE_WINDOW")
        w.set_wm_protocols([WM_DELETE_WINDOW])
        w.map()
        self.d.flush()

    def _init_graphics(self):
        self.pix = self.root.create_pixmap(self.win_w, self.win_h, self.depth)
        self.gc = self.win.create_gc()
        self._load_fonts()
        self.renderer = Renderer(self.win_w, self.win_h,
                                 self.font_ui, self.font_ui_b, self.font_mono)

    def _load_fonts(self):
        def load(path, size):
            if path and os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except OSError:
                    pass
            return ImageFont.load_default(size)

        self.font_ui = load(theme.FONT_UI, 13)
        self.font_ui_b = load(theme.FONT_UI_B, 13) or self.font_ui
        self.font_mono = load(theme.FONT_MONO, 13)

    def _init_state(self):
        self.filter = FILTER_INSTALLED
        self.view_mode = theme.VIEW_LIST
        self.search_text = ""
        self.show_hidden = False
        self.sort_key = apt.SORT_NAME
        self.sort_reverse = False
        self.entries: list = []
        self.filtered: list = []
        self.detail: Optional[apt.Package] = None
        self.messages: "queue.Queue[apt.Message]" = queue.Queue()
        self.worker = apt.Worker(self.messages)
        self._cancel = [False]
        self.status = ""
        self.status_color = theme.TEXT_DIM
        self.status_until = 0.0
        self.mode = "browse"
        self.menu: Optional[Menu] = None
        self.prompt: Optional[Prompt] = None
        self.confirm: Optional[ConfirmDialog] = None
        self._typeahead = ""
        self._typeahead_at = 0.0
        self._shift_now = False
        self._dirty = True
        self._last_layout = (0, 0)
        self._apt_cache = None
        self._loading = False
        self._refresh_after_update = False
        self.dragging_scroll = False

        if apt._HAS_PYTHON_APT:
            try:
                self._apt_cache = apt._apt.Cache()
                self._apt_cache.open()
                self._apt_cache.upgradable = True
            except Exception:
                self._apt_cache = None

    def _init_widgets(self):
        actions = [
            ("Install", "install", "Ctrl+I  Install selected"),
            ("Remove", "remove", "Delete  Remove selected"),
            ("Purge", "purge", "Ctrl+Shift+Delete  Purge"),
            ("Upgrade", "upgrade_all", "Ctrl+U  Upgrade all"),
            ("Refresh", "refresh", "F5 / Ctrl+R  Refresh (apt update)"),
            ("Sources", "show_sources", "Ctrl+S  APT sources"),
        ]
        self.buttons = [Button(l, a, h) for l, a, h in actions]
        self.search_box = TextBox(font=self.font_ui,
                                  placeholder="Search packages (Ctrl+F)")
        self.list_view = ListView()
        self.grid_view = GridView(origin_fetcher=lambda n: apt.fetch_origin(n, self._apt_cache))

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _layout(self):
        if (self.win_w, self.win_h) == self._last_layout:
            return
        self._last_layout = (self.win_w, self.win_h)
        x = theme.PAD
        y = (theme.TOOLBAR_H - theme.BTN_H) // 2
        for b in self.buttons:
            bw = max(theme.BTN_W_MIN, self.renderer.text_w(b.label) + 22)
            b.set_bounds(x, y, bw, theme.BTN_H)
            x += bw + 6
        # search box
        btn_right = x + 10
        search_w = self.win_w - btn_right - theme.PAD
        self.search_box.set_bounds(btn_right, y, search_w, theme.BTN_H)
        list_y = theme.TOOLBAR_H + 1
        list_h = self.win_h - list_y - theme.STATUSBAR_H
        self.list_view.set_bounds(0, list_y, self.win_w, list_h)
        self.grid_view.set_bounds(0, list_y, self.win_w, list_h)

    def _scrollbar_geom(self):
        lv = self.list_view
        if not lv.entries:
            return None
        track = lv.list_h - 4
        total = len(lv.entries)
        visible = lv.visible_rows
        if total <= visible:
            return None
        thumb_h = max(24, int(track * visible / total))
        max_scroll = lv.max_scroll
        frac = lv.scroll / max_scroll if max_scroll else 0
        thumb_y = lv.y + lv.HEADER_H + 2 + int((track - thumb_h) * frac)
        return (lv.x + lv.w - theme.SCROLL_W, lv.y + lv.HEADER_H,
                theme.SCROLL_W, track + 4, thumb_y, thumb_h)

    # ------------------------------------------------------------------
    # Package loading
    # ------------------------------------------------------------------
    def _load_packages(self):
        """Kick off an async package listing.  Results arrive via the queue as
        ("packages_loaded", entries)."""
        if self._loading:
            return
        self._loading = True
        self._cancel = [False]
        self._set_status("Loading package list\u2026", theme.TEXT_DIM, 0)
        self.list_view.set_entries([])
        self.invalidate()

        def _do():
            try:
                pkgs = apt.list_packages(
                    filter=self.filter, show_hidden=self.show_hidden,
                    search=self.search_text, cache=self._apt_cache)
                self.messages.put(("packages_loaded", pkgs))
            except Exception as e:
                self.messages.put(("error", f"Failed to load: {e}"))

        threading.Thread(target=_do, daemon=True).start()

    def _close_reopen_cache(self):
        """Re-open the APT cache after an apt-get update so fresh data is seen."""
        if self._apt_cache is not None:
            try:
                self._apt_cache.close()
            except Exception:
                pass
        if apt._HAS_PYTHON_APT:
            try:
                self._apt_cache = apt._apt.Cache()
                self._apt_cache.open()
                self._apt_cache.upgradable = True
            except Exception:
                self._apt_cache = None

    def _apply_view(self):
        self.filtered = apt.sort_packages(
            self.entries, self.sort_key, False, self.sort_reverse)
        self.list_view.set_entries(self.filtered)
        self.grid_view.entries = self.filtered
        self.grid_view.scroll = 0
        self.grid_view.sel = None

    def _apply_search(self):
        """Re-apply the search filter and re-sort; called after search/sort/filter
        changes that operate on already-loaded entries."""
        if self.search_text:
            sl = self.search_text.lower()
            self.filtered = [p for p in self.entries
                             if sl in p.name.lower() or sl in p.description.lower()]
        else:
            self.filtered = list(self.entries)
        self.filtered = apt.sort_packages(
            self.filtered, self.sort_key, False, self.sort_reverse)
        self.list_view.set_entries(self.filtered)
        self.grid_view.entries = self.filtered
        self.grid_view.scroll = 0
        self.grid_view.sel = None
        self.invalidate()

    # ------------------------------------------------------------------
    # Redraw
    # ------------------------------------------------------------------
    def redraw(self):
        r = self.renderer
        r.begin_frame()
        self._layout()
        self._draw_toolbar(r)
        self._draw_main(r)
        self._draw_statusbar(r)
        if self.mode == "detail" and self.detail:
            self._draw_detail(r)
        if self.mode == "menu" and self.menu:
            self.menu.render(r)
        if self.mode == "prompt" and self.prompt:
            self.prompt.render(r, self.win_w, self.win_h)
        if self.mode == "confirm" and self.confirm:
            self.confirm.render(r, self.win_w, self.win_h)
        self._push_frame()
        self._dirty = False

    def _push_frame(self):
        r = self.renderer
        data = r.pixel_bytes()
        w, h = r.width, r.height
        row_bytes = w * 4
        max_rows = max(1, 250000 // row_bytes)
        for sy in range(0, h, max_rows):
            sh = min(max_rows, h - sy)
            off = sy * row_bytes
            PutImage(display=self.d.display, drawable=self.pix, gc=self.gc,
                     width=w, height=sh, dst_x=0, dst_y=sy, left_pad=0,
                     depth=self.depth, format=X.ZPixmap,
                     data=data[off:off + sh * row_bytes])
        self.win.copy_area(self.gc, self.pix, 0, 0, w, h, 0, 0)
        self.d.flush()

    def invalidate(self):
        self._dirty = True

    # -- drawing ---------------------------------------------------------
    def _draw_toolbar(self, r):
        r.rect(0, 0, self.win_w, theme.TOOLBAR_H, theme.PANEL)
        r.hline(0, theme.TOOLBAR_H - 1, self.win_w, theme.PANEL_LINE)
        for b in self.buttons:
            b.render(r)
        # search box display
        if self.search_box.focus and self.mode == "search":
            self.search_box.render(r)
        else:
            r.push_clip(self.search_box.x, self.search_box.y,
                        self.search_box.w, self.search_box.h)
            r.rect(self.search_box.x, self.search_box.y,
                   self.search_box.w, self.search_box.h, theme.CARD_BG)
            r.outline(self.search_box.x, self.search_box.y,
                      self.search_box.w, self.search_box.h, theme.PANEL_LINE, 1)
            th = r.text_h(self.font_ui)
            r.text(self.search_box.x + theme.PAD,
                   self.search_box.y + (self.search_box.h - th) // 2,
                   self.search_text or self.search_box.placeholder,
                   theme.TEXT_FAINT if not self.search_text else theme.TEXT,
                   self.font_ui)
            r.pop_clip()
        # view / filter indicators
        vx = self.search_box.x + self.search_box.w + 8
        if vx < self.win_w - 120:
            view_label = "[GRID]" if self.view_mode == theme.VIEW_GRID else "[LIST]"
            r.text(vx, self.search_box.y + (self.search_box.h - 13) // 2,
                   view_label, theme.TEXT_DIM, self.font_ui)
            filt_label = f"[{self._filter_label()}]"
            r.text(vx + 52, self.search_box.y + (self.search_box.h - 13) // 2,
                   filt_label,
                   theme.ACCENT if self.filter != FILTER_ALL else theme.TEXT_DIM,
                   self.font_ui)

    def _filter_label(self) -> str:
        for k, label in FILTERS:
            if k == self.filter:
                return label
        return "All"

    def _draw_main(self, r):
        if self.view_mode == theme.VIEW_LIST:
            self.list_view.render(r, focused=True)
            self._draw_scrollbar(r)
        else:
            self.grid_view.render(r)

    def _draw_scrollbar(self, r):
        g = self._scrollbar_geom()
        if not g:
            return
        sx, sy, sw, sh, ty, th = g
        r.rect(sx, sy, sw, sh, theme.PANEL)
        r.outline(sx, sy, sw, sh, theme.PANEL_LINE)
        r.rect(sx + 1, ty, sw - 2, th, theme.SELECT)

    def _draw_statusbar(self, r):
        y = self.win_h - theme.STATUSBAR_H
        r.rect(0, y, self.win_w, theme.STATUSBAR_H, theme.PANEL)
        r.hline(0, y, self.win_w, theme.PANEL_LINE)
        th = r.text_h()
        ty = y + max(0, (theme.STATUSBAR_H - th) // 2)

        left = self.status or self._default_status()
        r.text(theme.PAD, ty, left, self.status_color)
        right = self._status_right()
        if right:
            r.text(self.win_w - theme.PAD, ty, right, theme.TEXT_DIM,
                   anchor="ra")

    def _default_status(self):
        n_inst = sum(1 for e in self.entries if e.installed)
        n_upg = sum(1 for e in self.entries if e.upgradable)
        n_src = len(apt.list_sources())
        return (f"{len(self.entries)} packages  \u00b7  "
                f"{n_inst} installed  \u00b7  "
                f"{n_upg} upgrades available  \u00b7  "
                f"{n_src} sources   \u2014   Ctrl+F search")

    def _status_right(self):
        parts = []
        if self.search_text:
            parts.append(f"filter: \"{self.search_text}\"")
        if self.worker.busy:
            parts.append("\u2026")
        if self._loading:
            parts.append("loading\u2026")
        return "  \u00b7  ".join(parts)

    # ------------------------------------------------------------------
    # Detail view
    # ------------------------------------------------------------------
    def _detail_buttons(self, r):
        """Return (x, y, w, h) for the action buttons in detail view."""
        bh = 28
        btn_w = 90
        gap = 8
        bx = (self.win_w - 2 * btn_w - gap) // 2
        y0 = self.win_h - theme.STATUSBAR_H - bh - 10
        return [(bx, y0, btn_w, bh),
                (bx + btn_w + gap, y0, btn_w, bh)]

    def _draw_detail(self, r):
        if not self.detail:
            return
        p = self.detail
        if not p.origin:
            p.origin = apt.fetch_origin(p.name, self._apt_cache)
        x0, y0, w0, h0 = theme.PAD, theme.TOOLBAR_H + 4, \
            self.win_w - theme.PAD * 2, theme.STATUSBAR_H
        content_y = y0 + 12
        content_h = self.win_h - theme.TOOLBAR_H - h0 - theme.STATUSBAR_H - 44

        # Dimmed background
        r.rect(x0, y0, w0, content_h + 24, theme.CARD_BG)
        r.outline(x0, y0, w0, content_h + 24, theme.PANEL_LINE)

        th = r.text_h(self.font_ui)
        th_b = r.text_h(self.font_ui_b)

        # Header: name (bold) + status badge
        name = p.name
        r.text(x0 + 16, content_y, name, theme.TEXT, self.font_ui_b)
        status_text = (p.status_label)
        if p.installed:
            if p.upgradable:
                status_text = "upgrade available"
            else:
                status_text = "installed"
        else:
            status_text = "not installed"
        scol = p.status_color
        sw = r.text_w(status_text, self.font_ui)
        sx = x0 + w0 - theme.PAD - 10 - sw - 16
        r.rect(sx, content_y, sw + 12, th + 6, scol)
        r.text(sx + 6, content_y + 3, status_text, theme.BG, self.font_ui)

        # Version line
        vy = content_y + th_b + 12
        r.text(x0 + 16, vy,
               f"Version: {p.display_version}", theme.TEXT, self.font_ui)
        if p.installed and p.installed_version and p.installed_version != p.version:
            r.text(x0 + 16, vy + th + 2,
                   f"Installed: {p.installed_version}",
                   theme.TEXT_DIM, self.font_ui)

        # Origin / Repository
        oy = vy + th * 2 + 8
        origin_label = p.origin if p.origin else "(no repository)"
        r.text(x0 + 16, oy, f"Repository: {origin_label}", theme.TEXT_DIM,
               self.font_ui)
        r.text(x0 + 16, oy + th + 2,
               f"Section: {p.section or 'unknown'}  \u00b7  "
               f"Size: {p.display_size}",
               theme.TEXT_DIM, self.font_ui)

        # Homepage link (clickable)
        hy = oy + th * 2 + 8
        if p.homepage:
            hw = r.text_w(p.homepage, self.font_ui)
            r.text(x0 + 16, hy, p.homepage, theme.ACCENT, self.font_ui)
            self._detail_homepage_rect = (x0 + 16, hy, hw, th)
        else:
            self._detail_homepage_rect = None

        # Description (wrapped)
        desc_y = hy + th + 8
        desc_text = p.description or "No description available."
        max_w = w0 - 32
        r.text(x0 + 16, desc_y, "Description:", theme.TEXT_DIM, self.font_ui)
        r.text_wrap(desc_text, self.font_ui, theme.TEXT_DIM,
                    x0 + 16, desc_y + th + 6, max_w)
        r.text_wrap("", self.font_ui, theme.TEXT, 0, 0, 0)  # reset cache hit

        # Action buttons at bottom
        r.rect(0, self.win_h - theme.STATUSBAR_H - 28 - 10,
               self.win_w, 28, theme.PANEL)
        btns = self._detail_buttons(r)
        self._detail_btn_rects = btns
        act_label = "Remove" if p.installed else "Install"
        act_color = theme.ERR_RED if p.installed else theme.OK_GREEN
        b1x, b1y, b1w, b1h = btns[0]
        r.rect(b1x, b1y, b1w, b1h, theme.ROW_HOVER if False else theme.SELECT)
        r.outline(b1x, b1y, b1w, b1h, theme.PANEL_LINE)
        r.text(b1x + b1w // 2 - r.text_w(act_label) // 2,
               b1y + (b1h - th) // 2, act_label, act_color, self.font_ui)
        # second button
        b2x, b2y, b2w, b2h = btns[1]
        b2_label = "Purge" if p.installed else "Open"
        r.rect(b2x, b2y, b2w, b2h, theme.SELECT)
        r.outline(b2x, b2y, b2w, b2h, theme.PANEL_LINE)
        r.text(b2x + b2w // 2 - r.text_w(b2_label) // 2,
               b2y + (b2h - th) // 2, b2_label, theme.TEXT, self.font_ui)

    def _detail_install(self):
        if not self.detail:
            return
        names = [self.detail.name]
        if self.detail.installed:
            self._open_confirm("Remove package?", "remove", names,
                               theme.ERR_RED)
        else:
            self._open_confirm("Install package?", "install", names,
                               theme.OK_GREEN)

    def _detail_remove(self):
        if not self.detail:
            return
        self._open_confirm("Remove package?", "remove",
                           [self.detail.name], theme.ERR_RED)

    def _detail_purge(self):
        if not self.detail:
            return
        self._open_confirm("Purge package?", "purge",
                           [self.detail.name], theme.ERR_RED)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action(self, name: str):
        fn = getattr(self, "do_" + name, None)
        if fn:
            fn()

    def do_install(self):
        pkgs = self._current_selection()
        if not pkgs:
            self._set_status("No packages selected", theme.WARN_YELLOW, 3)
            return
        names = [p.name for p in pkgs if not p.installed]
        if not names:
            self._set_status("Selected packages are already installed",
                             theme.WARN_YELLOW, 3)
            return
        self._open_confirm("Install packages?", "install",
                           names, theme.OK_GREEN)

    def do_remove(self):
        self._do_remove(purge=False)

    def do_purge(self):
        self._do_remove(purge=True)

    def _do_remove(self, purge: bool):
        pkgs = self._current_selection()
        if not pkgs:
            self._set_status("No packages selected", theme.WARN_YELLOW, 3)
            return
        names = [p.name for p in pkgs if p.installed]
        if not names:
            self._set_status("Selected packages are not installed",
                             theme.WARN_YELLOW, 3)
            return
        act = "purge" if purge else "remove"
        self._open_confirm(f"Remove {len(names)} package(s)?", act,
                           names, theme.ERR_RED)

    def do_upgrade_all(self):
        if self.worker.busy:
            return
        self._open_confirm("Upgrade all packages?", "upgrade",
                           [], theme.OK_GREEN)

    def do_refresh(self):
        """Refresh: run apt-get update (if root), then reload packages."""
        if self.worker.busy:
            self._set_status("An operation is already running",
                             theme.WARN_YELLOW, 3)
            return
        if os.geteuid() != 0:
            # Non-root: just re-read the cache
            self._load_packages()
            return
        self._cancel = [False]
        self._set_status("Updating package index\u2026", theme.TEXT_DIM, 0)
        self._refresh_after_update = True
        self.worker.run(apt.op_apt_update, self.messages, self._cancel)

    def do_show_sources(self):
        """Show the list of configured APT sources as a menu."""
        if self._apt_cache is None:
            self._set_status("APT cache unavailable", theme.ERR_RED, 3)
            return
        sources = apt.list_sources()
        items = []
        for s in sources:
            label = s.name
            if not s.enabled:
                label = f"[disabled] {label}"
            if s.uri:
                label += f" \u2014 {s.uri}"
            items.append(MenuItem(label, "noop", s.enabled))
        if not items:
            items = [MenuItem("(no sources configured)", "", False)]
        n = len(items)
        self.menu = Menu(items, self.win_w // 2 - theme.MENU_W // 2,
                         min(theme.TOOLBAR_H + 40,
                             self.win_h - theme.MENU_ITEM_H * n - 4))
        self.mode = "menu"

    def do_view_toggle(self):
        self.view_mode = (theme.VIEW_GRID if self.view_mode == theme.VIEW_LIST
                          else theme.VIEW_LIST)
        self.invalidate()

    def do_filter(self, filt: str):
        if self.filter == filt:
            return
        self.filter = filt
        self._load_packages()

    def do_search(self):
        self.mode = "search"
        self.search_box.set_text(self.search_text)
        self.search_box.focus = True

    def do_activate(self, idx=None):
        """Show details for a package."""
        if idx is None:
            if self.view_mode == theme.VIEW_LIST:
                if len(self.list_view.sel) != 1:
                    return
                idx = self.list_view.sel[0]
            else:
                idx = self.grid_view.sel
                if idx is None:
                    return
        entries = self.filtered
        if not (0 <= idx < len(entries)):
            return
        self.detail = entries[idx]
        self.mode = "detail"
        self.invalidate()

    def do_back(self):
        if self.mode == "detail":
            self.mode = "browse"
            self.detail = None
            self.invalidate()
        elif self.mode == "search":
            self.search_box.focus = False
            self.mode = "browse"
            self.invalidate()
        else:
            self._open_context_menu(self.win_w // 2, theme.TOOLBAR_H + 40)

    def do_toggle_hidden(self):
        self.show_hidden = not self.show_hidden
        self._load_packages()

    def do_noop(self):
        """No-op handler for non-clickable menu items."""
        pass

    def do_refresh_sources(self):
        """Explicitly run apt-get update (same as Refresh when root)."""
        self.do_refresh()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _current_selection(self) -> list:
        if self.view_mode == theme.VIEW_LIST:
            return self.list_view.select_entries()
        if self.grid_view.sel is not None:
            return [self.grid_view.entries[self.grid_view.sel]]
        return []

    def _is_installed(self, name: str) -> bool:
        for e in self.entries:
            if e.name == name and e.installed:
                return True
        return False

    # ------------------------------------------------------------------
    # Search / filter
    # ------------------------------------------------------------------
    def _commit_search_box(self):
        self.search_text = self.search_box.text.strip()
        self._apply_search()
        self.mode = "browse"

    def _cancel_search(self):
        self.search_box.text = self.search_text
        self.search_box.cursor = len(self.search_text)
        self.search_box.anchor = None
        self.mode = "browse"
        self.invalidate()

    def _apply_filter(self):
        """Re-load from APT with the new filter (used by filter buttons)."""
        self._load_packages()

    # ------------------------------------------------------------------
    # Confirm dialog
    # ------------------------------------------------------------------
    def _open_confirm(self, title, action, names, color):
        self.confirm = ConfirmDialog(title, action, names, font=self.font_ui)
        self.mode = "confirm"
        self.invalidate()

    def _confirm_action(self):
        if not self.confirm:
            return
        c = self.confirm
        pkgs = self._current_selection()
        names = [p.name for p in pkgs]
        action = c.action
        self.confirm = None
        self.mode = "browse"
        if self.worker.busy:
            self._set_status("An operation is already running",
                             theme.WARN_YELLOW, 3)
            return
        self._cancel = [False]
        if action == "install":
            names = [n for n in names if not self._is_installed(n)]
            if not names:
                self._set_status("All selected packages are already installed",
                                 theme.WARN_YELLOW, 3)
                return
            self._set_status("Installing\u2026", theme.TEXT_DIM, 0)
            self.worker.run(apt.op_install, names, self.messages, self._cancel)
        elif action == "remove":
            self._set_status("Removing\u2026", theme.TEXT_DIM, 0)
            self.worker.run(apt.op_remove, names, self.messages, self._cancel,
                            False)
        elif action == "purge":
            self._set_status("Purging\u2026", theme.TEXT_DIM, 0)
            self.worker.run(apt.op_remove, names, self.messages, self._cancel,
                            True)
        elif action == "upgrade":
            self._set_status("Upgrading all packages\u2026", theme.TEXT_DIM, 0)
            self.worker.run(apt.op_upgrade, self.messages, self._cancel)

    # ------------------------------------------------------------------
    # Worker polling
    # ------------------------------------------------------------------
    def _poll_worker(self):
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "packages_loaded":
                    self.entries = payload
                    self._loading = False
                    if self._refresh_after_update:
                        self._refresh_after_update = False
                        self._close_reopen_cache()
                        self._load_packages()
                    else:
                        self._apply_view()
                        if self.filter == FILTER_UPGRADABLE:
                            n = sum(1 for e in self.entries if e.upgradable)
                            self._set_status(
                                f"{n} packages can be upgraded",
                                theme.TEXT_DIM, 4)
                        else:
                            srcs = apt.list_sources()
                            self._set_status(
                                f"{len(srcs)} sources, "
                                f"{len(self.entries)} packages",
                                theme.TEXT_DIM, 5)
                elif kind == "progress":
                    self._set_status(str(payload), theme.TEXT_DIM, 0)
                elif kind == "done":
                    self._set_status("Operation complete",
                                     theme.OK_GREEN, 4)
                    if self._refresh_after_update:
                        self._load_packages()
                    else:
                        self._load_packages()
                elif kind == "error":
                    self._set_status(str(payload), theme.ERR_RED, 8)
        except queue.Empty:
            pass

    def _set_status(self, text, color, seconds=0):
        self.status = text
        self.status_color = color
        self.status_until = time.time() + seconds if seconds else 0.0
        self.invalidate()

    def _default_status_expire(self):
        if self.status_until and time.time() > self.status_until:
            self.status = ""
            self.status_color = theme.TEXT_DIM
            self.status_until = 0.0
            self.invalidate()

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------
    def _keysym(self, event):
        shift = event.state & X.ShiftMask
        ks = self.d.keycode_to_keysym(event.detail, 1 if shift else 0)
        if ks == X.NoSymbol:
            ks = self.d.keycode_to_keysym(event.detail, 0)
        return ks, KEYSYM_REV.get(ks)

    def on_key(self, event):
        ks, name = self._keysym(event)
        state = event.state
        ctrl = bool(state & X.ControlMask)
        alt = bool(state & X.Mod1Mask)
        shift = bool(state & X.ShiftMask)
        self._shift_now = shift
        ch = self.d.lookup_string(ks) or ""
        if len(ch) > 1:
            ch = ""
        if not ch.isprintable():
            ch = ""

        if self.mode == "prompt" and self.prompt:
            self._key_prompt(name, ch, ctrl)
        elif self.mode == "menu" and self.menu:
            self._key_menu(name, ch)
        elif self.mode == "confirm" and self.confirm:
            if name in ("Return", "KP_Enter"):
                self._confirm_action()
            elif name == "Escape":
                self.mode = "browse"
                self.confirm = None
                self.invalidate()
        elif self.mode == "search":
            self._key_search(name, ch, ctrl)
        elif self.mode == "detail":
            if name == "Escape":
                self.do_back()
                return
            if name == "Return" or name == "KP_Enter":
                self._detail_install()
                return
            if ctrl and name in ("R", "r"):
                self._detail_remove()
                return
            if ctrl and name in ("P", "p"):
                self._detail_purge()
                return
            if ctrl and name == "D":
                self.do_back()
                return
            if name == "I" or name == "i":
                self._detail_install()
                return
            self.do_back()
        else:
            self._key_browse(name, ch, ctrl, alt, ks)
        self.invalidate()

    def _key_prompt(self, name, ch, ctrl):
        p = self.prompt
        if name in ("Return", "KP_Enter"):
            self._confirm_prompt()
        elif name == "Escape":
            self._cancel_prompt()
        elif ctrl and name in ("V", "v"):
            self._paste_from_x_clipboard(p.input)
        elif ctrl and name in ("C", "c"):
            self._copy_to_x_clipboard(p.input.selected_text())
        else:
            p.input.key(name, ch,
                        (4 if ctrl else 0) | (1 if self._shift_now else 0))

    def _key_menu(self, name, ch):
        m = self.menu
        if name == "Escape":
            self.mode = "browse"; self.menu = None; return
        if name == "Up":
            m.index = 0 if m.index is None else max(0, m.index - 1)
            self._skip_disabled(m, -1)
        elif name == "Down":
            m.index = 0 if m.index is None else min(len(m.items) - 1, m.index + 1)
            self._skip_disabled(m, 1)
        elif name in ("Return", "KP_Enter"):
            self._menu_activate(m.index)
        else:
            self.mode = "browse"; self.menu = None

    def _skip_disabled(self, m, delta):
        for _ in range(len(m.items)):
            it = m.items[m.index]
            if it.enabled:
                return
            m.index = (m.index + delta) % len(m.items)

    def _menu_activate(self, idx):
        if idx is None or idx < 0:
            return
        m = self.menu
        self.menu = None
        self.mode = "browse"
        if 0 <= idx < len(m.items) and m.items[idx].enabled:
            self.action(m.items[idx].action)

    def _key_search(self, name, ch, ctrl):
        if name == "Escape":
            self._cancel_search(); return
        if name in ("Return", "KP_Enter"):
            self._commit_search_box(); return
        if ctrl and name in ("C", "c"):
            self._copy_to_x_clipboard(self.search_box.selected_text()); return
        if ctrl and name in ("V", "v"):
            self._paste_from_x_clipboard(self.search_box); return
        self.search_box.key(name, ch,
                            (4 if ctrl else 0) |
                            (1 if self._shift_now else 0))

    def _key_browse(self, name, ch, ctrl, alt, ks):
        lv = self.list_view
        gv = self.grid_view

        if alt and name == "Left":
            self.do_back(); return

        if ctrl and name == "F":
            self.do_search(); return
        if ctrl and name == "U":
            self.do_upgrade_all(); return
        if ctrl and name == "I":
            self.do_install(); return
        if ctrl and name == "R":
            self.do_refresh(); return
        if ctrl and name == "S":
            self.do_show_sources(); return
        if name == "F5":
            self.do_refresh(); return
        if ctrl and name == "H":
            self.do_toggle_hidden(); return
        if ctrl and name == "D":
            self.do_back(); return
        if ctrl and name == "1":
            self.do_filter(FILTER_ALL); return
        if ctrl and name == "2":
            self.do_filter(FILTER_INSTALLED); return
        if ctrl and name == "3":
            self.do_filter(FILTER_AVAILABLE); return
        if ctrl and name == "4":
            self.do_filter(FILTER_UPGRADABLE); return

        if name == "Tab":
            self.do_view_toggle(); return
        if name == "F1":
            self._show_help(); return

        if name == "Return" or name == "KP_Enter":
            self.do_activate(); return
        if name == "BackSpace":
            self.do_back(); return
        if name == "Escape":
            if self.search_text:
                self.search_text = ""
                self._apply_search()
            return

        if name == "Up" or name == "KP_Up":
            self._move_sel(-1, ctrl, alt); return
        if name == "Down" or name == "KP_Down":
            self._move_sel(1, ctrl, alt); return
        if name == "Prior":
            self._move_sel(-lv.visible_rows, ctrl, alt); return
        if name == "Next":
            self._move_sel(lv.visible_rows, ctrl, alt); return
        if name == "Home":
            self.lv_move(0); return
        if name == "End":
            self.lv_move(len(self.filtered) - 1); return

        if name == "Delete":
            self.do_remove(); return

        if self.view_mode == theme.VIEW_GRID:
            if name == "Left" or name == "KP_Left":
                gv.scroll_by(-1); return
            if name == "Right" or name == "KP_Right":
                gv.scroll_by(1); return

        if ch:
            self._typeahead_add(ch)
            return

    def lv_move(self, idx, extend=False):
        lv = self.list_view
        idx = max(0, min(len(self.filtered) - 1, idx))
        if extend:
            lv.select_range(idx)
        else:
            lv.select_one(idx)

    def _move_sel(self, delta, ctrl, shift):
        lv = self.list_view
        if not self.filtered:
            return
        cur = lv.anchor
        if cur is None:
            cur = -delta
        target = max(0, min(len(self.filtered) - 1, cur + delta))
        if ctrl:
            lv.select_one(target)
        elif shift:
            lv.select_range(target)
        else:
            lv.select_one(target)

    def _typeahead_add(self, ch):
        now = time.time()
        if now - self._typeahead_at > 1.5:
            self._typeahead = ""
        self._typeahead += ch.lower()
        self._typeahead_at = now
        for i, e in enumerate(self.filtered):
            if e.name.lower().startswith(self._typeahead):
                self.list_view.select_one(i)
                break
        self.invalidate()

    # ------------------------------------------------------------------
    # X clipboard
    # ------------------------------------------------------------------
    def _copy_to_x_clipboard(self, text):
        if not text:
            return
        try:
            self._set_clipboard_owner(text)
        except Exception:
            pass

    def _paste_from_x_clipboard(self, box: TextBox):
        try:
            text = self._read_clipboard()
        except Exception:
            text = ""
        if text:
            box.replace_selection(text)

    def _set_clipboard_owner(self, text):
        import subprocess
        subprocess.Popen(["xclip", "-selection", "clipboard"],
                         stdin=subprocess.PIPE, text=True).communicate(text)

    def _read_clipboard(self):
        import subprocess
        out = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                             capture_output=True, text=True)
        return out.stdout if out.returncode == 0 else ""

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------
    def _hit_button(self, mx, my):
        for b in self.buttons:
            if b.contains(mx, my):
                return b
        return None

    def on_button_press(self, e):
        x, y = e.event_x, e.event_y
        button = e.detail
        state = e.state
        ctrl = bool(state & X.ControlMask)
        lv = self.list_view
        gv = self.grid_view
        now = time.time()

        # detail view
        if self.mode == "detail" and self.detail:
            rects = getattr(self, "_detail_btn_rects", None)
            if rects:
                b1x, b1y, b1w, b1h = rects[0]
                if b1x <= x < b1x + b1w and b1y <= y < b1y + b1h:
                    self._detail_install()
                    return
                b2x, b2y, b2w, b2h = rects[1]
                if b2x <= x < b2x + b2w and b2y <= y < b2y + b2h:
                    if self.detail.installed:
                        self._detail_purge()
                    else:
                        apt.op_reveal(self.detail.homepage or "")
                        self.invalidate()
                    return
            # click the homepage link
            hr = getattr(self, "_detail_homepage_rect", None)
            if hr and hr[0] <= x < hr[0] + hr[2] and hr[1] <= y < hr[1] + hr[3]:
                apt.op_reveal(self.detail.homepage or "")
                self.invalidate()
                return
            if button == 1:
                self.do_back()
                return

        # confirm dialog
        if self.mode == "confirm" and self.confirm:
            c = self.confirm
            if c.ok_btn.contains(x, y):
                self._confirm_action()
            elif c.cancel_btn.contains(x, y):
                self.mode = "browse"
                self.confirm = None
                self.invalidate()
            return

        # search overlay
        if self.mode == "search" and self.search_box.contains(x, y):
            self.search_box.focus = True
            self.search_box.mouse_down(x, y)
            return

        if self.mode == "search" and not self.search_box.contains(x, y):
            # click outside search -> commit
            self._commit_search_box()
            return

        # prompt overlay
        if self.mode == "prompt" and self.prompt:
            p = self.prompt
            if p.input.contains(x, y):
                p.input.focus = True
                p.input.mouse_down(x, y)
            elif p.ok_btn.contains(x, y):
                self._confirm_prompt()
            elif p.cancel_btn.contains(x, y):
                self._cancel_prompt()
            return

        # menu overlay
        if self.mode == "menu" and self.menu:
            if button == 1:
                idx = self.menu.item_at(x, y)
                if idx is not None:
                    self._menu_activate(idx)
                else:
                    self.mode = "browse"
                    self.menu = None
            return

        if button in (4, 5, 6, 7):
            self._wheel(button)
            return

        # toolbar buttons
        b = self._hit_button(x, y)
        if b:
            if b.enabled:
                self.action(b.action)
            return

        # search box click (from browse mode)
        if self.search_box.contains(x, y) and self.mode != "search":
            self.do_search()
            self.search_box.focus = True
            self.search_box.mouse_down(x, y)
            return

        # main area
        if self.view_mode == theme.VIEW_LIST:
            if lv.header_hit(y) and x < self.win_w - theme.SCROLL_W:
                col = lv.column_at(x)
                if col == self.sort_key:
                    self.sort_reverse = not self.sort_reverse
                else:
                    self.sort_key = col
                    self.sort_reverse = False
                self._apply_search()
                return
            g = self._scrollbar_geom()
            if g and g[0] <= x < g[0] + g[2] and g[1] <= y < g[1] + g[3]:
                self.dragging_scroll = True
                self._scrollbar_drag_to(y)
                return
            idx = lv.row_at(y)
            if button == 1:
                if ctrl:
                    if idx is not None:
                        lv.toggle(idx)
                elif self._shift_now:
                    if idx is not None:
                        lv.select_range(idx)
                else:
                    if idx is not None:
                        dbl = (idx == lv._last_click_index and
                               now - lv._last_click < 0.4)
                        lv._last_click = now
                        lv._last_click_index = idx
                        if dbl:
                            self.do_activate(idx)
                        else:
                            lv.select_one(idx)
                    else:
                        lv.clear()
            elif button == 3:
                if idx is not None and idx not in lv.sel:
                    lv.select_one(idx)
                self._open_context_menu(x, y)
        else:
            idx = gv.entry_at(x, y)
            if idx is not None:
                if button == 1:
                    dbl = (idx == gv.sel and
                           now - getattr(gv, "_last_click", 0) < 0.4)
                    gv._last_click = now
                    if dbl:
                        gv.sel = idx
                        self.do_activate(idx)
                    else:
                        gv.sel = idx
                        gv.ensure_visible(idx)
                elif button == 3:
                    gv.sel = idx
                    gv.ensure_visible(idx)
                    self._open_context_menu(x, y)

    def _wheel(self, button):
        if self.view_mode == theme.VIEW_LIST:
            lv = self.list_view
            step = 3
            delta = -step if button == 4 else step
            lv.scroll += delta
            lv.clamp_scroll()
        else:
            delta = -3 if button == 4 else 3
            self.grid_view.scroll_by(delta)
        self.invalidate()

    def _scrollbar_drag_to(self, y):
        lv = self.list_view
        g = self._scrollbar_geom()
        if not g:
            return
        sx, sy, sw, sh, _ty, _th = g
        track = sh - 4
        frac = (y - sy) / track if track > 0 else 0
        lv.scroll = int(frac * lv.max_scroll)
        lv.clamp_scroll()
        self.invalidate()

    def on_motion(self, e):
        x, y = e.event_x, e.event_y
        lv = self.list_view
        gv = self.grid_view
        changed = False

        if self.dragging_scroll:
            self._scrollbar_drag_to(y)
            return

        for b in self.buttons:
            hover = b.contains(x, y)
            if hover != b.hover:
                b.hover = hover
                changed = True

        if self.mode == "menu" and self.menu:
            idx = self.menu.item_at(x, y)
            if idx != self.menu.index:
                self.menu.index = idx
                changed = True

        if self.mode == "prompt" and self.prompt:
            p = self.prompt
            for btn in (p.ok_btn, p.cancel_btn):
                hov = btn.contains(x, y)
                if hov != btn.hover:
                    btn.hover = hov
                    changed = True
            return

        if self.mode == "confirm" and self.confirm:
            c = self.confirm
            for btn in (c.ok_btn, c.cancel_btn):
                hov = btn.contains(x, y)
                if hov != btn.hover:
                    btn.hover = hov
                    changed = True
            return

        if self.mode == "search" and self.search_box.contains(x, y):
            return

        if self.mode == "browse":
            if self.view_mode == theme.VIEW_LIST:
                idx = lv.row_at(y)
                if idx != lv.hover:
                    lv.hover = idx
                    changed = True
            else:
                idx = gv.entry_at(x, y)
                if idx != gv.hover:
                    gv.hover = idx
                    changed = True

        if changed:
            self.invalidate()

    def on_button_release(self, e):
        self.dragging_scroll = False

    # -- context menu ---------------------------------------------------
    def _open_context_menu(self, x, y):
        sel = self._current_selection()
        n = len(sel)
        items = []
        if n == 0:
            items = [
                MenuItem("Refresh", "refresh", True, "F5"),
                MenuItem("Refresh sources", "refresh_sources", True, "Ctrl+R"),
                MenuItem("Sources", "show_sources", True, "Ctrl+S"),
                MenuItem("Search", "search", True, "Ctrl+F"),
                MenuItem("List view", "view_toggle", True, "Tab"),
                MenuItem("Show hidden", "toggle_hidden", True, "Ctrl+H"),
            ]
        else:
            items = [
                MenuItem("Install", "install", n > 0, "Ctrl+I"),
                MenuItem("Remove", "remove",
                         any(p.installed for p in sel), "Delete"),
                MenuItem("Purge", "purge",
                         any(p.installed for p in sel), ""),
                MenuItem("", "", False),
                MenuItem("Upgrade all", "upgrade_all", True, "Ctrl+U"),
                MenuItem("", "", False),
                MenuItem("Refresh", "refresh", True, "F5"),
                MenuItem("Refresh sources", "refresh_sources", True, "Ctrl+R"),
                MenuItem("Sources", "show_sources", True, "Ctrl+S"),
                MenuItem("Search", "search", True, "Ctrl+F"),
                MenuItem("List view", "view_toggle", True, "Tab"),
                MenuItem("", "", False),
                MenuItem("Show hidden", "toggle_hidden", True, "Ctrl+H"),
            ]
        n_items = len(items)
        self.menu = Menu(items,
                         min(x, self.win_w - theme.MENU_W - 4),
                         min(y, self.win_h -
                             theme.MENU_ITEM_H * n_items - 4))
        self.mode = "menu"

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------
    def _show_help(self):
        self._set_status(
            "\u2191\u2193 move \u00b7 Enter details \u00b7 Tab list/grid \u00b7 "
            "Ctrl+I install \u00b7 Delete remove \u00b7 Ctrl+U upgrade all \u00b7 "
            "Ctrl+F search \u00b7 F5 / Ctrl+R refresh (apt update) \u00b7 "
            "Ctrl+S sources \u00b7 Ctrl+H hidden \u00b7 "
            "Ctrl+1/2/3/4 filter \u00b7 F1 help",
            theme.ACCENT_HOT, 15)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def _handle(self, e):
        t = e.type
        if t == X.Expose:
            if e.count == 0:
                self.invalidate()
        elif t == X.ConfigureNotify:
            if (e.width, e.height) != (self.win_w, self.win_h):
                self._resize(e.width, e.height)
        elif t == X.KeyPress:
            self.on_key(e)
        elif t == X.ButtonPress:
            self.on_button_press(e)
        elif t == X.ButtonRelease:
            self.on_button_release(e)
        elif t == X.MotionNotify:
            self.on_motion(e)
        elif t == X.ClientMessage:
            try:
                fmt, arr = e.data
                if fmt == 32 and arr and arr[0] == WM_DELETE_WINDOW:
                    self.running = False
            except (TypeError, ValueError):
                pass

    def _resize(self, w, h):
        self.win_w = max(600, w)
        self.win_h = max(400, h)
        self.pix = self.root.create_pixmap(self.win_w, self.win_h, self.depth)
        self.gc = self.win.create_gc()
        self.renderer = Renderer(self.win_w, self.win_h,
                                 self.font_ui, self.font_ui_b, self.font_mono)
        self._last_layout = (0, 0)
        self.invalidate()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        try:
            while self.running:
                if self.d.pending_events():
                    self._handle(self.d.next_event())
                else:
                    self._poll_worker()
                    self._default_status_expire()
                    if self._dirty:
                        self.redraw()
                    r, _w, _x = select.select([self.d.fileno()], [], [], 0.02)
        except KeyboardInterrupt:
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            if self._apt_cache is not None:
                try:
                    self._apt_cache.close()
                except Exception:
                    pass
            self.d.close()


def main(argv=None):
    Planet().run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
