"""
Builds addon/ : an Adobe Express add-on (manifest v2, panel entry point) that
shows the engine's before/after results and can drop the result onto the
Express canvas. Everything is pre-rendered, so the panel needs no server.

    python -m viewer.build_addon

Load it in Express via Add-ons > Add-on Development > "Connect to local add-on"
(serve the addon/ folder, e.g. `npx @adobe/ccweb-add-on-scripts start` or any
static server over https), or zip the folder for the add-on bundle.
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.resize import resize
from viewer.serve import list_designs, render_b64

OUT = Path("addon")
SIZES = [("Instagram Post", 1080, 1080), ("Instagram Story", 1080, 1920), ("Facebook Post", 1200, 630),
         ("YouTube Thumbnail", 1280, 720), ("LinkedIn Banner", 1584, 396), ("Medium Rectangle", 300, 250)]
DESIGNS = ["chameleon_sample_level1_warmup", "chameleon_sample_level2_core", "chameleon_sample_level3_advanced"]

MANIFEST = {
    "testId": "chameleon-viewer",
    "name": "Chameleon Viewer",
    "version": "1.0.0",
    "manifestVersion": 2,
    "requirements": {"apps": [{"name": "Express", "apiVersion": 1}]},
    "entryPoints": [{"type": "panel", "id": "panel1", "main": "index.html"}],
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chameleon Viewer</title>
<script src="https://new.express.adobe.com/static/add-on-sdk/sdk.js"></script>
<style>
  :root { --bg:#fff; --ink:#1f1722; --mute:#6b6270; --line:#e1dce3; --accent:#e86a3a; }
  @media (prefers-color-scheme: dark) { :root { --bg:#1f1722; --ink:#f3eef5; --mute:#a99fb0; --line:#3a2d42; } }
  body { margin:0; padding:12px; background:var(--bg); color:var(--ink); font:13px/1.4 system-ui, sans-serif; }
  h1 { font-size:15px; margin:0 0 4px; } p { margin:0 0 10px; color:var(--mute); }
  label { display:block; font-size:11px; color:var(--mute); margin:8px 0 3px; }
  select, button { width:100%; font:inherit; padding:7px 8px; border:1px solid var(--line); border-radius:8px;
                   background:var(--bg); color:var(--ink); }
  button { background:var(--accent); border:0; color:#fff; font-weight:600; margin-top:10px; cursor:pointer; }
  button:disabled { opacity:.5; }
  h2 { font-size:11px; color:var(--mute); margin:12px 0 4px; font-weight:600; }
  img { width:100%; display:block; border-radius:6px; background:#8882; }
  #msg { margin-top:6px; min-height:16px; color:var(--mute); }
</style></head>
<body>
<h1>Chameleon</h1>
<p>One design, any canvas: the engine rearranges it instead of stretching it.</p>
<label>Design</label><select id="design"></select>
<label>Target canvas</label><select id="size"></select>
<h2 id="outT">Resized</h2><img id="out" alt="resized design">
<button id="add" disabled>Add result to canvas</button><div id="msg"></div>
<h2 id="srcT">Original</h2><img id="src" alt="original design">
<script>
const DATA = __DATA__;
const $ = id => document.getElementById(id);
$("design").innerHTML = Object.keys(DATA).map(n => `<option>${n}</option>`).join("");
function sizes() { $("size").innerHTML = Object.keys(DATA[$("design").value].results).map(n => `<option>${n}</option>`).join(""); show(); }
function show() {
  const d = DATA[$("design").value];
  $("src").src = "data:image/png;base64," + d.source; $("srcT").textContent = "Original " + d.source_size;
  $("out").src = "data:image/png;base64," + d.results[$("size").value]; $("outT").textContent = "Resized " + $("size").value;
}
$("design").onchange = sizes; $("size").onchange = show; sizes();
// Optional: place the result on the Express canvas when running inside Express.
if (window.addOnUISdk) {
  addOnUISdk.ready.then(() => {
    const b = $("add"); b.disabled = false;
    b.onclick = async () => {
      try {
        const blob = await (await fetch($("out").src)).blob();
        await addOnUISdk.app.document.addImage(blob);
        $("msg").textContent = "Added to your canvas.";
      } catch (e) { $("msg").textContent = "Could not add: " + e.message; }
    };
  });
}
</script></body></html>
"""


def main() -> None:
    avail = list_designs()
    data = {}
    for name in DESIGNS:
        src = json.loads(avail[name].read_text(encoding="utf-8"))["source"]
        entry = {"source": render_b64({"canvas": src["canvas"], "elements": src["elements"]}),
                 "source_size": f'{src["canvas"]["width"]}x{src["canvas"]["height"]}', "results": {}}
        for label, w, h in SIZES:
            entry["results"][f"{label} {w}x{h}"] = render_b64(resize(src, {"width": w, "height": h}))
        data[name] = entry
    OUT.mkdir(exist_ok=True)
    (OUT / "manifest.json").write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
    (OUT / "index.html").write_text(PAGE.replace("__DATA__", json.dumps(data)), encoding="utf-8")
    print("wrote addon/manifest.json and addon/index.html")


if __name__ == "__main__":
    main()
