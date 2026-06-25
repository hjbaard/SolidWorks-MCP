# SolidWorks MCP — Progress log

## Environment (verified 2026-06-25)

- SolidWorks: **2026 (3DEXPERIENCE R2026x)**, file version 34.2.0.0127, COM ProgID
  `SldWorks.Application`, CLSID `{666aaee2-7a21-40fc-b768-2078840a88c3}`.
- Install dir: `C:\Program Files\Dassault Systemes\SOLIDWORKS 3DEXPERIENCE R2026x\SOLIDWORKS\`.
- Typelibs: `sldworks.tlb` (GUID `{83A33D31-27C5-11CE-BFD4-00400513BB57}`, v34.0),
  `swconst.tlb` (constants).
- Python 3.13.7, venv at `.venv`, `pywin32 312`, `mcp 1.28.0`.

## Milestones

### M0 — Connection ✅
`scripts/probe_connection.py`. Attaches via `GetActiveObject`, reads
`RevisionNumber` (34.2.0), reports active doc.

### M1 — Build + measure ✅
`scripts/m1_block.py`. new part → rectangle on first plane → extrude →
`CreateMassProperty`. Block 40×20×10 mm → volume 8000 mm³, area 2800 mm²,
centre of mass (20,10,5) mm, mass 0.008 kg — all match hand calc exactly.

### M2 — Parametric ✅
`scripts/m2_parametric.py`. Change `D1@BlockExtrude` 10→25 mm → rebuild →
volume 8000→20000 mm³ (ratio 2.5). Proves the parametric loop.

### M3 — Full agent loop via MCP server ✅
`scripts/test_mcp_server.py`. Drives the server over stdio (10 tools):
get_status → new_part → add_box → measure → set_dimension → measure →
bounding_box → export STEP (15.8 KB, valid AP203) + STL → screenshot (valid
isometric render) → close_part. All checks pass.

### Hardening (post-review, 2026-06-25)
Adversarial code-review pass (4 dimensions, findings verified). Applied:
- **Call timeout** in `ComWorker.call` — a blocked COM call (modal dialog) now
  surfaces a readable timeout error instead of wedging the server forever.
- **Mass properties fail loud** if SI units can't be forced (`UseSystemUnits`),
  instead of silently returning wrong-scaled numbers.
- **`set_dimension` reads the value back** after rebuild and reports the applied
  value + an `applied` flag, so a driven/equation dimension that ignores the
  write is visible to the agent.
- **Readable COM errors** (extract `excepinfo` description) and a sketch-result
  check in `add_box` (fail at the root cause, not later at the extrude).
- Removed dead code (`DOC_TYPE_NAMES`, unused doc-type constants,
  `deg_to_rad`/`rad_to_deg`). Re-run: M1/M2/MCP all still green.

### M4 — Cut-extrude (hole) + face selection 🚧
`scripts/m4_hole.py`. Kickoff M3 spec: 40x20x10 block with a centred Ø8
through-hole -> volume 7497.345 mm³ (= block - π·4²·10), matches to machine
precision. Added a reusable `_planar_face_by_normal` selection helper (picks a
face by outward normal, e.g. +Z) and an `add_hole` tool; the MCP end-to-end test
now covers it (11 tools).

Lesson (caught by the screenshot check): a cut sketched on a reference plane
coincident with a face fails for *every* direction/end-condition; sketching on
the actual selected face makes the cut unambiguous. The hole runs along the
add_box depth axis (the +Z profile face), i.e. through the plate thickness.

`scripts/m4_fillet.py` + `add_fillet`: rounds all edges with a constant radius
via `_select_all_edges` (extends the selection layer to edges) + FeatureFillet3.
40x20x10 block, r=2 on all 12 edges -> 7770.36 mm³ (matches the hand estimate
~7760; difference is the corner patches). Verified visually.

Fillet lesson: FeatureFillet3 needs `swFeatureFilletUniformRadius` in Options to
use the single R1 radius; without it the API expects a per-edge Radii array and
returns None.

`scripts/m4_chamfer.py` + `add_chamfer`: 45° chamfer on all edges via
`InsertFeatureChamfer`, reusing `_select_all_edges`. 40x20x10, d=2 -> 7482.67 mm³
(removed 517.33, matches the ~500 estimate). Verified visually.

Chamfer lesson: `swChamferEqualDistance` (16) is a silent no-op here (returns a
feature that removes nothing); use `swChamferAngleDistance` (1) with Width +
45° angle. Caught by the volume check (feature built but volume unchanged).

`scripts/m4_select_edges.py` + `_select_edges(body, selector)`: directional edge
selection for fillet/chamfer. `edges='x'|'y'|'z'` selects straight edges parallel
to that world axis (via start/end vertices; curved/closed edges like a hole's
circle have no vertices and never match). Verified: a 40x20x10 box has exactly 4
edges per axis; r=2 fillet removes 137/69/34 mm³ for x/y/z (proportional to the
40/20/10 lengths). Visually confirmed only the depth edges round for 'z'.

### Hardening (M4 review, 2026-06-25)
Adversarial review of the M4 code (8 findings confirmed). Applied:
- **Off-center hole verified**: a hole at (10,6) lands at COM (20.670, 10.268) =
  the prediction, confirming (x,y) really use add_box coordinates (the centred
  test was symmetric and couldn't catch a mirrored frame).
- **Planar-face guard** in `_planar_face_by_normal` (`ISurface.IsPlane`) and
  **line-edge guard** in `_edge_parallel_to` (`ICurve.IsLine`) — so a cylinder
  left by a hole or an arc left by a fillet is never picked as a sketch base /
  axis edge on composed parts.
- Dropped tangent **propagation** on fillet/chamfer so `edges_filleted/chamfered`
  equals exactly what was selected; tightened the direction tolerance to ~2.6°.
- `_finish_feature` helper (DRY across the 4 builders) now also returns
  `rebuild_ok`, mirroring set_dimension. Full M-suite + MCP test still green.

### M4 — Revolve (cylinder) 🚧
`scripts/m4_cylinder.py` + `add_cylinder`: first revolve-based primitive. Sketches
a radius x height rectangle + a single centerline (the axis at x=0) and calls
FeatureRevolve2 with Dir1Type=blind, Dir1Angle=360°. Ø20 x h20 -> 6283.185 mm³
(= π·10²·20) to machine precision; bbox [20,20,20]. Verified visually.

Revolve lesson: a lone centerline in the sketch is auto-detected as the axis
(UseAutoSelect), so no explicit axis selection is needed. The same plumbing
extends to cones (trapezoid profile) and general profiles.

## Key API findings (this build)

These were read from the installed typelib (`scripts/introspect_api.py`), not
guessed — and several differ from common web docs:

- `swDefaultTemplatePart = 8` (not 9, as widely stated online).
- `GetActiveObject` returns a dispatch whose `GetTypeInfo()` raises
  `TYPE_E_ELEMENTNOTFOUND` → `EnsureDispatch`/`CastTo` fail → **early binding via
  manual wrapping in generated classes is mandatory** (see `binding.py`).
- `IModelDoc2` has **no** `GetBox`. Part bounding box comes from
  `IPartDoc.GetPartBox(NoConversion=True)` (returns metres).
- `RevisionNumber` resolves as a property under late binding, a method under
  early binding — handle both (`callable` check).
- `FeatureManager.FeatureExtrusion3` takes 23 args; types verified
  (bool/int/double) against the typelib.
- Plane selection by name (`"Front Plane"`) is language-dependent; we walk the
  feature tree for `GetTypeName2() == "RefPlane"` instead.

## Next

- General revolve: cone (trapezoid profile) and arbitrary (radius, z) profiles,
  reusing the add_cylinder plumbing; generic sketch primitives (line/arc).
- More selection: choose the hole face (not just +Z), index-based edge/face
  picking, and a `list_edges`/`list_faces` tool so an agent can inspect geometry.
- Equations (`IEquationMgr`).
- Richer rebuild-error reporting (feature-level error/warning enumeration).

## Notes

- Standalone scripts run single-threaded (no COM worker needed); the server
  pins COM to one worker thread.
- Repeated `--keep-open` runs leave untitled parts (Part1, Part2, …) open in
  SolidWorks; harmless, close them manually.
