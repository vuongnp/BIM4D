#!/usr/bin/env python3
"""Construction progress estimation engine based on BIM matching and ICP registration.

Uses standard Python library only (no external C/numpy dependencies required).
Coordinates and dimensions are in metres.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import struct
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ELEMENT_TYPES = {
    "IFCWALL", "IFCWALLSTANDARDCASE", "IFCSLAB", "IFCDOOR", "IFCWINDOW",
    "IFCCOLUMN", "IFCBEAM", "IFCFLOWSEGMENT", "IFCPIPE", "IFCBUILDINGELEMENTPROXY",
    "IFCROOF", "IFCFOOTING", "IFCSTAIR", "IFCSTAIRFLIGHT", "IFCRAILING", "IFCMEMBER",
    "IFCPLATE", "IFCCURTAINWALL", "IFCFURNISHINGELEMENT",
}


def split_step_args(s: str) -> List[str]:
    """Parse comma-separated STEP entity arguments handling quotes and nested parentheses."""
    result, start, depth, quoted, i = [], 0, 0, False, 0
    while i < len(s):
        char = s[i]
        if char == "'":
            if quoted and i + 1 < len(s) and s[i + 1] == "'":
                i += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                result.append(s[start:i].strip())
                start = i + 1
        i += 1
    result.append(s[start:].strip())
    return result


def step_list(s: str) -> List[str]:
    if not (s.startswith("(") and s.endswith(")")):
        raise ValueError(f"Expected STEP list, got {s}")
    return split_step_args(s[1:-1])


def read_ifc_boxes(path: Path) -> List[Dict[str, Any]]:
    """Parse building elements and their bounding box geometries from an IFC2X3 / IFC4 file.
    Supports both extruded solids (IFCEXTRUDEDAREASOLID) and polygonal / tessellated meshes
    (IFCPOLYGONALFACESET, IFCTRIANGULATEDFACESET, IFCFACETEDBREP).
    """
    entities: Dict[str, Tuple[str, List[str]]] = {}
    content = path.read_text(encoding="utf-8", errors="replace")
    for line in content.splitlines():
        line = line.strip()
        m = re.fullmatch(r"#(\d+)=(\w+)\((.*)\);", line)
        if m:
            entities["#" + m.group(1)] = (m.group(2), split_step_args(m.group(3)))

    def get_ent(ref: str) -> Optional[Tuple[str, List[str]]]:
        return entities.get(ref)

    def point(ref: str) -> Tuple[float, float, float]:
        ent = get_ent(ref)
        if not ent:
            return (0.0, 0.0, 0.0)
        coords = tuple(map(float, step_list(ent[1][0])))
        if len(coords) == 2:
            return (coords[0], coords[1], 0.0)
        return (coords[0], coords[1], coords[2])

    def placement(ref: str) -> Tuple[float, float, float]:
        if ref == "$" or not ref:
            return (0.0, 0.0, 0.0)
        ent = get_ent(ref)
        if not ent or ent[0] != "IFCLOCALPLACEMENT":
            return (0.0, 0.0, 0.0)
        parent, axis_ref = ent[1]
        axis = get_ent(axis_ref)
        if not axis:
            return (0.0, 0.0, 0.0)
        local = point(axis[1][0])
        base = placement(parent)
        return (base[0] + local[0], base[1] + local[1], base[2] + local[2])

    float_pat = r"[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
    point_pat = rf"\(\s*({float_pat})\s*,\s*({float_pat})\s*,\s*({float_pat})\s*\)"

    boxes = []
    for ref, (kind, args) in entities.items():
        if kind not in ELEMENT_TYPES:
            continue
        try:
            elem_id = args[2].strip("'") if len(args) > 2 and args[2] != "$" else ref
            elem_guid = args[0].strip("'") if len(args) > 0 and args[0] != "$" else ref
            rep_ref = args[6] if len(args) > 6 else "$"
            place_ref = args[5] if len(args) > 5 else "$"
            origin = placement(place_ref)

            parsed_box = None
            if rep_ref != "$" and rep_ref in entities:
                prod_shape = get_ent(rep_ref)
                if prod_shape and prod_shape[0] == "IFCPRODUCTDEFINITIONSHAPE":
                    reps_list = step_list(prod_shape[1][2])
                    for r in reps_list:
                        shape_rep = get_ent(r)
                        if not shape_rep or shape_rep[0] != "IFCSHAPEREPRESENTATION":
                            continue
                        items = step_list(shape_rep[1][3])
                        for it in items:
                            item_ent = get_ent(it)
                            if not item_ent:
                                continue
                            it_kind, it_args = item_ent

                            # 1. Extruded Area Solid (Standard rectangular profile)
                            if it_kind == "IFCEXTRUDEDAREASOLID":
                                prof = get_ent(it_args[0])
                                if prof and prof[0] == "IFCRECTANGLEPROFILEDEF":
                                    dx, dy, dz = float(prof[1][3]), float(prof[1][4]), float(it_args[3])
                                    c2d = point(get_ent(prof[1][2])[1][0])
                                    x0 = origin[0] + c2d[0] - dx / 2
                                    y0 = origin[1] + c2d[1] - dy / 2
                                    z0 = origin[2]
                                    x1, y1, z1 = x0 + dx, y0 + dy, z0 + dz
                                    parsed_box = {
                                        "bounds": (
                                            (round(x0, 4), round(y0, 4), round(z0, 4)),
                                            (round(x1, 4), round(y1, 4), round(z1, 4)),
                                        ),
                                        "dimensions": (round(dx, 4), round(dy, 4), round(dz, 4)),
                                        "volume_m3": round(dx * dy * dz, 4),
                                    }
                                    break

                            # 2. Polygonal Face Set or Triangulated Face Set (Mesh representations like APEC)
                            elif it_kind in ("IFCPOLYGONALFACESET", "IFCTRIANGULATEDFACESET"):
                                pt_list_ref = it_args[0]
                                pt_ent = get_ent(pt_list_ref)
                                if pt_ent and pt_ent[0] == "IFCCARTESIANPOINTLIST3D":
                                    raw_pts = ",".join(pt_ent[1])
                                    pts = []
                                    for m in re.finditer(point_pat, raw_pts):
                                        px, py, pz = float(m.group(1)), float(m.group(2)), float(m.group(3))
                                        pts.append((origin[0] + px, origin[1] + py, origin[2] + pz))
                                    if pts:
                                        x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
                                        y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
                                        z0, z1 = min(p[2] for p in pts), max(p[2] for p in pts)
                                        dx = max(0.05, x1 - x0)
                                        dy = max(0.05, y1 - y0)
                                        dz = max(0.05, z1 - z0)
                                        parsed_box = {
                                            "bounds": (
                                                (round(x0, 4), round(y0, 4), round(z0, 4)),
                                                (round(x1, 4), round(y1, 4), round(z1, 4)),
                                            ),
                                            "dimensions": (round(dx, 4), round(dy, 4), round(dz, 4)),
                                            "volume_m3": round(dx * dy * dz, 4),
                                        }
                                        break

                            # 3. Faceted BRep / Shell representations
                            elif it_kind in ("IFCFACETEDBREP", "IFCSHELLBASEDSURFACEMODEL", "IFCMANIFOLDSOLIDBREP"):
                                item_raw = " ".join([entities[k][1] if isinstance(entities[k][1], str) else " ".join(entities[k][1]) for k in re.findall(r"#\d+", " ".join(it_args)) if k in entities])
                                pts = []
                                for m in re.finditer(point_pat, item_raw):
                                    px, py, pz = float(m.group(1)), float(m.group(2)), float(m.group(3))
                                    pts.append((origin[0] + px, origin[1] + py, origin[2] + pz))
                                if pts:
                                    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
                                    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
                                    z0, z1 = min(p[2] for p in pts), max(p[2] for p in pts)
                                    dx = max(0.05, x1 - x0)
                                    dy = max(0.05, y1 - y0)
                                    dz = max(0.05, z1 - z0)
                                    parsed_box = {
                                        "bounds": (
                                            (round(x0, 4), round(y0, 4), round(z0, 4)),
                                            (round(x1, 4), round(y1, 4), round(z1, 4)),
                                        ),
                                        "dimensions": (round(dx, 4), round(dy, 4), round(dz, 4)),
                                        "volume_m3": round(dx * dy * dz, 4),
                                    }
                                    break

                        if parsed_box:
                            break

            if parsed_box:
                lo, hi = parsed_box["bounds"]
                boxes.append({
                    "id": elem_id,
                    "guid": elem_guid,
                    "ifc_class": kind,
                    "bounds": parsed_box["bounds"],
                    "dimensions": parsed_box["dimensions"],
                    "center": (round((lo[0] + hi[0]) / 2, 4), round((lo[1] + hi[1]) / 2, 4), round((lo[2] + hi[2]) / 2, 4)),
                    "volume_m3": parsed_box["volume_m3"],
                })
        except Exception:
            continue

    if not boxes:
        raise ValueError(f"No supported 3D geometry elements found in IFC: {path}")
    return boxes


def read_ply(path: Path) -> List[Tuple[float, float, float]]:
    """Read a PLY file (ASCII or binary little-endian) and return (x, y, z) vertices."""
    with path.open("rb") as f:
        magic = f.readline().strip()
        if magic != b"ply":
            raise ValueError(f"Not a valid PLY file: {path}")
        fmt_line = f.readline().strip().decode("ascii", errors="replace")
        is_binary = "binary_little_endian" in fmt_line
        is_ascii = "ascii" in fmt_line
        if not (is_binary or is_ascii):
            raise ValueError(f"Unsupported PLY format: {fmt_line}")

        vertex_count = 0
        properties = []
        in_vertex = False

        while True:
            line = f.readline()
            if not line:
                break
            line_str = line.strip().decode("ascii", errors="replace")
            words = line_str.split()
            if not words:
                continue
            if words[:2] == ["element", "vertex"]:
                vertex_count = int(words[2])
                in_vertex = True
            elif words[0] == "element":
                in_vertex = False
            elif words[0] == "property" and in_vertex:
                properties.append((words[1], words[2]))  # (type, name)
            elif words[0] == "end_header":
                break

        names = [p[1] for p in properties]
        if any(axis not in names for axis in ("x", "y", "z")):
            raise ValueError("PLY header does not define x, y, z properties")

        ix = names.index("x")
        iy = names.index("y")
        iz = names.index("z")

        points: List[Tuple[float, float, float]] = []

        if is_ascii:
            for _ in range(vertex_count):
                line = f.readline()
                if not line:
                    break
                parts = line.split()
                points.append((float(parts[ix]), float(parts[iy]), float(parts[iz])))
        else:
            type_map = {
                "float": ("f", 4), "float32": ("f", 4), "double": ("d", 8), "float64": ("d", 8),
                "char": ("b", 1), "int8": ("b", 1), "uchar": ("B", 1), "uint8": ("B", 1),
                "short": ("h", 2), "int16": ("h", 2), "ushort": ("H", 2), "uint16": ("H", 2),
                "int": ("i", 4), "int32": ("i", 4), "uint": ("I", 4), "uint32": ("I", 4),
            }
            fmt_str = "<" + "".join(type_map[p[0]][0] for p in properties)
            row_size = struct.calcsize(fmt_str)
            raw = f.read(row_size * vertex_count)
            for i in range(vertex_count):
                offset = i * row_size
                unpacked = struct.unpack_from(fmt_str, raw, offset)
                points.append((float(unpacked[ix]), float(unpacked[iy]), float(unpacked[iz])))

    return points


def write_ply(path: Path, points: List[Tuple[float, float, float]], colors: Optional[List[Tuple[int, int, int]]] = None) -> None:
    """Write vertices to an ASCII PLY file, optionally with RGB colors."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("ply\nformat ascii 1.0\n")
        f.write(f"element vertex {len(points)}\n")
        f.write("property float x\nproperty float y\nproperty float z\n")
        if colors and len(colors) == len(points):
            f.write("property uchar red\nproperty uchar green\nproperty uchar blue\n")
        f.write("end_header\n")
        if colors and len(colors) == len(points):
            for (x, y, z), (r, g, b) in zip(points, colors):
                f.write(f"{x:.6f} {y:.6f} {z:.6f} {int(r)} {int(g)} {int(b)}\n")
        else:
            for x, y, z in points:
                f.write(f"{x:.6f} {y:.6f} {z:.6f}\n")


