"""
CHAMELEON engine - rule-based "move the furniture" resizer.

Pipeline (see README.md for the long version):

  1. Classify elements: full-bleed background, decorative shapes, assets that
     are missing from the brand kit, and "content".
  2. Bundle content into units. Pieces the designer tied together (same
     group_id, a panel behind text, text sitting on a picture, overlapping
     layers) become rigid clusters that scale as one. Everything else is a
     free image or free text unit. Rows of similar chips become flow rows.
  3. Search over column layouts: every contiguous split of the reading-order
     list into 1..4 columns, with several column-width recipes. For each one
     binary-search the largest scale at which text (re-wrapped with the
     renderer's own wrapping rule) and pictures fit. Prefer the layout whose
     column count matches the canvas shape.
  4. Place units, grow the hero picture into leftover space, spread gaps,
     then park decorative pieces where they collide with the least text.

Nothing here touches pixels: elements keep their ids, images keep their
aspect ratio, fonts and copy are untouched, only geometry and font size move.
"""

from __future__ import annotations

import itertools
import math
import os
import threading
from pathlib import Path
from typing import Any

try:  # Pillow is in requirements.txt, but never let a missing lib crash us.
    from PIL import ImageFont
except Exception:  # pragma: no cover
    ImageFont = None

_ROOT = Path(__file__).resolve().parent.parent
_FONT_DIR = _ROOT / "brand_kit" / "fonts"
_ASSET_DIR = _ROOT / "brand_kit" / "assets"

_BOLD = {"bold", "semibold", "extrabold", "black", "700", "800", "900", "600"}


# --------------------------------------------------------------------------
# Text measurement (mirrors renderer/converter/wrap.py)
# --------------------------------------------------------------------------

_REF = 200
_tls = threading.local()   # .exact: verify final text sizes with the renderer's own measurer
_fm_cache: dict[str, Any] = {}


def _exact_widths(el: dict, size: float, text: str):
    """Per-character widths exactly as the renderer measures them for wrapping
    (skia, same font lookup), or None if the renderer package is unavailable."""
    try:
        if "fm" not in _fm_cache:
            from renderer.converter.brand_fonts import load_brand_font_manager
            _fm_cache["fm"] = load_brand_font_manager(str(_FONT_DIR))
        from renderer.converter.convert import _BOLD_WEIGHTS, _normalize_weight, _weight_style_to_font_style
        from renderer.converter.wrap import char_widths
        weight = _normalize_weight(str(el.get("font_weight") or ""))
        italic = str(el.get("font_style") or "").lower() == "italic"
        style = _weight_style_to_font_style(weight in _BOLD_WEIGHTS, italic)
        return char_widths(_fm_cache["fm"], str(el.get("font_family") or "Solstice Sans"), weight, style,
                           size, text)
    except Exception:
        return None
_font_objs: dict[Path, Any] = {}
_adv_cache: dict[tuple, float] = {}


def _font_path(family: str, bold: bool, italic: bool) -> Path | None:
    fam = (family or "").lower()
    base = "SolsticeSerif" if ("serif" in fam and "sans" not in fam) else "SolsticeSans"
    style = ("Bold" if bold else "") + ("Italic" if italic else "") or "Regular"
    for name in (f"{base}-{style}.ttf", f"{base}-{'Bold' if bold else 'Regular'}.ttf",
                 f"{base}-Regular.ttf"):
        p = _FONT_DIR / name
        if p.exists():
            return p
    return None


def _adv(family: str, bold: bool, italic: bool, ch: str) -> float:
    """Advance of one glyph in units of font size (no kerning, like skia getWidths)."""
    key = (family, bold, italic, ch)
    v = _adv_cache.get(key)
    if v is not None:
        return v
    v = 0.28 if ch == " " else 0.55
    path = _font_path(family, bold, italic)
    if ImageFont is not None and path is not None:
        try:
            f = _font_objs.get(path)
            if f is None:
                f = _font_objs[path] = ImageFont.truetype(str(path), _REF)
            v = f.getlength(ch) / _REF
        except Exception:
            pass
    _adv_cache[key] = v
    return v


def _is_bold(w: Any) -> bool:
    return str(w or "").lower() in _BOLD


class _Txt:
    """Measures one text element: wraps exactly like the renderer does."""

    def __init__(self, el: dict):
        self.el = el
        self.text = str(el.get("content", ""))
        self.f = max(1.0, float(el.get("font_size", 16) or 16))
        self.lh = float(el.get("line_height") or 1.2)
        self.ls0 = float(el.get("letter_spacing") or 0.0)
        ml = el.get("max_lines")
        self.max_lines = int(ml) if isinstance(ml, (int, float)) and ml and ml > 0 else None
        fam = str(el.get("font_family") or "")
        bold = _is_bold(el.get("font_weight"))
        italic = str(el.get("font_style") or "").lower() == "italic"
        flags = [(bold, italic)] * len(self.text)
        for run in el.get("style_runs") or []:
            try:
                s, e = int(run["start"]), int(run["end"])
            except Exception:
                continue
            rb = _is_bold(run["font_weight"]) if "font_weight" in run else bold
            ri = (str(run["font_style"]).lower() == "italic") if "font_style" in run else italic
            for i in range(max(0, s), min(len(self.text), e)):
                flags[i] = (rb, ri)
        # The renderer wraps with the *base* style's glyph widths, but draws runs
        # in their own weight and upper-cases when `capitalize` is set: keep both.
        self.adv = [_adv(fam, bold, italic, ch) for ch in self.text]
        up = bool(el.get("capitalize"))
        self.advd = [_adv(fam, b, i, (ch.upper() if up and len(ch.upper()) == 1 else ch))
                     for ch, (b, i) in zip(self.text, flags)]
        self.P = [0.0]
        for a in self.adv:
            self.P.append(self.P[-1] + a)
        self.PD = [0.0]
        for a in self.advd:
            self.PD.append(self.PD[-1] + a)
        self._c: dict[tuple, tuple] = {}

    def height(self, n: int, fs: float) -> float:
        """Vertical extent of n lines. The renderer draws the first line at a
        fixed offset (~1.27 x font size to the descenders) whatever line_height
        is, so tight line heights must not shrink the block below that."""
        return (n - 1) * self.lh * fs + max(self.lh, 1.27) * fs

    def layout(self, fs: float, max_w: float) -> tuple[int, float]:
        key = (round(fs, 4), round(max_w, 3))
        r = self._c.get(key)
        if r is not None:
            return r
        adv, P, PD, text = self.adv, self.P, self.PD, self.text
        ls = self.ls0 * fs / self.f
        lw, last = 0.0, -1
        cuts = []
        for i, ch in enumerate(text):
            if ch == "\n":
                cuts.append(i)
                lw, last = 0.0, -1
                continue
            w = adv[i] * fs
            if lw + w > max_w and last >= 0:
                cuts.append(last)
                lw = (P[i + 1] - P[last + 1]) * fs
                last = -1
            else:
                lw += w
            if ch == " ":
                last = i
        cuts = sorted(set(cuts))
        a, widest = 0, 0.0
        for b in cuts + [len(text)]:
            widest = max(widest, (PD[b] - PD[a]) * fs + ls * (b - a))
            a = b + 1
        r = (len(cuts) + 1, widest)
        self._c[key] = r
        return r

    def fit(self, fs: float, max_w: float, max_h: float | None = None,
            min_fs: float = 0.0) -> tuple[float, int, float]:
        """Shrink fs until the widest line fits max_w (always) and the block
        fits max_h (down to min_fs). Returns (fs, lines, text_height)."""
        fs = max(1.0, fs)
        n, w = self.layout(fs, max_w)
        for _ in range(80):
            if w > max_w * 1.0005 + 0.01 and fs > 1.0:
                fs = max(1.0, fs * max(0.5, 0.995 * max_w / w))
            elif self.max_lines and n > self.max_lines and fs > 1.0:  # a hard cap: the search sees the smaller font
                fs = max(1.0, fs * 0.96)
            elif max_h is not None and self.height(n, fs) > max_h + 0.01 and fs > max(min_fs, 1.0):
                fs = max(min_fs, 1.0, fs * 0.95)
            else:
                break
            n, w = self.layout(fs, max_w)
        # Sizes are rounded down to 0.01px on output; measure at exactly that size.
        fs = max(1.0, math.floor(fs * 100) / 100)
        n, w = self.layout(fs, max_w)
        while w > max_w * 1.0005 + 0.01 and fs > 1.0:
            fs = max(1.0, round(fs - 0.01, 2))
            n, w = self.layout(fs, max_w)
        if getattr(_tls, "exact", False) and self.text.strip():
            # Final answer: confirm with the renderer's own measurement; if it would
            # wrap onto more lines (or overflow) than we predicted, step down.
            for _ in range(14):
                r = self._exact(fs, max_w)
                if r is None or (r[0] <= n and r[1] <= max_w * 1.0005 + 0.01):
                    break
                fs = max(1.0, math.floor(fs * 0.995 * 100) / 100)
                n, w = self.layout(fs, max_w)
        return fs, n, self.height(n, fs)

    def _exact(self, fs: float, max_w: float):
        widths = _exact_widths(self.el, fs, self.text)
        if widths is None or len(widths) != len(self.text):
            return None
        lw, last, cuts = 0.0, -1, []
        for i, ch in enumerate(self.text):
            if ch == "\n":
                cuts.append(i)
                lw, last = 0.0, -1
                continue
            w = widths[i]
            if lw + w > max_w and last >= 0:
                cuts.append(last)
                lw = sum(widths[j] for j in range(last + 1, i + 1) if self.text[j] != "\n")
                last = -1
            else:
                lw += w
            if ch == " ":
                last = i
        cuts = sorted(set(cuts))
        a, widest = 0, 0.0
        ls = self.ls0 * fs / self.f
        for b in cuts + [len(self.text)]:
            widest = max(widest, sum(widths[a:b]) + ls * (b - a))
            a = b + 1
        return len(cuts) + 1, widest


