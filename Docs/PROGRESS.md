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

### M5 — end-to-end 3D-print part ✅
`scripts/m5_demo_bracket.py`. Builds a functional mounting bracket through the full
build->measure->verify loop, asserting volume vs a hand calc after EVERY step:
100x80x8 plate -> Ø16 motor bore -> 4-bolt circle (add_circular_pattern)
-> 4 corner counterbores -> R5 corner fillets -> 30x6 cable slot -> fine STL +
screenshot. All 10 checkpoints matched exactly (final 58297.635 mm^3), bbox
[100,80,8]. This is the whole-toolset validation that drove the 3D-print feature
work; it composes 8 tools into one printable part. Gotcha found + fixed in the demo:
a through slot adds tangent z-parallel edges, so add_fillet(edges="z") must run
BEFORE the slot to round only the 4 box corners.

### 3D-print features (2026-06-26)
Demand-driven from the bracket demo (research workflow first; HoleWizard rejected as
locale-fragile, same class as the mirror dead-end):
- **add_counterbore_hole**: deterministic = clearance shank THROUGH_ALL + larger
  coaxial pocket BLIND, two FeatureCut4 cuts on +Z (flush cap-head screws / heat-set
  inserts). Factored `_cut_circle_on_z` (now shared with add_hole, the 3rd use).
  Verified: Ø5 through + Ø10x4 cbore removes 431.97 mm^3.
- **export() STL/3MF resolution**: quality 'coarse'|'fine' (default fine), or explicit
  deviation_mm + angle_deg (Custom). Sets swSTLQuality/Deviation/AngleTolerance on
  ISldWorks before SaveAs3 and RESTORES them after (they are global prefs). Verified:
  fine STL > coarse file size; prefs unchanged after export. Mesh-only (stl/3mf).
- Deferred (researched, ranked lower): countersink (needs a revolve about an axis at
  arbitrary (x,y) -- the one real complication) and emboss/engrave text (font/transform
  fragility, medium ROI).

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

### M4 — inspection, cone, shell, index selection 🚧
- `list_faces` / `list_edges`: read-only geometry inspection (face normal/area/
  centre via IFace2.GetArea + ISurface.IsPlane; edge type/axis/length/midpoint via
  ICurve.IsLine/IsCircle + vertices). Box → 6 planar faces (2800 mm²), 12 lines.
- **Index edge selection**: fillet/chamfer `edges` now also takes indices ("2,5")
  from list_edges, not just an axis.
- `add_cone` (`scripts/m4_cone.py`): trapezoid-profile revolve; top Ø=0 → full
  cone. Frustum 3665.191, full cone 2094.395 mm³ vs formula.
- `add_shell` (`scripts/m4_shell.py`): InsertFeatureShell to a wall thickness,
  removing a chosen planar face (+x/-x/.../-z) or fully closed. 40x20x10 t=2 →
  open 3392 / closed 4544 mm³. Returns no feature object (build dict inline).
All verified + visual; 18 MCP tools; end-to-end green.

### M4 — toolset expansion for AI-driven design 🚧
A long autonomous run adding the capabilities an AI needs for advanced parts:
- **`add_extruded_profile`** (`m4_profile.py`): extrude any closed polygon
  `[[x,y],...]` (CreateLine loop + FeatureExtrusion3). L-bracket = shoelace area
  1800 × 10 = 18000 mm³. The big unlock beyond box/cylinder/cone.
- **`cut_profile`** (`m4_cut.py`): cut a polygon pocket/slot from the +Z face,
  blind or through. 20×10 pocket -> blind 7200 / through 6000 mm³.
- **`save_part` / `open_part`** (`m4_saveopen.py`): SaveAs3 to .sldprt / OpenDoc6;
  build->save->close->open round-trip preserves volume.
- **`set_material`** (`m4_material.py`): IPartDoc.SetMaterialPropertyName2 (DB=""
  finds the default DB). 6061 Alloy -> 2700 kg/m³, mass 0.0216 kg. Mass props now
  include `density_kg_m3`.
- `scripts/demo_plate.py`: full autonomous design (plate + bore + 6-bolt circle +
  rounded corners), spec-verified + exported.

### M4 — review hardening + pytest suite 🚧
Adversarial review of the toolset code (14 findings). Applied the real ones:
- polygon inputs normalised (`_clean_polygon`: drop coincident/closing dups,
  require >= 3 distinct) shared by add_extruded_profile + cut_profile;
- `set_material` verifies via GetMaterialPropertyName2 name read-back (not a
  density heuristic) + returns rebuild_ok;