def inside(bounds: Tuple[Tuple[float, float, float], Tuple[float, float, float]], p: List[float]) -> bool:
    lo, hi = bounds
    return (lo[0] - 1e-7 <= p[0] <= hi[0] + 1e-7 and
            lo[1] - 1e-7 <= p[1] <= hi[1] + 1e-7 and
            lo[2] - 1e-7 <= p[2] <= hi[2] + 1e-7)


def sample_ifc_surfaces(boxes: List[Dict[str, Any]], spacing: float = 0.15) -> Tuple[List[Tuple[float, float, float]], List[int]]:
    """Sample outer surfaces of IFC boxes uniformly, excluding shared interior contact faces."""
    points, owners = [], []
    max_samples_per_face = 200
    for index, box in enumerate(boxes):
        lo, hi = box["bounds"]
        for fixed in range(3):
            a, b = [i for i in range(3) if i != fixed]
            na = max(1, math.ceil((hi[a] - lo[a]) / spacing))
            nb = max(1, math.ceil((hi[b] - lo[b]) / spacing))
            if na * nb > max_samples_per_face:
                scale = math.sqrt(max_samples_per_face / (na * nb))
                na = max(1, int(na * scale))
                nb = max(1, int(nb * scale))
            for side in (0, 1):
                if "slab" in box["id"].lower() and fixed == 2 and side == 0 and lo[2] <= 0.05:
                    continue  # Under-slab ground contact is omitted
                for ia in range(na):
                    for ib in range(nb):
                        p = [0.0, 0.0, 0.0]
                        p[fixed] = lo[fixed] if side == 0 else hi[fixed]
                        p[a] = lo[a] + (ia + 0.5) * (hi[a] - lo[a]) / na
                        p[b] = lo[b] + (ib + 0.5) * (hi[b] - lo[b]) / nb
                        outside = p.copy()
                        outside[fixed] += -0.001 if side == 0 else 0.001
                        if any(inside(other["bounds"], outside) for j, other in enumerate(boxes) if j != index):
                            continue
                        points.append((p[0], p[1], p[2]))
                        owners.append(index)
    return points, owners


