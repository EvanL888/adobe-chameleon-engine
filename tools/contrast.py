"""
Render-based legibility check: for every text element, render the design
WITHOUT any text, look at the pixels behind the text box, and compute

  * contrast  - WCAG ratio between the text colour (alpha-blended over the
                average background) and that background
  * busy      - luminance spread of the background under the text (0..1)

    python -m tools.contrast                         # samples + a design subset x some sizes
    python -m tools.contrast --design t32 --sizes 1080x1920 300x250
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

os.environ["CHAMELEON_DEBUG"] = "1"

import numpy as np
from PIL import Image

from engine.main import ASSETS_DIR, render_to_png
from engine.resize import resize

RGBA = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+))?\s*\)")


def parse(color: str):
    m = RGBA.match(str(color or ""))
    if not m:
        return (255.0, 255.0, 255.0, 1.0)
    r, g, b, a = m.groups()
    return float(r), float(g), float(b), float(a) if a is not None else 1.0


def lum(rgb) -> float:
    c = np.asarray(rgb, dtype=float) / 255.0
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return float(0.2126 * c[..., 0] + 0.7152 * c[..., 1] + 0.0722 * c[..., 2])


def analyse(design: dict) -> list[dict]:
    """One finding dict per text element."""
    texts = [e for e in design["elements"] if e["type"] == "text" and str(e.get("content", "")).strip()]
    if not texts:
        return []
    bare = {"canvas": design["canvas"], "elements": [e for e in design["elements"] if e["type"] != "text"]}
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "bare.png"
        render_to_png(bare, ASSETS_DIR, png)
        img = np.asarray(Image.open(png).convert("RGB"), dtype=float)
    H, W = img.shape[:2]
    res = []
    for e in texts:
        x0, y0 = int(max(0, e["x"])), int(max(0, e["y"]))
        x1, y1 = int(min(W, e["x"] + e["width"])), int(min(H, e["y"] + e["height"]))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        reg = img[y0:y1, x0:x1].reshape(-1, 3)
        mean = reg.mean(axis=0)
        tr, tg, tb, ta = parse(e.get("color"))
        ta *= float(e.get("opacity", 1) or 1)
        blended = np.array([tr, tg, tb]) * ta + mean * (1 - ta)
        l1, l2 = lum(blended), lum(mean)
        ratio = (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)
        lums = np.array([lum(p) for p in reg[:: max(1, len(reg) // 400)]])
        res.append({"id": e["id"], "contrast": round(ratio, 2), "busy": round(float(lums.std()), 3),
                    "fs": e["font_size"], "bold": e.get("font_weight") == "bold"})
    return res


def problems(findings: list[dict]) -> list[str]:
    out = []
    for f in findings:
        large = f["fs"] >= 24 or (f["bold"] and f["fs"] >= 18.66)
        need = 3.0 if large else 4.5
        if f["contrast"] < need:
            out.append(f"{f['id']}: contrast {f['contrast']} < {need}")
        if f["busy"] > 0.12:
            out.append(f"{f['id']}: busy background (spread {f['busy']})")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--design", default="")
    ap.add_argument("--sizes", nargs="*", default=["1080x1080", "1080x1920", "1600x900", "970x250", "300x250"])
    args = ap.parse_args()
    paths = sorted(Path("input").glob("*.json")) + sorted(Path("tests/designs").glob("*.json"))
    n = bad = 0
    worst = []
    for dp in [p for p in paths if args.design in p.name]:
        src = json.loads(dp.read_text(encoding="utf-8"))["source"]
        try:
            base = problems(analyse({"canvas": src["canvas"], "elements": src["elements"]}))
        except Exception:  # a deliberately malformed source cannot be rendered
            base = []
        base_ids = {b.split(":")[0] for b in base}
        for sz in args.sizes:
            w, h = map(int, sz.split("x"))
            out = resize(src, {"width": w, "height": h})
            fnd = analyse(out)
            pr = [p for p in problems(fnd) if p.split(":")[0] not in base_ids]  # only NEW problems
            n += 1
            if pr:
                bad += 1
                worst.append((dp.stem, sz, pr[:3]))
    for w in worst:
        print(f"{w[0][:30]:30s} {w[1]:>10s}  " + " | ".join(w[2]))
    print(f"\n{n} renders analysed, {bad} with legibility problems that the source did not have")


if __name__ == "__main__":
    main()
