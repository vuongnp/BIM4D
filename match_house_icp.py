#!/usr/bin/env python3
"""Align a PLY scan to the sample IFC house with point-to-point ICP.

This ports the registration pattern used by pose_estimation_assignment/
prepare_submission.py without its RGB-D detector or model server. The IFC
reader supports the axis-aligned, extruded-box IFC4 geometry generated here.
All coordinates are metres. Python standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path


ELEMENT_TYPES = {"IFCWALL", "IFCSLAB", "IFCDOOR", "IFCWINDOW"}


def split_step_args(s: str) -> list[str]:
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


def step_list(s: str) -> list[str]:
    if not (s.startswith("(") and s.endswith(")")):
        raise ValueError(f"Expected STEP list, got {s}")
    return split_step_args(s[1:-1])


def read_ifc_boxes(path: Path) -> list[dict]:
    entities = {}
    for line in path.read_text().splitlines():
        m = re.fullmatch(r"#(\d+)=(\w+)\((.*)\);", line)
        if m:
            entities["#" + m[1]] = (m[2], split_step_args(m[3]))

    def entity(ref: str, expected: str | None = None) -> list[str]:
        kind, args = entities[ref]
        if expected and kind != expected:
            raise ValueError(f"Expected {expected}, found {kind} at {ref}")
        return args

    def point(ref: str) -> tuple[float, ...]:
        return tuple(map(float, step_list(entity(ref, "IFCCARTESIANPOINT")[0])))

    def placement(ref: str) -> tuple[float, float, float]:
        if ref == "$":
            return (0.0, 0.0, 0.0)
        parent, axis_ref = entity(ref, "IFCLOCALPLACEMENT")
        axis = entity(axis_ref, "IFCAXIS2PLACEMENT3D")
        local = point(axis[0])
        if len(axis) > 1 and axis[1] != "$":
            assert tuple(map(float, step_list(entity(axis[1], "IFCDIRECTION")[0]))) == (0.0, 0.0, 1.0)
        if len(axis) > 2 and axis[2] != "$":
            assert tuple(map(float, step_list(entity(axis[2], "IFCDIRECTION")[0]))) == (1.0, 0.0, 0.0)
        base = placement(parent)
        return tuple(base[i] + local[i] for i in range(3))

    boxes = []
    for ref, (kind, args) in entities.items():
        if kind not in ELEMENT_TYPES:
            continue
        shape_ref = step_list(entity(args[6], "IFCPRODUCTDEFINITIONSHAPE")[2])[0]
        solid_ref = step_list(entity(shape_ref, "IFCSHAPEREPRESENTATION")[3])[0]
        solid = entity(solid_ref, "IFCEXTRUDEDAREASOLID")
        profile = entity(solid[0], "IFCRECTANGLEPROFILEDEF")
        dx, dy, dz = float(profile[3]), float(profile[4]), float(solid[3])
        center = point(entity(profile[2], "IFCAXIS2PLACEMENT2D")[0])
        origin = placement(args[5])
        x0, y0, z0 = origin[0] + center[0] - dx / 2, origin[1] + center[1] - dy / 2, origin[2]
        boxes.append({
            "id": args[2].strip("'"), "guid": args[0].strip("'"), "ifc_class": kind,
            "bounds": ((x0, y0, z0), (x0 + dx, y0 + dy, z0 + dz)),
        })
    if not boxes:
        raise ValueError("No supported extruded house elements found in IFC")
    return boxes


def read_ply(path: Path) -> list[tuple[float, float, float]]:
    with path.open() as f:
        if f.readline().strip() != "ply" or f.readline().strip() != "format ascii 1.0":
            raise ValueError("Expected ASCII PLY")
        count = None
        properties = []
        in_vertex = False
        for line in f:
            words = line.split()
            if not words:
                continue
            if words[:2] == ["element", "vertex"]:
                count = int(words[2])
                in_vertex = True
            elif words[0] == "element":
                in_vertex = False
            elif words[0] == "property" and in_vertex:
                properties.append(words[-1])
            elif words[0] == "end_header":
                break
        if count is None or any(axis not in properties for axis in ("x", "y", "z")):
            raise ValueError("PLY lacks x/y/z vertices")
        ix = [properties.index(axis) for axis in ("x", "y", "z")]
        points = []
        for _ in range(count):
            fields = next(f).split()
            points.append(tuple(float(fields[i]) for i in ix))
    return points


def write_ply(path: Path, points: list[tuple[float, float, float]]) -> None:
    with path.open("w") as f:
        f.write(f"ply\nformat ascii 1.0\nelement vertex {len(points)}\n")
        f.write("property float x\nproperty float y\nproperty float z\nend_header\n")
        for p in points:
            f.write(f"{p[0]:.6f} {p[1]:.6f} {p[2]:.6f}\n")


def inside(bounds, p) -> bool:
    lo, hi = bounds
    return all(lo[i] - 1e-8 <= p[i] <= hi[i] + 1e-8 for i in range(3))


def sample_ifc_surfaces(boxes: list[dict], spacing: float) -> tuple[list[tuple[float, float, float]], list[int]]:
    points, owners = [], []
    for index, box in enumerate(boxes):
        lo, hi = box["bounds"]
        for fixed in range(3):
            a, b = [i for i in range(3) if i != fixed]
            na = max(1, math.ceil((hi[a] - lo[a]) / spacing))
            nb = max(1, math.ceil((hi[b] - lo[b]) / spacing))
            for side in (0, 1):
                if box["id"] == "slab" and fixed == 2 and side == 0:
                    continue
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
                        points.append(tuple(p))
                        owners.append(index)
    return points, owners


def voxel_downsample(points: list[tuple[float, float, float]], size: float) -> list[tuple[float, float, float]]:
    cells = {}
    for p in points:
        key = tuple(math.floor(v / size) for v in p)
        if key not in cells:
            cells[key] = p
    return list(cells.values())


def slab_initial_pose(scan, boxes):
    """Estimate yaw and translation from the dense, completed horizontal slab."""
    slab = next((box for box in boxes if box["id"] == "slab"), None)
    if slab is None:
        raise ValueError("Automatic initialization needs an IFC element named 'slab'")
    z_bins = defaultdict(list)
    for p in scan:
        z_bins[round(p[2] / 0.02)].append(p)
    slab_points = max(z_bins.values(), key=len)
    if len(slab_points) < 100:
        raise ValueError("Could not find a dense slab plane for automatic initialization")
    mx = sum(p[0] for p in slab_points) / len(slab_points)
    my = sum(p[1] for p in slab_points) / len(slab_points)
    xx = sum((p[0] - mx) ** 2 for p in slab_points) / len(slab_points)
    yy = sum((p[1] - my) ** 2 for p in slab_points) / len(slab_points)
    xy = sum((p[0] - mx) * (p[1] - my) for p in slab_points) / len(slab_points)
    angle = 0.5 * math.atan2(2 * xy, xx - yy)
    c, s = math.cos(angle), math.sin(angle)
    R = [[c, s, 0], [-s, c, 0], [0, 0, 1]]
    oriented = [matvec(R, p) for p in slab_points]
    cx = (min(p[0] for p in oriented) + max(p[0] for p in oriented)) / 2
    cy = (min(p[1] for p in oriented) + max(p[1] for p in oriented)) / 2
    lo, hi = slab["bounds"]
    t = [(lo[0] + hi[0]) / 2 - cx,
         (lo[1] + hi[1]) / 2 - cy,
         hi[2] - sorted(p[2] for p in slab_points)[len(slab_points) // 2]]
    return R, t


class Grid:
    def __init__(self, points: list[tuple[float, float, float]], cell: float):
        self.points = points
        self.cell = cell
        self.cells = defaultdict(list)
        for i, p in enumerate(points):
            self.cells[self.key(p)].append(i)

    def key(self, p):
        return tuple(math.floor(v / self.cell) for v in p)

    def nearest(self, p, cutoff: float) -> tuple[int | None, float]:
        center = self.key(p)
        radius = math.ceil(cutoff / self.cell)
        best_d2, best_index = cutoff * cutoff, None
        for ix in range(center[0] - radius, center[0] + radius + 1):
            for iy in range(center[1] - radius, center[1] + radius + 1):
                for iz in range(center[2] - radius, center[2] + radius + 1):
                    for index in self.cells.get((ix, iy, iz), ()):
                        q = self.points[index]
                        d2 = sum((p[j] - q[j]) ** 2 for j in range(3))
                        if d2 < best_d2:
                            best_d2, best_index = d2, index
        return best_index, math.sqrt(best_d2)


def matvec(R, p):
    return tuple(sum(R[i][j] * p[j] for j in range(3)) for i in range(3))


def apply(R, t, p):
    rp = matvec(R, p)
    return tuple(rp[i] + t[i] for i in range(3))


def rigid_fit(pairs: list[tuple[tuple[float, ...], tuple[float, ...]]]):
    """Horn's quaternion least-squares fit: transform first points onto second."""
    n = len(pairs)
    a = [sum(pair[0][i] for pair in pairs) / n for i in range(3)]
    b = [sum(pair[1][i] for pair in pairs) / n for i in range(3)]
    S = [[0.0] * 3 for _ in range(3)]
    for source, target in pairs:
        x = [source[i] - a[i] for i in range(3)]
        y = [target[i] - b[i] for i in range(3)]
        for i in range(3):
            for j in range(3):
                S[i][j] += x[i] * y[j]
    xx, xy, xz = S[0]
    yx, yy, yz = S[1]
    zx, zy, zz = S[2]
    N = [
        [xx + yy + zz, yz - zy, zx - xz, xy - yx],
        [yz - zy, xx - yy - zz, xy + yx, zx + xz],
        [zx - xz, xy + yx, -xx + yy - zz, yz + zy],
        [xy - yx, zx + xz, yz + zy, -xx - yy + zz],
    ]
    # Symmetric Jacobi eigensolver; choose the largest eigenvalue's vector.
    V = [[float(i == j) for j in range(4)] for i in range(4)]
    for _ in range(80):
        p, q = max(((i, j) for i in range(4) for j in range(i + 1, 4)), key=lambda ij: abs(N[ij[0]][ij[1]]))
        if abs(N[p][q]) < 1e-10:
            break
        theta = 0.5 * math.atan2(2 * N[p][q], N[q][q] - N[p][p])
        c, s = math.cos(theta), math.sin(theta)
        # Columns of J are [c,-s] and [s,c] in the p/q block.
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
        [1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)],
        [2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)],
        [2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)],
    ]
    ra = matvec(R, a)
    t = [b[i] - ra[i] for i in range(3)]
    return R, t


