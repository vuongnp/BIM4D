# Manual point-cloud alignment plan

## Goal

Let a user move a captured point cloud onto the IFC model in the existing 3D progress app, inspect the overlap, and use the chosen pose to estimate construction progress. Manual mode must work when the slab-based rough alignment is inaccurate or the slab is missing. The IFC stays fixed; only the scan pose changes.

The canonical transform maps **raw scan coordinates to BIM coordinates**, in metres:

```text
p_BIM = R × p_scan + t
```

`R` is a proper 3 × 3 rotation, stored row-major in JSON, and `t` is a 3-vector. Scaling is not allowed. The raw PLY and IFC files are never modified by alignment.

## Current integration points

- `progress_app/static/app.js` already renders IFC elements as Three.js box meshes and the aligned scan as `THREE.Points`, with `OrbitControls`, camera presets, layer toggles, and a tolerance heatmap.
- `progress_app/engine.py::estimate_progress` currently always runs `slab_initial_pose` followed by ICP before measuring surface support. The measurement code can be reused after a manual transform.
- `progress_app/server.py` handles `/api/estimate`, keeps the last result for downloads, and exports aligned PLY and JSON.
- `match_house_icp.py --initial` already accepts a JSON object with top-level `R` and `t`, so the saved manual pose can also initialize the CLI workflow.
- `progress_app.py` is currently empty, although the README advertises it as the launcher. Wire the app entry point when implementing this feature so the new UI can be started as documented.

The first version should support the IFC geometry this repository currently reads: axis-aligned extruded boxes, including the sample house and complex facility. The viewer should show the same solids that the matching engine samples. General IFC curved/triangulated geometry needs a separate geometry-reader upgrade.

## User workflow

1. Select an IFC and a raw PLY scan, then choose **Manual Align**. Load and display both *before* running ICP or progress estimation. The model is fixed and translucent; the scan uses a contrasting color.
2. Start from identity, a previously saved pose for this exact IFC/scan pair, or an explicitly requested **Auto suggestion**. Never silently replace the manual pose with slab initialization.
3. Move the scan with a translate/rotate gizmo. Provide X/Y/Z translation, Z-axis rotation, and an advanced XYZ rotation mode. Numeric fields show position in metres and angles in degrees; arrow/step controls allow fine adjustments. Keep existing orbit, pan, zoom, and Top/Front/Side camera presets. Disable `OrbitControls` only while dragging the gizmo.
4. Show the raw scan, fixed BIM solids/outlines, and an optional mismatch heatmap. After a drag ends or a numeric value is committed, update a **preview** surface-support score. Label this preview as approximate; run the existing full measurement only when the user selects **Use this alignment**.
5. **Use this alignment** estimates progress directly from the manual pose, skipping both `slab_initial_pose` and ICP. Show the final pose and label the report's alignment method `manual`.
6. Offer a separate **Try ICP refinement** action. It uses the manual pose as the initial pose, displays the proposed new pose and fit diagnostics, and requires **Accept refinement** to replace the manual pose. **Discard refinement** restores the exact manual pose. The preview action must not change exports or final progress results.
7. Allow **Save pose**, **Load pose**, **Reset pose**, and **Undo/redo**. Export the aligned PLY and progress report using the accepted pose. A dataset switch clears the active pose unless a matching saved pose is explicitly loaded.

## 3D interaction design

- Reuse the vendored Three.js r160 and `OrbitControls`. Add a matching-version `TransformControls` locally so the app remains usable offline; attach it to a group containing the **raw** scan points. Do not transform or rewrite every point during a drag.
- Use the scan centroid as the editing pivot for natural rotation, but export the equivalent world-space `R,t` for `p_BIM = R × p_scan + t`. Display BIM X/Y/Z axes and a metre scale/grid. Keep camera movement separate from scan movement.
- Make transform mode and axis visible: **Move**, **Rotate**, world/local axis toggle, numeric pose fields, and a visible reset control. Offer coarse and fine step sizes, such as 0.1/0.01 m and 5°/0.1°.
- BIM geometry must remain selectable for inspection but must not be moved. Make scan opacity and point size adjustable so overlapping walls and slabs can be judged visually. The tolerance heatmap remains a separate layer from the manual transform gizmo.
- Validate the exported matrix convention carefully: Three.js matrices are column-major internally while the JSON `R` is row-major. Tests should transform several known points both in browser and Python and compare their coordinates.

