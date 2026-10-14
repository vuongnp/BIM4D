#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a self-contained, interactive tolerance heatmap for the house sample.

Run after match_house_icp.py. The output HTML works offline in a browser.
"""

import os
import sys

# On some macOS installations, `python` still means Python 2. Keep this entry
# point parseable there, then run the actual tool with Python 3.
if sys.version_info[0] < 3:
    try:
        os.execvp("python3", ["python3", os.path.abspath(__file__)] + sys.argv[1:])
    except OSError as exc:
        raise SystemExit("Python 3 is required: {}".format(exc))

import argparse
import json
from pathlib import Path

from match_house_icp import Grid, read_ifc_boxes, read_ply, sample_ifc_surfaces


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BIM vs scan · tolerance heatmap</title>
<style>
  :root { color-scheme: light; font-family: system-ui, -apple-system, sans-serif; }
  * { box-sizing: border-box; }
  body { margin: 0; color: #15202b; background: #f3f5f7; }
  header { padding: 18px 24px 12px; background: #fff; border-bottom: 1px solid #dce3e8; }
  h1 { font-size: 21px; margin: 0 0 4px; }
  header p, .note { margin: 0; color: #586674; font-size: 13px; line-height: 1.5; }
  main { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px; padding: 16px; max-width: 1600px; margin: auto; }
  .panel { background: #fff; border: 1px solid #dce3e8; border-radius: 10px; overflow: hidden; }
  .toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 10px 18px; padding: 12px 15px; border-bottom: 1px solid #e5ebef; }
  .toolbar label { font-size: 13px; font-weight: 600; }
  input[type=range] { width: min(220px, 32vw); vertical-align: middle; accent-color: #087c62; }
  #toleranceValue { display: inline-block; min-width: 54px; font-weight: 700; }
  #canvasWrap { position: relative; height: min(70vh, 740px); min-height: 420px; background: #e8eff1; }
  canvas { width: 100%; height: 100%; display: block; cursor: grab; }
  canvas:active { cursor: grabbing; }
  .hint { position: absolute; bottom: 10px; left: 12px; padding: 5px 8px; border-radius: 5px; background: #fffdfbdc; font-size: 12px; color: #4b5c68; pointer-events: none; }
  .legend { display: flex; align-items: center; gap: 8px; padding: 10px 15px; font-size: 12px; color: #566675; }
  .gradient { display: inline-block; width: 150px; height: 10px; border-radius: 5px; background: linear-gradient(90deg,#078d6d 0%,#f3c94d 50%,#ce3f3b 100%); }
  aside { padding: 16px; align-self: start; }
  .stat { font-size: 34px; font-weight: 750; color: #087c62; line-height: 1; margin: 5px 0 3px; }
  .statlabel { font-size: 12px; color: #62717d; margin-bottom: 18px; }
  h2 { font-size: 15px; margin: 18px 0 10px; }
  .row { display: grid; grid-template-columns: minmax(0, 1fr) 52px; gap: 2px 8px; align-items: center; padding: 6px 0; border-bottom: 1px solid #eef1f3; font-size: 12px; }
  .row .name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row .pct { text-align: right; font-variant-numeric: tabular-nums; font-weight: 700; }
  .bar { grid-column: 1 / -1; height: 4px; background: #edf0f1; border-radius: 4px; overflow: hidden; }
  .fill { height: 100%; }
  .note { margin-top: 16px; }
  @media (max-width: 850px) { main { grid-template-columns: 1fr; } #canvasWrap { height: 55vh; } }
</style>
</head>
<body>
<header>
  <h1>BIM vs unfinished scan</h1>
  <p>Each dot is a surface sample from the complete IFC model. Its color shows the distance to the nearest aligned scan point.</p>
</header>
<main>
  <section class="panel">
    <div class="toolbar">
      <label for="tolerance">Tolerance <input id="tolerance" type="range" min="20" max="200" step="5" value="120"></label>
      <span id="toleranceValue">120 mm</span>
      <label><input id="showScan" type="checkbox"> Show aligned scan</label>
      <button id="resetView" type="button">Reset view</button>
    </div>
    <div id="canvasWrap"><canvas id="view"></canvas><div class="hint">Drag to rotate · Scroll to zoom</div></div>
    <div class="legend"><span>0</span><span class="gradient"></span><span>≥ 2 × tolerance</span></div>
  </section>
  <aside class="panel">
    <div style="padding:16px">
      <div class="stat" id="overall">—</div>
      <div class="statlabel">BIM surface samples within tolerance</div>
      <div id="counts" class="statlabel"></div>
      <h2>Element surface support</h2>
      <div id="elements"></div>
      <p class="note">Green means the BIM surface has nearby scan evidence; red marks unmatched design surfaces. This is a surface coverage view, not a built-volume measurement. Occlusion can also leave a built surface red.</p>
    </div>
  </aside>
</main>
<script>
const data = __DATA__;
const canvas = document.getElementById('view');
const ctx = canvas.getContext('2d');
const tolerance = document.getElementById('tolerance');
const showScan = document.getElementById('showScan');
const valueLabel = document.getElementById('toleranceValue');
const model = data.model;
const scan = data.scan;
let yaw = -0.65, pitch = 0.48, zoom = 1;
let dragging = false, lastX = 0, lastY = 0;
const center = data.center;

function color(distance, tol) {
  const ratio = Math.min(2, distance / tol);
  if (ratio <= 1) {
    const t = ratio;
    return `rgb(${Math.round(7 + 236*t)},${Math.round(141 + 60*t)},${Math.round(109 - 32*t)})`;
  }
  const t = ratio - 1;
  return `rgb(${Math.round(243 - 37*t)},${Math.round(201 - 138*t)},${Math.round(77 - 18*t)})`;
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}

function resize() {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

function projected(p, width, height, scale) {
  const x = p[0] - center[0], y = p[1] - center[1], z = p[2] - center[2];
  const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
  const rx = cy*x - sy*y;
  const ry = sy*x + cy*y;
  return [width/2 + rx*scale, height/2 - (cp*z - sp*ry)*scale, cp*ry + sp*z];
}

function draw() {
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width || !height) return;
  ctx.clearRect(0, 0, width, height);
  const scale = Math.min(width / 12.5, height / 9.0) * zoom;
  if (showScan.checked) {
    ctx.fillStyle = 'rgba(55, 76, 94, 0.22)';
    for (const p of scan) {
      const [x, y] = projected(p, width, height, scale);
      ctx.fillRect(x, y, 2.2, 2.2);
    }
  }
  const tol = Number(tolerance.value) / 1000;
  const projectedModel = model.map(p => ({screen: projected(p, width, height, scale), d: p[3]}));
  projectedModel.sort((a, b) => a.screen[2] - b.screen[2]);
  for (const item of projectedModel) {
    const [x, y] = item.screen;
    ctx.fillStyle = color(item.d, tol);
    ctx.fillRect(x - 1.5, y - 1.5, 3, 3);
  }
}

function updateStats() {
  const tol = Number(tolerance.value) / 1000;
  valueLabel.textContent = `${tolerance.value} mm`;
  const totals = new Array(data.elements.length).fill(0);
  const supported = new Array(data.elements.length).fill(0);
  let allSupported = 0;
  for (const p of model) {
    totals[p[4]]++;
    if (p[3] <= tol) { supported[p[4]]++; allSupported++; }
  }
  document.getElementById('overall').textContent = `${(100*allSupported/model.length).toFixed(1)}%`;
  let complete=0, partial=0, missing=0;
  document.getElementById('elements').innerHTML = data.elements.map((name, i) => {
    const fraction = totals[i] ? supported[i] / totals[i] : 0;
    if (fraction >= 0.85) complete++; else if (fraction < 0.35) missing++; else partial++;
    const percent = (100*fraction).toFixed(0);
    const hue = fraction >= 0.85 ? '#078d6d' : fraction < 0.35 ? '#ce3f3b' : '#d49a1a';
    const safeName = escapeHtml(name);
    return `<div class="row"><span class="name" title="${safeName}">${safeName}</span><span class="pct">${percent}%</span><div class="bar"><div class="fill" style="width:${percent}%;background:${hue}"></div></div></div>`;
  }).join('');
  document.getElementById('counts').textContent = `${complete} complete · ${partial} partial · ${missing} unsupported (heuristic)`;
  draw();
}

canvas.addEventListener('pointerdown', e => { dragging=true; lastX=e.clientX; lastY=e.clientY; canvas.setPointerCapture(e.pointerId); });
canvas.addEventListener('pointermove', e => {
  if (!dragging) return;
  yaw += (e.clientX-lastX)*0.006;
  pitch = Math.max(-1.35, Math.min(1.35, pitch+(e.clientY-lastY)*0.006));
  lastX=e.clientX; lastY=e.clientY; draw();
});
canvas.addEventListener('pointerup', () => { dragging=false; });
canvas.addEventListener('wheel', e => { e.preventDefault(); zoom=Math.max(0.35,Math.min(4,zoom*Math.exp(-e.deltaY*0.001))); draw(); }, {passive:false});
document.getElementById('resetView').addEventListener('click', () => { yaw=-0.65; pitch=0.48; zoom=1; draw(); });
tolerance.addEventListener('input', updateStats);
showScan.addEventListener('change', draw);
window.addEventListener('resize', resize);
resize();
updateStats();
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ifc", type=Path, default=Path("sample_house/full_house.ifc"))
    parser.add_argument("--scan", type=Path, default=Path("sample_house/aligned_scan.ply"))
    parser.add_argument("--out", type=Path, default=Path("sample_house/tolerance_heatmap.html"))
    args = parser.parse_args()

    boxes = read_ifc_boxes(args.ifc)
    scan = read_ply(args.scan)
    model, owners = sample_ifc_surfaces(boxes, 0.15)
    grid = Grid(scan, 0.20)
    max_distance = 0.40
    distances = [grid.nearest(p, max_distance)[1] for p in model]
    lo = [min(p[i] for p in model) for i in range(3)]
    hi = [max(p[i] for p in model) for i in range(3)]
    data = {
        "model": [[round(p[0], 4), round(p[1], 4), round(p[2], 4),
                   round(d, 4), owner] for p, d, owner in zip(model, distances, owners)],
        "scan": [[round(v, 4) for v in p] for p in scan],
        "elements": [box["id"] for box in boxes],
        "center": [(lo[i] + hi[i]) / 2 for i in range(3)],
    }
    embedded = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(HTML.replace("__DATA__", embedded))
    print("Wrote {}: {} BIM samples, {} aligned scan points".format(
        args.out, len(model), len(scan)))


if __name__ == "__main__":
    main()