def icp(scan, model, initial_R, initial_t, max_iterations=30):
    grid = Grid(model, 0.20)
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
            raise ValueError(f"ICP has only {len(matched)} correspondences; provide a closer initial pose")
        matched.sort(key=lambda item: item[0])
        kept = matched[:max(30, int(0.85 * len(matched)))]
        next_R, next_t = rigid_fit([(p, q) for _, p, q in kept])
        rms = math.sqrt(sum(distance * distance for distance, _, _ in kept) / len(kept))
        history.append({"iteration": iteration + 1, "correspondences": len(kept), "rms_m": round(rms, 6)})
        change = max(abs(next_R[i][j] - R[i][j]) for i in range(3) for j in range(3))
        change = max(change, max(abs(next_t[i] - t[i]) for i in range(3)))
        R, t = next_R, next_t
        if iteration >= 18 and change < 1e-6:
            break
    return R, t, history


def measure_support(boxes, model, owners, aligned_scan, cutoff=0.12):
    grid = Grid(aligned_scan, 0.15)
    total = [0] * len(boxes)
    supported = [0] * len(boxes)
    for p, owner in zip(model, owners):
        total[owner] += 1
        index, _ = grid.nearest(p, cutoff)
        if index is not None:
            supported[owner] += 1
    result = []
    for i, box in enumerate(boxes):
        fraction = supported[i] / total[i] if total[i] else 0
        result.append({
            "element_id": box["id"], "ifc_guid": box["guid"],
            "supported_surface_fraction": round(fraction, 4),
            "estimated_status": "complete" if fraction >= 0.85 else "not_started" if fraction < 0.35 else "partial",
            "supported_samples": supported[i], "surface_samples": total[i],
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ifc", type=Path, default=Path("sample_house/full_house.ifc"))
    parser.add_argument("--scan", type=Path, default=Path("sample_house/unfinished_scan.ply"))
    parser.add_argument("--out", type=Path, default=Path("sample_house/icp_alignment.json"))
    parser.add_argument("--aligned-ply", type=Path, default=Path("sample_house/aligned_scan.ply"))
    parser.add_argument("--initial", type=Path, help="JSON with initial R (3x3) and t (3), scan to BIM")
    parser.add_argument("--demo-perturb", action="store_true", help="Virtually offset the scan to test pose recovery")
    args = parser.parse_args()
    boxes = read_ifc_boxes(args.ifc)
    scan = read_ply(args.scan)
    if args.demo_perturb:
        angle = math.radians(4)
        perturb_R = [[math.cos(angle), -math.sin(angle), 0], [math.sin(angle), math.cos(angle), 0], [0, 0, 1]]
        scan = [apply(perturb_R, [0.18, -0.12, 0.06], p) for p in scan]
    model, owners = sample_ifc_surfaces(boxes, 0.15)
    downsampled = voxel_downsample(scan, 0.15)
    if args.initial:
        initial = json.loads(args.initial.read_text())
        R, t = initial["R"], initial["t"]
        initial_method = "user_pose"
    else:
        R, t = slab_initial_pose(scan, boxes)
        initial_method = "slab_plane_and_footprint"
    initial_pose = {"R": R, "t": t}
    R, t, history = icp(downsampled, model, R, t)
    aligned = [apply(R, t, p) for p in scan]
    support = measure_support(boxes, model, owners, aligned)
    model_grid = Grid(model, 0.15)
    distances = []
    for p in aligned:
        _, d = model_grid.nearest(p, 0.20)
        if d < 0.20:
            distances.append(d)
    report = {
        "direction": "scan_to_BIM", "units": "metres", "ifc": str(args.ifc), "scan": str(args.scan),
        "demo_perturb": args.demo_perturb, "initial_method": initial_method,
        "initial_pose": initial_pose, "R": R, "t": t,
        "scan_points": len(scan), "registration_points": len(downsampled),
        "model_surface_points": len(model), "iterations": history,
        "final_rms_m": history[-1]["rms_m"],
        "scan_inlier_fraction_0_20m": round(len(distances) / len(aligned), 4),
        "status_rule": "surface support <0.35: not_started; >=0.85: complete; otherwise partial",
        "elements": support,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.aligned_ply.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    write_ply(args.aligned_ply, aligned)
    print(f"Aligned {len(scan)} scan points to {len(model)} IFC surface samples")
    print(f"RMS: {history[-1]['rms_m']:.4f} m; scan inliers: {100 * report['scan_inlier_fraction_0_20m']:.1f}%")
    print(f"R: {R}\nt: {t}")
    print(f"Wrote {args.out} and {args.aligned_ply}")


if __name__ == "__main__":
    main()
