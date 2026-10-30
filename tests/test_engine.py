"""
Fast invariant tests (stdlib only; no pytest needed).

    python -m tests.test_engine
"""

from __future__ import annotations

import copy
import glob
import json
import math
import os
import sys

os.environ.pop("CHAMELEON_DEBUG", None)  # exercise the production path (fallbacks active)

from engine.resize import _Txt, resize

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def load(name: str) -> dict:
    return json.load(open(name, encoding="utf-8"))


def samples():
    return [load(p)["source"] for p in sorted(glob.glob("input/*.json"))]


def valid(out: dict, src: dict, W: int, H: int) -> bool:
    ids = [e["id"] for e in out["elements"]]
    if ids != [e["id"] for e in src["elements"]] or out["canvas"] != {"width": W, "height": H}:
        return False
    for e in out["elements"]:
        vals = [e["x"], e["y"], e["width"], e["height"]]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals):
            return False
        if e["x"] < -0.5 or e["y"] < -0.5 or e["x"] + e["width"] > W + 0.5 or e["y"] + e["height"] > H + 0.5:
            return False
    return True


def test_malformed_inputs() -> None:
    src = samples()[1]
    variants = {
        "null width": lambda s: s["elements"][5].__setitem__("width", None),
        "string x": lambda s: s["elements"][2].__setitem__("x", "abc"),
        "null font size": lambda s: s["elements"][6].__setitem__("font_size", None),
        "null content": lambda s: s["elements"][7].__setitem__("content", None),
        "nan height": lambda s: s["elements"][4].__setitem__("height", float("nan")),
        "negative size": lambda s: s["elements"][4].__setitem__("width", -50),
        "huge numbers": lambda s: s["elements"][4].__setitem__("width", 1e12),
        "bad style_runs": lambda s: s["elements"][6].__setitem__("style_runs", "oops"),
    }
    for name, mutate in variants.items():
        s = copy.deepcopy(src)
        mutate(s)
        try:
            out = resize(s, {"width": 1600, "height": 400})
            check(f"malformed: {name}", len(out["elements"]) == len(s["elements"]))
        except Exception as ex:  # noqa: BLE001
            check(f"malformed: {name}", False, repr(ex))
    for name, tgt in {"string size": {"width": "800", "height": "600"}, "float size": {"width": 800.4, "height": 600.6},
                      "extra keys": {"width": 800, "height": 600, "name": "x"}}.items():
        out = resize(src, tgt)
        check(f"target: {name}", out["canvas"]["width"] in (800,) and out["canvas"]["height"] in (600, 601))
    check("garbage source does not raise", isinstance(resize({"canvas": None, "elements": [1, None]},
                                                               {"width": 10, "height": 10}), dict))
    check("empty elements", resize({"canvas": {"width": 10, "height": 10}, "elements": []},
                                   {"width": 20, "height": 20})["elements"] == [])


def test_samples_everywhere() -> None:
    sizes = [(1, 1), (16, 16), (320, 50), (728, 90), (300, 250), (1080, 1080), (1080, 1920), (5400, 7200), (90, 3000)]
    ok = True
    for src in samples():
        for w, h in sizes:
            out = resize(src, {"width": w, "height": h})
            if not valid(out, src, w, h):
                ok = False
                print("   invalid:", src["canvas"], "->", w, h)
    check("samples valid at extreme sizes", ok)


def test_determinism_and_purity() -> None:
    src = samples()[2]
    snap = copy.deepcopy(src)
    a = resize(src, {"width": 970, "height": 250})
    b = resize(copy.deepcopy(src), {"width": 970, "height": 250})
    check("deterministic", a == b)
    check("input not mutated", src == snap)


def test_same_aspect_is_uniform() -> None:
    src = samples()[0]
    out = resize(src, {"width": 2160, "height": 2160})
    sx = {e["id"]: e for e in out["elements"]}
    h = next(e for e in src["elements"] if e["id"] == "headline")
    o = sx["headline"]
    check("same aspect scales uniformly", abs(o["x"] - h["x"] * 2) < 1 and abs(o["font_size"] - h["font_size"] * 2) < 0.1)


def test_capitalize_measured_uppercase() -> None:
    el = {"id": "t", "type": "text", "x": 0, "y": 0, "width": 400, "height": 100, "content": "wide wide wide wide",
          "font_family": "Solstice Sans", "font_size": 40, "color": "#fff", "capitalize": True}
    plain = dict(el, capitalize=False)
    check("capitalize widens measured line", _Txt(el).layout(40, 1e9)[1] > _Txt(plain).layout(40, 1e9)[1])
    src = {"canvas": {"width": 1000, "height": 1000}, "elements": [dict(el, width=900, height=300, x=50, y=50)]}
    out = resize(src, {"width": 600, "height": 900})
    o = out["elements"][0]
    check("capitalized text fits its box", _Txt(o).layout(o["font_size"], o["width"])[1] <= o["width"] * 1.01)


