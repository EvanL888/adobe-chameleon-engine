"""
Local stress test (NOT the official scorer).

Runs every sample in input/ against a catalogue of real-world canvas sizes,
checks the structural rules from RULES.md plus a few quality heuristics,
renders each result with the real renderer and writes contact sheets.

    python -m tools.eval_matrix                # all samples x all sizes
    python -m tools.eval_matrix --sizes 728x90 300x250
    python -m tools.eval_matrix --task level2  # filter samples by substring
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

from engine.resize import resize
from engine.main import render_to_png, ASSETS_DIR

OUT = Path("output/matrix")

CATALOGUE = {
    "Instagram Post": (1080, 1080),
    "Instagram Portrait": (1080, 1350),
    "Instagram Story": (1080, 1920),
    "Facebook Post": (1200, 630),
    "YouTube Thumbnail": (1280, 720),
    "Twitter Post": (1600, 900),
    "Pinterest Pin": (1000, 1500),
    "LinkedIn Banner": (1584, 396),
    "Facebook Cover": (820, 312),
    "Leaderboard": (728, 90),
    "Billboard": (970, 250),
    "Medium Rectangle": (300, 250),
    "Half Page": (300, 600),
    "Wide Skyscraper": (160, 600),
    "Email Header": (600, 200),
    "A4 Poster": (2480, 3508),
}


def _box(e):
    return (e["x"], e["y"], e["width"], e["height"])


def _inter(a, b):
    ix = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    iy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, ix) * max(0.0, iy)


def check(source, out):
    """Return a list of problem strings."""
    probs = []
    W, H = out["canvas"]["width"], out["canvas"]["height"]
    src = {e["id"]: e for e in source["elements"]}
    got = {e["id"]: e for e in out["elements"]}
    if set(src) != set(got) or len(out["elements"]) != len(source["elements"]):
        probs.append("ids differ")
    for i, e in got.items():
        s = src.get(i)
        if s is None:
            continue
        if e["x"] < -0.5 or e["y"] < -0.5 or e["x"] + e["width"] > W + 0.5 or e["y"] + e["height"] > H + 0.5:
            probs.append(f"{i}: off-canvas")
        if e["type"] == "image":
            full = s["width"] * s["height"] >= 0.85 * source["canvas"]["width"] * source["canvas"]["height"]
            if not full and s["height"] and e["height"]:
                a0, a1 = s["width"] / s["height"], e["width"] / e["height"]
                if abs(a1 / a0 - 1) > 0.05:
                    probs.append(f"{i}: aspect changed {a0:.2f}->{a1:.2f}")
        if e["type"] == "text":
            if e["font_size"] < 6:
                probs.append(f"{i}: tiny font {e['font_size']:.1f}")
            if e.get("font_family") != s.get("font_family") or e.get("content") != s.get("content"):
                probs.append(f"{i}: font/content changed")
    # collisions between non-decorative content that were not overlapping in the source
    items = [e for e in out["elements"] if e["type"] in ("text", "image") and
             not (e["type"] == "image" and e["width"] >= W * 0.98 and e["height"] >= H * 0.98)]
    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            ea, eb = items[a], items[b]
            ov = _inter(_box(ea), _box(eb))
            if ov <= 0:
                continue
            sa, sb = src[ea["id"]], src[eb["id"]]
            if _inter(_box(sa), _box(sb)) > 0:
                continue  # overlapped in the source on purpose
            if ov > 0.02 * min(ea["width"] * ea["height"], eb["width"] * eb["height"]):
                probs.append(f"collision {ea['id']} x {eb['id']}")
    return probs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", nargs="*", help="WxH list; default = whole catalogue")
    ap.add_argument("--task", default="", help="substring filter on sample file name")
    args = ap.parse_args()

    if args.sizes:
        sizes = {s: tuple(map(int, s.lower().split("x"))) for s in args.sizes}
    else:
        sizes = {f"{n} {w}x{h}": (w, h) for n, (w, h) in CATALOGUE.items()}

    OUT.mkdir(parents=True, exist_ok=True)
    tasks = [p for p in sorted(Path("input").glob("*.json")) if args.task in p.name]
    total_probs = 0
    for tp in tasks:
        task = json.loads(tp.read_text())
        src = task["source"]
        thumbs = []
        for name, (w, h) in sizes.items():
            t0 = time.time()
            out = resize(src, {"width": w, "height": h})
            dt = time.time() - t0
            probs = check(src, out)
            total_probs += len(probs)
            tag = f"{tp.stem}__{w}x{h}"
            png = OUT / f"{tag}.png"
            render_to_png(out, ASSETS_DIR, png)
            (OUT / f"{tag}.json").write_text(json.dumps(out, indent=1))
            print(f"{tp.stem[:28]:28s} {name:28s} {dt:5.2f}s  "
                  f"{'OK' if not probs else '; '.join(probs[:4])}")
            thumbs.append((name, png))
        sheet(tp.stem, thumbs)
    print(f"\n{total_probs} problem(s) total. Contact sheets in {OUT}/")
    return 0


def sheet(stem, thumbs, cell=420):
    pad, lab = 12, 18
    ims = []
    for name, png in thumbs:
        im = Image.open(png).convert("RGB")
        k = min(cell / im.width, cell / im.height, 1.0 if max(im.size) < cell else 9)
        k = min(cell / im.width, cell / im.height)
        im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
        ims.append((name, im))
    cols = 3
    rows = (len(ims) + cols - 1) // cols
    sheet_im = Image.new("RGB", (cols * (cell + pad) + pad, rows * (cell + lab + pad) + pad), "#dddddd")
    d = ImageDraw.Draw(sheet_im)
    for i, (name, im) in enumerate(ims):
        cx = pad + (i % cols) * (cell + pad)
        cy = pad + (i // cols) * (cell + lab + pad)
        d.text((cx, cy), name, fill="black")
        sheet_im.paste(im, (cx, cy + lab))
    sheet_im.save(OUT / f"sheet_{stem}.png")


if __name__ == "__main__":
    sys.exit(main())
