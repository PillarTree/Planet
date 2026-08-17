"""Software framebuffer + raster text cache, pushed to X11 via XPutImage.

All drawing goes into a Pillow image. Text rasterisation is cached per
(text, font, colour) so scrolling long package lists never re-renders glyphs --
it only blits cached tiles. The finished frame is pushed to the window with
XPutImage in horizontal bands, so there is no flicker.

This is the same pattern used by Cadet; the only extension here is an icon
cache for package icons loaded from the freedesktop icon theme.
"""

from __future__ import annotations

from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from . import theme

try:
    import numpy as _np
except Exception:
    _np = None


class Renderer:
    """Owns the framebuffer and exposes drawing primitives."""

    def __init__(self, width: int, height: int, font_ui, font_ui_b, font_mono):
        self.width = width
        self.height = height
        self.font_ui = font_ui
        self.font_ui_b = font_ui_b
        self.font_mono = font_mono
        self.img = Image.new("RGB", (width, height), theme.BG)
        self._draw = ImageDraw.Draw(self.img)
        self._clip = (0, 0, width, height)
        self._saved_clips: list = []
        self._text_cache: dict = {}
        self._icon_cache: dict = {}

    # -- frame lifecycle ----------------------------------------------------
    def begin_frame(self):
        self.img.paste(theme.BG, (0, 0, self.width, self.height))
        self._draw = ImageDraw.Draw(self.img)
        self._clip = (0, 0, self.width, self.height)
        self._saved_clips = []

    def pixel_bytes(self) -> bytes:
        if _np is not None:
            rgb = self.img.tobytes()
            arr = _np.frombuffer(rgb, dtype=_np.uint8)
            arr = arr.reshape(self.height, self.width, 3)
            out = _np.zeros((self.height, self.width, 4), dtype=_np.uint8)
            out[:, :, 0] = arr[:, :, 2]   # B
            out[:, :, 1] = arr[:, :, 1]   # G
            out[:, :, 2] = arr[:, :, 0]   # R
            return out.tobytes()
        rgb = self.img.tobytes()
        out = bytearray(len(rgb) * 4 // 3)
        o = 0
        for i in range(0, len(rgb), 3):
            out[o]      = rgb[i + 2]     # B
            out[o + 1]  = rgb[i + 1]     # G
            out[o + 2]  = rgb[i]         # R
            o += 4
        return bytes(out)

    # -- clipping -----------------------------------------------------------
    def push_clip(self, x, y, w, h):
        cx, cy, cw, ch = self._clip
        x = max(cx, x); y = max(cy, y)
        x2 = min(cx + cw, x + w); y2 = min(cy + ch, y + h)
        self._saved_clips.append(self._clip)
        self._clip = (x, y, max(0, x2 - x), max(0, y2 - y))

    def pop_clip(self):
        if self._saved_clips:
            self._clip = self._saved_clips.pop()

    def _intersect(self, x, y, w, h):
        cx, cy, cw, ch = self._clip
        x = max(cx, x); y = max(cy, y)
        x2 = min(cx + cw, x + w); y2 = min(cy + ch, y + h)
        return x, y, max(0, x2 - x), max(0, y2 - y)

    # -- primitives ---------------------------------------------------------
    def rect(self, x, y, w, h, fill):
        x, y, w, h = self._intersect(x, y, w, h)
        if w <= 0 or h <= 0:
            return
        self._draw.rectangle([x, y, x + w - 1, y + h - 1], fill=fill)

    def outline(self, x, y, w, h, color, width=1):
        x0, y0, w0, h0 = self._intersect(x, y, w, h)
        if w0 <= 0 or h0 <= 0:
            return
        self._draw.rectangle([x0, y0, x0 + w0 - 1, y0 + h0 - 1],
                             outline=color, width=width)

    def hline(self, x, y, w, color):
        x, y, w, h = self._intersect(x, y, w, 1)
        if w <= 0:
            return
        self._draw.line([(x, y), (x + w - 1, y)], fill=color)

    def vline(self, x, y, h, color):
        x, y, w, h = self._intersect(x, y, 1, h)
        if h <= 0:
            return
        self._draw.line([(x, y), (x, y + h - 1)], fill=color)

    def text(self, x, y, s, color=theme.TEXT, font=None, anchor="la"):
        if not s:
            return
        if font is None:
            font = self.font_ui
        rgb, mask = self._text_tile(s, font, color)
        tw, th = mask.size
        if anchor == "ra":
            x -= tw
        elif anchor == "ma":
            x -= tw // 2
        elif anchor == "lm":
            y -= th // 2
        x0, y0, w, h = self._intersect(x, y, tw, th)
        if w <= 0 or h <= 0:
            return
        box = (x0 - x, y0 - y, x0 - x + w, y0 - y + h)
        self.img.paste(rgb.crop(box), (x0, y0), mask.crop(box))

    def text_w(self, s, font=None) -> int:
        if not s:
            return 0
        if font is None:
            font = self.font_ui
        return font.getbbox(s)[2]

    def text_h(self, font=None) -> int:
        if font is None:
            font = self.font_ui
        a, d = font.getmetrics()
        return a + d

    def blit(self, img: Image.Image, x, y):
        x, y = int(round(x)), int(round(y))
        x0, y0, w, h = self._intersect(x, y, img.width, img.height)
        if w <= 0 or h <= 0:
            return
        box = (x0 - x, y0 - y, x0 - x + w, y0 - y + h)
        region = img.crop(box)
        if img.mode == "RGBA":
            self.img.paste(region, (x0, y0), region)
        else:
            self.img.paste(region, (x0, y0))

    # -- text wrapping --------------------------------------------------------
    def text_wrap(self, s, font, color, x, y, max_width) -> int:
        """Draw wrapped text starting at (x, y). Returns the number of lines."""
        if not s:
            return 0
        if font is None:
            font = self.font_ui
        line_h = self.text_h(font)
        words = s.split()
        lines: list = []
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            if self.text_w(test, font) <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        clip_y = self._clip[1]
        for i, line in enumerate(lines):
            ty = y + i * line_h
            if ty > clip_y + self._clip[3]:
                break
            self.text(x, ty, line, color, font)
        return len(lines)

    # -- text cache ---------------------------------------------------------
    def _text_tile(self, s, font, color):
        key = (s, id(font), color)
        item = self._text_cache.get(key)
        if item is None:
            mask = _render_tile(s, font, color)
            rgb = Image.new("RGB", mask.size, color)
            item = (rgb, mask)
            if len(self._text_cache) > 6000:
                self._text_cache.clear()
            self._text_cache[key] = item
        return item

    def clear_cache(self):
        self._text_cache.clear()
        self._icon_cache.clear()

    # -- icon cache ---------------------------------------------------------
    def icon(self, name: str, size: int = 24) -> Optional[Image.Image]:
        """Return a cached RGBA icon from the freedesktop icon theme, or None."""
        key = (name, size)
        img = self._icon_cache.get(key)
        if img is not None:
            return img
        img = _load_theme_icon(name, size)
        if img is None:
            self._icon_cache[key] = False
            return None
        self._icon_cache[key] = img
        if len(self._icon_cache) > 300:
            self._icon_cache.clear()
        return img


def _render_tile(s, font, color):
    bbox = font.getbbox(s)
    w = max(1, bbox[2] - bbox[0])
    h = max(1, bbox[3] - bbox[1])
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    d.text((-bbox[0], -bbox[1]), s, font=font, fill=255)
    return img


def _load_theme_icon(name: str, size: int) -> Optional[Image.Image]:
    """Best-effort icon lookup via the `tk`/`gio` icon theme or xdg-icon-resource."""
    if not name:
        return None
    candidates = []
    for d in ("/usr/share/pixmaps", "/usr/share/icons"):
        pass
    import os
    roots = ["/usr/share/icons", "/usr/share/pixmaps"]
    themes = ["hicolor", "Adwaita", "gnome", "ubuntu-mono-dark", "breeze"]
    for root in roots:
        for th in themes:
            for sub in ("apps", "actions", "categories", "status"):
                for ext in (".png", ".svg"):
                    for cand in (f"{root}/{th}/{size}x{size}/{sub}/{name}{ext}",
                                 f"{root}/{size}x{size}/{sub}/{name}{ext}",
                                 f"{root}/{sub}/{name}{ext}",
                                 f"{root}/{name}{ext}"):
                        if os.path.isfile(cand):
                            try:
                                im = Image.open(cand).convert("RGBA")
                                if im.size != (size, size):
                                    im = im.resize((size, size), Image.LANCZOS)
                                return im
                            except Exception:
                                pass
    return None
