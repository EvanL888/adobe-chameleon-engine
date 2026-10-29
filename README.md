# Chameleon engine: rearranging a design for any canvas

Our entry for the Adobe University Hackathon "Chameleon" challenge. Given a design (text, images, shapes) and a new canvas size, the engine returns the same elements re-laid-out so the result still looks designed: nothing stretched, nothing overlapping by accident, text readable, brand intact.

The engine is `engine/resize.py` (pure Python, offline, only Pillow). How it works is explained in [APPROACH.md](APPROACH.md).

## Run it

```bash
pip install -r requirements.txt
python -m engine.main            # every task in input/ -> output/<task>_output.json + .png
```

Exactly as the scorer runs it (Docker, no network):

```bash
docker build --platform linux/amd64 -t my-chameleon-engine .
docker run --rm --network none -v $(pwd)/input:/app/input -v $(pwd)/output:/app/output my-chameleon-engine
```

## Viewer

```bash
python -m viewer.serve           # open http://127.0.0.1:5057
```

Pick a design and any target size; the original and the engine's result are drawn side by side with the real renderer. `viewer/gallery.html` is a pre-rendered version that opens in any browser with no setup (rebuild with `python -m viewer.build_static`).

## Tests

```bash
python -m tests.test_engine                 # fast invariant tests
python -m tools.make_tests                  # regenerate the 54 adversarial designs in tests/designs/
python -m tools.eval_matrix --only-errors   # every design x 32 canvas sizes, strict checks
python -m tools.fuzz --n 200 --seed 1       # random designs, hunts crashes and rule violations
```

See APPROACH.md for what each check covers and the known limits. The organizers' original starter README is kept as [STARTER_README.md](STARTER_README.md); the rules are in [RULES.md](RULES.md).
