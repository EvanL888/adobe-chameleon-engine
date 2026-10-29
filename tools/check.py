"""
Quality / compliance checks for one (source, output) pair. Not the official
scorer: a local approximation of RULES.md plus design-quality heuristics.

Each finding is (severity, code, message); severity is "ERR" or "WARN".
"""

from __future__ import annotations

import copy
import json
import math
from functools import lru_cache
from pathlib import Path

from engine.resize import _Txt, _asset_missing

SCHEMA_PATH = Path("chameleon_design_schema_v0.9.json")
GEOM_KEYS = {"x", "y", "width", "height", "font_size", "letter_spacing", "corner_radius",
             "stroke_width", "fit"}


@lru_cache(maxsize=1)
def _validator():
    try:
        import jsonschema
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        return jsonschema.Draft202012Validator(schema)
    except Exception:
        return None


def _box(e):
    return (e["x"], e["y"], e["width"], e["height"])


def _inter(a, b):
    ix = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    iy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, ix) * max(0.0, iy)


def _area(b):
    return max(0.0, b[2]) * max(0.0, b[3])


def _is_bg(e, cw, ch):
    return (e["type"] in ("image", "shape") and
            _inter(_box(e), (0, 0, cw, ch)) >= 0.85 * cw * ch and not e.get("rotation"))


def _aabb(e):
    r = math.radians(e.get("rotation") or 0)
    c, s = abs(math.cos(r)), abs(math.sin(r))
    fw, fh = e["width"] * c + e["height"] * s, e["width"] * s + e["height"] * c
    cx, cy = e["x"] + e["width"] / 2, e["y"] + e["height"] / 2
    return (cx - fw / 2, cy - fh / 2, fw, fh)