# --------------------------------------------------------------------------
# Small geometry helpers
# --------------------------------------------------------------------------

def _rot(e: dict) -> float:
    return float(e.get("rotation") or 0.0)


def _fp(w: float, h: float, rot: float) -> tuple[float, float]:
    """Footprint of a rotated box: big enough to hold both the rotated outline
    and the unrotated box, so either way of checking "inside the canvas" passes."""
    r = math.radians(rot)
    c, s = abs(math.cos(r)), abs(math.sin(r))
    if not rot:
        return w, h
    return max(w, w * c + h * s), max(h, w * s + h * c)


def _inter(a: tuple, b: tuple) -> float:
    ix = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    iy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, ix) * max(0.0, iy)


def _box(e: dict) -> tuple[float, float, float, float]:
    return (float(e["x"]), float(e["y"]), float(e["width"]), float(e["height"]))


def _area(b: tuple) -> float:
    return max(0.0, b[2]) * max(0.0, b[3])


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _pr(prio: float, s: float) -> float:
    """Priority factor: below s~0.5 (tight canvas) minor pieces shrink faster
    than the headline, down to `prio` of the uniform scale."""
    return 1.0 - (1.0 - prio) * _clamp((0.5 - s) / 0.25, 0.0, 1.0)


def _floor_px(f: float, ff: tuple) -> float:
    """Smallest font size we are willing to render for a source size f.
    ff = (absolute px floor, fraction of the source size). The absolute floor
    keeps small print readable; the fraction keeps the hierarchy from collapsing.
    The search relaxes the fraction first and the absolute floor last."""
    return max(ff[0], ff[1] * f)


def _legib(minfs: float) -> float:
    """Score factor for the smallest rendered font: below ~8px text stops being
    readable in the rendered PNG, so this falls off quickly."""
    return min(1.0, minfs / 8.0) ** 1.5


_native_cache: dict[str, Any] = {}


def _native_size(asset_id: Any):
    key = str(asset_id)
    if key not in _native_cache:
        size = None
        try:
            from PIL import Image
            with Image.open(_ASSET_DIR / key) as im:
                size = im.size
        except Exception:
            size = None
        _native_cache[key] = size
    return _native_cache[key]


_ink_cache: dict[str, Any] = {}


def _ink_box(e: dict) -> tuple[float, float, float, float]:
    """The part of an image element that actually has pixels: for a contain-fit
    asset with transparent padding (bottle, wordmark) this is its alpha bounding
    box, mapped into the element's box. Anything else is the box itself."""
    b = _box(e)
    if e.get("type") != "image" or e.get("fit") not in (None, "contain") or _rot(e):
        return b
    key = str(e.get("asset_id"))
    if key not in _ink_cache:
        frac = None
        try:
            from PIL import Image
            with Image.open(_ASSET_DIR / key) as im:
                if im.mode == "RGBA":
                    bb, (iw, ih) = im.split()[-1].getbbox(), im.size
                    if bb:
                        frac = (bb[0] / iw, bb[1] / ih, (bb[2] - bb[0]) / iw, (bb[3] - bb[1]) / ih)
        except Exception:
            frac = None
        _ink_cache[key] = frac
    frac = _ink_cache[key]
    if not frac or b[2] <= 0 or b[3] <= 0:
        return b
    size = _native_size(key)
    if not size:
        return b
    k = min(b[2] / size[0], b[3] / size[1])  # contain: image centred in the box
    iw, ih = size[0] * k, size[1] * k
    ox, oy = b[0] + (b[2] - iw) / 2, b[1] + (b[3] - ih) / 2
    return (ox + frac[0] * iw, oy + frac[1] * ih, frac[2] * iw, frac[3] * ih)


def _native_cap(el: dict):
    """Largest scale at which a picture is still <=1.25x its native pixels
    (we may move and resize pictures but cannot add detail)."""
    if el.get("type") != "image" or not el.get("asset_id"):
        return None
    size = _native_size(el["asset_id"])
    w, h = float(el.get("width", 0)), float(el.get("height", 0))
    if not size or w <= 0 or h <= 0:
        return None
    ratios = (w / size[0], h / size[1])
    d0 = max(ratios) if el.get("fit") in ("cover", "fill") else min(ratios)
    return 1.25 / d0 if d0 > 0 else None


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------

class _Unit:
    kind = "unit"

    def __init__(self):
        self.boost = 1.0
        self.ff = (6.0, 0.4)
        self.is_hero = False
        self.min_w = 0.0    # legibility floor (px) for logo-like pictures
        self.min_side = 0.0  # the hero picture's longer side never drops below this (px)
        self.prio = 1.0     # <1: minor text that yields space to the headline when tight
        self.cap_w = None   # shared width cap so sibling units end up the same size
        self.sib = None     # list of sibling units (similar widgets)
        self.x0 = self.y0 = self.w = self.h = 0.0

    @property
    def cy(self) -> float:
        return self.y0 + self.h / 2

    @property
    def cx(self) -> float:
        return self.x0 + self.w / 2

    def has_text(self) -> bool:
        return False


class _ImageUnit(_Unit):
    kind = "image"

    def __init__(self, el: dict):
        super().__init__()
        self.el = el
        b = _box(el)
        self.rot = _rot(el)
        self.fw, self.fh = _fp(b[2], b[3], self.rot)
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        self.x0, self.y0, self.w, self.h = cx - self.fw / 2, cy - self.fh / 2, self.fw, self.fh
        self.wmin = self.fw
        self.g_native = _native_cap(el)

    def measure(self, s: float, cw: float) -> dict:
        pr = _pr(self.prio, s)  # pictures yield to text when tight; the hero grows back if room is left
        g = max(s * self.boost * pr, self.min_w * min(1.0, self.ff[0] / 6.0) / max(self.fw, 1e-6),
                self.min_side * (self.ff[0] >= 7.0) / max(self.fw, self.fh, 1e-6))  # yields once text is below 7px
        g = min(g, cw / max(self.fw, 1e-6))
        if self.g_native:
            g = min(g, self.g_native)
        return {"w": self.fw * g, "h": self.fh * g, "g": g, "minfs": 1e9}

    def floor_g(self) -> float:
        return 0.0

    def min_src_font(self) -> float:
        return 1e9

    def emit(self, g: float, ox: float, oy: float, wchip: float | None = None,
             hchip: float | None = None) -> list[dict]:
        e = dict(self.el)
        w, h = float(self.el["width"]) * g, float(self.el["height"]) * g
        e.update(x=ox + self.fw * g / 2 - w / 2, y=oy + self.fh * g / 2 - h / 2, width=w, height=h)
        return [e]

    def place(self, m: dict, x0: float, x1: float, top: float) -> list[dict]:
        g = m["g"]
        e = dict(self.el)
        w, h = float(self.el["width"]) * g, float(self.el["height"]) * g
        cx = (x0 + x1) / 2
        e.update(x=cx - w / 2, y=top + m["h"] / 2 - h / 2, width=w, height=h)
        return [e]


class _TextUnit(_Unit):
    kind = "text"

    def __init__(self, el: dict):
        super().__init__()
        self.el = el
        self.t = _Txt(el)
        self.x0, self.y0, self.w, self.h = _box(el)

    def has_text(self) -> bool:
        return True

    def measure(self, s: float, cw: float) -> dict:
        # When everything must shrink hard, minor copy (legal line, small print)
        # gives up more so the headline and call to action stay readable.
        pr = _pr(self.prio, s)
        fs = max(self.t.f * s * self.boost * pr, _floor_px(self.t.f, self.ff))  # the floor never yields
        maxw = min(cw, max(self.w * s * 1.6, 0.7 * cw))
        fs, n, th = self.t.fit(fs, maxw)
        return {"w": maxw, "h": th, "fs": fs, "minfs": fs, "n": n, "chars": len(self.t.text.strip()),
                "big": (self.t.f, fs)}

    def place(self, m: dict, x0: float, x1: float, top: float) -> list[dict]:
        e = dict(self.el)
        bw = m["w"]
        align = e.get("text_align", "left")
        if align == "center":
            x = (x0 + x1) / 2 - bw / 2
        elif align == "right":
            x = x1 - bw
        else:
            x = x0
        e.update(x=x, y=top, width=bw, height=m["h"], font_size=m["fs"])
        if e.get("letter_spacing"):
            e["letter_spacing"] = float(e["letter_spacing"]) * m["fs"] / self.t.f
        return [e]


