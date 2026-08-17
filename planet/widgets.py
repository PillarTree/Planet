"""Widgets drawn on the software framebuffer for Planet.

The set of widgets mirrors Cadet (Button, TextBox, ListView, Menu, Prompt)
with two additions for the "app center" experience:

* ``AppCard``  -- a grid tile rendered in the grid view, showing an icon,
  the package name and its installed state.
* ``SearchBar``-- an always-visible, rounded text field embedded in the
  toolbar that also shows a clear button.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from PIL import Image, ImageDraw

from . import theme
from .render import Renderer


def _center_y(row_h: int, text_h: int) -> int:
    return max(0, (row_h - text_h) // 2)


# ---------------------------------------------------------------------------
# Button
# ---------------------------------------------------------------------------
class Button:
    def __init__(self, label: str, action: Optional[str] = None, hint: str = ""):
        self.label = label
        self.action = action
        self.hint = hint
        self.x = self.y = self.w = self.h = 0
        self.hover = False
        self.enabled = True

    def set_bounds(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h

    def contains(self, mx, my):
        return (self.x <= mx < self.x + self.w
                and self.y <= my < self.y + self.h)

    def render(self, r: Renderer):
        fill = theme.PANEL
        if not self.enabled:
            color = theme.TEXT_FAINT
        elif self.hover:
            fill = theme.ROW_HOVER
            color = theme.ACCENT_HOT
        else:
            color = theme.TEXT
        r.rect(self.x, self.y, self.w, self.h, fill)
        r.outline(self.x, self.y, self.w, self.h, theme.PANEL_LINE)
        th = r.text_h()
        r.text(self.x + self.w // 2 - r.text_w(self.label) // 2,
               self.y + _center_y(self.h, th), self.label, color)


# ---------------------------------------------------------------------------
# TextBox  (editable single-line text field)
# ---------------------------------------------------------------------------
class TextBox:
    def __init__(self, font=None, placeholder: str = ""):
        self.text = ""
        self.cursor = 0
        self.anchor: Optional[int] = None
        self.focus = False
        self.placeholder = placeholder
        self.x = self.y = self.w = self.h = 0
        self.offset = 0
        self._prefix_w_cache: dict = {}
        self.font = font

    def set_bounds(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h
        self._ensure_visible()

    def contains(self, mx, my):
        return (self.x <= mx < self.x + self.w
                and self.y <= my < self.y + self.h)

    def set_text(self, text: str, move_end: bool = True):
        self.text = text
        self.cursor = len(text) if move_end else 0
        self.anchor = None
        self._prefix_w_cache.clear()
        self._ensure_visible()

    def _px(self, i: int) -> int:
        if i == 0:
            return 0
        if i in self._prefix_w_cache:
            return self._prefix_w_cache[i]
        if self.font is None:
            return 0
        w = self.font.getlength(self.text[:i])
        self._prefix_w_cache[i] = w
        return w

    def _char_at(self, mx) -> int:
        x = mx + self.offset
        best = 0
        for i in range(len(self.text) + 1):
            if abs(self._px(i) - x) <= abs(self._px(best) - x):
                best = i
        return best

    def _ensure_visible(self):
        caret = self._px(self.cursor)
        inner = self.w - theme.PAD * 2
        if caret - self.offset < 0:
            self.offset = max(0, caret - theme.PAD)
        elif caret - self.offset > inner:
            self.offset = caret - inner + theme.PAD

    def mouse_down(self, mx, my, shift: bool = False):
        if not self.contains(mx, my):
            return False
        self.focus = True
        pos = self._char_at(mx - self.x)
        if shift and self.anchor is not None:
            pass
        else:
            self.anchor = None
        self.cursor = pos
        self._ensure_visible()
        return True

    def mouse_drag(self, mx, my):
        if not self.contains(mx, my):
            return False
        self.cursor = self._char_at(mx - self.x)
        if self.anchor is None:
            self.anchor = self.cursor
        self._ensure_visible()
        return True

    def _delete_range(self):
        if self.anchor is not None and self.anchor != self.cursor:
            a, b = sorted((self.anchor, self.cursor))
            self.text = self.text[:a] + self.text[b:]
            self.cursor = a
            self.anchor = None
            self._prefix_w_cache.clear()
            return True
        return False

    def key(self, ksym, ch, modifiers) -> bool:
        if not self.focus:
            return False
        mods = modifiers
        ctrl = bool(mods & 0x4)
        if ksym in ("Home", "KP_Home"):
            if ctrl:
                self.offset = 0
            else:
                self.cursor = 0
                self.anchor = None
        elif ksym in ("End", "KP_End"):
            if ctrl:
                self.offset = 0
            else:
                self.cursor = len(self.text)
                self.anchor = None
        elif ksym in ("Left", "KP_Left"):
            if ctrl:
                i = self.cursor
                while i > 0 and self.text[i - 1] == " ":
                    i -= 1
                while i > 0 and self.text[i - 1] != " ":
                    i -= 1
                self.cursor = i
            else:
                self.cursor = max(0, self.cursor - 1)
            if not (mods & 0x1):
                self.anchor = None
        elif ksym in ("Right", "KP_Right"):
            if ctrl:
                i = self.cursor
                while i < len(self.text) and self.text[i] != " ":
                    i += 1
                while i < len(self.text) and self.text[i] == " ":
                    i += 1
                self.cursor = i
            else:
                self.cursor = min(len(self.text), self.cursor + 1)
            if not (mods & 0x1):
                self.anchor = None
        elif ksym in ("BackSpace",):
            if not self._delete_range():
                if self.cursor > 0:
                    self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
                    self.cursor -= 1
                    self._prefix_w_cache.clear()
        elif ksym in ("Delete", "KP_Delete"):
            if not self._delete_range():
                if self.cursor < len(self.text):
                    self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
                    self._prefix_w_cache.clear()
        elif ksym in ("A", "a") and ctrl:
            self.cursor = len(self.text)
            self.anchor = 0
        elif ksym in ("C", "c") and ctrl:
            pass
        elif ksym in ("X", "x") and ctrl:
            pass
        elif ksym in ("V", "v") and ctrl:
            pass
        elif len(ch) > 0 and ch.isprintable():
            if not self._delete_range():
                pass
            self.text = self.text[:self.cursor] + ch + self.text[self.cursor:]
            self.cursor += 1
            self._prefix_w_cache.clear()
        else:
            return False
        self._ensure_visible()
        return True

    def selected_text(self) -> str:
        if self.anchor is not None and self.anchor != self.cursor:
            a, b = sorted((self.anchor, self.cursor))
            return self.text[a:b]
        return ""

    def replace_selection(self, new: str):
        if self.anchor is not None:
            a, b = sorted((self.anchor, self.cursor))
            self.text = self.text[:a] + new + self.text[b:]
            self.cursor = a + len(new)
            self.anchor = None
        else:
            self.text = self.text[:self.cursor] + new + self.text[self.cursor:]
            self.cursor += len(new)
        self._prefix_w_cache.clear()
        self._ensure_visible()

    def render(self, r: Renderer):
        r.rect(self.x, self.y, self.w, self.h, theme.CARD_BG)
        r.outline(self.x, self.y, self.w, self.h,
                  theme.ACCENT if self.focus else theme.PANEL_LINE, 1)
        inner_x = self.x + theme.PAD
        tw = self.w - theme.PAD * 2
        th = r.text_h(self.font)
        ty = self.y + _center_y(self.h, th)

        sel_a = min(self.anchor, self.cursor) if self.anchor is not None else None
        sel_b = max(self.anchor, self.cursor) if self.anchor is not None else None

        r.push_clip(self.x + theme.PAD, self.y, tw, self.h)
        if not self.text and not self.focus:
            r.text(inner_x, ty, self.placeholder, theme.TEXT_FAINT, self.font)
        else:
            if sel_a is not None and sel_b != sel_a:
                xa = inner_x - self.offset + self._px(sel_a)
                xb = inner_x - self.offset + self._px(sel_b)
                r.rect(xa, self.y + 2, xb - xa, self.h - 4, theme.SELECT_FOC)
            r.text(inner_x - self.offset, ty, self.text, theme.TEXT, self.font)
            if self.focus:
                cx = inner_x - self.offset + self._px(self.cursor)
                r.vline(cx, self.y + 3, self.h - 6, theme.ACCENT_HOT)
        r.pop_clip()


# ---------------------------------------------------------------------------
# ListView  (table/list of packages)
# ---------------------------------------------------------------------------
class ListView:
    HEADER_H = 26

    def __init__(self):
        self.x = self.y = self.w = self.h = 0
        self.entries: list = []
        self.sel: List[int] = []
        self.anchor: Optional[int] = None
        self.hover: Optional[int] = None
        self.scroll = 0
        self.sort_key = "name"
        self.sort_reverse = False
        self.visible_rows = 0
        self._last_click = 0.0
        self._last_click_index = -1
        self.show_installed = True  # filter flag toggled by app

    # -- selection ----------------------------------------------------------
    @property
    def selected(self) -> Optional[int]:
        return self.anchor

    def select_one(self, idx: Optional[int]):
        self.sel = [idx] if idx is not None else []
        self.anchor = idx
        self.ensure_visible(idx)

    def toggle(self, idx: int):
        if idx in self.sel:
            self.sel = [i for i in self.sel if i != idx]
            if not self.sel and self.anchor == idx:
                self.anchor = None
        else:
            self.sel.append(idx)
            self.anchor = idx
            self.ensure_visible(idx)

    def select_range(self, idx: int):
        base = self.anchor if self.anchor is not None else idx
        a, b = sorted((base, idx))
        self.sel = list(range(a, b + 1))
        self.anchor = idx
        self.ensure_visible(idx)

    def select_all(self):
        self.sel = list(range(len(self.entries)))
        self.anchor = self.sel[-1] if self.sel else None

    def clear(self):
        self.sel = []
        self.anchor = None

    def select_entries(self) -> list:
        return [self.entries[i] for i in self.sel if 0 <= i < len(self.entries)]

    def set_bounds(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.visible_rows = max(0, (h - self.HEADER_H) // theme.ROW_H)

    @property
    def list_h(self) -> int:
        return self.h - self.HEADER_H

    @property
    def max_scroll(self) -> int:
        return max(0, len(self.entries) - self.visible_rows)

    def clamp_scroll(self):
        self.scroll = min(max(0, self.scroll), self.max_scroll)

    def ensure_visible(self, idx: int):
        if idx is None:
            return
        if idx < self.scroll:
            self.scroll = idx
        elif idx >= self.scroll + self.visible_rows:
            self.scroll = idx - self.visible_rows + 1
        self.clamp_scroll()

    def column_at(self, mx) -> str:
        name_w, size_w, _time_w = self.columns(self.w - theme.SCROLL_W)
        if mx < self.x + name_w:
            return "name"
        if mx < self.x + name_w + size_w:
            return "size"
        return "version"

    def header_hit(self, my) -> bool:
        return self.y <= my < self.y + self.HEADER_H

    def row_at(self, my) -> Optional[int]:
        if my < self.y + self.HEADER_H or my >= self.y + self.h:
            return None
        i = self.scroll + (my - (self.y + self.HEADER_H)) // theme.ROW_H
        if 0 <= i < len(self.entries):
            return i
        return None

    def set_entries(self, entries: list):
        self.entries = entries
        self.sel = []
        self.anchor = None
        self.scroll = 0

    def columns(self, list_w: int):
        name_w = int(list_w * 0.45)
        size_w = 90
        ver_w = 150
        name_w = max(120, list_w - size_w - ver_w)
        return name_w, size_w, ver_w

    def render(self, r: Renderer, focused: bool):
        r.rect(self.x, self.y, self.w, self.h, theme.BG)
        name_w, size_w, ver_w = self.columns(self.w - theme.SCROLL_W)
        th = r.text_h()

        # header
        r.rect(self.x, self.y, self.w - theme.SCROLL_W, self.HEADER_H, theme.PANEL)
        hdr_y = self.y + _center_y(self.HEADER_H, th)
        headers = [("name", "Name", name_w), ("size", "Size", size_w),
                   ("version", "Version", ver_w)]
        cx = self.x + theme.ICON_PAD_X
        for key, label, w in headers:
            color = theme.ACCENT_HOT if self.sort_key == key else theme.TEXT
            r.text(cx, hdr_y, label, color)
            if self.sort_key == key:
                arrow = "\u25bc" if self.sort_reverse else "\u25b2"
                r.text(cx + r.text_w(label) + 6, hdr_y, arrow, color)
            cx += w
        r.hline(self.x, self.y + self.HEADER_H - 1, self.w, theme.PANEL_LINE)

        # rows
        r.push_clip(self.x, self.y + self.HEADER_H, self.w, self.list_h)
        for row in range(self.visible_rows):
            idx = self.scroll + row
            if idx >= len(self.entries):
                break
            e = self.entries[idx]
            ry = self.y + self.HEADER_H + row * theme.ROW_H
            if idx in self.sel:
                r.rect(self.x, ry, self.w, theme.ROW_H,
                       theme.SELECT_FOC if focused else theme.SELECT)
            elif idx == self.hover and idx not in self.sel:
                r.rect(self.x, ry, self.w, theme.ROW_H, theme.ROW_HOVER)
            elif row % 2 == 1:
                r.rect(self.x, ry, self.w, theme.ROW_H, theme.ROW_ALT)

            name_color = theme.TEXT
            if e.installed:
                name_color = theme.INSTALLED
            elif e.upgradable:
                name_color = theme.UPGRADE
            if e.is_hidden:
                name_color = tuple(max(0, c - 70) for c in name_color)

            r.text(self.x + theme.ICON_PAD_X + 4,
                   ry + _center_y(theme.ROW_H, th),
                   e.name[:80], name_color)
            r.text(self.x + name_w + 8,
                   ry + _center_y(theme.ROW_H, th),
                   e.display_size, theme.TEXT_DIM, anchor="ra")
            r.text(self.x + name_w + size_w + 8,
                   ry + _center_y(theme.ROW_H, th),
                   e.display_version, theme.TEXT_DIM)
        r.pop_clip()


# ---------------------------------------------------------------------------
# GridView  (cards of install entries -- the "app center" view)
# ---------------------------------------------------------------------------
class GridView:
    def __init__(self, origin_fetcher: Optional[Callable] = None):
        self.x = self.y = self.w = self.h = 0
        self.entries: list = []
        self.scroll = 0
        self.sel: Optional[int] = None
        self.hover: Optional[int] = 0
        self._last_click = 0.0
        self._origin_fetcher = origin_fetcher

    def set_bounds(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h

    def _cols(self) -> int:
        c = max(1, (self.w + theme.GRID_GAP) // (180 + theme.GRID_GAP))
        return c

    def entry_at(self, mx, my) -> Optional[int]:
        cols = self._cols()
        cw = (self.w - (cols - 1) * theme.GRID_GAP) / cols
        if mx < self.x or my < self.y + theme.SEARCH_H:
            return None
        col = int((mx - self.x) / (cw + theme.GRID_GAP))
        row = int((my - self.y - theme.SEARCH_H) / (theme.CARD_H + theme.GRID_GAP))
        idx = row * cols + col
        if 0 <= idx < len(self.entries):
            return idx
        return None

    def ensure_visible(self, idx: int):
        cols = self._cols()
        row = idx // cols
        first_row = self.scroll // cols
        if row < first_row:
            self.scroll = row * cols
        elif row > first_row + self._visible_rows() - 1:
            self.scroll = max(0, (row - self._visible_rows() + 1) * cols)

    def _visible_rows(self) -> int:
        avail = self.h - theme.SEARCH_H
        return max(1, (avail + theme.GRID_GAP) // (theme.CARD_H + theme.GRID_GAP))

    @property
    def max_scroll(self) -> int:
        cols = self._cols()
        total_rows = (len(self.entries) + cols - 1) // cols
        return max(0, (total_rows - self._visible_rows()) * cols)

    def clamp_scroll(self):
        self.scroll = min(max(0, self.scroll), self.max_scroll)

    def render(self, r: Renderer):
        r.rect(self.x, self.y, self.w, self.h, theme.BG)
        cols = self._cols()
        cw = (self.w - (cols - 1) * theme.GRID_GAP) / cols
        top = self.y + theme.SEARCH_H
        visible_rows = self._visible_rows()
        start_row = self.scroll // cols
        for row in range(start_row, start_row + visible_rows + 1):
            for col in range(cols):
                idx = row * cols + col
                if idx >= len(self.entries) or idx < self.scroll:
                    continue
                cy = top + row * (theme.CARD_H + theme.GRID_GAP) - self.scroll % cols
                if cy < top - theme.CARD_H:
                    continue
                if cy > self.h:
                    break
                self._draw_card(r, idx, col, row, cw, cy, cols)

    def _draw_card(self, r: Renderer, idx: int, col: int, row: int,
                   cw: float, cy: int, cols: int):
        e = self.entries[idx]
        if not e.origin and self._origin_fetcher:
            e.origin = self._origin_fetcher(e.name)
        cx = self.x + col * (cw + theme.GRID_GAP)
        if idx == self.sel:
            fill = theme.SELECT_FOC
        elif idx == self.hover:
            fill = theme.ROW_HOVER
        else:
            fill = theme.CARD_BG
        r.push_clip(self.x, theme.SEARCH_H, self.w, self.h)
        r.rect(int(cx), int(cy), int(cw), theme.CARD_H, fill)
        r.outline(int(cx), int(cy), int(cw), theme.CARD_H, theme.PANEL_LINE)
        icon = r.icon(e.icon_name or "package-x-generic", 32)
        if icon:
            r.blit(icon, int(cx + cw / 2 - 16), int(cy + 8))
        else:
            r.rect(int(cx + cw / 2 - 16), int(cy + 8), 32, 32, theme.PANEL)
        th = r.text_h()
        name_y = int(cy + 44)
        r.text(int(cx + 8), name_y, e.name[:28], theme.TEXT,
               r.font_ui_b)
        r.text(int(cx + 8), name_y + th + 2,
               e.display_version, theme.TEXT_DIM)
        state = "\u2713 installed" if e.installed else (
            "\u2191 upgrade" if e.upgradable else "not installed")
        state_col = (theme.INSTALLED if e.installed else
                     theme.UPGRADE if e.upgradable else theme.NOT_INST)
        r.text(int(cx + 8), name_y + th * 2 + 6, state, state_col)
        if e.origin:
            r.text(int(cx + 8), name_y + th * 3 + 8,
                   e.origin[:20], theme.TEXT_FAINT)
        r.pop_clip()

    def scroll_by(self, delta: int):
        self.scroll += delta
        self.clamp_scroll()


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------
class MenuItem:
    def __init__(self, label: str, action: str, enabled: bool = True,
                 shortcut: str = ""):
        self.label = label
        self.action = action
        self.enabled = enabled
        self.shortcut = shortcut


class Menu:
    def __init__(self, items: List[MenuItem], x: int, y: int):
        self.items = items
        self.x, self.y = x, y
        self.w = theme.MENU_W
        self.h = len(items) * theme.MENU_ITEM_H + 2
        self.index: Optional[int] = None

    def contains(self, mx, my) -> bool:
        return (self.x <= mx < self.x + self.w
                and self.y <= my < self.y + self.h)

    def item_at(self, mx, my) -> Optional[int]:
        if not self.contains(mx, my):
            return None
        i = (my - self.y - 1) // theme.MENU_ITEM_H
        return i if 0 <= i < len(self.items) else None

    def render(self, r: Renderer):
        r.rect(self.x, self.y, self.w, self.h, theme.PANEL)
        r.outline(self.x, self.y, self.w, self.h, theme.PANEL_LINE)
        th = r.text_h()
        for i, it in enumerate(self.items):
            iy = self.y + 1 + i * theme.MENU_ITEM_H
            if i == self.index and it.enabled:
                r.rect(self.x + 1, iy, self.w - 2, theme.MENU_ITEM_H, theme.SELECT)
            color = theme.TEXT if it.enabled else theme.TEXT_FAINT
            r.text(self.x + 12, iy + _center_y(theme.MENU_ITEM_H, th),
                   it.label, color)
            if it.shortcut:
                r.text(self.x + self.w - r.text_w(it.shortcut) - 12,
                       iy + _center_y(theme.MENU_ITEM_H, th),
                       it.shortcut, theme.TEXT_DIM)
        r.hline(self.x + 8, self.y + 1 + len(self.items) * theme.MENU_ITEM_H - 1,
                self.w - 16, theme.PANEL_LINE)


# ---------------------------------------------------------------------------
# Prompt dialog (modal overlay)
# ---------------------------------------------------------------------------
class Prompt:
    def __init__(self, title: str, initial: str, ok_label: str, action: str,
                 hint: str = "", cancel_label: str = "Cancel", font=None):
        self.title = title
        self.input = TextBox(font=font)
        self.input.set_text(initial)
        self.ok_label = ok_label
        self.cancel_label = cancel_label
        self.action = action
        self.hint = hint
        self.box = (0, 0, 0, 0)
        self.ok_btn = Button(ok_label, action)
        self.cancel_btn = Button(cancel_label, "cancel_prompt")

    def layout(self, r: Renderer, win_w: int, win_h: int):
        w = min(theme.MAX_DIALOG_W, win_w - 80)
        h = 128
        x = (win_w - w) // 2
        y = (win_h - h) // 2
        self.box = (x, y, w, h)
        self.input.set_bounds(x + 16, y + 46, w - 32, 30)
        self.ok_btn.set_bounds(x + w - 170, y + h - 38, 72, 28)
        self.cancel_btn.set_bounds(x + w - 90, y + h - 38, 72, 28)

    def render(self, r: Renderer, win_w: int, win_h: int):
        self.layout(r, win_w, win_h)
        x, y, w, h = self.box
        dim = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 150))
        r.blit(dim, 0, 0)
        r.rect(x, y, w, h, theme.PANEL)
        r.outline(x, y, w, h, theme.ACCENT, 1)
        th = r.text_h()
        r.text(x + 16, y + 14, self.title, theme.TEXT, r.font_ui_b)
        if self.hint:
            r.text(x + 16, y + 88, self.hint, theme.TEXT_DIM)
        self.input.render(r)
        self.ok_btn.render(r)
        self.cancel_btn.render(r)