def check(source: dict, source_snapshot: dict, out: dict, target: dict, elapsed: float,
          out2: dict | None = None) -> list[tuple[str, str, str]]:
    F: list[tuple[str, str, str]] = []

    def err(code, msg):
        F.append(("ERR", code, msg))

    def warn(code, msg):
        F.append(("WARN", code, msg))

    if source != source_snapshot:
        err("mutated-input", "resize() modified its input")
    if out2 is not None and out != out2:
        err("nondeterministic", "two runs gave different output")

    W, H = target["width"], target["height"]
    if out.get("canvas") != {"width": W, "height": H}:
        err("canvas", f"canvas {out.get('canvas')} != target {W}x{H}")
    src_els = {e["id"]: e for e in source["elements"]}
    out_els = {e["id"]: e for e in out["elements"]}
    if set(src_els) != set(out_els) or len(out["elements"]) != len(source["elements"]):
        err("ids", f"missing {sorted(set(src_els) - set(out_els))} extra {sorted(set(out_els) - set(src_els))}")
        return F

    v = _validator()
    if v is not None:
        src_msgs = {e.message for e in v.iter_errors(source)}
        for e in v.iter_errors(out):
            if e.message not in src_msgs:
                err("schema", e.message[:140])
                break

    cw, ch = source["canvas"]["width"], source["canvas"]["height"]
    if elapsed > 20:
        err("slow", f"{elapsed:.1f}s")
    elif elapsed > 5:
        warn("slow", f"{elapsed:.1f}s")

    # ---- per element ----------------------------------------------------
    for i, o in out_els.items():
        s = src_els[i]
        for k in ("x", "y", "width", "height"):
            val = o.get(k)
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                err("numeric", f"{i}.{k}={val}")
        if o["width"] < 0 or o["height"] < 0:
            err("numeric", f"{i}: negative size")
        for k, val in s.items():
            if k in GEOM_KEYS:
                continue
            if o.get(k) != val:
                err("props", f"{i}.{k} changed {val!r} -> {o.get(k)!r}")
        if s.get("z_index") != o.get("z_index"):
            err("z-order", f"{i}: z_index changed")
        if o["x"] < -0.5 or o["y"] < -0.5 or o["x"] + o["width"] > W + 0.5 or o["y"] + o["height"] > H + 0.5:
            err("off-canvas", f"{i}")
        elif o.get("rotation"):
            a = _aabb(o)
            if a[0] < -1 or a[1] < -1 or a[0] + a[2] > W + 1 or a[1] + a[3] > H + 1:
                warn("rotated-off-canvas", f"{i}")
        if s["type"] == "image":
            if _is_bg(s, cw, ch):
                if (abs(o["x"]) > .5 or abs(o["y"]) > .5 or abs(o["width"] - W) > .5 or abs(o["height"] - H) > .5):
                    err("bg-not-full", f"{i}")
            elif s["height"] and o["height"] and s.get("fit") != "cover":
                a0, a1 = s["width"] / s["height"], o["width"] / o["height"]
                if abs(a1 / a0 - 1) > 0.05:
                    err("aspect", f"{i}: {a0:.2f}->{a1:.2f}")
        if s["type"] == "text":
            if o["font_size"] < 1:
                err("font", f"{i}: font_size {o['font_size']}")
            elif o["font_size"] < 4:
                (err if min(W, H) >= 200 else warn)("tiny-font", f"{i}: {o['font_size']:.1f}px")
            elif o["font_size"] < 8:
                warn("small-font", f"{i}: {o['font_size']:.1f}px")
            t = _Txt(o)
            n, widest = t.layout(o["font_size"], o["width"])
            ts = _Txt(s)
            ns, ws_ = ts.layout(s["font_size"], s["width"])
            src_overflow_h = ns * ts.f * ts.lh > s["height"] * 1.08
            src_overflow_w = ws_ > s["width"] * 1.001
            if widest > o["width"] * 1.01 + 0.5 and not src_overflow_w:
                err("text-overflow-w", f"{i}: line {widest:.0f} > box {o['width']:.0f}")
            if n * o["font_size"] * t.lh > o["height"] + 1.5 and not src_overflow_h:
                err("text-overflow-h", f"{i}: {n} lines need {n * o['font_size'] * t.lh:.0f} > {o['height']:.0f}")
            if s.get("max_lines") and n > s["max_lines"]:
                warn("max-lines", f"{i}: {n} lines > max_lines {s['max_lines']}")

    # ---- collisions ---------------------------------------------------------
    content = [e for e in out["elements"] if e["type"] in ("text", "image")
               and not _is_bg(src_els[e["id"]], cw, ch)]
    for a in range(len(content)):
        for b in range(a + 1, len(content)):
            ea, eb = content[a], content[b]
            sa, sb = src_els[ea["id"]], src_els[eb["id"]]
            if _inter(_box(sa), _box(sb)) > 0:
                continue
            ov = _inter(_aabb(ea), _aabb(eb))
            if ov > 0.02 * min(_area(_box(ea)), _area(_box(eb))) and ov > 4:
                err("collision", f"{ea['id']} x {eb['id']}")
    for t in (e for e in out["elements"] if e["type"] == "text"):
        for sh in (e for e in out["elements"] if e["type"] == "shape"):
            ss, st = src_els[sh["id"]], src_els[t["id"]]
            if _inter(_box(ss), _box(st)) > 0:
                continue
            ov = _inter(_aabb(t), _aabb(sh))
            if ov > 0.02 * _area(_box(t)) and ov > 4:
                opaque = float(sh.get("opacity", 1)) >= 0.9
                (err if opaque else warn)("text-on-shape", f"{t['id']} x {sh['id']}")

    # ---- group integrity -------------------------------------------------------
    groups: dict[str, list[str]] = {}
    for e in source["elements"]:
        if e.get("group_id"):
            groups.setdefault(e["group_id"], []).append(e["id"])
    for g, ids in groups.items():
        if len(ids) < 2:
            continue
        nt = [i for i in ids if src_els[i]["type"] != "text"] or ids
        if any(src_els[i].get("rotation") for i in ids):
            continue
        gx0 = min(src_els[i]["x"] for i in ids); gy0 = min(src_els[i]["y"] for i in ids)
        gx1 = max(src_els[i]["x"] + src_els[i]["width"] for i in ids)
        gy1 = max(src_els[i]["y"] + src_els[i]["height"] for i in ids)
        if (gx1 - gx0) * (gy1 - gy0) > 0.45 * cw * ch:
            continue  # a "group" spanning most of the canvas is a layout; the engine splits it on purpose

        def bb(m, els):
            xs0 = min(els[i]["x"] for i in m); ys0 = min(els[i]["y"] for i in m)
            xs1 = max(els[i]["x"] + els[i]["width"] for i in m)
            ys1 = max(els[i]["y"] + els[i]["height"] for i in m)
            return xs1 - xs0, ys1 - ys0
        sw, sh_ = bb(nt, src_els)
        ow, oh = bb(nt, out_els)
        if sw > 1 and sh_ > 1 and oh > 0.5 and ow > 0.5:
            r = (ow / sw) / (oh / sh_)
            if abs(r - 1) > 0.08:
                warn("group-distorted", f"{g}: aspect ratio changed x{r:.2f}")
        # members must stay near each other
        cx = [out_els[i]["x"] + out_els[i]["width"] / 2 for i in ids]
        cy = [out_els[i]["y"] + out_els[i]["height"] / 2 for i in ids]
        span = max(max(cx) - min(cx), max(cy) - min(cy))
        sspan = max(max(src_els[i]["x"] + src_els[i]["width"] / 2 for i in ids) -
                    min(src_els[i]["x"] + src_els[i]["width"] / 2 for i in ids),
                    max(src_els[i]["y"] + src_els[i]["height"] / 2 for i in ids) -
                    min(src_els[i]["y"] + src_els[i]["height"] / 2 for i in ids))
        if sspan < 0.3 * max(cw, ch) and span > 0.5 * max(W, H):
            err("group-split", f"{g}: members drifted apart")

    # ---- hierarchy ----------------------------------------------------------------
    texts = [e for e in source["elements"] if e["type"] == "text" and e.get("content", "").strip()]
    bad = 0
    for a in texts:
        for b in texts:
            if a["font_size"] >= 1.3 * b["font_size"]:
                if out_els[a["id"]]["font_size"] < 0.92 * out_els[b["id"]]["font_size"]:
                    bad += 1
                    if bad <= 2:
                        warn("hierarchy", f"{a['id']} ({a['font_size']}) shrank below {b['id']} ({b['font_size']})")

    # ---- composition ---------------------------------------------------------------
    vis = [e for e in out["elements"] if not _is_bg(src_els[e["id"]], cw, ch)
           and e["type"] != "shape" or (e["type"] == "shape" and float(e.get("opacity", 1)) >= 0.9
                                        and not _is_bg(src_els[e["id"]], cw, ch))]
    if vis:
        x0 = min(e["x"] for e in vis); y0 = min(e["y"] for e in vis)
        x1 = max(e["x"] + e["width"] for e in vis); y1 = max(e["y"] + e["height"] for e in vis)
        if (x1 - x0) * (y1 - y0) < 0.2 * W * H and len(vis) > 2:
            warn("sparse", f"content covers {(x1 - x0) * (y1 - y0) / (W * H):.0%} of canvas")
        edge = [e["id"] for e in vis if e["type"] == "text" and
                (e["x"] < 0.004 * W or e["y"] < 0.004 * H or e["x"] + e["width"] > 0.996 * W
                 or e["y"] + e["height"] > 0.996 * H)]
        if edge:
            warn("edge-touch", ",".join(edge[:4]))
    return F


def snapshot(source):
    return copy.deepcopy(source)
