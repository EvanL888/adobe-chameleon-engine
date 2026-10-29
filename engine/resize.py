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
        start = fs
        n, w = self.layout(fs, max_w)
        for _ in range(80):
            if w > max_w * 1.0005 + 0.01 and fs > 1.0:
                fs = max(1.0, fs * max(0.5, 0.995 * max_w / w))
            elif self.max_lines and n > self.max_lines and fs > max(min_fs, 1.0, 0.5 * start):
                fs = max(min_fs, 1.0, 0.5 * start, fs * 0.96)
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


def _floor_px(f: float, ff: float) -> float:
    """Smallest font size we are willing to render for a source size f."""
    return ff * max(6.0, 0.4 * f)


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
        self.ff = 1.0
        self.is_hero = False
        self.min_w = 0.0    # legibility floor (px) for logo-like pictures
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
        pr = 1.0 - (1.0 - self.prio) * _clamp((0.4 - s) / 0.2, 0.0, 1.0)  # pictures yield to text when tight
        g = max(s * self.boost * pr, self.min_w * self.ff / max(self.fw, 1e-6))
        g = min(g, cw / max(self.fw, 1e-6))
        if self.g_native:
            g = min(g, self.g_native)
        return {"w": self.fw * g, "h": self.fh * g, "g": g, "minfs": 1e9}

    def floor_g(self) -> float:
        return 0.0

    def min_src_font(self) -> float:
        return 1e9

    def emit(self, g: float, ox: float, oy: float, wchip: float | None = None) -> list[dict]:
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
        pr = 1.0 - (1.0 - self.prio) * _clamp((0.4 - s) / 0.2, 0.0, 1.0)
        fs = max(self.t.f * s * self.boost * pr, _floor_px(self.t.f, self.ff) * pr)
        maxw = min(cw, max(self.w * s * 1.6, 0.7 * cw))
        fs, n, th = self.t.fit(fs, maxw)
        return {"w": maxw, "h": th, "fs": fs, "minfs": fs, "n": n, "chars": len(self.t.text.strip())}

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
        self.wmin = self.w
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
                self.chip = self.wmin < self.w - 1.0

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

    def measure(self, s: float, cw: float) -> dict:
        g = self.scale_for(s, cw)
        return {"w": self.width_at(g, cw), "h": self.h * g, "g": g, "minfs": g * self.min_src_font()}

    def emit(self, g: float, ox: float, oy: float, wchip: float | None = None) -> list[dict]:
        shapes = [e for e in self.els if e["type"] == "shape"]
        hx = 1.0
        if self.chip and wchip is not None and self.w * g > 0:
            hx = max(0.05, min(1.0, wchip / (self.w * g)))
        out = []
        for e in self.els:
            x, y, w, h = _box(e)
            ncx = ox + (x + w / 2 - self.x0) * g * hx
            ncy = oy + (y + h / 2 - self.y0) * g
            nw, nh = w * g * hx, h * g
            n = dict(e)
            if e["type"] == "text":
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
                for sh in shapes:
                    sx, sy, sw, shh = _box(sh)
                    if (sx <= x + w / 2 <= sx + sw and sy <= y + h / 2 <= sy + shh
                            and abs((y + h / 2) - (sy + shh / 2)) < 0.25 * shh):
                        top = (oy + (sy + shh / 2 - self.y0) * g) - th / 2
                        break
                nh = max(th, min(nh, th * 1.0)) if th > nh or top != ncy - nh / 2 else nh
                n.update(x=ncx - nw / 2, y=top, width=nw, height=nh)
            else:
                n.update(x=ncx - nw / 2, y=ncy - nh / 2, width=nw, height=nh)
                _scale_style(n, g)
            out.append(n)
        return out

    def place(self, m: dict, x0: float, x1: float, top: float) -> list[dict]:
        return self.emit(m["g"], (x0 + x1) / 2 - m["w"] / 2, top, m["w"] if self.chip else None)


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
            if too_wide or len(t_lines) < len(lines):
                lines, tight = t_lines, True
        gap = self.gap_src * g
        h = sum(max(k.h for k in ln) * g for ln in lines) + gap * 0.8 * (len(lines) - 1)
        w = max(sum(self._kw(k, g, tight) for k in ln) + gap * (len(ln) - 1) for ln in lines)
        return {"w": w, "h": h, "g": g, "lines": lines, "tight": tight,
                "minfs": g * min(k.min_src_font() for k in self.kids)}

    def place(self, m: dict, x0: float, x1: float, top: float) -> list[dict]:
        g, gap, tight = m["g"], self.gap_src * m["g"], m["tight"]
        out, y = [], top
        for ln in m["lines"]:
            lw = sum(self._kw(k, g, tight) for k in ln) + gap * (len(ln) - 1)
            lh = max(k.h for k in ln) * g
            x = (x0 + x1) / 2 - lw / 2
            for k in ln:
                kw = self._kw(k, g, tight)
                out += k.emit(g, x, y + (lh - k.h * g) / 2, kw if tight else None)
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


