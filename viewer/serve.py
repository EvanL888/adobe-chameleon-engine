"""
CHAMELEON viewer: pick a design, pick (or type) a target size, see the source
and the engine's rearranged result side by side, rendered with the real
renderer. Pure standard library + the engine itself; runs offline.

    python -m viewer.serve            # then open http://127.0.0.1:5057
    python -m viewer.serve --port 8080

Run from the repository root (it needs brand_kit/ and input/).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from engine.main import ASSETS_DIR, render_to_png
from engine.resize import resize

ROOT = Path(__file__).resolve().parent
DESIGN_DIRS = [Path("input"), Path("tests/designs")]
MAX_SIDE = 900  # preview size; the renderer still gets the true canvas


def list_designs() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for d in DESIGN_DIRS:
        for p in sorted(d.glob("*.json")):
            found[p.stem] = p
    return found


def render_b64(design: dict) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "r.png"
        render_to_png(design, ASSETS_DIR, png)
        im = Image.open(png).convert("RGB")
    k = min(1.0, MAX_SIDE / max(im.size))
    if k < 1.0:
        im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def run(design_name: str, w: int, h: int) -> dict:
    designs = list_designs()
    if design_name not in designs:
        raise ValueError("unknown design")
    if not (16 <= w <= 8000 and 16 <= h <= 8000):
        raise ValueError("size must be between 16 and 8000 px")
    task = json.loads(designs[design_name].read_text(encoding="utf-8"))
    src = task["source"]
    t0 = time.time()
    out = resize(src, {"width": w, "height": h})
    dt = time.time() - t0
    return {
        "source_png": render_b64({"canvas": src["canvas"], "elements": src["elements"]}),
        "output_png": render_b64(out),
        "output": out,
        "source_size": [src["canvas"]["width"], src["canvas"]["height"]],
        "seconds": round(dt, 3),
        "elements": len(out["elements"]),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/designs":
            self._send(200, json.dumps(list(list_designs())).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        if self.path != "/api/resize":
            self._send(404, b"not found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n))
            res = run(str(req["design"]), int(req["width"]), int(req["height"]))
            self._send(200, json.dumps(res).encode(), "application/json")
        except Exception as ex:  # noqa: BLE001
            self._send(400, json.dumps({"error": str(ex)}).encode(), "application/json")

    def log_message(self, *a):  # keep the console quiet
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5057)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"CHAMELEON viewer running at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
