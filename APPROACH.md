# Our approach: rule-based layout search

`engine/resize.py` is a deterministic, offline engine. No model, no network, only Pillow (already in `requirements.txt`) for font metrics.

## How it works

1. **Same shape, just scale.** If the target aspect ratio is within ~2% of the source, nothing needs rearranging: everything is scaled uniformly (this also covers identity). Text that already overflowed its source box is shrunk to fit.
2. **Classify.** Full-bleed images/shapes become the background (set to cover the new canvas; a missing background asset still counts). Faint shapes are decoration. Small pictures parked in a corner (watermark, corner logo) are *anchored* and stay in a corner. Small missing assets are parked too; large missing assets are treated as a normal picture.
3. **Bundle.** Pieces the designer tied together are *rigid clusters* that scale as one: same `group_id`, a visible panel/scrim behind text or a picture, text sitting on a picture, overlapping layers. A "group" spanning most of the canvas is treated as a layout and split. Look-alike widgets (badges, tags, feature cards, a grid of thumbnails) become a wrapping *flow row* so they can re-form as a row or a grid. Siblings that land in different columns are forced to the same width, so they end up the same size.
4. **Search.** Units are sorted into reading order and split into 1-4 columns in every contiguous way (sampled when there are many), with several column-width recipes. A coarse binary search finds the largest uniform scale at which everything fits; the best few candidates are then evaluated exactly. Text is wrapped with a re-implementation of the renderer's own greedy wrap and the brand fonts' glyph widths, so measured line counts match the render. The score rewards scale, a column count that suits the canvas shape (tall canvas -> one column), legibility (smallest font) and how big the hero picture can grow. If text cannot reach its minimum size, the minimum is relaxed in steps.
5. **Place.** The hero picture grows into spare height; gaps are stretched in proportion to the source gaps and the composition keeps the source's vertical extent (logo high, call-to-action low). Labels are centred on their button/badge shape. A smaller source text can never end up larger than a clearly bigger one.
6. **Legibility details.** Pill/badge shapes tighten around their label when a column is narrow (so the label can stay big); logo-like wordmarks keep a minimum width; pictures are never enlarged beyond ~1.25x their native pixels; pictures that shared a left/right edge with aligned text keep that edge; very wide canvases may use up to 5-6 columns. Text height uses the renderer's real first-line offset, so tight `line_height` values (<1) cannot spill into neighbours, and `capitalize` text is measured upper-case.
7. **Decorations and anchored pieces** are re-anchored to the nearest corner and placed where they overlap least with text.

Hard rules honoured: same ids and count, nothing off-canvas (rotated elements: both the rotated outline and the unrotated box), images keep their aspect ratio (backgrounds crop with `cover`), fonts/copy untouched, z-order untouched, corner radius / stroke scale with the shape, `max_lines` respected, no pixel generation. Malformed input (null/NaN/strings in numeric fields, missing canvas, garbage elements) is normalised first; if anything still throws,  a uniform contain-scale fallback is returned so the engine never crashes.

## Run

```bash
pip install -r requirements.txt
python -m engine.main                       # samples in input/ -> output/
CHAMELEON_DEBUG=1 python -m engine.main     # raise instead of silently falling back
```

## Testing (all local, none of it is the official scorer)

```bash
python -m tests.test_engine                 # fast invariants (malformed input, wrap parity with the real renderer, ...)
python -m tools.make_tests                  # (re)generate 54 adversarial designs in tests/designs/
python -m tools.eval_matrix --only-errors   # every design x 26 catalogue sizes x 6 derived sizes
python -m tools.eval_matrix --design t32 --render --sizes 1080x1920 728x90
python -m tools.vis t32 t41 --sizes 1080x1920 1600x900   # contact sheets in output/vis/
python -m tools.fuzz --n 200 --seed 1       # random designs, hunts crashes and rule violations
```

`tools/check.py` checks: ids and count, schema validity, input not mutated, determinism, canvas size, finite numbers, off-canvas (also rotated), unchanged non-geometry properties, z-order, background coverage, image aspect (+/-5%), text overflow (width and height, measured with the renderer's wrap), tiny fonts, unintended collisions (text/image and text/opaque shape), group distortion/splitting, font hierarchy, `max_lines`, sparse layouts, runtime.

The adversarial designs cover: empty / whitespace / newline / CJK / emoji / unbreakable-word / huge / tiny / tracked / spaced / italic / style-run text, unknown fonts, `max_lines`; rotated images, text and groups; zero-size lines and dividers; elements bleeding off the source canvas; fractional geometry; missing optional fields; missing hero, corner and background assets; duplicated assets; fill/contain backgrounds, a full-bleed scrim, an inset background; opaque panels, translucent scrims, text over a photo; a canvas-wide group, single-member groups, feature cards, seven chips, a circular badge; 24-text, 12-image and 60-element stress grids; 14 decorative shapes; sources that already collide; a corner logo; banner, story and 728x90 *sources*; ghost text; a long quote.

## Known limits

- Extreme shapes with a lot of copy (e.g. 300x250 or 728x90 from the Level 3 design, or anything <= 90px tall) produce very small text because stage one forbids dropping or rewriting elements. Badge pills are rigid, so in narrow columns their labels get small; a "tight pill" re-fit is a possible improvement.
- Unbreakable words longer than the canvas shrink the whole text to fit (the renderer never breaks mid-word).
- Fonts without CJK/emoji glyphs render as tofu in the renderer; we only measure.
- No contrast/"busy background" analysis: we keep text off other content and decoration but cannot recolour text.
- Docker build not verified on the author's machine (no Docker installed); the engine imports only the stdlib and Pillow.
