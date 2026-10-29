"""
Generates adversarial test designs into tests/designs/*.json (same task
format as input/). Every design targets one specific failure mode; the
docstring of each builder says which.

    python -m tools.make_tests
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path("tests/designs")
BGA, BOT, LOGO = "solstice_bg_gradient.png", "solstice_bottle.png", "solstice_logo_lockup.png"
W_ = "rgba(255,255,255,1)"
ORANGE = "rgba(232,106,58,1)"
SERIF, SANS = "Solstice Serif", "Solstice Sans"
LOREM = ("Botanical sparkling infusions blended with wild honey, blood orange zest "
         "and a whisper of smoked rosemary, bottled at the peak of golden hour.")

_z = [0]


def _next_z():
    _z[0] += 1
    return _z[0]


def T(id, x, y, w, h, content, fs=40, fam=SANS, color=W_, align="center", z=None, **kw):
    e = dict(id=id, type="text", x=x, y=y, width=w, height=h, z_index=z if z is not None else _next_z(),
             content=content, font_family=fam, font_size=fs, color=color, text_align=align)
    e.update(kw)
    return e


def I(id, x, y, w, h, asset=BOT, fit="contain", z=None, **kw):
    e = dict(id=id, type="image", x=x, y=y, width=w, height=h, z_index=z if z is not None else _next_z(),
             asset_id=asset, fit=fit)
    e.update(kw)
    return e


def S(id, x, y, w, h, shape="rectangle", fill=ORANGE, z=None, **kw):
    e = dict(id=id, type="shape", x=x, y=y, width=w, height=h, z_index=z if z is not None else _next_z(),
             shape_type=shape, fill_color=fill)
    e.update(kw)
    return e


def BG(W, H, **kw):
    return I("bg", 0, 0, W, H, BGA, "cover", z=0, **kw)


def button(prefix, x, y, w, h, label, group=None, fs=22):
    g = group or prefix
    return [S(prefix + "_bg", x, y, w, h, corner_radius=h / 2, group_id=g),
            T(prefix + "_label", x, y + (h - fs * 1.2) / 2, w, fs * 1.2, label, fs=fs,
              bold=None, font_weight="bold", group_id=g)]


def task(name, W, H, els, note):
    for e in els:
        e.pop("bold", None)
    return name, {"task_id": name, "difficulty": 0, "difficulty_label": "custom", "notes": note,
                  "source": {"canvas": {"width": W, "height": H}, "elements": els},
                  "target_canvas": {"width": W, "height": H}}


def build():
    D = []
    _z[0] = 0

    def add(name, W, H, els, note):
        D.append(task(name, W, H, els, note))

    # ---- minimal / degenerate inputs -------------------------------------
    add("t01_only_bg", 1080, 1080, [BG(1080, 1080)], "only a background, nothing else")
    add("t02_single_text", 1080, 1080, [BG(1080, 1080),
        T("h", 140, 440, 800, 200, "Chase the Light", 90, SERIF, font_weight="bold")], "one text element")
    add("t03_no_bg", 1080, 1350, [
        I("logo", 380, 60, 320, 107, LOGO), I("p", 290, 300, 500, 500, BOT),
        T("h", 90, 860, 900, 150, "Chase the Light", 80, SERIF, font_weight="bold", color="rgba(43,27,46,1)")],
        "no background element at all")
    add("t04_single_image", 1080, 1080, [I("p", 190, 190, 700, 700, BOT)], "just one picture")
    add("t05_empty_text", 1080, 1080, [BG(1080, 1080),
        T("empty", 140, 200, 800, 100, "", 50), T("blank", 140, 320, 800, 100, "   ", 50),
        T("h", 140, 500, 800, 150, "Real headline", 80, SERIF, font_weight="bold")],
        "empty and whitespace-only text")

    # ---- text edge cases ---------------------------------------------------
    add("t06_long_word", 1080, 1080, [BG(1080, 1080),
        T("h", 90, 300, 900, 200, "Supercalifragilisticexpialidocious-Antidisestablishmentarianism-Pneumonoultramicroscopic",
          60, SERIF, font_weight="bold"),
        T("s", 140, 600, 800, 120, "Short normal subheading under it", 30)], "unbreakable 80-char word")
    add("t07_newlines", 1080, 1350, [BG(1080, 1350),
        T("poem", 140, 300, 800, 500, "Roses are red\nViolets are blue\n\nSugar is sweet\nAnd so are you", 52,
          SERIF, align="left", line_height=1.5),
        T("by", 140, 900, 800, 60, "— anonymous", 28, align="right")], "explicit newlines, left/right align")
    add("t08_cjk_emoji", 1080, 1080, [BG(1080, 1080),
        T("zh", 90, 250, 900, 200, "夏日限定气泡饮品现已上市欢迎品尝", 60, bold=None, font_weight="bold"),
        T("em", 140, 520, 800, 100, "Summer ☀️🍹 déjà-vu naïve café", 44),
        T("jp", 140, 700, 800, 100, "黄金の時間を追いかけて", 40)], "CJK (no spaces), emoji, accents")
    add("t09_unknown_font", 1080, 1080, [BG(1080, 1080),
        T("h", 90, 300, 900, 200, "Fallback font headline text", 64, "Arial", font_weight="bold"),
        T("s", 140, 560, 800, 120, "Another family that is not in the brand kit", 32, "Comic Sans MS")],
        "font families not in brand kit")
    add("t10_style_runs", 1080, 1080, [BG(1080, 1080),
        T("h", 90, 300, 900, 200, "Chase the Light today", 70, SERIF, font_weight="bold",
          style_runs=[{"start": 6, "end": 9, "color": "rgba(240,178,66,1)", "font_style": "italic"},
                      {"start": 10, "end": 15, "font_weight": "regular"}]),
        T("i", 140, 560, 800, 100, "Italic subheading line here", 36, font_style="italic")],
        "style runs and italics")
    add("t11_spacing_lineheight", 1080, 1080, [BG(1080, 1080),
        T("a", 90, 150, 900, 300, "WIDE TRACKED HEADLINE FOR TEST", 56, SERIF, font_weight="bold",
          letter_spacing=8, line_height=1.0),
        T("b", 140, 520, 800, 400, "Loose leading paragraph " + LOREM, 34, line_height=2.0)],
        "letter_spacing and non-default line_height")
    add("t12_max_lines", 1080, 1080, [BG(1080, 1080),
        T("h", 90, 300, 900, 160, "A headline that insists on at most two lines no matter what happens next",
          54, SERIF, font_weight="bold", max_lines=2),
        T("s", 140, 560, 800, 120, LOREM, 30, max_lines=3)], "max_lines present")
    add("t13_align_mix", 1080, 1350, [BG(1080, 1350),
        T("l", 80, 200, 600, 150, "Left aligned title", 60, SERIF, align="left", font_weight="bold"),
        T("c", 140, 450, 800, 150, "Centered middle line of text", 50),
        T("r", 400, 700, 600, 150, "Right aligned closing note", 44, align="right"),
        T("l2", 80, 900, 700, 200, LOREM, 30, align="left")], "left/center/right mixed")
    add("t14_huge_font", 1080, 1080, [BG(1080, 1080),
        T("h", 40, 200, 1000, 400, "BOOM", 330, SERIF, font_weight="bold"),
        T("s", 140, 750, 800, 100, "tiny caption", 26)], "enormous headline font vs tiny caption")
    add("t15_tiny_text", 1080, 1350, [BG(1080, 1350),
        T("a", 60, 200, 960, 40, "All of this copy is only eight pixels tall " * 3, 8, align="left"),
        T("b", 60, 300, 960, 40, "Second micro line " * 6, 9, align="left"),
        T("h", 140, 600, 800, 100, "Headline", 70, SERIF, font_weight="bold")], "very small source text")

    # ---- geometry edge cases ------------------------------------------------
    add("t16_rotations", 1080, 1350, [BG(1080, 1350),
        I("r90", 100, 100, 300, 100, LOGO, rotation=90),
        I("r45", 600, 100, 300, 300, BOT, rotation=-45),
        T("rt", 200, 600, 700, 100, "Tilted headline", 60, SERIF, rotation=15, font_weight="bold"),
        S("rb", 300, 900, 400, 80, fill="rgba(220,60,50,1)", rotation=-30, group_id="rib"),
        T("rl", 300, 920, 400, 40, "SALE TAG", 30, font_weight="bold", rotation=-30, group_id="rib")],
        "rotated images, text, and a rotated group")
    add("t17_lines_zero", 1080, 1080, [BG(1080, 1080),
        T("h", 140, 200, 800, 150, "Above the line", 70, SERIF, font_weight="bold"),
        S("rule", 240, 400, 600, 3, fill=W_),
        S("vline", 540, 450, 0, 200, "line", fill=W_, stroke_color=W_, stroke_width=2),
        T("b", 140, 700, 800, 150, "Below the line", 50)], "opaque divider rule, zero-width line")
    add("t18_offcanvas_source", 1080, 1080, [BG(1080, 1080),
        S("bleed1", -200, -200, 600, 600, "ellipse", W_, opacity=0.1),
        S("bleed2", 800, 800, 500, 500, "ellipse", W_, opacity=0.1),
        I("big", -100, 100, 1300, 300, LOGO),
        T("h", 140, 600, 800, 200, "Content with bleeding art", 70, SERIF, font_weight="bold")],
        "elements partly outside the source canvas")
    add("t19_float_coords", 1080, 1350, [BG(1080, 1350),
        I("logo", 380.37, 60.5, 320.25, 106.75, LOGO),
        T("h", 91.3, 620.7, 897.4, 180.2, "Fractional geometry", 61.7, SERIF, font_weight="bold"),
        T("s", 141.9, 840.1, 796.3, 100.9, LOREM, 23.3)], "fractional coordinates and font sizes")
    add("t20_missing_fields", 1080, 1350, [
        {k: v for k, v in BG(1080, 1350).items() if k != "z_index"},
        {"id": "h", "type": "text", "x": 90, "y": 500, "width": 900, "height": 160, "content": "Bare minimum fields",
         "font_family": SERIF, "font_size": 70, "color": W_},
        {"id": "s", "type": "text", "x": 140, "y": 700, "width": 800, "height": 100, "content": LOREM,
         "font_family": SANS, "font_size": 28, "color": W_}], "no z_index / opacity / align / weight / group_id")
    add("t21_same_everything", 1080, 1080, [BG(1080, 1080), I("logo", 380, 60, 320, 107, LOGO),
        T("h", 90, 500, 900, 150, "Identity resize", 80, SERIF, font_weight="bold")], "target == source size")

    # ---- missing / duplicate assets --------------------------------------------
    add("t22_missing_assets", 1080, 1350, [BG(1080, 1350),
        I("m_hero", 240, 200, 600, 600, "nope_hero.png"),
        I("m_corner", 880, 1150, 160, 120, "nope_corner.png"),
        T("h", 90, 850, 900, 160, "Hero asset is missing", 70, SERIF, font_weight="bold")],
        "large missing hero + missing corner asset")
    add("t23_missing_bg", 1080, 1080, [I("bg", 0, 0, 1080, 1080, "gone_bg.png", "cover", z=0),
        T("h", 90, 440, 900, 160, "Background asset missing", 70, SERIF, font_weight="bold")],
        "full-bleed background asset is missing")
    add("t24_dup_assets", 1080, 1350, [BG(1080, 1350),
        I("p1", 80, 200, 300, 300), I("p2", 390, 200, 300, 300), I("p3", 700, 200, 300, 300),
        T("h", 90, 600, 900, 150, "Same asset three times", 64, SERIF, font_weight="bold")],
        "same asset_id reused")

    # ---- backgrounds / overlays / panels ------------------------------------------
    add("t25_bg_variants", 1080, 1350, [
        I("bg", 0, 0, 1080, 1350, BGA, "fill", z=0),
        S("scrim", 0, 0, 1080, 1350, fill="rgba(0,0,0,1)", opacity=0.45, z=1),
        T("h", 90, 500, 900, 160, "Fill bg + full scrim", 70, SERIF, font_weight="bold")],
        "bg with fit=fill and a full-bleed translucent overlay")
    add("t26_bg_not_full", 1080, 1350, [
        I("bg", 40, 40, 1000, 1270, BGA, "cover", z=0),
        T("h", 140, 500, 800, 160, "Inset background", 70, SERIF, font_weight="bold"),
        T("s", 140, 700, 800, 100, LOREM, 28)], "bg image covers ~79% (not detected as full-bleed)")
    add("t27_panel_card", 1080, 1350, [BG(1080, 1350),
        S("panel", 140, 300, 800, 700, fill="rgba(255,255,255,1)", corner_radius=40, z=1),
        T("h", 190, 360, 700, 160, "Card headline", 70, SERIF, color="rgba(43,27,46,1)", font_weight="bold"),
        T("b", 190, 560, 700, 300, LOREM, 32, color="rgba(43,27,46,1)", align="left"),
        *[e for e in button("cta", 340, 880, 400, 80, "Buy now")]], "opaque panel behind text, ungrouped")
    add("t28_translucent_scrim_rect", 1080, 1350, [BG(1080, 1350),
        S("scrim", 0, 760, 1080, 590, fill="rgba(0,0,0,1)", opacity=0.55, z=1),
        T("h", 90, 820, 900, 160, "Text on a bottom scrim", 66, SERIF, font_weight="bold"),
        T("s", 140, 1020, 800, 200, LOREM, 30), I("p", 340, 120, 400, 400, BOT)],
        "translucent rectangle behind text (should travel with it)")
    add("t29_text_over_photo", 1080, 1080, [BG(1080, 1080),
        I("photo", 100, 100, 880, 880, BOT, z=2),
        T("cap", 200, 700, 680, 120, "Caption on the picture", 56, SERIF, z=3, font_weight="bold"),
        T("tag", 140, 1010, 800, 50, "outside caption", 26)], "text sitting on top of a picture")

    # ---- groups ----------------------------------------------------------------------
    add("t30_giant_group", 1080, 1350, [BG(1080, 1350),
        I("logo", 380, 40, 320, 107, LOGO, group_id="all"),
        T("h", 90, 600, 900, 150, "Everything is one group", 70, SERIF, group_id="all", font_weight="bold"),
        T("foot", 60, 1280, 960, 40, "footer far away but same group", 24, group_id="all")],
        "one group spanning the whole canvas")
    add("t31_single_member_group", 1080, 1080, [BG(1080, 1080),
        T("h", 90, 300, 900, 150, "Lonely group member", 70, SERIF, group_id="solo", font_weight="bold"),
        T("s", 140, 520, 800, 100, "Normal text", 32)], "group with a single member")
    add("t32_cards_row", 1600, 900, [BG(1600, 900),
        T("title", 300, 60, 1000, 120, "Three features", 80, SERIF, font_weight="bold"),
        *sum([[S(f"c{i}_bg", 100 + i * 490, 260, 420, 520, fill="rgba(255,255,255,1)", opacity=0.92, corner_radius=30,
                 group_id=f"c{i}"),
               I(f"c{i}_icon", 230 + i * 490, 290, 160, 160, BOT, group_id=f"c{i}"),
               T(f"c{i}_t", 120 + i * 490, 480, 380, 60, f"Feature {i + 1}", 40, SERIF,
                 color="rgba(43,27,46,1)", group_id=f"c{i}", font_weight="bold"),
               T(f"c{i}_b", 120 + i * 490, 560, 380, 180, "Short description of what this does well.", 26,
                 color="rgba(43,27,46,1)", group_id=f"c{i}")] for i in range(3)], [])],
        "three card groups side by side (wide -> tall must reflow)")
    add("t33_many_chips", 1080, 1350, [BG(1080, 1350),
        T("h", 90, 200, 900, 150, "Seven chips", 70, SERIF, font_weight="bold"),
        *sum([button(f"chip{i}", 60 + (i % 4) * 250, 500 + (i // 4) * 80, 230, 56, f"TAG {i + 1}", fs=18)
              for i in range(7)], [])], "seven similar chips on two rows")
    add("t34_ellipse_badge", 1080, 1080, [BG(1080, 1080),
        S("circ", 760, 80, 240, 240, "ellipse", ORANGE, group_id="b"),
        T("circ_l", 760, 170, 240, 60, "-30%", 44, font_weight="bold", group_id="b"),
        T("h", 90, 500, 900, 150, "Round badge", 80, SERIF, font_weight="bold")], "circular badge keeps its shape")

    # ---- many elements / stress ---------------------------------------------------------
    add("t35_many_text_grid", 1080, 1350, [BG(1080, 1350)] + [
        T(f"g{i}", 60 + (i % 3) * 330, 80 + (i // 3) * 150, 300, 100, f"Item {i + 1}: short label {i}", 30)
        for i in range(24)], "24 text boxes in a grid")
    add("t36_many_images", 1080, 1080, [BG(1080, 1080), T("h", 140, 40, 800, 100, "Gallery", 70, SERIF)] + [
        I(f"im{i}", 60 + (i % 4) * 245, 200 + (i // 4) * 245, 220, 220, BOT if i % 2 else LOGO)
        for i in range(12)], "12 pictures in a grid")
    add("t37_sixty_elements", 1080, 1350, [BG(1080, 1350)] + [
        (T(f"x{i}", 40 + (i % 5) * 200, 40 + (i // 5) * 100, 180, 70, f"Label {i}", 22) if i % 3
         else S(f"y{i}", 40 + (i % 5) * 200, 40 + (i // 5) * 100, 180, 70, fill=ORANGE, opacity=1.0))
        for i in range(60)], "60 mixed elements (performance)")
    add("t38_deco_heavy", 1080, 1350, [BG(1080, 1350)] + [
        S(f"d{i}", (i * 173) % 1000, (i * 331) % 1250, 80 + (i % 4) * 60, 80 + (i % 4) * 60,
          "ellipse" if i % 2 else "rectangle", W_, opacity=0.08 + 0.02 * (i % 5)) for i in range(14)] + [
        T("h", 90, 500, 900, 160, "Lots of decoration", 70, SERIF, font_weight="bold"),
        T("s", 140, 700, 800, 120, LOREM, 30)], "14 decorative shapes")
    add("t39_source_collisions", 1080, 1080, [BG(1080, 1080),
        T("a", 140, 300, 800, 120, "First line crowded", 70, SERIF, font_weight="bold"),
        T("b", 140, 380, 800, 100, "Second line overlapping it", 50),
        I("p", 300, 400, 500, 500, BOT)], "source already has partial overlaps")

    # ---- anchored items -------------------------------------------------------------------
    add("t40_corner_logo", 1080, 1350, [BG(1080, 1350),
        I("corner", 900, 1220, 150, 50, LOGO),
        T("h", 90, 500, 900, 160, "Logo lives in the corner", 70, SERIF, font_weight="bold"),
        T("s", 140, 700, 800, 150, LOREM, 30), I("p", 340, 880, 400, 300, BOT)], "small logo in a corner")

    # ---- reverse direction sources ----------------------------------------------------------
    add("t41_src_banner", 1600, 400, [BG(1600, 400),
        I("logo", 40, 150, 260, 87, LOGO), I("p", 360, 30, 340, 340, BOT),
        T("h", 760, 90, 560, 120, "Banner headline here", 56, SERIF, align="left", font_weight="bold"),
        T("s", 760, 220, 560, 80, "Short supporting line", 26, align="left"),
        *button("cta", 1340, 150, 220, 70, "Shop now", fs=20)], "wide banner source -> tall targets")
    add("t42_src_story", 1080, 1920, [BG(1080, 1920),
        I("logo", 380, 100, 320, 107, LOGO), I("p", 190, 380, 700, 700, BOT),
        T("h", 90, 1180, 900, 220, "Tall story headline", 100, SERIF, font_weight="bold"),
        T("s", 140, 1440, 800, 150, LOREM, 34),
        *button("cta", 290, 1680, 500, 110, "Swipe up", fs=36)], "story source -> landscape targets")
    add("t43_src_leaderboard", 728, 90, [BG(728, 90),
        I("logo", 10, 15, 150, 50, LOGO), T("h", 180, 20, 360, 50, "Tiny banner source", 26, SERIF, align="left"),
        *button("cta", 560, 20, 150, 50, "Go", fs=18)], "728x90 source -> everything else")
    add("t44_src_wide_skinny_text", 1200, 300, [BG(1200, 300),
        T("h", 40, 40, 1120, 100, "One long wide single line headline that spans the banner nicely", 44, SERIF,
          font_weight="bold"),
        T("s", 40, 170, 1120, 80, "And a single wide supporting line below it", 26)],
        "text-only wide source")

    # ---- misc --------------------------------------------------------------------------------
    add("t45_opacity_text", 1080, 1080, [BG(1080, 1080),
        T("ghost", 90, 300, 900, 300, "GHOST", 240, SERIF, opacity=0.2, font_weight="bold", z=1),
        T("h", 140, 450, 800, 150, "Text over ghost word", 64, SERIF, font_weight="bold", z=2)],
        "faint giant word behind a headline (union as layered text)")
    add("t46_quote", 1080, 1350, [BG(1080, 1350),
        T("q", 90, 280, 900, 500, "“Design is not just what it looks like and feels like. Design is how it works.”",
          58, SERIF, align="left", font_style="italic"),
        T("a", 90, 900, 900, 60, "— someone wise", 30, align="right")], "long quote + attribution")
    return D


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()
    designs = build()
    for name, d in designs:
        (OUT / f"{name}.json").write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(designs)} designs to {OUT}/")


if __name__ == "__main__":
    main()