def _col_factor(k: int, kp: int) -> float:
    """Tall canvases stay single-column; elsewhere extra columns must earn it."""
    if kp == 1:
        return 1.0 - 0.4 * (k - 1)
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
    lo, hi = 1.0, 1.8
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


def _set_ff(units: list[_Unit], ff: float) -> None:
    for u in units:
        u.ff = ff
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
    levels = (1.0, 0.75, 0.55, 0.4, 0.25, 0.0) if n <= 14 else (1.0, 0.5, 0.0)
    cap_parts = 120 if n <= 14 else max(3, 240 // (3 * len(ks)))
    coarse = []
    for ff in levels:
        _set_ff(units, ff)
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
                    if ff > 0 and any(_too_narrow(u, cw) for c, cw in zip(cols, widths) for u in c):
                        continue
                    _apply_caps(units, cols, widths)
                    s = _max_scale(cols, widths, hc, s_cap, 8)
                    if s is None:
                        continue
                    level_best = max(level_best, s)
                    coarse.append((s * _col_factor(k, kp), cols, widths, ff))
        if level_best >= 0.6 * s_cap:
            break
    coarse.sort(key=lambda c: -c[0])
    best = None
    for _, cols, widths, ff in coarse[:8]:
        _set_ff(units, ff)
        k = len(cols)
        _apply_caps(units, cols, widths)
        s = _max_scale(cols, widths, hc, s_cap, 18)
        if s is None:
            continue
        hb = max(_hero_boost(c, s, cw, hc) for c, cw in zip(cols, widths))
        minfs = min(m["minfs"] for c, cw in zip(cols, widths) for m in _measure_col(c, s, cw)[0])
        score = _measure_factor(cols, widths, s) * s * min(1.0, minfs / 7.0) * _col_factor(k, kp) * (1.0 + 0.12 * (hb - 1.0))
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


def _layout(units: list[_Unit], geo: _Geo, s_cap: float):
    """Returns (placed elements, occupied rects [(x,y,w,h,weight)])."""
    best = _search(units, geo, s_cap)
    if best is None:
        return None
    _, s, cols, widths, ff = best
    _tls.exact = True
    _apply_caps(units, cols, widths)
    for u in units:
        u.ff = ff
        if isinstance(u, _RowFlow):
            for k in u.kids:
                k.ff = ff
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
                if e["type"] != "shape":
                    wgt = 1.0 if e["type"] == "text" else 0.15
                    fw, fh = _fp(e["width"], e["height"], _rot(e))  # rotated boxes occupy their outline
                    cx, cy = e["x"] + e["width"] / 2, e["y"] + e["height"] / 2
                    occ.append((cx - fw / 2, cy - fh / 2, fw, fh, wgt))
            y += m["h"] + (gaps[i] if i < len(gaps) else 0.0)
        x += cw + geo.colgap
    return out, occ, s


# --------------------------------------------------------------------------
# Decorations
# --------------------------------------------------------------------------

def _place_deco(deco: list[dict], src_wh: tuple[int, int], geo: _Geo,
                occ: list[tuple], kfix: float | None = None) -> list[dict]:
    """Park decoration (or, with kfix, corner-anchored pictures at that scale)
    where it collides least with what is already placed."""
    Ws, Hs = src_wh
    W, H = geo.W, geo.H
    sd = kfix if kfix is not None else min(W / Ws, H / Hs)
    shrinks = (1.0, 0.8, 0.65, 0.5, 0.35, 0.25) if kfix is not None else (1.0, 0.75, 0.55, 0.4, 0.25)
    placed = []
    for e in sorted(deco, key=lambda e: -_area(_box(e))):
        x, y, w, h = _box(e)
        rot = _rot(e)
        fw0, fh0 = _fp(w, h, rot)
        fw0, fh0 = max(fw0, 1e-3), max(fh0, 1e-3)
        cx, cy = x + w / 2, y + h / 2
        best = None
        for shrink in shrinks:
            k = min(sd * shrink, 0.8 * W / fw0, 0.8 * H / fh0)
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
            for px, py in cands:
                px = _clamp(px, fw / 2, W - fw / 2)
                py = _clamp(py, fh / 2, H - fh / 2)
                rect = (px - fw / 2, py - fh / 2, fw, fh)
                cost = sum(_inter(rect, o[:4]) * (6.0 if kfix is not None else o[4]) for o in occ) / max(1.0, fw * fh)
                cost += 0.15 * (1 - shrink)
                if best is None or cost < best[0] - 1e-9:
                    best = (cost, px, py, k)
                if cost < 0.01 and shrink == 1.0:
                    break
            if best[0] < 0.01:
                break
        _, px, py, k = best
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
             skip_groups: frozenset = frozenset()) -> list[list[dict]]:
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