def test_tight_line_height_block() -> None:
    for lh in (0.8, 1.0, 1.2, 2.0):
        el = {"id": "t", "type": "text", "x": 0, "y": 0, "width": 300, "height": 300, "content": "aaa bbb ccc ddd eee",
              "font_family": "Solstice Sans", "font_size": 50, "color": "#fff", "line_height": lh}
        t = _Txt(el)
        n, _ = t.layout(50, 300)
        # ink of the last line ends ~1.3 x font size below its line top
        check(f"line_height {lh}: block covers last line ink",
              t.height(n, 50) >= (n - 1) * lh * 50 + 1.25 * 50 - 0.01)


def test_wrap_matches_renderer() -> None:
    try:
        from renderer.converter.brand_fonts import load_brand_font_manager
        from renderer.converter.wrap import char_widths, wrap_text
        fm = load_brand_font_manager("brand_kit/fonts")
    except Exception as ex:  # noqa: BLE001
        print("SKIP wrap parity (renderer not importable):", ex)
        return
    mism = total = 0
    for src in samples():
        for w, h in [(1080, 1920), (728, 90), (300, 250), (1600, 900)]:
            for e in resize(src, {"width": w, "height": h})["elements"]:
                if e["type"] != "text" or not e.get("content"):
                    continue
                wt = "bold" if e.get("font_weight") == "bold" else "regular"
                cw = char_widths(fm, e["font_family"], wt, "bold" if wt == "bold" else "regular",
                                 e["font_size"], e["content"])
                real = wrap_text(e["content"], cw, e["width"]).count("\n") + 1
                total += 1
                mism += real != _Txt(e).layout(e["font_size"], e["width"])[0]
    check(f"wrap parity with renderer ({total} texts)", mism == 0, f"{mism} mismatches")


def test_designs_dir() -> None:
    bad = []
    for p in sorted(glob.glob("tests/designs/*.json")):
        src = load(p)["source"]
        for w, h in [(1080, 1920), (728, 90), (1600, 900)]:
            try:
                out = resize(src, {"width": w, "height": h})
                if len(out["elements"]) != len(src["elements"]):
                    bad.append((os.path.basename(p), w, h))
            except Exception:  # noqa: BLE001
                bad.append((os.path.basename(p), w, h))
    check("all adversarial designs produce output", not bad, str(bad[:3]))


def test_legibility_and_widgets() -> None:
    """Banner sizes keep every text readable; widgets found by the round-3 designs hold together."""
    lvl3 = load("input/chameleon_sample_level3_advanced.json")["source"]
    for w, h in [(728, 90), (300, 250), (970, 250)]:
        out = resize(lvl3, {"width": w, "height": h})
        small = [(e["id"], e["font_size"]) for e in out["elements"] if e["type"] == "text" and e["font_size"] < 6]
        check(f"level 3 at {w}x{h}: no text under 6px", not small, str(small))

    def by_id(out):
        return {e["id"]: e for e in out["elements"]}

    o = by_id(resize(load("tests/designs/t56_icon_rows.json")["source"], {"width": 1080, "height": 1920}))
    paired = all(o[f"lb{i}"]["x"] >= o[f"ic{i}"]["x"] + o[f"ic{i}"]["width"] - 1
                 and o[f"lb{i}"]["y"] < o[f"ic{i}"]["y"] + o[f"ic{i}"]["height"]
                 and o[f"lb{i}"]["y"] + o[f"lb{i}"]["height"] > o[f"ic{i}"]["y"] for i in range(4))
    check("icon + label rows stay paired", paired)

    o = by_id(resize(load("tests/designs/t62_split_panels.json")["source"], {"width": 1080, "height": 1920}))
    check("half-canvas panel is drawn as a full-height band", o["left"]["y"] == 0 and o["left"]["height"] == 1920)

    o = by_id(resize(load("tests/designs/t57_price_tag.json")["source"], {"width": 1600, "height": 400}))
    check("price bubble texts do not overlap", o["amt"]["y"] + o["amt"]["height"] <= o["per"]["y"] + 1)

    o = by_id(resize(load("tests/designs/t55_two_column_landscape.json")["source"], {"width": 1080, "height": 1920}))
    check("call to action stays below the body copy", o["cta_bg"]["y"] > o["s"]["y"] + o["s"]["height"] - 1)


def test_corner_pieces_never_land_on_content() -> None:
    from engine.resize import _ink_box
    from tools.check import check as full_check, snapshot
    for f, w, h in [("input/chameleon_sample_level2_core.json", 320, 50),
                    ("tests/designs/t22_missing_assets.json", 5400, 7200),
                    ("tests/designs/t22_missing_assets.json", 1112, 1350)]:
        src = load(f)["source"]
        out = resize(src, {"width": w, "height": h})
        errs = [c for c in full_check(src, snapshot(src), out, {"width": w, "height": h}, 0.0) if c[0] == "ERR"]
        check(f"no collisions: {os.path.basename(f)} at {w}x{h}", not errs, str(errs[:2]))
    out = resize(load("input/chameleon_sample_level2_core.json")["source"], {"width": 320, "height": 50})
    p = next(e for e in out["elements"] if e["id"] == "product")
    check("hero picture stays visible on a 320x50 banner", max(p["width"], p["height"]) >= 20, str(p["width"]))


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)