def voxel_downsample(points: List[Tuple[float, float, float]], size: float = 0.15) -> List[Tuple[float, float, float]]:
    """Downsample point cloud by keeping one representative point per 3D voxel."""
    cells: Dict[Tuple[int, int, int], Tuple[float, float, float]] = {}
    inv_size = 1.0 / size
    for p in points:
        key = (int(p[0] * inv_size), int(p[1] * inv_size), int(p[2] * inv_size))
        if key not in cells:
            cells[key] = p
    return list(cells.values())


def matvec(R: List[List[float]], p: Tuple[float, float, float]) -> Tuple[float, float, float]:
    return (
        R[0][0] * p[0] + R[0][1] * p[1] + R[0][2] * p[2],
        R[1][0] * p[0] + R[1][1] * p[1] + R[1][2] * p[2],
        R[2][0] * p[0] + R[2][1] * p[1] + R[2][2] * p[2],
    )


def apply(R: List[List[float]], t: List[float], p: Tuple[float, float, float]) -> Tuple[float, float, float]:
    rp = matvec(R, p)
    return (rp[0] + t[0], rp[1] + t[1], rp[2] + t[2])


def slab_initial_pose(scan: List[Tuple[float, float, float]], boxes: List[Dict[str, Any]]) -> Tuple[List[List[float]], List[float]]:
    """Estimate yaw angle and 3D translation from the dense horizontal slab.

    Supports multi-slab foundations (L-shaped, T-shaped) and evaluates 360-degree
    yaw orientations against the IFC base slab footprint to guarantee correct alignment.
    """
    slabs = [b for b in boxes if b.get("ifc_class") == "IFCSLAB" and b["bounds"][0][2] < 0.1 and "roof" not in b.get("id", "").lower()]
    if not slabs:
        slabs = [b for b in boxes if "slab" in b.get("id", "").lower() and "roof" not in b.get("id", "").lower()]
    if not slabs:
        slabs = [b for b in boxes if b.get("ifc_class") == "IFCSLAB"]
    if not slabs:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], [0.0, 0.0, 0.0]

    z_bins = defaultdict(list)
    for p in scan:
        z_bins[round(p[2] / 0.02)].append(p)
    if not z_bins:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], [0.0, 0.0, 0.0]

    slab_points = max(z_bins.values(), key=len)
    if len(slab_points) < 50:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], [0.0, 0.0, 0.0]

    lo = [min(b["bounds"][0][i] for b in slabs) for i in range(3)]
    hi = [max(b["bounds"][1][i] for b in slabs) for i in range(3)]
    target_cx = (lo[0] + hi[0]) / 2
    target_cy = (lo[1] + hi[1]) / 2
    slab_z = sorted(p[2] for p in slab_points)[len(slab_points) // 2]
    tz = hi[2] - slab_z

    def in_slabs_xy(x: float, y: float) -> bool:
        for s in slabs:
            (sx0, sy0, _), (sx1, sy1, _) = s["bounds"]
            if (sx0 - 0.20 <= x <= sx1 + 0.20) and (sy0 - 0.20 <= y <= sy1 + 0.20):
                return True
        return False

    sub_sample = slab_points[::max(1, len(slab_points) // 250)]
    fast_slab = slab_points[::max(1, len(slab_points) // 100)]

    best_deg = 0
    best_score = -1

    # Coarse search in 5-degree increments across 360 degrees (takes ~15ms)
    for deg in range(0, 360, 5):
        rad = math.radians(deg)
        c, s = math.cos(rad), math.sin(rad)
        cand_R = [[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]]
        oriented = [matvec(cand_R, p) for p in fast_slab]
        cx = (min(p[0] for p in oriented) + max(p[0] for p in oriented)) / 2
        cy = (min(p[1] for p in oriented) + max(p[1] for p in oriented)) / 2
        cand_t = [target_cx - cx, target_cy - cy, tz]

        score = sum(1 for p in sub_sample if in_slabs_xy(
            cand_R[0][0] * p[0] + cand_R[0][1] * p[1] + cand_t[0],
            cand_R[1][0] * p[0] + cand_R[1][1] * p[1] + cand_t[1]
        ))
        if score > best_score:
            best_score = score
            best_deg = deg

    # Fine search in 1-degree increments around best candidate (takes ~2ms)
    for delta in (-4, -3, -2, -1, 1, 2, 3, 4):
        deg = (best_deg + delta) % 360
        rad = math.radians(deg)
        c, s = math.cos(rad), math.sin(rad)
        cand_R = [[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]]
        oriented = [matvec(cand_R, p) for p in fast_slab]
        cx = (min(p[0] for p in oriented) + max(p[0] for p in oriented)) / 2
        cy = (min(p[1] for p in oriented) + max(p[1] for p in oriented)) / 2
        cand_t = [target_cx - cx, target_cy - cy, tz]

        score = sum(1 for p in sub_sample if in_slabs_xy(
            cand_R[0][0] * p[0] + cand_R[0][1] * p[1] + cand_t[0],
            cand_R[1][0] * p[0] + cand_R[1][1] * p[1] + cand_t[1]
        ))
        if score > best_score:
            best_score = score
            best_deg = deg

    final_rad = math.radians(best_deg)
    c, s = math.cos(final_rad), math.sin(final_rad)
    R = [[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]]
    oriented = [matvec(R, p) for p in slab_points]
    cx = (min(p[0] for p in oriented) + max(p[0] for p in oriented)) / 2
    cy = (min(p[1] for p in oriented) + max(p[1] for p in oriented)) / 2
    t = [target_cx - cx, target_cy - cy, tz]
    return R, t


class FastGrid:
    """Optimized spatial hash grid for fast 3D radius nearest neighbor search."""

    __slots__ = ("points", "cell", "inv_cell", "cells")

    def __init__(self, points: List[Tuple[float, float, float]], cell: float):
        self.points = points
        self.cell = cell
        self.inv_cell = 1.0 / cell
        cells: Dict[Tuple[int, int, int], List[int]] = {}
        inv_cell = self.inv_cell
        for i, p in enumerate(points):
            k = (int(p[0] * inv_cell), int(p[1] * inv_cell), int(p[2] * inv_cell))
            if k in cells:
                cells[k].append(i)
            else:
                cells[k] = [i]
        self.cells = cells

    def nearest(self, p: Tuple[float, float, float], cutoff: float) -> Tuple[Optional[int], float]:
        px, py, pz = p
        inv_cell = self.inv_cell
        cx = int(px * inv_cell)
        cy = int(py * inv_cell)
        cz = int(pz * inv_cell)
        radius = math.ceil(cutoff * inv_cell)
        best_d2 = cutoff * cutoff
        best_index = None
        cells = self.cells
        points = self.points

        for ix in range(cx - radius, cx + radius + 1):
            for iy in range(cy - radius, cy + radius + 1):
                for iz in range(cz - radius, cz + radius + 1):
                    bucket = cells.get((ix, iy, iz))
                    if bucket is not None:
                        for index in bucket:
                            qx, qy, qz = points[index]
                            dx = px - qx
                            dy = py - qy
                            dz = pz - qz
                            d2 = dx * dx + dy * dy + dz * dz
                            if d2 < best_d2:
                                best_d2 = d2
                                best_index = index
        return best_index, math.sqrt(best_d2)


def rigid_fit(pairs: List[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]) -> Tuple[List[List[float]], List[float]]:
    """Horn's quaternion closed-form least-squares rigid transformation."""
    n = len(pairs)
    a = [sum(pair[0][i] for pair in pairs) / n for i in range(3)]
    b = [sum(pair[1][i] for pair in pairs) / n for i in range(3)]
    S = [[0.0] * 3 for _ in range(3)]
    for source, target in pairs:
        x0 = source[0] - a[0]
        x1 = source[1] - a[1]
        x2 = source[2] - a[2]
        y0 = target[0] - b[0]
        y1 = target[1] - b[1]
        y2 = target[2] - b[2]
        S[0][0] += x0 * y0; S[0][1] += x0 * y1; S[0][2] += x0 * y2
        S[1][0] += x1 * y0; S[1][1] += x1 * y1; S[1][2] += x1 * y2
        S[2][0] += x2 * y0; S[2][1] += x2 * y1; S[2][2] += x2 * y2

    xx, xy, xz = S[0]
    yx, yy, yz = S[1]
    zx, zy, zz = S[2]
    N = [
        [xx + yy + zz, yz - zy, zx - xz, xy - yx],
        [yz - zy, xx - yy - zz, xy + yx, zx + xz],
        [zx - xz, xy + yx, -xx + yy - zz, yz + zy],
        [xy - yx, zx + xz, yz + zy, -xx - yy + zz],
    ]
    V = [[float(i == j) for j in range(4)] for i in range(4)]
    for _ in range(80):
        p, q = max(((i, j) for i in range(4) for j in range(i + 1, 4)), key=lambda ij: abs(N[ij[0]][ij[1]]))
        if abs(N[p][q]) < 1e-10:
            break
        theta = 0.5 * math.atan2(2 * N[p][q], N[q][q] - N[p][p])
        c, s = math.cos(theta), math.sin(theta)
        for row in range(4):
            np_, nq_ = N[row][p], N[row][q]
            N[row][p], N[row][q] = c * np_ - s * nq_, s * np_ + c * nq_
            vp, vq = V[row][p], V[row][q]
            V[row][p], V[row][q] = c * vp - s * vq, s * vp + c * vq
        for col in range(4):
            np_, nq_ = N[p][col], N[q][col]
            N[p][col], N[q][col] = c * np_ - s * nq_, s * np_ + c * nq_

    col = max(range(4), key=lambda i: N[i][i])
    w, x, y, z = (V[i][col] for i in range(4))
    R = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    ra = matvec(R, (a[0], a[1], a[2]))
    t = [b[i] - ra[i] for i in range(3)]
    return R, t


def icp(scan: List[Tuple[float, float, float]], model: List[Tuple[float, float, float]],
        initial_R: List[List[float]], initial_t: List[float],
        max_iterations: int = 30) -> Tuple[List[List[float]], List[float], List[Dict[str, Any]]]:
    """Trimmed point-to-point ICP with distance scheduling and convergence check."""
    grid = FastGrid(model, 0.20)
    R, t = initial_R, initial_t
    history = []

    for iteration in range(max_iterations):
        cutoff = 0.40 if iteration < 8 else 0.25 if iteration < 18 else 0.16
        matched = []
        for p in scan:
            transformed = apply(R, t, p)
            j, distance = grid.nearest(transformed, cutoff)
            if j is not None:
                matched.append((distance, p, model[j]))

        if len(matched) < 30:
            raise ValueError(f"ICP has only {len(matched)} correspondences; need a closer initial pose.")

        matched.sort(key=lambda item: item[0])
        kept = matched[:max(30, int(0.85 * len(matched)))]
        next_R, next_t = rigid_fit([(p, q) for _, p, q in kept])
        rms = math.sqrt(sum(distance * distance for distance, _, _ in kept) / len(kept))
        history.append({
            "iteration": iteration + 1,
            "correspondences": len(kept),
            "rms_m": round(rms, 6),
            "cutoff_m": cutoff,
        })

        change = max(abs(next_R[i][j] - R[i][j]) for i in range(3) for j in range(3))
        change = max(change, max(abs(next_t[i] - t[i]) for i in range(3)))
        R, t = next_R, next_t

        if iteration >= 10 and change < 1e-5:
            break

    return R, t, history


def measure_support(boxes: List[Dict[str, Any]], model: List[Tuple[float, float, float]],
                    owners: List[int], aligned_scan: List[Tuple[float, float, float]],
                    cutoff: float = 0.12,
                    status_thresholds: Tuple[float, float] = (0.35, 0.85)
                    ) -> Tuple[List[Dict[str, Any]], List[float]]:
    """Compute per-element surface support fraction, distance distribution, and completion status."""
    grid = FastGrid(aligned_scan, 0.15)
    total = [0] * len(boxes)
    supported = [0] * len(boxes)
    elem_distances = [[] for _ in range(len(boxes))]
    all_distances = []

    for p, owner in zip(model, owners):
        total[owner] += 1
        index, distance = grid.nearest(p, cutoff)
        all_distances.append(distance if index is not None else 0.40)
        if index is not None:
            supported[owner] += 1
            elem_distances[owner].append(distance)

    not_started_thresh, complete_thresh = status_thresholds
    elements_result = []

    for i, box in enumerate(boxes):
        tot = total[i]
        sup = supported[i]
        fraction = sup / tot if tot else 0.0
        status = "complete" if fraction >= complete_thresh else "not_started" if fraction < not_started_thresh else "partial"
        mean_d = sum(elem_distances[i]) / len(elem_distances[i]) if elem_distances[i] else cutoff

        elements_result.append({
            "element_id": box["id"],
            "ifc_guid": box["guid"],
            "ifc_class": box["ifc_class"],
            "bounds": box["bounds"],
            "dimensions": box["dimensions"],
            "center": box["center"],
            "volume_m3": box["volume_m3"],
            "surface_samples": tot,
            "supported_samples": sup,
            "supported_surface_fraction": round(fraction, 4),
            "completion_pct": round(fraction * 100, 1),
            "estimated_status": status,
            "mean_deviation_mm": round(mean_d * 1000, 1),
        })

    return elements_result, all_distances


def estimate_progress(
    ifc_path: Path,
    scan_path: Path,
    tolerance_m: float = 0.12,
    downsample_spacing: float = 0.15,
    demo_perturb: bool = False,
    gt_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """All-in-one one-click construction progress estimation pipeline."""
    t0 = time.time()
    if not ifc_path.is_file():
        raise FileNotFoundError(f"IFC file not found: {ifc_path}")
    if not scan_path.is_file():
        raise FileNotFoundError(f"Scan PLY not found: {scan_path}")

    # 1. Read inputs
    boxes = read_ifc_boxes(ifc_path)
    scan = read_ply(scan_path)
    t_read = time.time()

    # 2. Perturbation option (for robustness demonstration)
    if demo_perturb:
        perturb_R, perturb_t = get_demo_perturbation()
        scan = [apply(perturb_R, perturb_t, p) for p in scan]

    # 3. Surface sampling & downsampling
    model, owners = sample_ifc_surfaces(boxes, downsample_spacing)
    downsampled = voxel_downsample(scan, downsample_spacing)
    t_prep = time.time()

    # 4. Initial pose estimation & ICP registration
    initial_R, initial_t = slab_initial_pose(scan, boxes)
    final_R, final_t, history = icp(downsampled, model, initial_R, initial_t)
    t_icp = time.time()

    # 5. Transform scan to BIM
    aligned_scan = [apply(final_R, final_t, p) for p in scan]

    # 6. Measure support & progress
    elements_support, point_distances = measure_support(
        boxes, model, owners, aligned_scan, cutoff=tolerance_m
    )

    # 7. Model scan inlier check (within 20 cm)
    model_grid = FastGrid(model, 0.15)
    inliers = 0
    for p in aligned_scan:
        _, d = model_grid.nearest(p, 0.20)
        if d < 0.20:
            inliers += 1
    scan_inlier_frac = round(inliers / len(aligned_scan), 4) if aligned_scan else 0.0

    # 8. Aggregated Progress Stats
    total_surface_samples = sum(e["surface_samples"] for e in elements_support)
    total_supported_samples = sum(e["supported_samples"] for e in elements_support)
    surface_progress_pct = round(100.0 * total_supported_samples / total_surface_samples, 1) if total_surface_samples else 0.0

    complete_count = sum(1 for e in elements_support if e["estimated_status"] == "complete")
    partial_count = sum(1 for e in elements_support if e["estimated_status"] == "partial")
    not_started_count = sum(1 for e in elements_support if e["estimated_status"] == "not_started")
    total_elements = len(elements_support)

    # Estimated element completion score (complete=1.0, partial=fraction, not_started=0.0)
    element_progress_pct = round(
        100.0 * sum(
            1.0 if e["estimated_status"] == "complete" else (e["supported_surface_fraction"] if e["estimated_status"] == "partial" else 0.0)
            for e in elements_support
        ) / total_elements, 1
    ) if total_elements else 0.0

    # 9. Comparison with ground truth if available
    ground_truth = None
    if gt_path and gt_path.is_file():
        try:
            gt_data = json.loads(gt_path.read_text(encoding="utf-8"))
            gt_elements = {item["element_id"]: item for item in gt_data.get("elements", [])}
            for e in elements_support:
                eid = e["element_id"]
                if eid in gt_elements:
                    gt_elem = gt_elements[eid]
                    e["ground_truth"] = {
                        "status": gt_elem.get("status"),
                        "built_volume_fraction": gt_elem.get("built_volume_fraction"),
                        "built_volume_pct": round(gt_elem.get("built_volume_fraction", 0.0) * 100, 1),
                    }
            ground_truth = {
                "overall_built_volume_fraction": gt_data.get("overall_built_volume_fraction"),
                "overall_built_volume_pct": round(gt_data.get("overall_built_volume_fraction", 0.0) * 100, 1),
                "stage": gt_data.get("stage"),
            }
        except Exception:
            pass

    # 10. Prepare 3D point cloud payload (subsampled for fast WebGL display)
    step = max(1, len(aligned_scan) // 6000)
    viz_scan = [[round(p[0], 4), round(p[1], 4), round(p[2], 4)] for p in aligned_scan[::step]]

    # BIM surface samples with distance to scan for tolerance coloring
    m_step = max(1, len(model) // 6000)
    viz_model = [
        [round(p[0], 4), round(p[1], 4), round(p[2], 4), round(d, 4), owner]
        for p, d, owner in zip(model[::m_step], point_distances[::m_step], owners[::m_step])
    ]

    # Global bounding box center for 3D camera orbit
    lo = [min(p[i] for p in model) for i in range(3)]
    hi = [max(p[i] for p in model) for i in range(3)]
    center = [round((lo[i] + hi[i]) / 2, 4) for i in range(3)]
    extents = [round(hi[i] - lo[i], 4) for i in range(3)]

    t_total = time.time() - t0

    result = {
        "status": "success",
        "timing_s": {
            "read_files": round(t_read - t0, 3),
            "prep_sampling": round(t_prep - t_read, 3),
            "icp_registration": round(t_icp - t_prep, 3),
            "progress_measurement": round(time.time() - t_icp, 3),
            "total_execution": round(t_total, 3),
        },
        "summary": {
            "overall_progress_pct": surface_progress_pct,
            "element_completion_pct": element_progress_pct,
            "total_elements": total_elements,
            "complete_elements": complete_count,
            "partial_elements": partial_count,
            "not_started_elements": not_started_count,
            "tolerance_mm": round(tolerance_m * 1000, 1),
            "scan_points_total": len(scan),
            "registration_points": len(downsampled),
            "bim_surface_samples": len(model),
            "icp_final_rms_mm": round(history[-1]["rms_m"] * 1000, 2),
            "icp_iterations": len(history),
            "scan_inlier_pct": round(scan_inlier_frac * 100, 1),
            "demo_perturb": demo_perturb,
        },
        "ground_truth": ground_truth,
        "registration": {
            "initial_R": initial_R,
            "initial_t": [round(v, 6) for v in initial_t],
            "R": final_R,
            "t": [round(v, 6) for v in final_t],
            "iterations": history,
        },
        "elements": elements_support,
        "viz_data": {
            "center": center,
            "extents": extents,
            "scan_points": viz_scan,
            "model_samples": viz_model,
        },
        "paths": {
            "ifc": str(ifc_path),
            "scan": str(scan_path),
        },
    }
    return result


def match_point_pairs(
    scan_points: List[Tuple[float, float, float]],
    bim_points: List[Tuple[float, float, float]],
) -> Dict[str, Any]:
    """Calculate closed-form Horn quaternion rigid transformation from corresponding point pairs.

    Expects at least 5 point pairs. Maps scan_points -> bim_points:
    p_BIM ~= R * p_scan + t
    """
    if len(scan_points) != len(bim_points):
        raise ValueError(
            f"Mismatched point pair counts: {len(scan_points)} scan points vs {len(bim_points)} BIM points."
        )
    if len(scan_points) < 5:
        raise ValueError(
            f"At least 5 corresponding point pairs are required for rigid alignment (received {len(scan_points)})."
        )

    pairs = list(zip(scan_points, bim_points))
    R, t = rigid_fit(pairs)

    residuals_mm = []
    for s_pt, b_pt in pairs:
        pred = apply(R, t, s_pt)
        dist = math.dist(pred, b_pt)
        residuals_mm.append(round(dist * 1000, 2))

    rms_mm = round(math.sqrt(sum(d * d for d in residuals_mm) / len(residuals_mm)), 2)

    return {
        "status": "success",
        "pair_count": len(pairs),
        "R": R,
        "t": [round(v, 6) for v in t],
        "residual_rms_mm": rms_mm,
        "pair_residuals_mm": residuals_mm,
    }


def get_demo_perturbation() -> Tuple[List[List[float]], List[float]]:
    """Return realistic unaligned sensor pose: +20 deg yaw, +6 deg pitch, offset [2.0, -1.5, 0.4] m."""
    yaw = math.radians(20.0)
    pitch = math.radians(6.0)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    # R = Rz(yaw) * Ry(pitch)
    R = [
        [cy * cp, -sy, cy * sp],
        [sy * cp,  cy, sy * sp],
        [-sp,     0.0, cp],
    ]
    t = [2.0, -1.5, 0.4]
    return R, t


def get_scene_preview(
    ifc_path: Path,
    scan_path: Path,
    demo_perturb: bool = False,
    max_scan_points: int = 15000,
    scan_scale: Tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> Dict[str, Any]:
    """Load IFC boxes and raw scan point cloud for initial 3D visualization and landmark picking."""
    if not ifc_path.is_file():
        raise FileNotFoundError(f"IFC file not found: {ifc_path}")
    if not scan_path.is_file():
        raise FileNotFoundError(f"Scan PLY not found: {scan_path}")

    boxes = read_ifc_boxes(ifc_path)
    scan = read_ply(scan_path)

    # Apply demo perturbation if enabled
    perturb_R = None
    perturb_t = None
    if demo_perturb:
        perturb_R, perturb_t = get_demo_perturbation()
        scan = [apply(perturb_R, perturb_t, p) for p in scan]

    # BIM bounding box & center
    lo_x = min(b["bounds"][0][0] for b in boxes)
    lo_y = min(b["bounds"][0][1] for b in boxes)
    lo_z = min(b["bounds"][0][2] for b in boxes)
    hi_x = max(b["bounds"][1][0] for b in boxes)
    hi_y = max(b["bounds"][1][1] for b in boxes)
    hi_z = max(b["bounds"][1][2] for b in boxes)

    bim_center = [round((lo_x + hi_x) / 2, 4), round((lo_y + hi_y) / 2, 4), round((lo_z + hi_z) / 2, 4)]
    bim_extents = [round(hi_x - lo_x, 4), round(hi_y - lo_y, 4), round(hi_z - lo_z, 4)]

    # Scan bounding box & center
    scan_lo_x = min(p[0] for p in scan)
    scan_lo_y = min(p[1] for p in scan)
    scan_lo_z = min(p[2] for p in scan)
    scan_hi_x = max(p[0] for p in scan)
    scan_hi_y = max(p[1] for p in scan)
    scan_hi_z = max(p[2] for p in scan)
    scan_extents = [round(scan_hi_x - scan_lo_x, 4), round(scan_hi_y - scan_lo_y, 4), round(scan_hi_z - scan_lo_z, 4)]

    # Compute unscaled 3D bounding box ratios (BIM vs Scan)
    raw_scan_diag = math.dist((scan_lo_x, scan_lo_y, scan_lo_z), (scan_hi_x, scan_hi_y, scan_hi_z)) if scan else 1.0
    bim_diag = math.dist((lo_x, lo_y, lo_z), (hi_x, hi_y, hi_z))
    bbox_ratio_uniform = round(bim_diag / max(0.0001, raw_scan_diag), 4)
    bbox_ratio_axes = [
        round(bim_extents[0] / max(0.0001, scan_extents[0]), 4),
        round(bim_extents[1] / max(0.0001, scan_extents[1]), 4),
        round(bim_extents[2] / max(0.0001, scan_extents[2]), 4),
    ]

    # Apply user scale if specified
    if scan_scale and scan_scale != (1.0, 1.0, 1.0):
        sx, sy, sz = scan_scale
        scan = [(p[0] * sx, p[1] * sy, p[2] * sz) for p in scan]
        scan_lo_x = min(p[0] for p in scan)
        scan_lo_y = min(p[1] for p in scan)
        scan_lo_z = min(p[2] for p in scan)
        scan_hi_x = max(p[0] for p in scan)
        scan_hi_y = max(p[1] for p in scan)
        scan_hi_z = max(p[2] for p in scan)
        scan_extents = [round(scan_hi_x - scan_lo_x, 4), round(scan_hi_y - scan_lo_y, 4), round(scan_hi_z - scan_lo_z, 4)]

    scan_center = [round((scan_lo_x + scan_hi_x) / 2, 4), round((scan_lo_y + scan_hi_y) / 2, 4), round((scan_lo_z + scan_hi_z) / 2, 4)]

    # Subsample scan points for responsive rendering
    step = max(1, len(scan) // max_scan_points)
    viz_scan = [[round(p[0], 4), round(p[1], 4), round(p[2], 4)] for p in scan[::step]]

    # Generate 5+ salient suggested landmark pairs for user convenience
    slabs = [b for b in boxes if "slab" in b.get("id", "").lower()]
    base_box = slabs[0] if slabs else boxes[0]
    b_lo, b_hi = base_box["bounds"]

    suggested_bim_pts = [
        [b_lo[0], b_lo[1], b_hi[2]],  # Corner 1: top-SW of base slab
        [b_hi[0], b_lo[1], b_hi[2]],  # Corner 2: top-SE of base slab
        [b_hi[0], b_hi[1], b_hi[2]],  # Corner 3: top-NE of base slab
        [b_lo[0], b_hi[1], b_hi[2]],  # Corner 4: top-NW of base slab
    ]

    # Add 5th and 6th landmark from a wall or column top
    walls_or_cols = [b for b in boxes if b.get("ifc_class") in ("IFCWALL", "IFCCOLUMN") and b["bounds"][1][2] > b_hi[2] + 1.0]
    if walls_or_cols:
        target_box = walls_or_cols[0]
        w_lo, w_hi = target_box["bounds"]
        suggested_bim_pts.append([w_lo[0], w_lo[1], w_hi[2]])
        if len(walls_or_cols) > 1:
            w2_lo, w2_hi = walls_or_cols[1]["bounds"]
            suggested_bim_pts.append([w2_hi[0], w2_lo[1], w2_hi[2]])
    else:
        suggested_bim_pts.append([bim_center[0], bim_center[1], b_hi[2]])

    suggested_pairs = []
    for pt in suggested_bim_pts:
        bim_p = [round(v, 4) for v in pt]
        if demo_perturb and perturb_R and perturb_t:
            scan_p_val = apply(perturb_R, perturb_t, (pt[0], pt[1], pt[2]))
            scan_p = [round(v, 4) for v in scan_p_val]
        else:
            scan_p = [round(v, 4) for v in pt]
        suggested_pairs.append({
            "bim": bim_p,
            "scan": scan_p,
        })

    elements_meta = []
    for b in boxes:
        elements_meta.append({
            "element_id": b["id"],
            "ifc_guid": b["guid"],
            "ifc_class": b["ifc_class"],
            "bounds": b["bounds"],
            "dimensions": b["dimensions"],
            "center": b["center"],
            "volume_m3": b["volume_m3"],
        })

    return {
        "status": "success",
        "elements": elements_meta,
        "scan_points": viz_scan,
        "total_scan_points": len(scan),
        "center": bim_center,
        "extents": bim_extents,
        "bim_center": bim_center,
        "bim_extents": bim_extents,
        "scan_center": scan_center,
        "scan_extents": scan_extents,
        "demo_perturb": demo_perturb,
        "suggested_pairs": suggested_pairs,
        "bbox_ratio_uniform": bbox_ratio_uniform,
        "bbox_ratio_axes": bbox_ratio_axes,
        "scan_scale": list(scan_scale),
    }


def estimate_progress_from_pairs(
    ifc_path: Path,
    scan_path: Path,
    scan_points: List[Tuple[float, float, float]],
    bim_points: List[Tuple[float, float, float]],
    tolerance_m: float = 0.12,
    downsample_spacing: float = 0.15,
    demo_perturb: bool = False,
    run_icp: bool = True,
    gt_path: Optional[Path] = None,
    scan_scale: Tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> Dict[str, Any]:
    """Align BIM vs point cloud using corresponding point pairs, optionally refine with ICP,
    and measure construction progress.
    """
    t0 = time.time()
    if not ifc_path.is_file():
        raise FileNotFoundError(f"IFC file not found: {ifc_path}")
    if not scan_path.is_file():
        raise FileNotFoundError(f"Scan PLY not found: {scan_path}")
    if len(scan_points) != len(bim_points):
        raise ValueError(f"Mismatched pair count: {len(scan_points)} scan vs {len(bim_points)} BIM points.")
    if len(scan_points) < 5:
        raise ValueError(f"At least 5 point pairs required for matching (received {len(scan_points)}).")

    # 1. Read inputs
    boxes = read_ifc_boxes(ifc_path)
    scan = read_ply(scan_path)
    t_read = time.time()

    # Apply scan scale if provided
    if scan_scale and scan_scale != (1.0, 1.0, 1.0):
        sx, sy, sz = scan_scale
        scan = [(p[0] * sx, p[1] * sy, p[2] * sz) for p in scan]

    # Apply demo perturbation if enabled
    if demo_perturb:
        perturb_R, perturb_t = get_demo_perturbation()
        scan = [apply(perturb_R, perturb_t, p) for p in scan]

    # 2. Compute rigid transformation from 5+ point pairs using Horn's method
    pair_match = match_point_pairs(scan_points, bim_points)
    pair_R = pair_match["R"]
    pair_t = pair_match["t"]
    t_pairs = time.time()

    # 3. Surface sampling & downsampling
    model, owners = sample_ifc_surfaces(boxes, downsample_spacing)
    downsampled = voxel_downsample(scan, downsample_spacing)
    t_prep = time.time()

    # 4. Optional ICP refinement initialized from pair alignment
    if run_icp:
        final_R, final_t, history = icp(downsampled, model, pair_R, pair_t)
        icp_final_rms_mm = round(history[-1]["rms_m"] * 1000, 2)
        icp_iterations = len(history)
    else:
        final_R, final_t = pair_R, pair_t
        history = [{
            "iteration": 0,
            "correspondences": len(scan_points),
            "rms_m": pair_match["residual_rms_mm"] / 1000.0,
            "cutoff_m": 0.0,
        }]
        icp_final_rms_mm = pair_match["residual_rms_mm"]
        icp_iterations = 0
    t_icp = time.time()

    # 5. Transform scan to BIM using final (pair+ICP) alignment
    aligned_scan = [apply(final_R, final_t, p) for p in scan]

    # 6. Measure support & progress
    elements_support, point_distances = measure_support(
        boxes, model, owners, aligned_scan, cutoff=tolerance_m
    )

    # 7. Model scan inlier check (within 20 cm)
    model_grid = FastGrid(model, 0.15)
    inliers = 0
    for p in aligned_scan:
        _, d = model_grid.nearest(p, 0.20)
        if d < 0.20:
            inliers += 1
    scan_inlier_frac = round(inliers / len(aligned_scan), 4) if aligned_scan else 0.0

    # 8. Aggregated Progress Stats
    total_surface_samples = sum(e["surface_samples"] for e in elements_support)
    total_supported_samples = sum(e["supported_samples"] for e in elements_support)
    surface_progress_pct = round(100.0 * total_supported_samples / total_surface_samples, 1) if total_surface_samples else 0.0

    complete_count = sum(1 for e in elements_support if e["estimated_status"] == "complete")
    partial_count = sum(1 for e in elements_support if e["estimated_status"] == "partial")
    not_started_count = sum(1 for e in elements_support if e["estimated_status"] == "not_started")
    total_elements = len(elements_support)

    element_progress_pct = round(
        100.0 * sum(
            1.0 if e["estimated_status"] == "complete" else (e["supported_surface_fraction"] if e["estimated_status"] == "partial" else 0.0)
            for e in elements_support
        ) / total_elements, 1
    ) if total_elements else 0.0

    # 9. Comparison with ground truth if available
    ground_truth = None
    if gt_path and gt_path.is_file():
        try:
            gt_data = json.loads(gt_path.read_text(encoding="utf-8"))
            gt_elements = {item["element_id"]: item for item in gt_data.get("elements", [])}
            for e in elements_support:
                eid = e["element_id"]
                if eid in gt_elements:
                    gt_elem = gt_elements[eid]
                    e["ground_truth"] = {
                        "status": gt_elem.get("status"),
                        "built_volume_fraction": gt_elem.get("built_volume_fraction"),
                        "built_volume_pct": round(gt_elem.get("built_volume_fraction", 0.0) * 100, 1),
                    }
            ground_truth = {
                "overall_built_volume_fraction": gt_data.get("overall_built_volume_fraction"),
                "overall_built_volume_pct": round(gt_data.get("overall_built_volume_fraction", 0.0) * 100, 1),
                "stage": gt_data.get("stage"),
            }
        except Exception:
            pass

    # 10. Prepare 3D point cloud payload
    step = max(1, len(aligned_scan) // 6000)
    viz_scan = [[round(p[0], 4), round(p[1], 4), round(p[2], 4)] for p in aligned_scan[::step]]

    # BIM surface samples with distance to scan for tolerance coloring
    m_step = max(1, len(model) // 6000)
    viz_model = [
        [round(p[0], 4), round(p[1], 4), round(p[2], 4), round(d, 4), owner]
        for p, d, owner in zip(model[::m_step], point_distances[::m_step], owners[::m_step])
    ]

    lo = [min(p[i] for p in model) for i in range(3)]
    hi = [max(p[i] for p in model) for i in range(3)]
    center = [round((lo[i] + hi[i]) / 2, 4) for i in range(3)]
    extents = [round(hi[i] - lo[i], 4) for i in range(3)]

    t_total = time.time() - t0

    return {
        "status": "success",
        "alignment_mode": "point_pairs_icp" if run_icp else "point_pairs_only",
        "timing_s": {
            "read_files": round(t_read - t0, 3),
            "pair_fitting": round(t_pairs - t_read, 3),
            "prep_sampling": round(t_prep - t_pairs, 3),
            "icp_registration": round(t_icp - t_prep, 3),
            "progress_measurement": round(time.time() - t_icp, 3),
            "total_execution": round(t_total, 3),
        },
        "summary": {
            "overall_progress_pct": surface_progress_pct,
            "element_completion_pct": element_progress_pct,
            "total_elements": total_elements,
            "complete_elements": complete_count,
            "partial_elements": partial_count,
            "not_started_elements": not_started_count,
            "tolerance_mm": round(tolerance_m * 1000, 1),
            "scan_points_total": len(scan),
            "registration_points": len(downsampled),
            "bim_surface_samples": len(model),
            "pair_fit_rms_mm": pair_match["residual_rms_mm"],
            "icp_final_rms_mm": icp_final_rms_mm,
            "icp_iterations": icp_iterations,
            "scan_inlier_pct": round(scan_inlier_frac * 100, 1),
            "demo_perturb": demo_perturb,
            "point_pair_count": len(scan_points),
        },
        "pair_alignment": pair_match,
        "ground_truth": ground_truth,
        "registration": {
            "initial_R": pair_R,
            "initial_t": [round(v, 6) for v in pair_t],
            "R": final_R,
            "t": [round(v, 6) for v in final_t],
            "iterations": history,
        },
        "elements": elements_support,
        "viz_data": {
            "center": center,
            "extents": extents,
            "scan_points": viz_scan,
            "model_samples": viz_model,
        },
        "paths": {
            "ifc": str(ifc_path),
            "scan": str(scan_path),
        },
    }

