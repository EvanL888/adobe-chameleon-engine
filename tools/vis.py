"""
Contact sheet for quick eyeballing: source + several target sizes per design.

    python -m tools.vis t06 t08 --sizes 1080x1920 1600x900 728x90
Writes output/vis/sheet_<design>.png
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ["CHAMELEON_DEBUG"] = "1"

from engine.main import ASSETS_DIR, render_to_png
from engine.resize import resize
import tools.eval_matrix as EM

OUT = Path("output/vis")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("designs", nargs="+", help="substrings of design file names")
    ap.add_argument("--sizes", nargs="*", default=["1080x1920", "1600x900", "970x250", "300x250"])
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    EM.OUT = OUT
    paths = sorted(Path("input").glob("*.json")) + sorted(Path("tests/designs").glob("*.json"))
    for key in args.designs:
        for dp in [p for p in paths if key in p.name]:
            d = json.loads(dp.read_text(encoding="utf-8"))
            th = []
            src_png = OUT / f"{dp.stem}__SRC.png"
            render_to_png({"canvas": d["source"]["canvas"], "elements": d["source"]["elements"]}, ASSETS_DIR, src_png)
            th.append(("SOURCE", src_png))
            for sz in args.sizes:
                w, h = map(int, sz.split("x"))
                try:
                    out = resize(d["source"], {"width": w, "height": h})
                except Exception as ex:  # noqa: BLE001
                    print(dp.stem, sz, "CRASH", ex)
                    continue
                p = OUT / f"{dp.stem}__{sz}.png"
                render_to_png(out, ASSETS_DIR, p)
                th.append((sz, p))
            EM.sheet(dp.stem, th, cell=300)
            print("sheet", dp.stem)


if __name__ == "__main__":
    main()