def _build_units(els: list[dict], Ws: int, Hs: int):
    bg, deco, lost, content = [], [], [], []
    for e in els:
        if e.get("group_id"):
            content.append(e)  # a designer-grouped piece always travels with its group
            continue
        cov = _coverage(e, Ws, Hs)
        if (e["type"] == "image" and cov >= 0.85 and not _rot(e)) or \
           (e["type"] == "shape" and cov >= 0.9 and float(e.get("opacity", 1)) >= 0.5 and not _rot(e)):
            bg.append(e)
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
    for members in _cluster(content, panels, True):
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
                parts = _cluster(members, panels, True, False, frozenset(giant))
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
        u.prio = 0.55 if u.is_hero else 0.8
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
    units.sort(key=lambda u: (u.cy, u.cx))
    _link_siblings(units)
    return bg, deco, lost + anchors, anchors, units


def _scale_style(n: dict, k: float) -> dict:
    for key in ("corner_radius", "stroke_width"):
        if n.get(key):
            n[key] = float(n[key]) * k
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
    for b in texts:
        e = placed.get(b.el["id"])
        if not e:
            continue
        cap = None
        for a in texts:
            if a is b or a.t.f < 1.3 * b.t.f:
                continue
            fa = placed.get(a.el["id"])
            if fa:
                c = fa["font_size"] * 0.95 * (b.t.f / a.t.f) ** 0.5
                cap = c if cap is None else min(cap, c)
        if cap is not None and e["font_size"] > cap:
            fs, n, th = b.t.fit(cap, e["width"])
            e["font_size"] = fs
            e["height"] = th
            if e.get("letter_spacing"):
                e["letter_spacing"] = b.t.ls0 * fs / b.t.f


def _resize(source: dict, target: dict) -> dict:
    Ws, Hs = int(source["canvas"]["width"]), int(source["canvas"]["height"])
    W, H = int(target["width"]), int(target["height"])
    if abs(math.log((W / H) / (Ws / Hs))) < 0.02:
        return _uniform(source, target)
    els = [dict(e) for e in source["elements"]]
    bg, deco, lost, anchors, units = _build_units(els, Ws, Hs)
    geo = _Geo(W, H, Ws, Hs)
    s_cap = 1.6 * max(math.sqrt((W * H) / (Ws * Hs)), 0.7 * max(W / Ws, H / Hs))

    out: dict[str, dict] = {}
    for e in bg:
        n = dict(e)
        n.update(x=0, y=0, width=W, height=H)
        if n["type"] == "image" and n.get("fit") in ("fill", "contain"):
            n["fit"] = "cover"
        out[n["id"]] = n

    occ: list[tuple] = []
    s_used = 0.5 * min(W / Ws, H / Hs)
    if units:
        res = _layout(units, geo, s_cap)
        if res is None:
            raise RuntimeError("no feasible layout")
        placed, occ, s_used = res
        for e in placed:
            out[e["id"]] = e
        _cap_hierarchy(units, out)

    # Corner-anchored pictures and small missing assets keep a corner; place them first
    # (they matter more than decoration) and let decoration fill in around them.
    pieces = list({e["id"]: e for e in list(lost) + list(anchors)}.values())
    anchored = _place_deco(pieces, (Ws, Hs), geo, occ, kfix=max(s_used, 0.5 * min(W / Ws, H / Hs)))
    for e in anchored:
        out[e["id"]] = e
        occ.append((e["x"], e["y"], e["width"], e["height"], 1.0))
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