class _Cluster(_Unit):
    """Rigid bundle of elements that keeps its internal arrangement."""
    kind = "cluster"

    def __init__(self, els: list[dict]):
        super().__init__()
        self.els = els
        xs0, ys0, xs1, ys1 = [], [], [], []
        self.floor_f = 0.0  # largest (floor / source font size) over text members
        self.txt: dict[str, _Txt] = {}
        self.src_fit: dict[str, bool] = {}
        for e in els:
            x, y, w, h = _box(e)
            fw, fh = _fp(w, h, _rot(e))
            cx, cy = x + w / 2, y + h / 2
            xs0.append(cx - fw / 2); ys0.append(cy - fh / 2)
            xs1.append(cx + fw / 2); ys1.append(cy + fh / 2)
            if e["type"] == "text":
                t = _Txt(e)
                self.txt[e["id"]] = t
                n, _ = t.layout(t.f, w)
                self.src_fit[e["id"]] = n * t.f * t.lh <= h * 1.08
        self.x0, self.y0 = min(xs0), min(ys0)
        self.w, self.h = max(xs1) - self.x0, max(ys1) - self.y0
        self.has_rot = any(_rot(e) for e in els)
        caps = [c for c in (_native_cap(e) for e in els) if c]
        self.g_native = min(caps) if caps else None
        # A pill/badge: one rectangle with one centred single-line label. Its
        # shape can be tightened to the label, so it stays legible in narrow spaces.
        self.chip = False
        self.wmin, self.hmin = self.w, self.h
        shapes = [e for e in els if e["type"] == "shape"]
        texts = [e for e in els if e["type"] == "text"]
        if len(els) == 2 and len(shapes) == 1 and len(texts) == 1 and not self.has_rot \
                and shapes[0].get("shape_type") == "rectangle" and texts[0].get("text_align") == "center":
            sx_, sy_, sw_, sh_ = _box(shapes[0])
            tx_, ty_, tw_, th_ = _box(texts[0])
            t = self.txt[texts[0]["id"]]
            n1, one_line = t.layout(t.f, 1e9)
            if n1 == 1 and sx_ <= tx_ + tw_ / 2 <= sx_ + sw_ and sy_ <= ty_ + th_ / 2 <= sy_ + sh_:
                pad = max(0.0, (sw_ - one_line) / 2)
                self.wmin = min(self.w, one_line + 2 * min(pad, 0.5 * sh_) + 1.0)
                pad_v = max(0.0, (sh_ - 1.2 * t.f) / 2)
                self.hmin = min(self.h, 1.27 * t.f + 2 * min(pad_v, 0.25 * t.f) + 1.0)
                self.chip = self.wmin < self.w - 1.0 or self.hmin < self.h - 1.0

    def has_text(self) -> bool:
        return bool(self.txt)

    def min_src_font(self) -> float:
        fs = [float(e.get("font_size", 16)) for e in self.els if e["type"] == "text"]
        return min(fs) if fs else 1e9

    def floor_g(self) -> float:
        g = 0.0
        for e in self.els:
            if e["type"] == "text":
                f = float(e.get("font_size", 16))
                g = max(g, _floor_px(f, self.ff) / f)
        return g

    def scale_for(self, s: float, cw: float) -> float:
        g = max(s * self.boost, self.floor_g())
        g = min(g, cw / max(self.wmin, 1e-6))
        if self.g_native:
            g = min(g, self.g_native)
        return g

    def width_at(self, g: float, cw: float | None = None, tight: bool = False) -> float:
        if not self.chip:
            return self.w * g
        if tight:
            return self.wmin * g
        return self.w * g if cw is None else max(self.wmin * g, min(self.w * g, cw))

    def tight_at(self, s: float) -> bool:
        """A pill whose label is pinned at the legibility floor is in a cramped
        layout: its padding gives way (both directions) so the label can stay big."""
        return self.chip and self.floor_g() > s * self.boost + 1e-9

    def height_at(self, g: float, tight: bool = False) -> float:
        return (self.hmin if tight else self.h) * g

    def measure(self, s: float, cw: float) -> dict:
        g = self.scale_for(s, cw)
        tight = self.tight_at(s)
        return {"w": self.width_at(g, cw, tight), "h": self.height_at(g, tight), "g": g,
                "minfs": g * self.min_src_font(), "tight": tight,
                "big": (max(t.f for t in self.txt.values()), g * max(t.f for t in self.txt.values())) if self.txt else None}

    def emit(self, g: float, ox: float, oy: float, wchip: float | None = None,
             hchip: float | None = None) -> list[dict]:
        shapes = [e for e in self.els if e["type"] == "shape"]
        hx = hy = 1.0
        if self.chip and wchip is not None and self.w * g > 0:
            hx = max(0.05, min(1.0, wchip / (self.w * g)))
        if self.chip and hchip is not None and self.h * g > 0:
            hy = max(0.05, min(1.0, hchip / (self.h * g)))
        out = []
        for e in self.els:
            x, y, w, h = _box(e)
            ncx = ox + (x + w / 2 - self.x0) * g * hx
            ncy = oy + (y + h / 2 - self.y0) * g * hy
            nw, nh = w * g * hx, h * g * hy
            n = dict(e)
            if e["type"] == "text":
                nh = h * g  # a tightened pill moves its label but never squashes it
                t = self.txt[e["id"]]
                fs0 = t.f * g
                fs, lines, th = t.fit(fs0, nw, nh * 1.08 if self.src_fit[e["id"]] else None,
                                      min_fs=0.6 * fs0)
                n["font_size"] = fs
                if e.get("letter_spacing"):
                    n["letter_spacing"] = float(e["letter_spacing"]) * fs / t.f
                top = ncy - nh / 2
                # Label sitting centred on a shape (button, badge): centre the
                # text block on the shape so it does not hug the top edge.
                for sh in shapes if len(self.txt) == 1 else []:
                    sx, sy, sw, shh = _box(sh)
                    if (sx <= x + w / 2 <= sx + sw and sy <= y + h / 2 <= sy + shh
                            and shh <= 3.0 * h  # a label-sized shape, not a panel
                            and abs((y + h / 2) - (sy + shh / 2)) < 0.25 * shh):
                        top = (oy + (sy + shh / 2 - self.y0) * g * hy) - th / 2
                        break
                nh = max(th, min(nh, th * 1.0)) if th > nh or top != ncy - nh / 2 else nh
                n.update(x=ncx - nw / 2, y=top, width=nw, height=nh)
            else:
                n.update(x=ncx - nw / 2, y=ncy - nh / 2, width=nw, height=nh)
                _scale_style(n, g)
            out.append(n)
        return out

    def place(self, m: dict, x0: float, x1: float, top: float) -> list[dict]:
        return self.emit(m["g"], (x0 + x1) / 2 - m["w"] / 2, top, m["w"] if self.chip else None,
                         m["h"] if self.chip else None)


def _beside(img: dict, txt: dict, canvas_area: float) -> bool:
    """A small picture with a text line starting right beside it (icon + label)."""
    if img["type"] != "image" or txt["type"] != "text" or _rot(img) or _rot(txt):
        return False
    if _area(_box(img)) > 0.03 * canvas_area:
        return False
    ax, ay, aw, ah = _box(img)
    bx, by, bw, bh = _box(txt)
    beside = 0 <= bx - (ax + aw) <= max(0.6 * aw, 0.03 * math.sqrt(canvas_area))
    return beside and ay - 0.25 * ah <= by + bh / 2 <= ay + 1.25 * ah and bh <= 2.5 * ah


class _IconLabel(_Unit):
    """A small picture with its label beside it: the icon scales, the label
    re-wraps in whatever width is left, both stay vertically centred."""
    kind = "iconlabel"

    def __init__(self, img: dict, txt: dict):
        super().__init__()
        self.img, self.el = img, txt
        self.t = _Txt(txt)
        ix, iy, iw, ih = _box(img)
        tx, ty, tw, th = _box(txt)
        self.iw, self.ih = iw, ih
        self.gap_src = max(2.0, tx - (ix + iw))
        self.x0, self.y0 = ix, min(iy, ty)
        self.w, self.h = tx + tw - ix, max(iy + ih, ty + th) - self.y0
        self.g_native = _native_cap(img)

    def has_text(self) -> bool:
        return True

    def floor_g(self) -> float:
        return _floor_px(self.t.f, self.ff) / self.t.f

    def min_src_font(self) -> float:
        return self.t.f

    def measure(self, s: float, cw: float) -> dict:
        g = max(s * self.boost, self.floor_g())
        if self.g_native:
            g = min(g, self.g_native)
        g = min(g, 0.4 * cw / max(self.iw, 1e-6))  # the icon never takes more than 40% of the row
        fs = max(self.t.f * s * self.boost, _floor_px(self.t.f, self.ff))
        gap = self.gap_src * g
        tw = max(1.0, cw - self.iw * g - gap)
        fs, n, th = self.t.fit(fs, tw)
        return {"w": cw, "h": max(self.ih * g, th), "g": g, "fs": fs, "n": n, "th": th, "tw": tw,
                "minfs": fs, "big": (self.t.f, fs)}

    def place(self, m: dict, x0: float, x1: float, top: float) -> list[dict]:
        g = m["g"]
        iw, ih = self.iw * g, self.ih * g
        icon = dict(self.img)
        icon.update(x=x0, y=top + (m["h"] - ih) / 2, width=iw, height=ih)
        lab = dict(self.el)
        lab.update(x=x0 + iw + self.gap_src * g, y=top + (m["h"] - m["th"]) / 2, width=m["tw"], height=m["th"],
                   font_size=m["fs"])
        if lab.get("letter_spacing"):
            lab["letter_spacing"] = float(lab["letter_spacing"]) * m["fs"] / self.t.f
        return [icon, lab]