- `_cylindrical_face_near` now requires ISurface.IsCylinder + a distance floor;
- `_last_feature_name` skips folders/sketches/fillet/chamfer/shell/patterns so the
  default pattern seed is a real boss/cut/hole;
- `save_part`/`export` verify the file's mtime advanced (no stale-file success);
- linear pattern selects the analysed edge directly (Select4+Mark), not by
  coordinate; shared `_select_planar_face` helper for +Z/face selection.

**Tests** (`tests/`, pytest): two layers. 13 pure unit tests (units, selector/
direction parsing, polygon cleaning) run with no SolidWorks in ~0.02s
(`pytest -m "not solidworks"`); 21 integration tests (marker `solidworks`,
auto-skip if absent) verify every feature's volume against a hand calc. 34/34
green. This replaces running the m4_*.py scripts by hand.

### M4 — holes on any face (model→sketch transform) 🚧
`add_hole_on_face(diameter, face, x, y, z)`: drill through any planar face at a 3D
point. `_model_to_sketch_uv` maps a model point to the face-sketch's 2D frame via
`ISketch.ModelToSketchTransform` + `IMathUtility.CreatePoint`/`MultiplyTransform`.
Key gotcha: `CreatePoint` needs a `win32com.client.VARIANT(VT_ARRAY|VT_R8, [...])`
— a plain Python list is mis-marshalled (garbage). Verified: side holes through
the +X (40 mm) and +Y (20 mm) faces; 36 pytest tests green. Unblocks side holes,
pockets on any face, and bolt circles on cylinder end-faces.

### M4 — disc / round flange 🚧
`add_disc(diameter, thickness)`: a circle extruded along +Z (axis Z, centred at
origin). Chosen over reorienting add_cylinder or generalising circular_pattern:
because the disc's flat faces are +Z/-Z, the existing add_hole (+Z) and
add_circular_pattern (Z-axis) compose directly. **Round flange** = add_disc +
centre bore + bolt hole + circular pattern, verified end to end (Ø80x15, Ø20 bore,
6x Ø10 bolts -> 63617.25 mm³). Also a nice sanity check of the line-edge guard:
add_fillet(edges="z") on a disc correctly finds 0 straight edges and raises.

### M4 — slotted hole (first arc-based sketch) 🚧
`cut_slot(length, width, x, y, angle, depth)`: a straight slot/obround cut on the
+Z face. The first sketch using **arcs** rather than only lines/circles, via
`ISketchManager.CreateSketchSlot(CreationType, LengthType, Width, x1,y1,z1,
x2,y2,z2, x3,y3,z3, CenterArcDirection, AddDimension)`. Empirically nailed (a
throwaway `_debug_slot.py`): CreationType=line(0), LengthType=centre-to-centre(0),
the **first two points are the two end-arc centres**, the **third point sits on the
width side** (its perpendicular offset = W/2), and `Width` drives the slot width
directly. We compute the three points from (centre, angle, length, width):
centres at `centre ± (L/2)·(cosθ,sinθ)`, width point at `centre + (W/2)·(-sinθ,cosθ)`.
Box 40x20x10, L=20/W=10 slot 5 mm deep -> removed `(L·W + π(W/2)²)·d` = 1392.70 ->
6607.30 mm³ (matches hand calc). Cut blind or through via the existing FeatureCut4.
This is the building block for rounded profiles generally.

