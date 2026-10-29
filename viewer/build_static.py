"""
Builds viewer/gallery.html: a single self-contained page (no server, no
network) with pre-rendered results for the sample designs at common sizes.
Open the file in any browser.

    python -m viewer.build_static
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.resize import resize
from viewer.serve import list_designs, render_b64

SIZES = [("Instagram Post", 1080, 1080), ("Instagram Story", 1080, 1920), ("Facebook Post", 1200, 630),
         ("YouTube Thumbnail", 1280, 720), ("LinkedIn Banner", 1584, 396), ("Leaderboard", 728, 90),
         ("Medium Rectangle", 300, 250), ("Half Page", 300, 600), ("Skyscraper", 160, 600)]
DESIGNS = ["chameleon_sample_level1_warmup", "chameleon_sample_level2_core", "chameleon_sample_level3_advanced",
           "t32_cards_row", "t27_panel_card", "t41_src_banner", "t33_many_chips"]


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
        print("rendered", name)
    page = Path("viewer/index.html").read_text(encoding="utf-8")
    # Reuse the live page's look; swap its script for a static one.
    head, _ = page.split("<div class=\"bar\">")
    body = """
<div class="bar">
  <label>Design <select id="design"></select></label>
  <label>Target size <select id="size"></select></label>
</div>
<div id="status">Pre-rendered gallery. Run <code>python -m viewer.serve</code> to try any size live.</div>
<div class="grid">
  <div class="card"><h2 id="srcTitle">Original</h2><div class="frame"><img id="src" alt="original"></div></div>
  <div class="card"><h2 id="outTitle">Resized</h2><div class="frame"><img id="out" alt="resized"></div></div>
</div>
<script>
const DATA = %s;
const $ = id => document.getElementById(id);
$("design").innerHTML = Object.keys(DATA).map(n => `<option>${n}</option>`).join("");
function fillSizes() {
  $("size").innerHTML = Object.keys(DATA[$("design").value].results).map(n => `<option>${n}</option>`).join("");
  show();
}
function show() {
  const d = DATA[$("design").value];
  $("src").src = "data:image/png;base64," + d.source;
  $("out").src = "data:image/png;base64," + d.results[$("size").value];
  $("srcTitle").textContent = "Original " + d.source_size;
  $("outTitle").textContent = "Resized " + $("size").value;
}
$("design").onchange = fillSizes; $("size").onchange = show;
fillSizes();
</script>
</body>
</html>
""" % json.dumps(data)
    Path("viewer/gallery.html").write_text(head + body, encoding="utf-8")
    print("wrote viewer/gallery.html")


if __name__ == "__main__":
    main()