class _RowFlow(_Unit):
    """Several similar chips (badges...) that sit on one row; wraps if narrow."""
    kind = "row"

    def __init__(self, kids: list):
        super().__init__()
        self.kids = sorted(kids, key=lambda k: k.x0)
        self.x0 = min(k.x0 for k in kids)
        self.y0 = min(k.y0 for k in kids)
        self.w = max(k.x0 + k.w for k in kids) - self.x0
        self.h = max(k.y0 + k.h for k in kids) - self.y0
        gaps = [b.x0 - (a.x0 + a.w) for a, b in zip(self.kids, self.kids[1:])]
        self.gap_src = max(2.0, sorted(gaps)[len(gaps) // 2]) if gaps else 8.0

    def has_text(self) -> bool:
        return True

    @staticmethod
    def _kw(k, g: float, tight: bool) -> float:
        return (k.wmin if tight else k.w) * g

    @staticmethod
    def _kh(k, g: float, tight: bool) -> float:
        return (getattr(k, "hmin", k.h) if tight else k.h) * g

    def _flow(self, g: float, cw: float, tight: bool) -> list[list]:
        gap = self.gap_src * g
        lines, cur, curw = [], [], 0.0
        for k in self.kids:
            kw = self._kw(k, g, tight)
            if cur and curw + gap + kw > cw + 0.01:
                lines.append(cur)
                cur, curw = [], 0.0
            curw = curw + (gap if cur else 0.0) + kw
            cur.append(k)
        if cur:
            lines.append(cur)
        return lines

    def measure(self, s: float, cw: float) -> dict:
        for k in self.kids:
            k.ff = self.ff
        g = max(s * self.boost, max(k.floor_g() for k in self.kids))
        g = min(g, cw / max(k.wmin for k in self.kids))
        natives = [k.g_native for k in self.kids if getattr(k, "g_native", None)]
        if natives:
            g = min(g, min(natives))
        lines = self._flow(g, cw, False)
        tight = False
        if any(getattr(k, "chip", False) for k in self.kids):
            gap0 = self.gap_src * g
            too_wide = any(sum(self._kw(k, g, False) for k in ln) + gap0 * (len(ln) - 1) > cw + 0.01
                           for ln in lines)
            t_lines = self._flow(g, cw, True)
            pinned = any(k.tight_at(s) for k in self.kids if isinstance(k, _Cluster))
            if too_wide or pinned or len(t_lines) < len(lines):
                lines, tight = t_lines, True
        gap = self.gap_src * g
        h = sum(max(self._kh(k, g, tight) for k in ln) for ln in lines) + gap * 0.8 * (len(lines) - 1)
        w = max(sum(self._kw(k, g, tight) for k in ln) + gap * (len(ln) - 1) for ln in lines)
        return {"w": w, "h": h, "g": g, "lines": lines, "tight": tight,
                "minfs": g * min(k.min_src_font() for k in self.kids),
                "big": max((k.measure(s, cw).get("big") for k in self.kids if isinstance(k, _Cluster)),
                           key=lambda b: b[0] if b else 0.0, default=None)}

    def place(self, m: dict, x0: float, x1: float, top: float) -> list[dict]:
        g, gap, tight = m["g"], self.gap_src * m["g"], m["tight"]
        out, y = [], top
        for ln in m["lines"]:
            lw = sum(self._kw(k, g, tight) for k in ln) + gap * (len(ln) - 1)
            lh = max(self._kh(k, g, tight) for k in ln)
            x = (x0 + x1) / 2 - lw / 2
            for k in ln:
                kw, kh = self._kw(k, g, tight), self._kh(k, g, tight)
                out += k.emit(g, x, y + (lh - kh) / 2, kw if tight else None, kh if tight else None)
                x += kw + gap
            y += lh + gap * 0.8
        return out


# --------------------------------------------------------------------------
# Column layout search
# --------------------------------------------------------------------------

class _Geo:
    def __init__(self, W: int, H: int, Ws: int, Hs: int):
        self.W, self.H = W, H
        self.Ws, self.Hs = Ws, Hs
        m = _clamp(0.05 * min(W, H), 4.0, 80.0)
        self.my = m
        self.mx = max(m, 0.03 * W)
        self.colgap = max(m, 0.02 * W)


def _gap(a: _Unit, b: _Unit, s: float) -> float:
    src = b.y0 - (a.y0 + a.h)
    gmin = max(2.0, 8.0 * s)
    return _clamp(src * s, gmin, max(gmin, 60.0 * s))


def _measure_col(col: list[_Unit], s: float, cw: float):
    ms = [u.measure(s, min(cw, u.cap_w) if u.cap_w else cw) for u in col]
    hs = sum(m["h"] for m in ms)
    gaps = [_gap(a, b, s) for a, b in zip(col, col[1:])]
    return ms, gaps, hs + sum(gaps)


def _feasible(cols, widths, s, hc) -> bool:
    return all(_measure_col(c, s, cw)[2] <= hc for c, cw in zip(cols, widths))


def _max_scale(cols, widths, hc, s_cap, iters: int = 18) -> float | None:
    if _feasible(cols, widths, s_cap, hc):
        return s_cap
    lo, hi = 0.01, s_cap
    if not _feasible(cols, widths, lo, hc):
        return None
    for _ in range(iters):
        mid = (lo + hi) / 2
        if _feasible(cols, widths, mid, hc):
            lo = mid
        else:
            hi = mid
    return lo


def _col_factor(k: int, kp: int, aspect: float = 1.0) -> float:
    """Tall canvases stay single-column; elsewhere extra columns must earn it.
    The penalty grows with tallness: a story (0.56) loses ~38% per extra
    column, a square ~24%, a 300x250 (1.2) ~18%."""
    if kp == 1:
        return 1.0 - (k - 1) * (0.15 + 0.25 * _clamp((1.3 - aspect) / 0.8, 0.0, 1.0))
    return (1.0 - 0.04 * (k - 1)) * (1.0 - 0.015 * max(0, kp - k))


def _k_pref(W: int, H: int) -> int:
    a = W / H
    if a < 1.3:
        return 1
    if a < 2.4:
        return 2
    if a < 4.5:
        return 3
    if a < 7:
        return 4
    return 5


def _partitions(n: int, k: int):
    for cuts in itertools.combinations(range(1, n), k - 1):
        yield (0,) + cuts + (n,)


def _width_recipes(cols: list[list[_Unit]]) -> list[list[float]]:
    nat = [max(u.w for u in c) for c in cols]
    area = [math.sqrt(sum(u.w * u.h for u in c)) for c in cols]
    base = [[1.0] * len(cols), nat, area]
    has_hero = [any(u.is_hero for u in c) for c in cols]
    if not any(has_hero) or len(cols) == 1:
        return base
    return base + [[r * (1.5 if h else 1.0) for r, h in zip(rec, has_hero)] for rec in base]


def _hero_boost(col: list[_Unit], s: float, cw: float, hc: float) -> float:
    """Largest multiplier (<=1.8) the hero picture can take in this column."""
    heroes = [u for u in col if u.is_hero]
    if not heroes:
        return 1.0
    old = [u.boost for u in heroes]
    lo, hi = 1.0, 1.8 / _pr(heroes[0].prio, s)  # may grow back what the priority took away
    for u in heroes:
        u.boost = 1.0
    if _measure_col(col, s, cw)[2] > hc:
        return 1.0
    for _ in range(12):
        mid = (lo + hi) / 2
        for u in heroes:
            u.boost = mid
        if _measure_col(col, s, cw)[2] <= hc:
            lo = mid
        else:
            hi = mid
    for u, b in zip(heroes, old):
        u.boost = b
    return lo


def _similar(a: _Unit, b: _Unit) -> bool:
    if type(a) is not type(b) or isinstance(a, _RowFlow):
        return False
    close = lambda p, q, tol: abs(p / max(q, 1e-6) - 1) <= tol
    if isinstance(a, _TextUnit):
        return (close(a.t.f, b.t.f, 0.04) and a.el.get("font_family") == b.el.get("font_family")
                and a.el.get("font_weight") == b.el.get("font_weight"))
    if not (close(a.w, b.w, 0.12) and close(a.h, b.h, 0.12)):
        return False
    if isinstance(a, _Cluster):
        return sorted(e["type"] for e in a.els) == sorted(e["type"] for e in b.els)
    return True


def _link_siblings(units: list[_Unit]) -> None:
    groups: list[list[_Unit]] = []
    for u in units:
        for g in groups:
            if _similar(g[0], u):
                g.append(u)
                break
        else:
            groups.append([u])
    for g in groups:
        for u in g:
            u.sib = g if len(g) > 1 else None


def _apply_caps(units: list[_Unit], cols: list[list[_Unit]], widths: list[float]) -> None:
    """Siblings that land in different columns share the narrowest column's width."""
    where = {id(u): cw for c, cw in zip(cols, widths) for u in c}
    for u in units:
        u.cap_w = None
    for u in units:
        if u.sib and u.cap_w is None:
            cws = [where[id(m)] for m in u.sib if id(m) in where]
            if cws and max(cws) - min(cws) > 1e-6:
                for m in u.sib:
                    m.cap_w = min(cws)


def _too_narrow(u: _Unit, cw: float) -> bool:
    """True if a rigid unit cannot keep its text at the floor size in this column."""
    if isinstance(u, _Cluster):
        return u.floor_g() * u.wmin > cw
    if isinstance(u, _RowFlow):
        return any(k.floor_g() * k.wmin > cw for k in u.kids)
    return False


def _stats(cols, widths, s: float) -> tuple[float, float]:
    """(smallest rendered font, effective scale). When text is pinned at the
    floor the raw s undersells the layout: the scale that counts is the one
    the text actually got."""
    ms = [m for c, cw in zip(cols, widths) for m in _measure_col(c, s, cw)[0]]
    minfs = min(m["minfs"] for m in ms)
    bigs = [m["big"] for m in ms if m.get("big")]
    if not bigs:
        return minfs, s
    f, fs = max(bigs, key=lambda b: b[0])  # the largest source text: its achieved scale is the one that counts
    return minfs, max(s, fs / f)


def _measure_factor(cols, widths, s: float) -> float:
    """Penalise headline-like copy squeezed into a narrow column (many short
    lines is hard to read and looks like a layout accident)."""
    items = []
    for c, cw in zip(cols, widths):
        for u, m in zip(c, _measure_col(c, s, cw)[0]):
            if isinstance(u, _TextUnit) and m.get("chars", 0) >= 12:
                items.append((u.t.f, m["n"], m["chars"]))
    f = 1.0
    for _, n, chars in sorted(items, reverse=True)[:2]:
        if n > 3:
            f *= 1.0 / (1.0 + 0.07 * (n - 3))
        if chars / n < 12:
            f *= 0.85
    return f


def _set_ff(units: list[_Unit], ff: tuple, geo: "_Geo | None" = None) -> None:
    for u in units:
        u.ff = ff
        if geo is not None and isinstance(u, _Cluster) and u.txt and not u.has_rot \
                and any(e["type"] == "image" for e in u.els):
            # Text on a picture whose small print would need it bigger than the canvas (a price
            # bubble on a product, on a 90px banner) gets a floor it can meet, so it does
            # not drag every other text down to no floor at all.
            gmax = 0.9 * min((geo.H - 2 * geo.my) / max(u.h, 1e-6), (geo.W - 2 * geo.mx) / max(u.w, 1e-6))
            fg = u.floor_g()
            if fg > gmax:
                u.ff = (ff[0] * gmax / fg, ff[1] * gmax / fg)
        if isinstance(u, _RowFlow):
            for k in u.kids:
                k.ff = ff


def _search(units: list[_Unit], geo: _Geo, s_cap: float):
    """Two stages: a cheap coarse pass over many column splits, then an exact
    evaluation (scale, hero growth, legibility) of the best few."""
    W, H = geo.W, geo.H
    n = len(units)
    kp = _k_pref(W, H)
    hc = H - 2 * geo.my
    ks = list(range(1, min(6 if W / H >= 5 else 4, n) + 1))
    # (absolute px floor, fraction of source size): hierarchy relaxes before legibility.
    levels = ((7.0, 0.4), (7.0, 0.3), (7.0, 0.2), (7.0, 0.1), (7.0, 0.0), (6.0, 0.0), (5.0, 0.0), (4.0, 0.0), (0.0, 0.0)) \
        if n <= 14 else ((7.0, 0.4), (7.0, 0.2), (7.0, 0.0), (5.0, 0.0), (0.0, 0.0))
    cap_parts = 120 if n <= 14 else max(3, 240 // (3 * len(ks)))
    coarse = []
    for ff in levels:
        _set_ff(units, ff, geo)
        level_best = 0.0
        for k in ks:
            parts = list(_partitions(n, k))
            if len(parts) > cap_parts:
                parts = parts[:: max(1, len(parts) // cap_parts)]
            for p in parts:
                cols = [units[p[i]:p[i + 1]] for i in range(k)]
                avail = W - 2 * geo.mx - (k - 1) * geo.colgap
                for rec in _width_recipes(cols):
                    tot = sum(rec)
                    widths = [avail * r / tot for r in rec]
                    if k > 1 and min(widths) < min(0.16, 0.7 / k) * W:
                        continue
                    if ff != (0.0, 0.0) and any(_too_narrow(u, cw) for c, cw in zip(cols, widths) for u in c):
                        continue
                    _apply_caps(units, cols, widths)
                    s = _max_scale(cols, widths, hc, s_cap, 8)
                    if s is None:
                        continue
                    level_best = max(level_best, s)
                    minfs, s_eff = _stats(cols, widths, s)
                    coarse.append((s_eff * _col_factor(k, kp, W / H) * _legib(minfs), cols, widths, ff))
        if level_best >= 0.6 * s_cap:
            break
    coarse.sort(key=lambda c: -c[0])
    best = None
    for _, cols, widths, ff in coarse[:8]:
        _set_ff(units, ff, geo)
        k = len(cols)
        _apply_caps(units, cols, widths)
        s = _max_scale(cols, widths, hc, s_cap, 18)
        if s is None:
            continue
        hb = max(_hero_boost(c, s, cw, hc) for c, cw in zip(cols, widths))
        minfs, s_eff = _stats(cols, widths, s)
        score = _measure_factor(cols, widths, s) * s_eff * _legib(minfs) * _col_factor(k, kp, W / H) * (1.0 + 0.12 * (hb - 1.0))
        if best is None or score > best[0] + 1e-9:
            best = (score, s, cols, widths, ff)
    return best


def _side_aligned(u: _Unit, col: list[_Unit], geo: _Geo):
    """A picture/widget that shared a left (or right) edge with the column's
    left- (right-) aligned text in the source keeps that shared edge."""
    if isinstance(u, _TextUnit):
        return None
    texts = [t for t in col if isinstance(t, _TextUnit)]
    if not texts:
        return None
    tol = 0.03 * geo.Ws
    if all(t.el.get("text_align", "left") == "left" for t in texts):
        ref = sorted(t.x0 for t in texts)[len(texts) // 2]
        if abs(u.x0 - ref) <= tol:
            return "left"
    if all(t.el.get("text_align") == "right" for t in texts):
        ref = sorted(t.x0 + t.w for t in texts)[len(texts) // 2]
        if abs(u.x0 + u.w - ref) <= tol:
            return "right"
    return None


_HERO_MIN = 20.0  # px: a product shot smaller than this is a smudge, not a picture


def _hero_side(units: list[_Unit], best, geo: _Geo) -> float | None:
    """Longer side of the hero picture as the chosen layout would place it
    (after it grows into leftover room), or None if there is no free hero."""
    hero = next((u for u in units if isinstance(u, _ImageUnit) and u.is_hero), None)
    if hero is None or best is None:
        return None
    _, s, cols, widths, ff = best
    _set_ff(units, ff, geo)
    _apply_caps(units, cols, widths)
    for c, cw in zip(cols, widths):
        if hero in c:
            hero.boost = 1.0
            hero.boost = _hero_boost(c, s, cw, geo.H - 2 * geo.my)
            m = hero.measure(s, min(cw, hero.cap_w) if hero.cap_w else cw)
            hero.boost = 1.0
            return max(m["w"], m["h"])
    return None


def _layout(units: list[_Unit], geo: _Geo, s_cap: float):
    """Returns (placed elements, occupied rects [(x,y,w,h,weight)])."""
    best = _search(units, geo, s_cap)
    if best is None:
        return None
    side = _hero_side(units, best, geo)
    if side is not None and side < _HERO_MIN:
        # The hero shrank to a smudge. Try again with it held at a visible size
        # and keep whichever layout scores better once invisibility is charged.
        hero = next(u for u in units if isinstance(u, _ImageUnit) and u.is_hero)
        hero.min_side = _HERO_MIN
        alt = _search(units, geo, s_cap)
        penalised = best[0] * (max(side, 1.0) / _HERO_MIN) ** 0.3
        if alt is not None and alt[0] > penalised:
            best = alt
        else:
            hero.min_side = 0.0
    score, s, cols, widths, ff = best
    _tls.exact = True
    _apply_caps(units, cols, widths)
    _set_ff(units, ff, geo)
    W, H = geo.W, geo.H
    hc = H - 2 * geo.my
    out, occ = [], []
    x = geo.mx
    for col, cw in zip(cols, widths):
        # Grow the hero picture into whatever vertical room is left.
        for u in col:
            u.boost = 1.0
        hb = _hero_boost(col, s, cw, hc)
        for u in col:
            if u.is_hero:
                u.boost = hb
        ms, gaps, total = _measure_col(col, s, cw)
        # Keep the composition's vertical extent proportional to the source's
        # (logo high, call-to-action low), nudged halfway toward centred.
        src_top = min(u.y0 for u in col) / geo.Hs
        src_bot = max(u.y0 + u.h for u in col) / geo.Hs
        span = _clamp((src_bot - src_top) * H, total, hc)
        centre = 0.5 * ((src_top + src_bot) / 2 * H) + 0.5 * (H / 2)
        top = _clamp(centre - span / 2, geo.my, H - geo.my - span)
        if gaps and span > total:
            wts = [max(6.0, b.y0 - (a.y0 + a.h)) for a, b in zip(col, col[1:])]
            tw = sum(wts)
            gaps = [g + (span - total) * w / tw for g, w in zip(gaps, wts)]
        y = top
        for i, (u, m) in enumerate(zip(col, ms)):
            ax0, ax1 = x, x + cw
            side = _side_aligned(u, col, geo)
            if side == "left":
                ax1 = x + m["w"]
            elif side == "right":
                ax0 = x + cw - m["w"]
            els = u.place(m, ax0, ax1, y) if not isinstance(u, _TextUnit) else u.place(m, x, x + cw, y)
            out += els
            for e in els:
                if e["type"] != "shape" or float(e.get("opacity", 1)) >= 0.5:
                    wgt = 1.0 if e["type"] == "text" else (0.5 if e["type"] == "shape" else 0.15)
                    bx, by, bw, bh = _ink_box(e)  # transparent padding of a picture is free space
                    fw, fh = _fp(bw, bh, _rot(e))  # rotated boxes occupy their outline
                    cx, cy = bx + bw / 2, by + bh / 2
                    occ.append((cx - fw / 2, cy - fh / 2, fw, fh, wgt))
            y += m["h"] + (gaps[i] if i < len(gaps) else 0.0)
        x += cw + geo.colgap
    return out, occ, s, score


# --------------------------------------------------------------------------
# Decorations
# --------------------------------------------------------------------------

def _place_deco(deco: list[dict], src_wh: tuple[int, int], geo: _Geo,
                occ: list[tuple], kfix: float | None = None, label_floor: float = 6.0) -> list[dict]:
    """Park decoration (or, with kfix, corner-anchored pictures at that scale)
    where it collides least with what is already placed."""
    Ws, Hs = src_wh
    W, H = geo.W, geo.H
    sd = kfix if kfix is not None else min(W / Ws, H / Hs)
    shrinks = (1.0, 0.8, 0.65, 0.5, 0.35, 0.25, 0.18, 0.12) if kfix is not None else (1.0, 0.75, 0.55, 0.4, 0.25)
    placed = []
    for e in sorted(deco, key=lambda e: -(e.w * e.h if isinstance(e, _Cluster) else _area(_box(e)))):
        if isinstance(e, _Cluster):  # a corner sticker (ribbon, badge): its box already holds the rotation
            x, y, w, h, rot = e.x0, e.y0, e.w, e.h, 0.0
            kmin = max([label_floor / float(m.get("font_size", 16)) for m in e.els if m["type"] == "text"] + [0.0])
        else:
            x, y, w, h = _box(e)
            rot = _rot(e)
            kmin = 0.0
        fw0, fh0 = _fp(w, h, rot)
        fw0, fh0 = max(fw0, 1e-3), max(fh0, 1e-3)
        cx, cy = x + w / 2, y + h / 2
        # What actually occupies space, relative to the footprint's top-left: each
        # member's own rotated outline (a tilted ribbon fills only a band of its box).
        parts = [(0.0, 0.0, fw0, fh0)]
        if isinstance(e, _Cluster):
            parts = []
            for m in e.els:
                mx_, my_, mw_, mh_ = _box(m)
                pw, ph = _fp(mw_, mh_, _rot(m))
                parts.append((mx_ + mw_ / 2 - pw / 2 - x, my_ + mh_ / 2 - ph / 2 - y, pw, ph))
        p_area = max(1.0, sum(pw * ph for _, _, pw, ph in parts))
        best = None
        for shrink in shrinks:
            k = min(sd * shrink, 0.8 * W / fw0, 0.8 * H / fh0)
            if k < kmin:  # a label never drops below the legibility floor just to dodge overlap
                if best is not None:
                    break
                k = min(kmin, W / fw0, H / fh0)
            fw, fh = fw0 * k, fh0 * k
            cands = [(cx / Ws * W, cy / Hs * H)]
            qx = 0 if cx < Ws / 2 else 1
            qy = 0 if cy < Hs / 2 else 1
            ox = (cx if qx == 0 else Ws - cx) * sd * shrink
            oy = (cy if qy == 0 else Hs - cy) * sd * shrink
            cands.append((ox if qx == 0 else W - ox, oy if qy == 0 else H - oy))
            for px in (fw / 2, W - fw / 2):
                for py in (fh / 2, H - fh / 2):
                    cands.append((px, py))
            cands += [(W / 2, fh / 2), (W / 2, H - fh / 2), (fw / 2, H / 2), (W - fw / 2, H / 2)]
            m = geo.my
            mx = m if fw + 2 * m <= W else 0.0  # keep a little air from the canvas edge...
            my = m if fh + 2 * m <= H else 0.0
            for (px, py), air in itertools.product(cands, (1, 0.35, 0)):
                if air < 1 and not (mx or my):
                    continue
                px = _clamp(px, mx * air + fw / 2, W - mx * air - fw / 2)
                py = _clamp(py, my * air + fh / 2, H - my * air - fh / 2)
                rx, ry = px - fw / 2, py - fh / 2
                cost = sum(_inter((rx + qx_ * k, ry + qy_ * k, qw * k, qh * k), o[:4]) * (6.0 if kfix is not None else o[4])
                           for qx_, qy_, qw, qh in parts for o in occ) / max(1.0, p_area * k * k)
                # ...unless less air avoids a collision; flush to the edge reads as clipped,
                # so it costs more than a modest shrink.
                cost += 0.15 * (1 - shrink) + {1: 0.0, 0.35: 0.035, 0: 0.08}[air]
                if best is None or cost < best[0] - 1e-9:
                    best = (cost, px, py, k)
                if cost < 0.01 and shrink == 1.0:
                    break
            if best[0] < 0.01:
                break
        _, px, py, k = best
        occ = occ + [(px - fw0 * k / 2, py - fh0 * k / 2, fw0 * k, fh0 * k, 1.0)]  # later pieces avoid this one
        if isinstance(e, _Cluster):
            placed += e.emit(k, px - w * k / 2, py - h * k / 2)
            continue
        n = dict(e)
        n.update(width=w * k, height=h * k, x=px - w * k / 2, y=py - h * k / 2)
        placed.append(_scale_style(n, k))
    return placed


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _coverage(e: dict, Ws: int, Hs: int) -> float:
    return _inter(_box(e), (0, 0, Ws, Hs)) / (Ws * Hs)


def _asset_missing(e: dict) -> bool:
    aid = e.get("asset_id")
    return not aid or not (_ASSET_DIR / str(aid)).exists()


def _cluster(content: list[dict], panels: list[dict], use_groups: bool, overlaps: bool = True,
             skip_groups: frozenset = frozenset(), canvas_area: float = 1e12) -> list[list[dict]]:
    idx = {e["id"]: i for i, e in enumerate(content)}
    parent = list(range(len(content)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    if use_groups:
        by_group: dict[str, int] = {}
        for i, e in enumerate(content):
            g = e.get("group_id")
            if g and g not in skip_groups:
                if g in by_group:
                    union(i, by_group[g])
                else:
                    by_group[g] = i
    for sh in panels:
        if sh["id"] not in idx:
            continue
        sb = _box(sh)
        for c in content:
            if c is sh or c["type"] == "shape":
                continue
            cb = _box(c)
            if _inter(sb, cb) >= 0.7 * _area(cb):
                union(idx[sh["id"]], idx[c["id"]])
    # A small picture with a text line right beside it (icon + label row) is one widget.
    for i, a in enumerate(content):
        for j, b in enumerate(content):
            if _beside(a, b, canvas_area):
                union(i, j)
    for i, a in enumerate(content if overlaps else []):
        for j in range(i + 1, len(content)):
            b = content[j]
            ia = _inter(_box(a), _box(b))
            if ia <= 0:
                continue
            ta, tb = a["type"], b["type"]
            aa, ab = _area(_box(a)), _area(_box(b))
            if {ta, tb} == {"text", "image"}:
                t_area = aa if ta == "text" else ab
                if ia >= 0.5 * t_area:
                    union(i, j)
            elif ta == tb and ta != "shape" and ia >= (0.3 if ta == "image" else 0.5) * min(aa, ab):
                union(i, j)
    buckets: dict[int, list[dict]] = {}
    for i, e in enumerate(content):
        buckets.setdefault(find(i), []).append(e)
    return list(buckets.values())


def _spans(e: dict, axis: str, size: float) -> bool:
    lo = float(e[axis])
    hi = lo + float(e["width" if axis == "x" else "height"])
    return lo <= 0.02 * size and hi >= 0.98 * size


def _build_units(els: list[dict], Ws: int, Hs: int):
    bg, bands, deco, lost, content = [], [], [], [], []
    for e in els:
        if e.get("group_id"):
            content.append(e)  # a designer-grouped piece always travels with its group
            continue
        cov = _coverage(e, Ws, Hs)
        if (e["type"] == "image" and cov >= 0.85 and not _rot(e)) or \
           (e["type"] == "shape" and cov >= 0.9 and not _rot(e)):  # a full-canvas tint/scrim, at any opacity
            bg.append(e)
        elif e["type"] == "shape" and 0.3 <= cov < 0.9 and float(e.get("opacity", 1)) >= 0.5 and not _rot(e) \
                and (_spans(e, "x", Ws) or _spans(e, "y", Hs)):
            bands.append(e)  # a half-canvas colour panel: drawn as the same band, content flows over it
        elif e["type"] == "image" and _asset_missing(e) and _area(_box(e)) < 0.04 * Ws * Hs:
            lost.append(e)
        elif e["type"] == "shape" and float(e.get("opacity", 1)) < 0.9:
            deco.append(e)
        else:
            content.append(e)

    # Shapes that frame text or a picture are panels: faint circles stay
    # decoration, but a visible rectangle behind text travels with it.
    panels = []
    for sh in list(deco) + [c for c in content if c["type"] == "shape" and not c.get("group_id")]:
        if sh.get("shape_type") == "line":
            continue
        op = float(sh.get("opacity", 1))
        if not (op >= 0.9 or (sh.get("shape_type") == "rectangle" and op >= 0.3)):
            continue
        sb = _box(sh)
        inside = [c for c in content if c["type"] in ("text", "image") and c is not sh
                  and _inter(sb, _box(c)) >= 0.7 * _area(_box(c))]
        if inside:
            if sh in deco:
                deco.remove(sh)
                content.append(sh)
            panels.append(sh)

    # A card: one opaque panel holding all the other content. The content reflows
    # like any layout and the panel is redrawn around wherever it lands, instead of
    # shrinking the whole card as one block (a tiny box of text on a banner).
    card = None
    for sh in sorted(panels, key=lambda p: -_area(_box(p))):
        rest = [c for c in content if c is not sh]
        sb = _box(sh)
        if not sh.get("group_id") and float(sh.get("opacity", 1)) >= 0.9 and len(rest) >= 2 \
                and all(_inter(sb, _box(c)) >= 0.7 * _area(_box(c)) for c in rest):
            card = sh
            content.remove(sh)
            panels.remove(sh)
        break

    # Small free pictures parked in a corner (watermark, corner logo) stay there.
    anchors = []
    for e in list(content):
        if e["type"] != "image" or e.get("group_id") or _rot(e):
            continue
        b = _box(e)
        cx, cy = (b[0] + b[2] / 2) / Ws, (b[1] + b[3] / 2) / Hs
        if _area(b) <= 0.04 * Ws * Hs and (cx < 0.22 or cx > 0.78) and (cy < 0.22 or cy > 0.78):
            content.remove(e)
            anchors.append(e)

    units: list[_Unit] = []
    for members in _cluster(content, panels, True, canvas_area=Ws * Hs):
        # A "group" spanning most of the canvas is a layout, not a widget: split it.
        parts = [members]
        if len(members) > 2:
            x0 = min(m["x"] for m in members)
            y0 = min(m["y"] for m in members)
            x1 = max(m["x"] + m["width"] for m in members)
            y1 = max(m["y"] + m["height"] for m in members)
            if (x1 - x0) * (y1 - y0) > 0.45 * Ws * Hs:
                giant = set()
                for gid in {m.get("group_id") for m in members if m.get("group_id")}:
                    gm = [m for m in members if m.get("group_id") == gid]
                    gx0 = min(m["x"] for m in gm)
                    gy0 = min(m["y"] for m in gm)
                    gx1 = max(m["x"] + m["width"] for m in gm)
                    gy1 = max(m["y"] + m["height"] for m in gm)
                    if (gx1 - gx0) * (gy1 - gy0) > 0.45 * Ws * Hs:
                        giant.add(gid)
                parts = _cluster(members, panels, True, False, frozenset(giant), Ws * Hs)
        for part in parts:
            part.sort(key=lambda e: e.get("z_index", 0))
            if len(part) == 1:
                e = part[0]
                if e["type"] == "text" and not _rot(e):
                    units.append(_TextUnit(e))
                elif e["type"] == "image":
                    units.append(_ImageUnit(e))
                else:
                    units.append(_Cluster(part))
            elif len(part) == 2 and _beside(*sorted(part, key=lambda e: e["type"]), Ws * Hs):
                units.append(_IconLabel(*sorted(part, key=lambda e: e["type"])))
            else:
                units.append(_Cluster(part))

    fmax = max([float(e.get("font_size", 16)) for e in els if e.get("type") == "text"] + [1.0])
    for u in units:
        if isinstance(u, _TextUnit):
            r = u.t.f / fmax
            u.prio = 1.0 if r >= 0.75 else (0.75 if r >= 0.45 else (0.6 if r >= 0.3 else 0.5))

    # Hero picture: the biggest free image, if it is a meaningful share of the canvas.
    imgs = [u for u in units if isinstance(u, _ImageUnit)]
    if imgs:
        hero = max(imgs, key=lambda u: u.w * u.h)
        if hero.w * hero.h >= 0.04 * Ws * Hs:
            hero.is_hero = True
    for u in imgs:
        u.prio = 0.4 if u.is_hero else 0.6
    for u in imgs:
        if not u.is_hero and u.fw >= 2.0 * u.fh:
            u.min_w = 56.0  # a wordmark below ~56px wide is unreadable

    # Similar chips (badges, tags...) on one row, or in neatly stacked rows,
    # become a flow row that can re-wrap as a grid.
    chips = sorted([u for u in units if isinstance(u, _Cluster) and len(u.els) >= 2
                    and not u.has_rot and u.w * u.h <= 0.08 * Ws * Hs], key=lambda u: u.cy)
    rows: list[list[_Cluster]] = []
    for u in chips:
        if rows:
            r = rows[-1]
            ref_h = sorted(k.h for k in r)[len(r) // 2]
            ref_w = sorted(k.w for k in r)[len(r) // 2]
            mean_cy = sum(k.cy for k in r) / len(r)
            same_row = abs(u.cy - mean_cy) <= 0.5 * max(u.h, ref_h) and 0.6 <= u.h / max(ref_h, 1e-6) <= 1.67
            next_row = (u.cy > mean_cy and u.cy - max(k.cy for k in r) <= 1.9 * max(u.h, ref_h)
                        and 0.8 <= u.h / max(ref_h, 1e-6) <= 1.25 and 0.75 <= u.w / max(ref_w, 1e-6) <= 1.33)
            if same_row or next_row:
                r.append(u)
                continue
        rows.append([u])
    for r in rows:
        if len(r) >= 3:
            for u in r:
                units.remove(u)
            units.append(_RowFlow(r))

    # Big look-alike widgets (feature cards...) side by side also flow as a row/grid.
    _link_siblings(units)
    seen: set[int] = set()
    for u in list(units):
        g = u.sib
        if not g or id(g) in seen or not isinstance(u, (_Cluster, _ImageUnit)):
            continue
        seen.add(id(g))
        row = [m for m in g if isinstance(m, (_Cluster, _ImageUnit)) and not getattr(m, 'has_rot', False)
               and not getattr(m, 'rot', 0) and m in units]
        if len(row) >= 3:
            for m in row:
                units.remove(m)
            units.append(_RowFlow(row))
    # A small widget parked in a corner (sale ribbon, sticker) is anchored there too,
    # unless it sits right by the copy (a call-to-action under the body text stays in the flow).
    texts = [u for u in units if isinstance(u, _TextUnit)]
    for u in list(units):
        if isinstance(u, _Cluster) and len(u.els) >= 2 and u.w * u.h <= 0.04 * Ws * Hs:
            cx, cy = u.cx / Ws, u.cy / Hs
            near = any(max(t.y0 - (u.y0 + u.h), u.y0 - (t.y0 + t.h)) <= 1.5 * u.h for t in texts)
            if (cx < 0.22 or cx > 0.78) and (cy < 0.22 or cy > 0.78) and (u.has_rot or not near):
                units.remove(u)
                anchors.append(u)
    units.sort(key=lambda u: (u.cy, u.cx))
    _link_siblings(units)
    return bg, bands, deco, lost + anchors, anchors, units, card


def _scale_style(n: dict, k: float) -> dict:
    for key in ("corner_radius", "stroke_width"):
        if n.get(key):
            n[key] = float(n[key]) * k
    if n.get("corner_radius"):
        n["corner_radius"] = min(n["corner_radius"], float(n["width"]) / 2, float(n["height"]) / 2)
    return n


def _uniform(source: dict, target: dict) -> dict:
    """Same aspect ratio (within ~2%): nothing to rearrange, just scale."""
    Ws, Hs = source["canvas"]["width"], source["canvas"]["height"]
    W, H = int(target["width"]), int(target["height"])
    k = min(W / Ws, H / Hs)
    ox, oy = (W - Ws * k) / 2, (H - Hs * k) / 2
    _tls.exact = True
    out = []
    for e in source["elements"]:
        n = dict(e)
        if _coverage(e, Ws, Hs) >= 0.85 and e["type"] in ("image", "shape") and not _rot(e):
            n.update(x=0, y=0, width=W, height=H)
            if n["type"] == "image" and n.get("fit") in ("fill", "contain"):
                n["fit"] = "cover"
        else:
            n.update(x=ox + e["x"] * k, y=oy + e["y"] * k, width=e["width"] * k, height=e["height"] * k)
            _scale_style(n, k)
            if e["type"] == "text":
                t = _Txt(e)
                fs = max(1.0, math.floor(float(e["font_size"]) * k * 100) / 100)
                if e.get("letter_spacing"):
                    n["letter_spacing"] = float(e["letter_spacing"]) * k
                fs, lines, th = t.fit(fs, n["width"], n["height"], 0.7 * fs)
                n["font_size"] = fs
                n["height"] = max(n["height"], th)
        out.append(n)
    return {"canvas": {"width": W, "height": H}, "elements": _sanitize(out, W, H)}


def _cap_hierarchy(units: list[_Unit], placed: dict[str, dict]) -> None:
    """A smaller source text must never end up bigger than a clearly larger one
    (a long unbreakable headline word can force its font down)."""
    texts = [u for u in units if isinstance(u, _TextUnit)]
    # References: free texts, plus the largest label of each cluster (a caption on a
    # photo is capped by the photo's resolution; copy must not outgrow it). Only the
    # largest, so a "$" set beside a price does not count as a headline.
    refs = [(u.t.f, u.el["id"]) for u in texts]
    refs += [max((t.f, i) for i, t in u.txt.items()) for u in units if isinstance(u, _Cluster) and u.txt]
    for b in texts:
        e = placed.get(b.el["id"])
        if not e:
            continue
        cap = None
        for fa_src, aid in refs:
            if aid == b.el["id"] or fa_src < 1.3 * b.t.f:
                continue
            fa = placed.get(aid)
            if fa and fa["font_size"] > 7.5:  # both tiny: legibility beats hierarchy
                c = fa["font_size"] * max(0.92, 0.95 * (b.t.f / fa_src) ** 0.5)
                cap = c if cap is None else min(cap, c)
        if cap is not None and e["font_size"] > cap:
            fs, n, th = b.t.fit(cap, e["width"])
            e["font_size"] = fs
            e["height"] = th
            if e.get("letter_spacing"):
                e["letter_spacing"] = b.t.ls0 * fs / b.t.f


def _card(card: dict, members: list[dict], laid: dict[str, dict], s: float, geo: _Geo, occ: list) -> dict:
    """Redraw a card panel around its reflowed content, keeping its padding (scaled)."""
    cx, cy, cw, ch = _box(card)
    sx0 = min(_box(m)[0] for m in members); sy0 = min(_box(m)[1] for m in members)
    sx1 = max(_box(m)[0] + _box(m)[2] for m in members); sy1 = max(_box(m)[1] + _box(m)[3] for m in members)
    pad_src = max(0.0, min(sx0 - cx, sy0 - cy, cx + cw - sx1, cy + ch - sy1))
    out = [laid[m["id"]] for m in members]
    x0 = min(e["x"] for e in out); y0 = min(e["y"] for e in out)
    x1 = max(e["x"] + e["width"] for e in out); y1 = max(e["y"] + e["height"] for e in out)
    # Never off the canvas, and keep some background showing around the card.
    pad = max(0.0, min(pad_src * s, 0.5 * min(x0, y0, geo.W - x1, geo.H - y1)))
    n = _scale_style(dict(card), s)
    n.update(x=x0 - pad, y=y0 - pad, width=x1 - x0 + 2 * pad, height=y1 - y0 + 2 * pad)
    occ.append((n["x"], n["y"], n["width"], n["height"], 0.5))
    return n


def _resize(source: dict, target: dict) -> dict:
    Ws, Hs = int(source["canvas"]["width"]), int(source["canvas"]["height"])
    W, H = int(target["width"]), int(target["height"])
    if abs(math.log((W / H) / (Ws / Hs))) < 0.06:  # ~6%: nothing to rearrange, scale and crop the bg
        return _uniform(source, target)
    els = [dict(e) for e in source["elements"]]
    bg, bands, deco, lost, anchors, units, card = _build_units(els, Ws, Hs)
    geo = _Geo(W, H, Ws, Hs)
    s_cap = 1.6 * max(math.sqrt((W * H) / (Ws * Hs)), 0.7 * max(W / Ws, H / Hs))

    out: dict[str, dict] = {}
    for e in bg:
        n = dict(e)
        n.update(x=0, y=0, width=W, height=H)
        if n["type"] == "image" and n.get("fit") in ("fill", "contain"):
            n["fit"] = "cover"
        out[n["id"]] = n

    for e in bands:  # same fraction of the canvas, full span along the axis it spanned
        n = dict(e)
        x, y, w, h = _box(e)
        if _spans(e, "y", Hs):
            n.update(x=x / Ws * W, y=0, width=w / Ws * W, height=H)
        else:
            n.update(x=0, y=y / Hs * H, width=W, height=h / Hs * H)
        out[n["id"]] = _scale_style(n, min(W / Ws, H / Hs))

    pieces, seen = [], set()
    for e in list(lost) + list(anchors):
        key = id(e) if isinstance(e, _Cluster) else e["id"]
        if key not in seen:
            seen.add(key)
            pieces.append(e)

    first = None
    for attempt in range(2):
        _tls.exact = False
        occ: list[tuple] = []
        s_used = 0.5 * min(W / Ws, H / Hs)
        score = 0.0
        laid: dict[str, dict] = {}
        if units:
            res = _layout(units, geo, s_cap)
            if res is None:
                raise RuntimeError("no feasible layout")
            placed, occ, s_used, score = res
            for e in placed:
                laid[e["id"]] = e
            _cap_hierarchy(units, laid)
        # Corner-anchored pictures and small missing assets keep a corner; place them first
        # (they matter more than decoration) and let decoration fill in around them.
        # Stickers scale with the layout, but never beyond ~1.3x their share of the canvas.
        kfix = min(max(s_used, 0.5 * min(W / Ws, H / Hs)), 1.3 * max(W / Ws, H / Hs), 1.3 * s_used)
        # A sticker's label need not stay bigger than the copy the layout could afford.
        fl = min([6.0] + [e["font_size"] for e in laid.values() if e["type"] == "text"
                          and str(e.get("content", "")).strip()])
        # ...but never under copy it outranks (a 20px ribbon must not end up below 15px badges).
        lab = [float(m.get("font_size", 16)) for p in pieces if isinstance(p, _Cluster)
               for m in p.els if m["type"] == "text"]
        src_fs = {e["id"]: float(e.get("font_size", 16)) for e in els if e["type"] == "text"}
        if lab:
            fl = max([fl] + [0.92 * e["font_size"] for i, e in laid.items()
                             if i in src_fs and 1.3 * src_fs[i] <= min(lab)])
        anchored = _place_deco(pieces, (Ws, Hs), geo, occ, kfix=kfix, label_floor=fl)
        # A corner widget that cannot find a free corner (tiny banner) rejoins the flow instead.
        by_id = {e["id"]: e for e in anchored}
        def _hits(m: dict, o: tuple) -> bool:
            # Same pairs the checker treats as collisions: text vs anything placed,
            # picture vs text/picture, shape vs text (o[4]: 1 text, 0.5 shape, 0.15 picture).
            t = m["type"]
            if (t == "image" and o[4] == 0.5) or (t == "shape" and o[4] != 1.0):
                return False
            e = by_id[m["id"]]
            fw, fh = _fp(e["width"], e["height"], _rot(e))
            r = (e["x"] + e["width"] / 2 - fw / 2, e["y"] + e["height"] / 2 - fh / 2, fw, fh)
            return _inter(r, o[:4]) > max(4.0, 0.02 * min(_area(r), _area(o[:4])))

        stuck = [p for p in pieces if isinstance(p, _Cluster) and attempt == 0
                 and any(_hits(m, o) for m in p.els for o in occ)]
        if attempt == 1 and score < 0.8 * first[0]:
            # Rescuing the corner widget would shrink everything: keep the first layout.
            score, laid, occ, anchored = first
            break
        first = (score, laid, occ, anchored)
        if not stuck:
            break
        pieces = [p for p in pieces if p not in stuck]
        units = sorted(units + stuck, key=lambda u: (u.cy, u.cx))
        _link_siblings(units)
    out.update(laid)
    if card is not None:
        out[card["id"]] = _card(card, [e for e in els if e["id"] in laid and e is not card], laid, s_used, geo, occ)
    for e in anchored:
        out[e["id"]] = e
        fw, fh = _fp(e["width"], e["height"], _rot(e))
        cx, cy = e["x"] + e["width"] / 2, e["y"] + e["height"] / 2
        occ.append((cx - fw / 2, cy - fh / 2, fw, fh, 1.0))
    for e in _place_deco(list(deco), (Ws, Hs), geo, occ):
        out[e["id"]] = e

    result = [out[e["id"]] for e in els]
    return {"canvas": {"width": W, "height": H}, "elements": _sanitize(result, W, H)}


def _sanitize(elements: list[dict], W: int, H: int) -> list[dict]:
    res = []
    for e in elements:
        e = dict(e)
        w, h = max(0.0, float(e["width"])), max(0.0, float(e["height"]))
        if e["type"] != "text" and (w > W or h > H) and w > 0 and h > 0:
            f = min(W / w, H / h)  # keep proportions when something is bigger than the canvas
            cx, cy = float(e["x"]) + w / 2, float(e["y"]) + h / 2
            w, h = w * f, h * f
            e["x"], e["y"] = cx - w / 2, cy - h / 2
        w, h = min(w, float(W)), min(h, float(H))
        x = _clamp(float(e["x"]), 0.0, W - w)
        y = _clamp(float(e["y"]), 0.0, H - h)
        if e["type"] == "text":
            w = min(float(W), math.ceil(w * 100) / 100)
            h = min(float(H), math.ceil(h * 100) / 100)
            x = _clamp(x, 0.0, W - w)
            y = _clamp(y, 0.0, H - h)
            e["font_size"] = max(1.0, math.floor(float(e["font_size"]) * 100) / 100)
            if e.get("letter_spacing"):
                e["letter_spacing"] = round(float(e["letter_spacing"]), 3)
        e.update(x=round(x, 2), y=round(y, 2), width=round(w, 2), height=round(h, 2))
        res.append(e)
    return res


def _fallback(source: dict, target: dict) -> dict:
    """Never-crash path: uniform 'contain' scale, centred, background to cover."""
    Ws, Hs = source["canvas"]["width"], source["canvas"]["height"]
    W, H = int(target["width"]), int(target["height"])
    k = min(W / Ws, H / Hs)
    ox, oy = (W - Ws * k) / 2, (H - Hs * k) / 2
    out = []
    for e in source["elements"]:
        n = dict(e)
        if _coverage(e, Ws, Hs) >= 0.85 and e["type"] in ("image", "shape"):
            n.update(x=0, y=0, width=W, height=H)
        else:
            n.update(x=ox + e["x"] * k, y=oy + e["y"] * k,
                     width=e["width"] * k, height=e["height"] * k)
            if e["type"] == "text":
                n["font_size"] = max(1.0, e["font_size"] * k)
        out.append(n)
    return {"canvas": {"width": W, "height": H}, "elements": _sanitize(out, W, H)}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _normalize(source: dict) -> dict:
    """Deep, defensive copy: every number is a finite float, text fields are
    strings, nothing can make later code raise on a malformed value."""
    els = []
    for i, e in enumerate(source.get("elements") or []):
        if not isinstance(e, dict):
            continue
        n = dict(e)
        if n.get("id") is None:
            n["id"] = f"__noid{i}"
        for k in ("x", "y"):
            n[k] = _num(n.get(k))
        for k in ("width", "height"):
            n[k] = max(0.0, _num(n.get(k)))
        for k in ("rotation", "opacity", "line_height", "letter_spacing", "corner_radius", "stroke_width"):
            if k in n:
                if n[k] is None or not math.isfinite(_num(n[k], float("nan"))):
                    del n[k]
                else:
                    n[k] = _num(n[k])
        if n.get("type") == "text":
            n["content"] = "" if n.get("content") is None else str(n["content"])
            n["font_size"] = max(1.0, _num(n.get("font_size"), 16.0))
            n["font_family"] = str(n.get("font_family") or "Solstice Sans")
            if not isinstance(n.get("style_runs"), list):
                n.pop("style_runs", None)
            if "max_lines" in n and not isinstance(n["max_lines"], (int, float)):
                n["max_lines"] = None
        els.append(n)
    canvas = source.get("canvas") or {}
    cw, ch = _num(canvas.get("width")), _num(canvas.get("height"))
    if cw < 1 or ch < 1:  # unusable canvas: derive one from the content
        cw = max([e["x"] + e["width"] for e in els] + [1.0])
        ch = max([e["y"] + e["height"] for e in els] + [1.0])
    return {**source, "canvas": {"width": int(round(cw)), "height": int(round(ch))}, "elements": els}


def _last_resort(source: dict, target: dict) -> dict:
    """Cannot raise: naive proportional scale of whatever can be read."""
    W = max(1, int(_num(target.get("width") if isinstance(target, dict) else 1, 1)))
    H = max(1, int(_num(target.get("height") if isinstance(target, dict) else 1, 1)))
    try:
        c = source.get("canvas") or {}
        Ws, Hs = max(1.0, _num(c.get("width"), 1.0)), max(1.0, _num(c.get("height"), 1.0))
    except Exception:
        Ws = Hs = 1.0
    k = min(W / Ws, H / Hs)
    out = []
    for e in (source.get("elements") or []):
        if not isinstance(e, dict):
            continue
        n = dict(e)
        w, h = max(0.0, _num(n.get("width"))) * k, max(0.0, _num(n.get("height"))) * k
        w, h = min(w, W), min(h, H)
        n["width"], n["height"] = round(w, 2), round(h, 2)
        n["x"] = round(min(max(0.0, _num(n.get("x")) * k), W - w), 2)
        n["y"] = round(min(max(0.0, _num(n.get("y")) * k), H - h), 2)
        if n.get("type") == "text":
            n["font_size"] = round(max(1.0, _num(n.get("font_size"), 16.0) * k), 2)
        out.append(n)
    return {"canvas": {"width": W, "height": H}, "elements": out}


def resize(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    """Return `source` re-laid-out for `target_canvas` (same ids, new geometry)."""
    try:
        return _resize_safe(source, target_canvas)
    finally:
        _tls.exact = False


def _resize_safe(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    try:
        clean = _normalize(source)
        tgt = {"width": max(1, int(round(_num(target_canvas["width"], 1)))),
               "height": max(1, int(round(_num(target_canvas["height"], 1))))}
        try:
            res = _resize(clean, tgt)
            if {e["id"] for e in res["elements"]} == {e["id"] for e in clean["elements"]}:
                return res
        except Exception:
            if os.environ.get("CHAMELEON_DEBUG"):
                raise
        return _fallback(clean, tgt)
    except Exception:
        if os.environ.get("CHAMELEON_DEBUG"):
            raise
        return _last_resort(source, target_canvas)
