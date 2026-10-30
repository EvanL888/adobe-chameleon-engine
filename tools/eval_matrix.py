"""
Local stress test (NOT the official scorer).

Runs designs (input/*.json + tests/designs/*.json) against a catalogue of
real-world sizes plus sizes derived from each design (same aspect, half,
transposed, +/-3% aspect), applies tools/check.py, optionally renders.

    python -m tools.eval_matrix                       # everything, no rendering
    python -m tools.eval_matrix --render --design t32 --sizes 1080x1920 728x90
    python -m tools.eval_matrix --only-errors
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

os.environ["CHAMELEON_DEBUG"] = "1"  # crashes must be visible, not silently fall back

from PIL import Image, ImageDraw

from engine.resize import resize
from tools.check import check, snapshot

OUT = Path("output/matrix")

CATALOGUE = {
    "Instagram Post": (1080, 1080), "Instagram Portrait": (1080, 1350), "Instagram Story": (1080, 1920),
    "Facebook Post": (1200, 630), "LinkedIn Post": (1200, 627), "YouTube Thumbnail": (1280, 720),
    "Twitter Post": (1600, 900), "Twitter Header": (1500, 500), "Pinterest Pin": (1000, 1500),
    "LinkedIn Banner": (1584, 396), "Facebook Cover": (820, 312), "Etsy Banner": (3360, 840),
    "Leaderboard": (728, 90), "Large Leaderboard": (970, 90), "Web Banner": (468, 60),
    "Mobile Banner": (320, 50), "Billboard": (970, 250), "Medium Rectangle": (300, 250),
    "Half Page": (300, 600), "Skyscraper": (120, 600), "Wide Skyscraper": (160, 600),
    "Email Header": (600, 200), "Business Card": (1050, 600), "Slide 16:9": (1920, 1080),
    "A4 Poster": (2480, 3508), "Big Poster": (5400, 7200),
}


def derived(w, h):
    return {"same-x2": (w * 2, h * 2), "half": (max(1, w // 2), max(1, h // 2)),
            "transpose": (h, w), "aspect+3%": (round(w * 1.03), h), "aspect-3%": (w, round(h * 1.03)),
            "identity": (w, h)}


def load_designs(filter_):
    paths = sorted(Path("input").glob("*.json")) + sorted(Path("tests/designs").glob("*.json"))
    return [p for p in paths if filter_ in p.name]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", nargs="*")
    ap.add_argument("--design", default="")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--only-errors", action="store_true")
    ap.add_argument("--no-derived", action="store_true")
    ap.add_argument("--skip-huge", action="store_true", help="skip canvases > 12 megapixels when rendering")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if args.render:
        from engine.main import render_to_png, ASSETS_DIR

    counts, results = Counter(), []
    worst_time = (0.0, "")
    for dp in load_designs(args.design):
        task = json.loads(dp.read_text(encoding="utf-8"))
        src = task["source"]
        w0, h0 = src["canvas"]["width"], src["canvas"]["height"]
        if args.sizes:
            sizes = {s: tuple(map(int, s.lower().split("x"))) for s in args.sizes}
        else:
            sizes = {f"{n} {w}x{h}": (w, h) for n, (w, h) in CATALOGUE.items()}
            if not args.no_derived:
                sizes.update({f"{n} {w}x{h}": (w, h) for n, (w, h) in derived(w0, h0).items()})
        thumbs = []
        for name, (w, h) in sizes.items():
            tgt = {"width": w, "height": h}
            snap = snapshot(src)
            t0 = time.time()
            try:
                out = resize(src, tgt)
                dt = time.time() - t0
                out2 = resize(copy.deepcopy(src), tgt)
                findings = check(src, snap, out, tgt, dt, out2)
            except Exception as ex:  # noqa: BLE001
                out, dt = None, time.time() - t0
                findings = [("ERR", "crash", f"{type(ex).__name__}: {ex}")]
                traceback.print_exc(limit=3)
            if dt > worst_time[0]:
                worst_time = (dt, f"{dp.stem} {w}x{h}")
            for sev, code, msg in findings:
                counts[(sev, code)] += 1
            results.append({"design": dp.stem, "size": f"{w}x{h}", "findings": findings, "t": round(dt, 2)})
            errs = [f for f in findings if f[0] == "ERR"]
            if findings and (errs or not args.only_errors):
                shown = sorted(findings, key=lambda f: f[0] != "ERR")[:4]  # errors first, never hidden behind warnings
                print(f"{dp.stem[:30]:30s} {w:>5}x{h:<5} " + " | ".join(f"{s} {c}: {m}" for s, c, m in shown))
            if args.render and out and not (args.skip_huge and w * h > 12_000_000):
                png = OUT / f"{dp.stem}__{w}x{h}.png"
                try:
                    render_to_png(out, ASSETS_DIR, png)
                    thumbs.append((f"{name}", png))
                except Exception as ex:  # noqa: BLE001
                    print(f"  render failed {dp.stem} {w}x{h}: {ex}")
        if thumbs:
            sheet(dp.stem, thumbs)

    print("\n== summary ==")
    for (sev, code), n in sorted(counts.items(), key=lambda kv: (kv[0][0], -kv[1])):
        print(f"{sev:5s} {code:20s} {n}")
    print(f"slowest: {worst_time[0]:.2f}s ({worst_time[1]})   runs: {len(results)}")
    (OUT / "report.json").write_text(json.dumps(results, indent=1))
    return 1 if any(s == "ERR" for s, _ in counts) else 0


def sheet(stem, thumbs, cell=380):
    pad, lab = 10, 16
    ims = []
    for name, png in thumbs:
        im = Image.open(png).convert("RGB")
        k = min(cell / im.width, cell / im.height)
        ims.append((name, im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)))
    cols = min(4, len(ims))
    rows = (len(ims) + cols - 1) // cols
    s = Image.new("RGB", (cols * (cell + pad) + pad, rows * (cell + lab + pad) + pad), "#dddddd")
    d = ImageDraw.Draw(s)
    for i, (name, im) in enumerate(ims):
        cx, cy = pad + (i % cols) * (cell + pad), pad + (i // cols) * (cell + lab + pad)
        d.text((cx, cy), name, fill="black")
        s.paste(im, (cx, cy + lab))
    s.save(OUT / f"sheet_{stem}.png")


if __name__ == "__main__":
    sys.exit(main())
