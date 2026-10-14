#!/usr/bin/env python3
"""Generate a small IFC4 house and an unfinished, aligned synthetic PLY scan.

Uses only the Python standard library. Coordinates and dimensions are metres.
"""

from __future__ import annotations

import csv
import json
import math
import random
import uuid
from dataclasses import dataclass
from pathlib import Path


OUT = Path(__file__).resolve().parent / "sample_house"
SEED = 20260929
SPACING = 0.10
NOISE_SIGMA = 0.008
KEEP_PROBABILITY = 0.82
IFC64 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float

    def volume(self) -> float:
        return (self.x1 - self.x0) * (self.y1 - self.y0) * (self.z1 - self.z0)

    def contains(self, point: tuple[float, float, float]) -> bool:
        x, y, z = point
        e = 1e-7
        return (self.x0 - e <= x <= self.x1 + e and
                self.y0 - e <= y <= self.y1 + e and
                self.z0 - e <= z <= self.z1 + e)

    def bounds(self) -> list[list[float]]:
        return [[self.x0, self.y0, self.z0], [self.x1, self.y1, self.z1]]


@dataclass(frozen=True)
class Element:
    name: str
    ifc_type: str
    full: Box
    built: Box | None
    guid: str


def guid_for(name: str) -> str:
    number = uuid.uuid5(uuid.NAMESPACE_URL, "bim4d.example/house-v1/" + name).int
    chars = []
    for _ in range(22):
        chars.append(IFC64[number & 63])
        number >>= 6
    return "".join(reversed(chars))


def house_elements() -> list[Element]:
    # Split walls at openings so the IFC solids have actual empty door/window bays.
    # The roof and openings are in the complete model but have not been built.
    specs = [
        ("slab", "IFCSLAB", Box(-0.2, -0.2, -0.2, 8.2, 6.2, 0), "full"),
        ("south_wall_west", "IFCWALL", Box(0, 0, 0, 3.2, 0.2, 3), 2.1),
        ("south_wall_east", "IFCWALL", Box(4.1, 0, 0, 8, 0.2, 3), None),
        ("south_door_lintel", "IFCWALL", Box(3.2, 0, 2.1, 4.1, 0.2, 3), None),
        ("north_wall_west", "IFCWALL", Box(0, 5.8, 0, 2, 6, 3), "full"),
        ("north_wall_east", "IFCWALL", Box(3.2, 5.8, 0, 8, 6, 3), "full"),
        ("north_window_sill", "IFCWALL", Box(2, 5.8, 0, 3.2, 6, 1), "full"),
        ("north_window_header", "IFCWALL", Box(2, 5.8, 2.2, 3.2, 6, 3), None),
        ("west_wall", "IFCWALL", Box(0, 0.2, 0, 0.2, 5.8, 3), "full"),
        ("east_wall_south", "IFCWALL", Box(7.8, 0.2, 0, 8, 2, 3), 1.5),
        ("east_wall_north", "IFCWALL", Box(7.8, 3.2, 0, 8, 5.8, 3), 1.5),
        ("east_window_sill", "IFCWALL", Box(7.8, 2, 0, 8, 3.2, 1), "full"),
        ("east_window_header", "IFCWALL", Box(7.8, 2, 2.2, 8, 3.2, 3), None),
        ("flat_roof", "IFCSLAB", Box(-0.2, -0.2, 3, 8.2, 6.2, 3.2), None),
        ("front_door", "IFCDOOR", Box(3.2, 0.06, 0, 4.1, 0.14, 2.1), None),
        ("north_window", "IFCWINDOW", Box(2, 5.86, 1, 3.2, 5.94, 2.2), None),
        ("east_window", "IFCWINDOW", Box(7.86, 2, 1, 7.94, 3.2, 2.2), None),
    ]
    elements = []
    for name, ifc_type, full, construction in specs:
        built = (full if construction == "full" else
                 None if construction is None else
                 Box(full.x0, full.y0, full.z0, full.x1, full.y1, construction))
        elements.append(Element(name, ifc_type, full, built, guid_for(name)))
    return elements


