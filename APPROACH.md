# Our approach: rule-based layout search

`engine/resize.py` is a deterministic, offline engine. No model, no network, only Pillow (already in `requirements.txt`) for font metrics.

1. **Classify.** Full-bleed images/shapes become the background (set to cover the new canvas). Translucent or unframed shapes are decoration. Images whose asset is missing from `brand_kit/assets` are parked as small corner pieces and never crash the run.
2. **Bundle.** Elements sharing a `group_id`, an opaque panel with text on it, text sitting on a picture, and overlapping layers become *rigid clusters* that scale as one (so button + label, ribbon + label stay together). Three or more similar chips on one row become a wrapping flow row (badges). Everything else is a free text or image unit.
3. **Search.** Units are sorted into reading order and split into 1-4 columns in every contiguous way, with several column-width recipes. For each, a binary search finds the largest uniform scale at which everything fits. Text is wrapped with the same greedy rule and font metrics as the renderer, so measured line counts match the render. Candidates are scored on scale, closeness of column count to the canvas shape, legibility (minimum font size) and how big the hero picture can grow.
4. **Place.** The hero image grows into spare height; gaps are stretched in proportion to the source gaps and the composition keeps its source vertical extent (logo high, CTA low). Labels are centred on their button/badge shapes.
5. **Decorations** are re-anchored to the nearest corner at a scaled size, choosing the spot with the least overlap with text.

Hard rules honoured: same ids, nothing off-canvas, images keep aspect ratio (backgrounds crop with `cover`), fonts/copy unchanged, no pixel generation. If anything throws, a uniform contain-scale fallback is returned so the engine never crashes.

## Run

```bash
pip install -r requirements.txt
python -m engine.main                 # samples in input/ -> output/
python -m tools.eval_matrix           # stress test across 16 catalogue sizes, contact sheets in output/matrix/
CHAMELEON_DEBUG=1 python -m engine.main   # raise instead of falling back
```

Docker is unchanged from the starter kit (`docker build` / `docker run --network none ...`).

## Known limits

Extreme shapes with lots of copy (e.g. 300x250 or 728x90 from the Level 3 design) produce very small text because we may not drop or rewrite elements in stage one.
