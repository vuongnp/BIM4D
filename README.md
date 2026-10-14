# BIM4D Construction Progress Estimator & Registration

A fast, self-contained toolkit to align 3D point cloud scans to IFC BIM models and estimate construction progress with one click.
Uses only the Python standard library (no numpy, scipy, or external C dependencies required).

---

## 🚀 One-Click Construction Progress Estimator App

Launch the interactive 3D Web UI with one command:

```bash
python3 progress_app.py
```

This starts a local web server (default `http://127.0.0.1:8765/`) and automatically opens your browser.

### Key Capabilities:
- **Interactive 3D Landmark Point Matching**: Displays both the BIM model and raw point cloud simultaneously. The user can select $\ge 5$ corresponding points on BIM (e.g., slab corners, wall corners, columns) and on the Point Cloud, view 3D numbered landmark pins and connecting dashed lines, and click **Align 5 Point Pairs ➔ ICP** to compute a closed-form Horn quaternion rigid fit ($R_0, t_0$) followed by fine trimmed ICP registration and progress measurement.
- **Auto 5 Pairs Helper**: Instant one-click shortcut (`✨ Auto 5 Pairs`) that extracts 5 salient geometric landmark pairs from the scene corners for instant verification.
- **Dataset Switcher**: Instant switching between the **Sample House** (17 elements, rectangular) and the **Complex Facility** (26 elements, non-square L-shaped footprint with interior columns, MEP piping network, and partition walls).
- **Interactive 3D Viewport**: Uses Three.js with OrbitControls to display 3D architectural solids colored by progress status (Green = Complete, Amber = In Progress, Red = Not Started), overlay the aligned point cloud, or view a continuous tolerance heatmap.
- **Dynamic Real-Time Tolerance**: Drag the tolerance slider (30 mm to 250 mm) to see completion percentages and status badges update live without re-running registration.
- **Interactive Inspector**: Click or hover any 3D element to view its IFC class, GUID, completion percentage, sample support count, dimensions, and ground truth comparison.
- **Exporting**: Save the registered scan and report to disk or download aligned PLY, JSON report, or CSV table directly from the browser.


---

## 🏢 Benchmark Datasets

### 1. Simple House (`sample_house/`)
Regenerate anytime via `python3 generate_house_sample.py`.
- **Footprint**: Rectangular single-story house (8 m × 6 m × 3 m high).
- **17 IFC Elements**: Base slab, perimeter wall segments around openings, door, windows, and flat roof.
- **Construction Stage**: Slab and north/west walls built; south/east walls partly built; roof, door, and windows absent.

### 2. Complex Facility (`sample_complex/`)
Regenerate anytime via `python3 generate_complex_sample.py`.
- **Footprint**: **Non-square L-shaped** industrial facility (14 m × 16 m overall envelope, with an inner courtyard notch).
- **26 IFC Elements across 6 IFC classes**:
  - Foundation Slabs (`IFCSLAB`): 2-part L-shaped foundation slab.
  - Structural Columns (`IFCCOLUMN`): 5 interior concrete columns ($0.40\text{ m} \times 0.40\text{ m}$) at interior bays and corner junction.
  - MEP Industrial Piping (`IFCFLOWSEGMENT`): Overhead main header ($13.5\text{ m}$), vertical supply riser, branch line into south wing, cooling loop, and drainage stack.
  - Exterior L-shaped Walls & Interior Partitions (`IFCWALL`): Exterior perimeter walls and interior dividing walls (control room partition, mechanical closet).
  - Openings (`IFCDOOR`, `IFCWINDOW`) & Roof Sections (`IFCSLAB`).
- **Challenging Test Case**:
  - Non-convex L-shaped geometry.
  - Interior columns and narrow overhead pipes requiring fine 3D registration.
  - Varied construction progress: some columns 100% poured, some partly poured ($2.2\text{ m}$, $2.8\text{ m}$), some not started; pipes partially installed; walls in various stages; roof absent.
  - 90,944 scan points with Gaussian noise ($\sigma = 8\text{ mm}$), occlusions, and random missing points.

---

## 🛠️ CLI Registration & Evaluation

### Align Simple House:
```bash
python3 match_house_icp.py
```

### Align Complex Facility:
```bash
python3 match_house_icp.py \
  --ifc sample_complex/complex_facility.ifc \
  --scan sample_complex/unfinished_scan.ply \
  --out sample_complex/icp_alignment.json \
  --aligned-ply sample_complex/aligned_scan.ply
```

### Test Robust Pose Recovery (Demo Perturb):
Offset the scan virtually by 4° and $(0.18, -0.12, 0.06)\text{ m}$ before running alignment:
```bash
python3 match_house_icp.py --demo-perturb
```

### Generate Offline Tolerance Heatmap HTML:
```bash
python3 visualize_house_tolerance.py \
  --ifc sample_complex/complex_facility.ifc \
  --scan sample_complex/aligned_scan.ply \
  --out sample_complex/tolerance_heatmap.html
```
Open `sample_complex/tolerance_heatmap.html` in any browser to inspect the result offline.
