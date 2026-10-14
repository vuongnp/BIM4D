#!/usr/bin/env python3
"""HTTP Server for the Construction Progress Estimator App.

Provides REST APIs for file discovery, interactive landmark matching,
manual coarse pose transformation, one-click ICP alignment, progress estimation,
and serving the interactive 3D WebGL user interface.
"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .engine import (
    apply,
    estimate_progress,
    estimate_progress_from_pairs,
    estimate_progress_from_pose,
    get_demo_perturbation,
    get_scene_preview,
    match_point_pairs,
    read_ply,
    write_ply,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class AppState:
    """Holds active project state and latest computation results."""

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = os.path.abspath(workspace_root or os.getcwd())
        self.browse_root = self.workspace_root

        # Default paths
        self.default_ifc = self._find_relative("sample_house/full_house.ifc")
        self.default_scan = self._find_relative("sample_house/unfinished_scan.ply")
        self.default_gt = self._find_relative("sample_house/ground_truth.json")

        self.last_result: Optional[Dict[str, Any]] = None
        self.last_aligned_points: Optional[List[tuple]] = None

    def _find_relative(self, rel: str) -> Optional[str]:
        p = os.path.join(self.workspace_root, rel)
        return p if os.path.isfile(p) else None

    def allowed(self, path: str) -> bool:
        if not self.browse_root:
            return True
        p = os.path.abspath(path)
        return p == self.browse_root or p.startswith(self.browse_root + os.sep)

    def browse(self, dir_path: Optional[str] = None, ext: Optional[str] = None) -> Dict[str, Any]:
        target = os.path.abspath(dir_path) if dir_path else self.workspace_root
        if not self.allowed(target):
            target = self.workspace_root

        if not os.path.isdir(target):
            target = os.path.dirname(target)
            if not os.path.isdir(target):
                target = self.workspace_root

        filter_ext = ext.lower().strip(".") if ext else None

        entries = []
        try:
            for item in sorted(os.listdir(target)):
                if item.startswith(".") and item not in (".", ".."):
                    continue
                full = os.path.join(target, item)
                is_dir = os.path.isdir(full)

                if not is_dir and filter_ext:
                    if not item.lower().endswith("." + filter_ext):
                        continue

                entries.append({
                    "name": item,
                    "path": full,
                    "rel_path": os.path.relpath(full, self.workspace_root),
                    "is_dir": is_dir,
                    "size_bytes": os.path.getsize(full) if not is_dir else 0,
                })
        except PermissionError:
            pass

        parent = os.path.dirname(target)
        can_go_up = self.allowed(parent) and parent != target

        return {
            "current": target,
            "current_rel": os.path.relpath(target, self.workspace_root),
            "can_up": can_go_up,
            "parent": parent if can_go_up else None,
            "entries": entries,
        }

    def get_preview(self, params: Dict[str, Any]) -> Dict[str, Any]:
        preset = params.get("preset", "house")
        if preset == "apec" and os.path.isdir(os.path.join(self.workspace_root, "apec")):
            default_ifc = os.path.join(self.workspace_root, "apec/apec_site_model.ifc")
            default_scan = os.path.join(self.workspace_root, "apec/pointcloud2.ply")
        elif preset == "complex" and os.path.isdir(os.path.join(self.workspace_root, "sample_complex")):
            default_ifc = os.path.join(self.workspace_root, "sample_complex/complex_facility.ifc")
            default_scan = os.path.join(self.workspace_root, "sample_complex/unfinished_scan.ply")
        else:
            default_ifc = self.default_ifc
            default_scan = self.default_scan

        ifc_str = params.get("ifc_path") or default_ifc
        scan_str = params.get("scan_path") or default_scan

        if not ifc_str or not os.path.isfile(ifc_str):
            raise FileNotFoundError(f"IFC file not found: {ifc_str}")
        if not scan_str or not os.path.isfile(scan_str):
            raise FileNotFoundError(f"Scan file not found: {scan_str}")

        demo_perturb = bool(params.get("demo_perturb", False) in (True, "true", "True", 1, "1"))

        # Parse scan_scale
        raw_scale = params.get("scan_scale")
        if isinstance(raw_scale, (list, tuple)) and len(raw_scale) == 3:
            scan_scale = (float(raw_scale[0]), float(raw_scale[1]), float(raw_scale[2]))
        elif "scale_x" in params or "scale_y" in params or "scale_z" in params:
            scan_scale = (
                float(params.get("scale_x", 1.0)),
                float(params.get("scale_y", 1.0)),
                float(params.get("scale_z", 1.0)),
            )
        elif "scale" in params:
            s_val = float(params["scale"])
            scan_scale = (s_val, s_val, s_val)
        else:
            scan_scale = (1.0, 1.0, 1.0)

        return get_scene_preview(Path(ifc_str), Path(scan_str), demo_perturb=demo_perturb, scan_scale=scan_scale)

    def match_and_estimate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        preset = params.get("preset", "house")
        if preset == "apec" and os.path.isdir(os.path.join(self.workspace_root, "apec")):
            default_ifc = os.path.join(self.workspace_root, "apec/apec_site_model.ifc")
            default_scan = os.path.join(self.workspace_root, "apec/pointcloud2.ply")
        elif preset == "complex" and os.path.isdir(os.path.join(self.workspace_root, "sample_complex")):
            default_ifc = os.path.join(self.workspace_root, "sample_complex/complex_facility.ifc")
            default_scan = os.path.join(self.workspace_root, "sample_complex/unfinished_scan.ply")
        else:
            default_ifc = self.default_ifc
            default_scan = self.default_scan

        ifc_str = params.get("ifc_path") or default_ifc
        scan_str = params.get("scan_path") or default_scan
        gt_str = params.get("gt_path") or self.default_gt

        if not ifc_str or not os.path.isfile(ifc_str):
            raise FileNotFoundError(f"IFC file not found: {ifc_str}")
        if not scan_str or not os.path.isfile(scan_str):
            raise FileNotFoundError(f"Scan file not found: {scan_str}")

        ifc_path = Path(ifc_str)
        scan_path = Path(scan_str)

        if not gt_str or not os.path.isfile(gt_str):
            candidate_gt = scan_path.parent / "ground_truth.json"
            gt_path = candidate_gt if candidate_gt.is_file() else None
        else:
            gt_path = Path(gt_str)

        tol = float(params.get("tolerance_m", 0.12))
        demo_perturb = bool(params.get("demo_perturb", False) in (True, "true", "True", 1, "1"))
        spacing = float(params.get("spacing", 0.15))
        run_icp = bool(params.get("run_icp", True) in (True, "true", "True", 1, "1"))

        # Parse scan_scale
        raw_scale = params.get("scan_scale")
        if isinstance(raw_scale, (list, tuple)) and len(raw_scale) == 3:
            scan_scale = (float(raw_scale[0]), float(raw_scale[1]), float(raw_scale[2]))
        elif "scale_x" in params or "scale_y" in params or "scale_z" in params:
            scan_scale = (
                float(params.get("scale_x", 1.0)),
                float(params.get("scale_y", 1.0)),
                float(params.get("scale_z", 1.0)),
            )
        elif "scale" in params:
            s_val = float(params["scale"])
            scan_scale = (s_val, s_val, s_val)
        else:
            scan_scale = (1.0, 1.0, 1.0)

        raw_scan_pts = params.get("scan_points") or []
        raw_bim_pts = params.get("bim_points") or []

        if len(raw_scan_pts) < 5 or len(raw_bim_pts) < 5:
            raise ValueError(
                f"At least 5 point pairs required for matching (received {len(raw_scan_pts)} scan points and {len(raw_bim_pts)} BIM points)."
            )

        scan_points = [tuple(float(v) for v in p) for p in raw_scan_pts]
        bim_points = [tuple(float(v) for v in p) for p in raw_bim_pts]

        res = estimate_progress_from_pairs(
            ifc_path=ifc_path,
            scan_path=scan_path,
            scan_points=scan_points,
            bim_points=bim_points,
            tolerance_m=tol,
            downsample_spacing=spacing,
            demo_perturb=demo_perturb,
            run_icp=run_icp,
            gt_path=gt_path,
            scan_scale=scan_scale,
        )

        self.last_result = res

        # Transform full scan for export
        raw_scan = read_ply(scan_path)
        if scan_scale and scan_scale != (1.0, 1.0, 1.0):
            sx, sy, sz = scan_scale
            raw_scan = [(p[0] * sx, p[1] * sy, p[2] * sz) for p in raw_scan]
        if demo_perturb:
            pR, pt = get_demo_perturbation()
            raw_scan = [apply(pR, pt, p) for p in raw_scan]

        R = res["registration"]["R"]
        t = res["registration"]["t"]
        self.last_aligned_points = [apply(R, t, p) for p in raw_scan]

        return res

    def match_pose_and_estimate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        preset = params.get("preset", "house")
        if preset == "apec" and os.path.isdir(os.path.join(self.workspace_root, "apec")):
            default_ifc = os.path.join(self.workspace_root, "apec/apec_site_model.ifc")
            default_scan = os.path.join(self.workspace_root, "apec/pointcloud2.ply")
        elif preset == "complex" and os.path.isdir(os.path.join(self.workspace_root, "sample_complex")):
            default_ifc = os.path.join(self.workspace_root, "sample_complex/complex_facility.ifc")
            default_scan = os.path.join(self.workspace_root, "sample_complex/unfinished_scan.ply")
        else:
            default_ifc = self.default_ifc
            default_scan = self.default_scan

        ifc_str = params.get("ifc_path") or default_ifc
        scan_str = params.get("scan_path") or default_scan
        gt_str = params.get("gt_path") or self.default_gt

        if not ifc_str or not os.path.isfile(ifc_str):
            raise FileNotFoundError(f"IFC file not found: {ifc_str}")
        if not scan_str or not os.path.isfile(scan_str):
            raise FileNotFoundError(f"Scan file not found: {scan_str}")

        ifc_path = Path(ifc_str)
        scan_path = Path(scan_str)

        if not gt_str or not os.path.isfile(gt_str):
            candidate_gt = scan_path.parent / "ground_truth.json"
            gt_path = candidate_gt if candidate_gt.is_file() else None
        else:
            gt_path = Path(gt_str)

        tol = float(params.get("tolerance_m", 0.12))
        demo_perturb = bool(params.get("demo_perturb", False) in (True, "true", "True", 1, "1"))
        spacing = float(params.get("spacing", 0.15))
        run_icp = bool(params.get("run_icp", True) in (True, "true", "True", 1, "1"))

        # Parse scan_scale
        raw_scale = params.get("scan_scale")
        if isinstance(raw_scale, (list, tuple)) and len(raw_scale) == 3:
            scan_scale = (float(raw_scale[0]), float(raw_scale[1]), float(raw_scale[2]))
        elif "scale_x" in params or "scale_y" in params or "scale_z" in params:
            scan_scale = (
                float(params.get("scale_x", 1.0)),
                float(params.get("scale_y", 1.0)),
                float(params.get("scale_z", 1.0)),
            )
        elif "scale" in params:
            s_val = float(params["scale"])
            scan_scale = (s_val, s_val, s_val)
        else:
            scan_scale = (1.0, 1.0, 1.0)

        # Parse initial coarse pose (R, t)
        raw_R = params.get("initial_R")
        raw_t = params.get("initial_t")
        if not raw_R or not raw_t:
            raise ValueError("initial_R (3x3) and initial_t (3) are required for manual coarse pose matching.")

        initial_R = [[float(v) for v in row] for row in raw_R]
        initial_t = [float(v) for v in raw_t]

        res = estimate_progress_from_pose(
            ifc_path=ifc_path,
            scan_path=scan_path,
            initial_R=initial_R,
            initial_t=initial_t,
            tolerance_m=tol,
            downsample_spacing=spacing,
            demo_perturb=demo_perturb,
            run_icp=run_icp,
            gt_path=gt_path,
            scan_scale=scan_scale,
        )

        self.last_result = res

        # Transform full scan for export
        raw_scan = read_ply(scan_path)
        if scan_scale and scan_scale != (1.0, 1.0, 1.0):
            sx, sy, sz = scan_scale
            raw_scan = [(p[0] * sx, p[1] * sy, p[2] * sz) for p in raw_scan]
        if demo_perturb:
            pR, pt = get_demo_perturbation()
            raw_scan = [apply(pR, pt, p) for p in raw_scan]

        R = res["registration"]["R"]
        t = res["registration"]["t"]
        self.last_aligned_points = [apply(R, t, p) for p in raw_scan]

        return res

    def run_estimation(self, params: Dict[str, Any]) -> Dict[str, Any]:
        preset = params.get("preset", "house")
        if preset == "apec" and os.path.isdir(os.path.join(self.workspace_root, "apec")):
            default_ifc = os.path.join(self.workspace_root, "apec/apec_site_model.ifc")
            default_scan = os.path.join(self.workspace_root, "apec/pointcloud2.ply")
        elif preset == "complex" and os.path.isdir(os.path.join(self.workspace_root, "sample_complex")):
            default_ifc = os.path.join(self.workspace_root, "sample_complex/complex_facility.ifc")
            default_scan = os.path.join(self.workspace_root, "sample_complex/unfinished_scan.ply")
        else:
            default_ifc = self.default_ifc
            default_scan = self.default_scan

        ifc_str = params.get("ifc_path") or default_ifc
        scan_str = params.get("scan_path") or default_scan
        gt_str = params.get("gt_path") or self.default_gt

        if not ifc_str or not os.path.isfile(ifc_str):
            raise FileNotFoundError(f"IFC file not found: {ifc_str}")
        if not scan_str or not os.path.isfile(scan_str):
            raise FileNotFoundError(f"Scan file not found: {scan_str}")

        ifc_path = Path(ifc_str)
        scan_path = Path(scan_str)

        if not gt_str or not os.path.isfile(gt_str):
            candidate_gt = scan_path.parent / "ground_truth.json"
            gt_path = candidate_gt if candidate_gt.is_file() else None
        else:
            gt_path = Path(gt_str)

        tol = float(params.get("tolerance_m", 0.12))
        demo_perturb = bool(params.get("demo_perturb", False) in (True, "true", "True", 1, "1"))
        spacing = float(params.get("spacing", 0.15))

        res = estimate_progress(
            ifc_path=ifc_path,
            scan_path=scan_path,
            tolerance_m=tol,
            downsample_spacing=spacing,
            demo_perturb=demo_perturb,
            gt_path=gt_path,
        )

        self.last_result = res

        raw_scan = read_ply(scan_path)
        if demo_perturb:
            pR, pt = get_demo_perturbation()
            raw_scan = [apply(pR, pt, p) for p in raw_scan]

        R = res["registration"]["R"]
        t = res["registration"]["t"]
        self.last_aligned_points = [apply(R, t, p) for p in raw_scan]

        return res


def make_handler(app: AppState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            code = args[1] if len(args) > 1 else "-"
            # Always print clean request logs to stdout immediately for user visibility
            print(f"[HTTP {code}] {self.command} {self.path}", flush=True)

        def _send(self, body: bytes, ctype: str = "application/octet-stream", code: int = 200, headers: Optional[Dict[str, str]] = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            if headers:
                for k, v in headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200):
            data = json.dumps(obj).encode("utf-8")
            self._send(data, ctype="application/json; charset=utf-8", code=code)

        def _static(self, rel_path: str):
            rel_path = rel_path.lstrip("/")
            if not rel_path:
                rel_path = "index.html"
            path = os.path.join(STATIC_DIR, rel_path)
            if not os.path.isfile(path):
                return self._send(b"Not found", "text/plain", 404)

            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if path.endswith(".js"):
                ctype = "text/javascript; charset=utf-8"
            elif path.endswith(".css"):
                ctype = "text/css; charset=utf-8"
            elif path.endswith(".html"):
                ctype = "text/html; charset=utf-8"

            with open(path, "rb") as f:
                self._send(f.read(), ctype=ctype)

        def _query(self) -> Dict[str, str]:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            return {k: v[0] for k, v in qs.items()}

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path

            if path in ("", "/"):
                return self._static("index.html")

            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])

            if path == "/api/status":
                q = self._query()
                preset = q.get("preset", "house")
                if preset == "apec" and os.path.isdir(os.path.join(app.workspace_root, "apec")):
                    ifc_p = os.path.join(app.workspace_root, "apec/apec_site_model.ifc")
                    scan_p = os.path.join(app.workspace_root, "apec/pointcloud2.ply")
                    gt_p = None
                elif preset == "complex" and os.path.isdir(os.path.join(app.workspace_root, "sample_complex")):
                    ifc_p = os.path.join(app.workspace_root, "sample_complex/complex_facility.ifc")
                    scan_p = os.path.join(app.workspace_root, "sample_complex/unfinished_scan.ply")
                    gt_p = os.path.join(app.workspace_root, "sample_complex/ground_truth.json")
                else:
                    ifc_p = app.default_ifc
                    scan_p = app.default_scan
                    gt_p = app.default_gt

                return self._json({
                    "ready": True,
                    "workspace": app.workspace_root,
                    "defaults": {
                        "ifc": ifc_p,
                        "ifc_rel": os.path.relpath(ifc_p, app.workspace_root) if ifc_p else None,
                        "scan": scan_p,
                        "scan_rel": os.path.relpath(scan_p, app.workspace_root) if scan_p else None,
                        "gt": gt_p,
                        "gt_rel": os.path.relpath(gt_p, app.workspace_root) if gt_p else None,
                    },
                    "has_cached_result": app.last_result is not None,
                })

            if path == "/api/browse":
                q = self._query()
                res = app.browse(q.get("path"), q.get("ext"))
                return self._json(res)

            if path == "/api/scene_preview":
                try:
                    q = self._query()
                    res = app.get_preview(q)
                    return self._json(res)
                except Exception as exc:
                    traceback.print_exc()
                    return self._json({"error": str(exc), "status": "failed"}, code=400)

            if path == "/api/download_aligned_ply":
                if not app.last_aligned_points:
                    return self._send(b"No aligned scan available yet. Run estimation first.", "text/plain", 400)
                buf = io.StringIO()
                buf.write("ply
format ascii 1.0
")
                buf.write(f"element vertex {len(app.last_aligned_points)}
")
                buf.write("property float x
property float y
property float z
end_header
")
                for x, y, z in app.last_aligned_points:
                    buf.write(f"{x:.4f} {y:.4f} {z:.4f}
")
                ply_bytes = buf.getvalue().encode("utf-8")
                headers = {"Content-Disposition": 'attachment; filename="aligned_scan.ply"'}
                return self._send(ply_bytes, "application/octet-stream", headers=headers)

            if path == "/api/download_csv":
                if not app.last_result or "elements" not in app.last_result:
                    return self._send(b"No estimation results available. Run estimation first.", "text/plain", 400)
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow([
                    "element_id", "ifc_class", "status", "completion_pct",
                    "supported_samples", "surface_samples", "mean_deviation_mm"
                ])
                for elem in app.last_result["elements"]:
                    writer.writerow([
                        elem.get("element_id"),
                        elem.get("ifc_class"),
                        elem.get("estimated_status"),
                        elem.get("completion_pct"),
                        elem.get("supported_samples"),
                        elem.get("surface_samples"),
                        elem.get("mean_deviation_mm"),
                    ])
                csv_bytes = buf.getvalue().encode("utf-8")
                headers = {"Content-Disposition": 'attachment; filename="progress_report.csv"'}
                return self._send(csv_bytes, "text/csv; charset=utf-8", headers=headers)

            return self._send(b"Not found", "text/plain", 404)

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b"{}"

            if path == "/api/estimate":
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                    res = app.run_estimation(payload)
                    return self._json(res)
                except Exception as exc:
                    traceback.print_exc()
                    return self._json({"error": str(exc), "status": "failed"}, code=400)

            if path == "/api/match_pairs":
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                    res = app.match_and_estimate(payload)
                    return self._json(res)
                except Exception as exc:
                    traceback.print_exc()
                    return self._json({"error": str(exc), "status": "failed"}, code=400)

            if path == "/api/match_pose":
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                    res = app.match_pose_and_estimate(payload)
                    return self._json(res)
                except Exception as exc:
                    traceback.print_exc()
                    return self._json({"error": str(exc), "status": "failed"}, code=400)

            if path == "/api/export":
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                    if not app.last_result:
                        return self._json({"error": "No estimation to export"}, code=400)

                    # Default to current project's folder
                    ifc_p = Path(app.last_result.get("paths", {}).get("ifc", "sample_house/full_house.ifc"))
                    folder = ifc_p.parent

                    out_json = payload.get("out_json") or str(folder / "icp_alignment.json")
                    out_ply = payload.get("out_ply") or str(folder / "aligned_scan.ply")

                    p_json = Path(out_json)
                    p_ply = Path(out_ply)

                    p_json.parent.mkdir(parents=True, exist_ok=True)
                    p_ply.parent.mkdir(parents=True, exist_ok=True)

                    p_json.write_text(json.dumps(app.last_result, indent=2) + "
", encoding="utf-8")
                    if app.last_aligned_points:
                        write_ply(p_ply, app.last_aligned_points)

                    return self._json({
                        "status": "success",
                        "written": [str(p_json), str(p_ply)],
                    })
                except Exception as exc:
                    traceback.print_exc()
                    return self._json({"error": str(exc)}, code=400)

            return self._send(b"Not found", "text/plain", 404)

    return Handler


def create_server(app: AppState, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create and bind a ThreadingHTTPServer, attempting fallback ports if busy."""
    current_port = port
    for _ in range(10):
        try:
            server = ThreadingHTTPServer((host, current_port), make_handler(app))
            return server
        except OSError as e:
            if "Address already in use" in str(e):
                current_port += 1
            else:
                raise
    raise OSError(f"Could not bind to {host} on ports {port}-{current_port}")
