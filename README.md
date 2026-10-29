# Chameleon engine

Given a finished design (text, images, shapes) and a new canvas size, the engine returns the **same elements re-laid-out** for that canvas: rearranged, not stretched. It is a deterministic, offline, rule-based Python engine (`engine/resize.py`, only Pillow as a dependency): no model, no network, no image generation.

## Approach

1. **Understand the design.** Detect the full-bleed background (re-cropped with `cover`), decoration, and corner-anchored pieces. Bundle what the designer tied together (same `group_id`, a panel behind text, text on a picture, overlapping layers) into rigid clusters; let look-alike badges, tags and cards re-flow as a row or grid.
2. **Search layouts.** Split the elements, in reading order, into 1-6 columns in every sensible way, and binary-search the largest scale at which everything fits. Text is wrapped and verified with the renderer's own wrapping rule and font metrics, so line counts match the render. Layouts are scored on size, legibility, hero-picture size and a column count that suits the canvas shape.
3. **Place with intent.** Centred or edge-aligned like the source, hierarchy preserved (smaller source text never ends up bigger than larger text), labels centred on their buttons, decorations parked where they overlap least.
4. **Never break the rules.** Same ids, nothing off-canvas, images keep their aspect ratio, fonts and copy untouched, z-order kept. Malformed input is cleaned up, and a safe fallback means the engine cannot crash.

Details, limits and known weaknesses: [APPROACH.md](APPROACH.md).

## Run

```bash
pip install -r requirements.txt
python -m engine.main        # every task in input/ -> output/<task>_output.json + .png
```

As the scorer runs it (Docker, no network):

```bash
docker build --platform linux/amd64 -t my-chameleon-engine .
docker run --rm --network none -v $(pwd)/input:/app/input -v $(pwd)/output:/app/output my-chameleon-engine
```

(PowerShell: use `${PWD}` instead of `$(pwd)`.)

## Tests and extras

```bash
python -m tests.test_engine                 # fast invariants, incl. wrap parity with the real renderer
python -m tools.eval_matrix --only-errors   # 63 adversarial designs x 32 canvas sizes, strict checks
python -m tools.fuzz --n 200 --seed 1       # random designs: crashes and rule violations
python -m viewer.serve                      # viewer: pick a design and size, see before/after (localhost:5057)
```

`viewer/gallery.html` is a pre-rendered viewer that opens in any browser; `addon/` is a small Adobe Express add-on version of it (`python -m viewer.build_addon`). The organizers' starter README is kept as [STARTER_README.md](STARTER_README.md).