class IFC:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, entity: str, *args: str) -> str:
        ref = f"#{len(self.lines) + 1}"
        self.lines.append(f"{ref}={entity}({','.join(args)});")
        return ref

    @staticmethod
    def string(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"


def write_ifc(elements: list[Element]) -> None:
    f = IFC()
    xyz = lambda x, y, z: f.add("IFCCARTESIANPOINT", f"({x:.6f},{y:.6f},{z:.6f})")
    origin = xyz(0, 0, 0)
    z_dir = f.add("IFCDIRECTION", "(0.,0.,1.)")
    x_dir = f.add("IFCDIRECTION", "(1.,0.,0.)")
    world_axis = f.add("IFCAXIS2PLACEMENT3D", origin, z_dir, x_dir)
    context = f.add("IFCGEOMETRICREPRESENTATIONCONTEXT", "'Model'", "'Model'", "3", "1.E-5", world_axis, "$")
    length = f.add("IFCSIUNIT", "*", ".LENGTHUNIT.", "$", ".METRE.")
    area = f.add("IFCSIUNIT", "*", ".AREAUNIT.", "$", ".SQUARE_METRE.")
    volume = f.add("IFCSIUNIT", "*", ".VOLUMEUNIT.", "$", ".CUBIC_METRE.")
    units = f.add("IFCUNITASSIGNMENT", f"({length},{area},{volume})")
    project = f.add("IFCPROJECT", IFC.string(guid_for("project")), "$", "'Simple one floor house'", "$", "$", "$", "$", f"({context})", units)

    def placement(parent: str, x: float = 0, y: float = 0, z: float = 0) -> str:
        point = xyz(x, y, z)
        axis = f.add("IFCAXIS2PLACEMENT3D", point, z_dir, x_dir)
        return f.add("IFCLOCALPLACEMENT", parent, axis)

    site_placement = placement("$")
    site = f.add("IFCSITE", IFC.string(guid_for("site")), "$", "'Site'", "$", "$", site_placement, "$", "$", ".ELEMENT.", "$", "$", "$", "$", "$")
    building_placement = placement(site_placement)
    building = f.add("IFCBUILDING", IFC.string(guid_for("building")), "$", "'House'", "$", "$", building_placement, "$", "$", ".ELEMENT.", "$", "$", "$")
    storey_placement = placement(building_placement)
    storey = f.add("IFCBUILDINGSTOREY", IFC.string(guid_for("storey")), "$", "'Ground floor'", "$", "$", storey_placement, "$", "$", ".ELEMENT.", "0.")
    for relation_name, parent, child in (("site", project, site), ("building", site, building), ("storey", building, storey)):
        f.add("IFCRELAGGREGATES", IFC.string(guid_for("aggregate-" + relation_name)), "$", "$", "$", parent, f"({child})")

    product_refs = []
    for element in elements:
        box = element.full
        dx, dy, dz = box.x1 - box.x0, box.y1 - box.y0, box.z1 - box.z0
        object_placement = placement(storey_placement, box.x0, box.y0, box.z0)
        profile_origin = f.add("IFCCARTESIANPOINT", f"({dx / 2:.6f},{dy / 2:.6f})")
        profile_axis = f.add("IFCAXIS2PLACEMENT2D", profile_origin, "$")
        profile = f.add("IFCRECTANGLEPROFILEDEF", ".AREA.", "$", profile_axis, f"{dx:.6f}", f"{dy:.6f}")
        solid = f.add("IFCEXTRUDEDAREASOLID", profile, world_axis, z_dir, f"{dz:.6f}")
        shape = f.add("IFCSHAPEREPRESENTATION", context, "'Body'", "'SweptSolid'", f"({solid})")
        representation = f.add("IFCPRODUCTDEFINITIONSHAPE", "$", "$", f"({shape})")
        common = [IFC.string(element.guid), "$", IFC.string(element.name), "$", "$", object_placement, representation, IFC.string(element.name)]
        if element.ifc_type == "IFCSLAB":
            extras = [".ROOF." if element.name == "flat_roof" else ".BASESLAB."]
        elif element.ifc_type == "IFCWALL":
            extras = [".NOTDEFINED."]
        elif element.ifc_type == "IFCDOOR":
            extras = ["2.1", "0.9", ".DOOR.", ".SINGLE_SWING_LEFT.", "$"]
        else:
            extras = ["1.2", "1.2", ".WINDOW.", ".NOTDEFINED.", "$"]
        product_refs.append(f.add(element.ifc_type, *(common + extras)))
    f.add("IFCRELCONTAINEDINSPATIALSTRUCTURE", IFC.string(guid_for("containment")), "$", "$", "$", f"({','.join(product_refs)})", storey)
    header = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [ReferenceView]'),'2;1');
FILE_NAME('full_house.ifc','2026-09-29T00:00:00',('BIM4D sample'),('BIM4D sample'),'BIM4D sample generator','Python standard library','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
"""
    (OUT / "full_house.ifc").write_text(header + "\n".join(f.lines) + "\nENDSEC;\nEND-ISO-10303-21;\n")


def sample_faces(box: Box, rng: random.Random):
    faces = (
        (0, box.x0, 1, box.y0, box.y1, 2, box.z0, box.z1, (-1, 0, 0)),
        (0, box.x1, 1, box.y0, box.y1, 2, box.z0, box.z1, (1, 0, 0)),
        (1, box.y0, 0, box.x0, box.x1, 2, box.z0, box.z1, (0, -1, 0)),
        (1, box.y1, 0, box.x0, box.x1, 2, box.z0, box.z1, (0, 1, 0)),
        (2, box.z0, 0, box.x0, box.x1, 1, box.y0, box.y1, (0, 0, -1)),
        (2, box.z1, 0, box.x0, box.x1, 1, box.y0, box.y1, (0, 0, 1)),
    )
    for fixed_axis, fixed, a_axis, a0, a1, b_axis, b0, b1, normal in faces:
        if box.z1 == 0 and normal == (0, 0, -1):
            continue  # The underside of the ground slab is buried.
        na = max(1, math.ceil((a1 - a0) / SPACING))
        nb = max(1, math.ceil((b1 - b0) / SPACING))
        for ia in range(na):
            for ib in range(nb):
                point = [0.0, 0.0, 0.0]
                point[fixed_axis] = fixed
                point[a_axis] = a0 + (ia + rng.uniform(0.25, 0.75)) * (a1 - a0) / na
                point[b_axis] = b0 + (ib + rng.uniform(0.25, 0.75)) * (b1 - b0) / nb
                yield tuple(point), normal


def write_scan(elements: list[Element]) -> int:
    rng = random.Random(SEED)
    built = [(e, e.built) for e in elements if e.built is not None]
    points: list[tuple[float, float, float, str, str]] = []
    for element, box in built:
        assert box is not None
        for point, normal in sample_faces(box, rng):
            if rng.random() > KEEP_PROBABILITY:
                continue
            outside = tuple(point[i] + 0.001 * normal[i] for i in range(3))
            if any(other.contains(outside) for other_element, other in built if other_element != element):
                continue  # A face concealed by another built element is not scanned.
            xyz = tuple(point[i] + rng.gauss(0, NOISE_SIGMA) for i in range(3))
            points.append((*xyz, element.name, element.guid))
    rng.shuffle(points)
    with (OUT / "unfinished_scan.ply").open("w") as file:
        file.write("ply\nformat ascii 1.0\n")
        file.write("comment Coordinates in metres; aligned with full_house.ifc\n")
        file.write(f"element vertex {len(points)}\nproperty float x\nproperty float y\nproperty float z\nend_header\n")
        for x, y, z, _, _ in points:
            file.write(f"{x:.5f} {y:.5f} {z:.5f}\n")
    with (OUT / "point_labels.csv").open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["point_index", "element_id", "ifc_guid"])
        for index, (_, _, _, name, guid) in enumerate(points):
            writer.writerow([index, name, guid])
    return len(points)


def write_ground_truth(elements: list[Element], point_count: int) -> None:
    records = []
    for element in elements:
        fraction = 0 if element.built is None else element.built.volume() / element.full.volume()
        records.append({
            "element_id": element.name,
            "ifc_guid": element.guid,
            "ifc_class": element.ifc_type,
            "full_bbox_m": element.full.bounds(),
            "built_bbox_m": element.built.bounds() if element.built else None,
            "built_volume_fraction": round(fraction, 6),
            "status": "complete" if fraction == 1 else "not_started" if fraction == 0 else "partial",
        })
    truth = {
        "coordinate_system": "local Cartesian, metres; x east, y north, z up",
        "stage": "Slab and north/west walls built; south/east walls partly built; roof, door and windows absent",
        "scan_seed": SEED,
        "scan_spacing_m": SPACING,
        "scan_noise_sigma_m": NOISE_SIGMA,
        "scan_keep_probability": KEEP_PROBABILITY,
        "point_count": point_count,
        "completion_definition": "Built solid volume divided by full BIM solid volume per IFC element",
        "overall_built_volume_fraction": round(
            sum(e.built.volume() for e in elements if e.built is not None) /
            sum(e.full.volume() for e in elements), 6),
        "elements": records,
    }
    (OUT / "ground_truth.json").write_text(json.dumps(truth, indent=2) + "\n")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    elements = house_elements()
    write_ifc(elements)
    point_count = write_scan(elements)
    write_ground_truth(elements, point_count)
    print(f"Generated {len(elements)} IFC elements and {point_count} scan points in {OUT}")


if __name__ == "__main__":
    main()
