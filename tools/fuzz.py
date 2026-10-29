"""
Random-design fuzzer: hunts for crashes and hard-rule violations.

    python -m tools.fuzz --n 300 --seed 1
Uses the same checks as eval_matrix but only reports ERR findings.
"""

from __future__ import annotations

import argparse
import copy
import os
import random
import time

os.environ["CHAMELEON_DEBUG"] = "1"

from engine.resize import resize
from tools.check import check, snapshot
from tools.eval_matrix import CATALOGUE

ASSETS = ["solstice_bg_gradient.png", "solstice_bottle.png", "solstice_logo_lockup.png", "missing_x.png"]
WORDS = ("light chase summer golden hour botanical sparkling infusion honey zest rosemary limited edition "
         "award winning small batch shop now new flavor sale sunrise sunset").split()


def rand_text(r, maxw):
    n = r.choice([0, 1, 2, 3, 5, 8, 15, 30, 60])
    t = " ".join(r.choice(WORDS) for _ in range(n))
    if r.random() < 0.07:
        t += " " + "x" * r.randint(20, 90)
    if r.random() < 0.05:
        t = t.replace(" ", "\n", 1)
    return t


def rand_design(r):
    W = r.choice([300, 640, 728, 800, 1080, 1200, 1600, 2000])
    H = r.choice([90, 250, 400, 628, 800, 1080, 1350, 1920])
    els = []
    if r.random() < 0.8:
        els.append({"id": "bg", "type": "image", "x": 0, "y": 0, "width": W, "height": H, "z_index": 0,
                    "asset_id": ASSETS[0], "fit": r.choice(["cover", "cover", "fill", "contain"])})
    groups = [f"g{i}" for i in range(r.randint(0, 4))]
    for i in range(r.randint(1, 22)):
        kind = r.choice(["text", "text", "text", "image", "shape", "shape"])
        w = r.uniform(0.03, 0.9) * W
        h = r.uniform(0.02, 0.5) * H
        x = r.uniform(-0.1, 1.0) * W if r.random() < 0.1 else r.uniform(0, max(1, W - w))
        y = r.uniform(-0.1, 1.0) * H if r.random() < 0.1 else r.uniform(0, max(1, H - h))
        e = {"id": f"e{i}", "type": kind, "x": round(x, 1), "y": round(y, 1), "width": round(w, 1),
             "height": round(h, 1)}
        if r.random() < 0.8:
            e["z_index"] = r.randint(1, 30)
        if r.random() < 0.15:
            e["rotation"] = r.choice([-45, -12, 5, 30, 90, 180])
        if r.random() < 0.2:
            e["opacity"] = round(r.uniform(0.05, 1), 2)
        if groups and r.random() < 0.3:
            e["group_id"] = r.choice(groups)
        if kind == "text":
            e.update(content=rand_text(r, w), font_family=r.choice(["Solstice Sans", "Solstice Serif", "Arial"]),
                     font_size=round(r.uniform(6, min(120, h * 1.2 + 8)), 1), color="rgba(255,255,255,1)",
                     text_align=r.choice(["left", "center", "right"]))
            if r.random() < 0.2:
                e["font_weight"] = "bold"
            if r.random() < 0.1:
                e["letter_spacing"] = r.choice([-1, 2, 6])
            if r.random() < 0.1:
                e["line_height"] = r.choice([0.9, 1.0, 1.6, 2.2])
            if r.random() < 0.05:
                e["max_lines"] = r.choice([1, 2, 3])
        elif kind == "image":
            e.update(asset_id=r.choice(ASSETS[1:]), fit=r.choice(["contain", "cover"]))
        else:
            e.update(shape_type=r.choice(["rectangle", "ellipse", "line"]),
                     fill_color="rgba(232,106,58,1)")
            if r.random() < 0.3:
                e["corner_radius"] = r.uniform(0, 40)
        els.append(e)
    return {"canvas": {"width": W, "height": H}, "elements": els}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    r = random.Random(args.seed)
    sizes = list(CATALOGUE.values())
    bad = runs = 0
    worst = 0.0
    for i in range(args.n):
        src = rand_design(r)
        w, h = r.choice(sizes)
        if r.random() < 0.2:
            w, h = r.randint(50, 2500), r.randint(50, 2500)
        tgt = {"width": w, "height": h}
        snap = snapshot(src)
        t0 = time.time()
        try:
            out = resize(src, tgt)
            dt = time.time() - t0
            out2 = resize(copy.deepcopy(src), tgt)
            fnd = check(src, snap, out, tgt, dt, out2)
        except Exception as ex:  # noqa: BLE001
            fnd = [("ERR", "crash", f"{type(ex).__name__}: {ex}")]
            dt = time.time() - t0
        worst = max(worst, dt)
        runs += 1
        errs = [f for f in fnd if f[0] == "ERR" and f[1] not in ("tiny-font", "collision", "text-on-shape")]
        if errs:
            bad += 1
            print(f"seed={args.seed} case={i} {src['canvas']['width']}x{src['canvas']['height']} -> {w}x{h}: "
                  + " | ".join(f"{c}: {m}" for _, c, m in errs[:3]))
    print(f"{runs} runs, {bad} with errors, slowest {worst:.2f}s")


if __name__ == "__main__":
    main()