## Backend and data flow

1. Add a scene-preview endpoint that reads IFC boxes and raw PLY once and returns IFC element geometry, a deterministic downsampled scan for interactive rendering, bounds, units, and dataset fingerprints. Keep the full scan server-side for final measurement and PLY export. Loading a scene should not run ICP.
2. Refactor `estimate_progress` into reusable steps: load/sample inputs, choose alignment method, transform scan, then measure support. Support explicit modes `automatic`, `manual`, and `manual_refined`. In `manual`, accept a supplied pose and bypass both the slab initializer and ICP. In `manual_refined`, run ICP from the supplied pose only after the user requests refinement.
3. Add a pose-preview endpoint for approximate support and, separately, a refinement endpoint that returns a **candidate** pose and diagnostics without mutating the accepted result. Throttle preview requests after gestures; avoid recomputing the full 90k-point complex-facility scan on every mouse movement.
4. Make the final `/api/estimate` request include the selected mode and pose. Return the alignment method, initial/manual pose, final/accepted pose, and existing element results. `last_result` and `last_aligned_points` must correspond to the same accepted pose so JSON, CSV, and PLY exports agree.
5. Validate the pose server-side: finite numbers, exact 3 × 3 and 3-vector shapes, near-orthonormal `R`, determinant near +1, plausible translation bounds, and no scale/shear. Reject a pose for a different IFC/scan fingerprint or unit convention. Apply the server's existing workspace path restrictions to new endpoints.

## Saved pose format

Save a small JSON file next to the scan, for example `manual_alignment.json`:

```json
{
  "schema": "bim4d.manual_alignment.v1",
  "direction": "scan_to_BIM",
  "units": "metres",
  "ifc_sha256": "...",
  "scan_sha256": "...",
  "R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
  "t": [0, 0, 0]
}
```

Keep `R` and `t` at the top level for `match_house_icp.py --initial` compatibility. The hashes prevent accidentally applying a pose to a different scan or model; the UI can show a clear mismatch error. Save the pose separately from the aligned PLY so it can be edited and reused without losing raw data.

## Implementation order

1. Make the app launcher functional and add a preview load path that displays raw IFC and scan data without estimation.
2. Add scan-only gizmo and numeric pose controls, including transform conversion, reset, undo/redo, and camera-control handoff.
3. Refactor the engine/API to accept manual poses and to calculate progress without automatic registration; connect **Use this alignment** and exports.
4. Add saved-pose load/save and dataset fingerprint checks.
5. Add approximate support preview, then optional ICP refinement with explicit accept/discard.
6. Update the README and add focused Python and browser interaction tests.

## Acceptance checks

- On the house sample, identity manual pose overlays the scan with the BIM and reproduces the current element support within normal rounding.
- On a deliberately rotated/translated scan, applying the known inverse pose manually restores the original alignment and progress result. Include at least one nonzero roll or pitch test to verify full 6-DoF matrix handling.
- In manual mode, spy/assert that neither slab initialization nor ICP runs. The saved raw PLY remains byte-for-byte unchanged.
- Save/reload preserves the exact pose; switching IFC or scan rejects an unrelated saved pose. Browser and Python transform the same test points to the same coordinates.
- Accepting ICP refinement updates the report and aligned PLY together; discarding it leaves the saved manual pose and previous result untouched.
- The house and 90k-point complex sample remain responsive while dragging; exact measurement occurs after commit. The UI clearly distinguishes approximate preview support from final results.