### M4 — general revolve 🚧
`add_revolved_profile(profile, angle)`: revolve any closed `(radius, height)`
polygon about the axis at r=0 (like add_extruded_profile, but spun instead of
extruded). Generalises the cone's revolve plumbing: reuses `_clean_polygon` +
`_draw_polygon_segments`, draws the profile in the (x=r, y=z) plane on Front, adds
a centreline along the axis spanning the profile's z-range, then FeatureRevolve2.
Guards: no negative radius (can't cross the axis), not all-on-axis, angle in
(0,360]. Verified vs hand calc: cylinder r10/h20 = 6283.19, **ring** (profile
offset from axis, outer10/inner5/h2) = 471.24, frustum = 3665.19, **half** (180°)
cylinder = 3141.59. Unlocks turned shafts, vases, rings/torus cross-sections, and
partial revolves -- the cone/cylinder are now just special cases.

### M4 — swept pipe (first sweep) 🚧
`add_swept_pipe(path, diameter, bend_radius)`: sweep a round profile along a 2D
path on the Front plane -> pipes, tubes, rods, wire, bent frames. The first SWEEP
feature, via `IFeatureManager.InsertProtrusionSwept4` (dispid 256, 20 args). Key
unlock: its **CircularProfile + CircularProfileDiameter** options auto-generate the
round profile perpendicular to the path -- so NO separate profile sketch and no
perpendicular-profile-plane gymnastics. We only build the path sketch, select it
(mark 4 = sweep path; mark 1 failed -> None), and sweep. Sharp polyline corners
can't be swept with a tube, so we **compute the rounded path geometry ourselves**
(`_round_polyline`, pure/unit-tested): each interior corner becomes a tangent arc
of bend_radius (setback = R/tan(θ/2), centre on the bisector at R/sin(θ/2), arc
direction from the turn's cross-product sign), drawn with CreateLine + CreateArc
(centre form, dispid 31). Verified vs Pappus (volume = π(d/2)²·path_length):
straight 50mm Ø10 = 3926.99, L-bend (R10) = 4375.29, U-bend (R8) = 7314.63 -- all
exact. Determinism over the native sketch-fillet API (which needs fiddly per-vertex
sketch-point selection). Path is planar (2D) for now; a 3D path is the next step.

### M4 — loft (blend) 🚧
`add_lofted_solid(profiles, heights)`: blend 2+ closed polygon profiles on parallel
planes stacked along +Z. Via `IFeatureManager.InsertProtrusionBlend` (dispid 16, 17
args). Per-profile a plane: profile 0 on Front, the rest on offset planes via
`InsertRefPlane(swRefPlaneReferenceConstraint_Distance=8, distance_m, 0,0,0,0)`.
Selection (the risk the review flagged, cracked empirically): each profile sketch
selected with **mark 1, Append=True**, in stacking order; InsertProtrusionBlend then
blends them. Verified: 2 squares (40->20 over 30) = **28000.00 exact** (ruled
prismatoid h/6*(a^2+(a+b)^2+b^2), no twist for aligned profiles); coaxial circles
(20->10 over 30) = 21990.4 vs 21991.1 frustum (circle tessellation). Note: 3+
profiles blend SMOOTHLY through intermediates (40->20->40 gave 47657, not the
piecewise 56000) -- only the 2-profile ruled case is exactly hand-calcable.
Two gotchas: (1) **InsertRefPlane's return is a generic dispatch without Select2** --
grab the new plane from the tree (`_last_ref_plane`) instead. (2) circles are
already covered by revolve/cone, so loft's niche is NON-rotational transitions.
Unlike mirror, loft works on this 3DEXPERIENCE build (the blocker there was the
mirror-body API, not ref geometry). Added `_iter_features` generator (DRY for the
new tree walks: `_profile_feature_names`, `_last_ref_plane`).

### Review-hardening pass (2026-06-26)
Multi-agent adversarial review of cut_slot/add_revolved_profile/add_swept_pipe
(24 findings, 18 confirmed). Fixes applied:
- **`_round_polyline` (critical)**: per-corner radius-fit missed two corners SHARING
  a segment -- their setbacks could sum past the shared length, self-overlapping the
  path; SolidWorks then failed opaquely. Now a 3-pass pure function: compute corner
  fillets, validate adjacent-corner setback sums vs the inter-vertex distance, then
  emit (skipping any zero-length connecting line). Fails fast in pure code.
- **`_round_polyline` (foldback)**: split the collinear guard -- theta~pi (straight
  pass) is skipped, theta~0 (180deg fold-back) now RAISES instead of silently
  flattening the excursion.
- **`add_swept_pipe` (2x high)**: (a) now selects the Front ref plane explicitly
  before InsertSketch (every other builder does; it had relied on default selection
  -> silent wrong-plane risk). (b) captures the path sketch via a before/after
  ProfileFeature-name diff instead of `_last_sketch_name` (robust to pre-existing
  sketches; `_last_sketch_name` removed as dead code).
- Tests: 67 green (was 52). Added pure geometry tests (right/obtuse/acute corners,
  S-chain, overlap+foldback raises, 2-pt-ignores-radius) and integration tests
  (S-bend pipe = both arc dirs end-to-end, revolve guards + 360 boundary, diameter
  guard, 45deg slot through).
API args (InsertProtrusionSwept4 / FeatureRevolve2 / CreateArc / CreateSketchSlot)
were re-checked against the typelib -- no mismatches.

### Flaky test note (2026-06-26)
`test_cut_profile_through` failed ONCE in a full 52-test run (got vol 3000 vs 6000)
but passes in isolation and on re-run -- non-deterministic state bleed under rapid
new_part/close_part cycling. The review's flaky-test hypothesis could NOT be
confirmed statically (no concrete root cause), so NO speculative fix was applied to
new_part/close_part. Pre-existing; the `part` fixture already closes each doc.
Revisit only with a reproduction; no sleep/poll band-aids.

### On-face fail-fast guard (2026-06-26)
`add_hole_on_face` / `cut_profile_on_face` document that the supplied (x,y,z) must
LIE on the chosen face, but nothing enforced it: `_model_to_sketch_uv` projected the
point onto the face-sketch plane and dropped the out-of-plane component, so a point a
few mm off the face was silently relocated to the projected spot (an error-masking gap
vs CLAUDE.md fail-fast). Verified empirically (throwaway script): ModelToSketchTransform's
`local[2]` is **exactly 0.0** for an on-face point and **equals the off-face distance**
otherwise (a 5 mm off-face point gave -5.000e-3 m with identical u,v), and the old code
drilled the hole anyway. Fix: `_model_to_sketch_uv` now takes `face` and raises a Dutch
`SolidWorksError` naming the face, point (mm) and measured offset when `abs(local[2])`
exceeds `_ON_FACE_TOLERANCE_MM = 1e-3` (1 um -- ~5000x below a real mistake, absorbs
transform round-off). Tests: 73 green (added off-face raises for both callers; on-face
happy paths unchanged).

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

### M4 — Linear pattern 🚧
`scripts/m4_pattern.py` + `add_linear_pattern`: repeat a feature N times along
+x/-x/+y/... via FeatureLinearPattern. Box + hole -> 3 holes = 6492.036 mm³.
Selection marks (found empirically): **direction edge = mark 1, seed feature =
mark 4** (via SelectByID2 with coords/name). The pattern follows the edge's
p1->p2 direction; we flip it to match the requested axis. seed defaults to the
last feature added (tree walk).

### M4 — Circular pattern (bolt circle) 🚧
`scripts/m4_circular.py` + `add_circular_pattern`: repeat a feature N times around
an axis = the cylindrical face nearest a given centre (so a centre hole's wall is
the axis — no reference-axis creation needed). Plate + centre hole + 6 bolt holes
= 13800.885 mm³. Lessons: select the axis face by ITERATION + Select4 with a Mark
via SelectionManager.CreateSelectData (SelectByID2-by-coordinate did NOT grab the
internal cylinder); marks axis=1, seed=4; FeatureCircularPattern's Spacing is the
PER-INSTANCE angle (360/count), not the total. This Select4+Mark machinery is now
reusable for mirror and other selection-heavy features.

### M4 — Equations 🚧
`scripts/m4_equation.py` + `set_equation`: add a global equation via
IEquationMgr.Add2(count, eq, solve=True). '"D1@BlockExtrude" = 2 * 12.5' drives
depth to 25 -> 20000 mm³ (expression evaluated). Persists a relation, unlike the
one-off set_dimension.

### Mirror — SHELVED (both routes blocked on this build)
Offset reference plane creation works (InsertRefPlane, Distance constraint=8,
metres, after selecting the Nth ref plane: order Front/Top/Right). But:
1. `InsertMirrorFeature`/`2` return None across all mark (0/1/2/4) and flip combos
   despite plane + seed both selecting True — the CALL is the snag, not selection.
2. The definition-object route also fails: `IFeatureManager.CreateDefinition`
   returns None for EVERY type id tried (4/7/12/98) on this 3DEXPERIENCE build.
Conclusion: shelved. It's a convenience an AI works around by placing features
symmetrically itself (compute mirrored coords + add_hole/add_hole_on_face) or via
a pattern. Revisit only if a macro-recorded sequence reveals a working path.

## Next

- **Mirror**: crack InsertMirrorFeature2 (or use a definition object). The offset
  reference plane half already works.
- **Holes on any face** (beyond +Z): the face-sketch 2D frame differs per face,
  so (x,y) needs a model->sketch transform.
- General revolve: arbitrary (radius, z) profiles; generic sketch primitives.
- Equations (`IEquationMgr`); richer rebuild-error reporting.
- Worth doing soon: an end-to-end **agentic-loop demo** on a non-trivial spec to
  validate the 20-tool set as a whole.

## Notes

- Standalone scripts run single-threaded (no COM worker needed); the server
  pins COM to one worker thread.
- Repeated `--keep-open` runs leave untitled parts (Part1, Part2, …) open in
  SolidWorks; harmless, close them manually.
