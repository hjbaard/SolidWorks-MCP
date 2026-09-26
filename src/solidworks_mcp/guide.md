# Modelling guide for the SolidWorks MCP server

How to get parts out of this server that are right the first time. Read the
short connect-time instructions first; this is the long version.

## 1. Coordinates and conventions

- All lengths are **millimetres**, angles **degrees**. The server converts to
  SolidWorks' metres and radians.
- New geometry starts on the **Front plane** (XY) and extrudes along **+Z** from
  z = 0: `add_box`, `add_extruded_profile`, `add_extruded_spline`, `add_disc`,
  `cut_profile` and the lofts all use this frame.
  - `add_box` spans x 0..width, y 0..height, z 0..depth (corner at the origin).
  - `add_disc` is centred on the origin, `add_cylinder` and
    `add_revolved_profile` revolve about the **Y** axis.
  - Sweeps: the path lies on the Front plane. For `add_swept_profile` it **must
    start at the origin heading +X**, and the profile is drawn on the Right
    plane (u along +Y, v along +Z).
- Faces are selected by the direction they face: `+x`, `-z`, ... picks the
  **outermost** planar face facing that way; `+z:inner` picks the **innermost**
  (a pocket floor, the inside of a shelled wall). Nothing in between can be
  selected, so plan the order of cuts around it (see 6).
- `*_on_face` tools take **3D points that lie on that face**; a point off the
  face is refused instead of being projected.

## 2. Work in small, verified steps

- Every modelling call returns the measured volume, mass and bounding box.
  **Compare with your own hand calculation after every step** and stop at the
  first mismatch. A wrong feature under ten correct ones is expensive to find.
- Hand calculations that work: prism = area x depth; revolve = area x 2 pi x
  centroid radius (Pappus); sweep = profile area x path length; a helical
  thread groove = area x 2 pi x centroid radius / pitch per mm of thread.
- Look before you select: `list_faces` / `list_edges` give indices, normals,
  areas and axes; `screenshot` shows the shape.
- A tool that cannot do what was asked returns `{ok: false, error}`. The error
  names the cause. Several SolidWorks calls fail silently (no feature, no
  error); the server checks for that and fails loud instead.

## 3. Dimensions: every sketch is fully defined

Every tool leaves its sketches fully defined, the way a designer would: a
relation only where the input is exactly horizontal, vertical or on the origin,
and one dimension from the origin for everything else. Nothing moves, and no
stray drag in SolidWorks can change the part.

- Tools return `dimensions`, `{role: name}`, and `fully_defined: true`. Change a
  dimension with `set_dimension(name, value)`; the new volume shows it worked.
  Roles follow the tool: box `width` `height` `depth`; disc `diameter`
  `thickness`; cylinder `radius` `height`; cone `bottom_radius` `top_radius`
  `height`; holes `diameter` `x` `y` (a counterbore also `clearance_diameter`,
  `cbore_diameter`, `cbore_x`, `cbore_y`, `cbore_depth`); slot `end1_x`
  `end1_y` `end2_x` `end2_y` `width`; loft `profile1_height`, `profile0_x1`, ...
- Polygons get `x<i>` / `y<i>`: the x or y of vertex i, measured from the
  origin, once per wall position (the vertices of one vertical wall share one
  `x`). On a face sketch these are that sketch's own horizontal and vertical
  directions, not the model axes. A coordinate of 0 is a relation, so it has no
  dimension.
- Profiles with more than 24 points (typically traced from a mesh), sweep paths
  and splines are **fixed** instead: dimensioning them takes minutes and gives
  nobody a usable handle. Rebuild them to change them.
- Drive several dimensions from one number with a global variable:
  `set_equation('"W" = 40')`, then `set_equation('"width@Sketch1" = "W"')`. An
  equation-driven dimension ignores `set_dimension` (`applied: false`).

## 4. Recipes

- **Holes**: `add_hole` (through, along Z), `add_counterbore_hole` (on +Z),
  `add_hole_on_face` (any planar face). Blind round holes on a face:
  `cut_profile_on_face` with a many-sided polygon.
- **Pockets and slots**: `cut_profile` (+Z face), `cut_profile_on_face` (any
  face), `cut_slot` (obround on +Z).
- **Side-view shapes** (wedges, windows, symmetric recesses):
  `cut_profile_through_plane` on the Front/Top/Right plane, through all or a
  depth centred on the plane.
- **Fillets and chamfers**: edges by axis (`x`/`y`/`z`), by index, or `all`.
  Round vertical edges **before** cutting slots: a slot adds tangent edges that
  an axis selector would catch too.
- **Ribs / gussets**: `add_rib` draws a straight rib in a plane parallel to the
  Front plane at `z_mm`; give `toward_mm` as a point on the side to fill (for an
  L-bracket: its inner corner).
- **Threads** (real, printable): `add_thread` with an ISO size as SolidWorks
  names it (`M10x1.5`, `M3x0.5`, `M10x1.0`). External: the rod must have the
  nominal diameter. Internal: drill the ISO basic minor diameter first,
  D - 1.0825 P (M10x1.5 -> 8.376 mm); the error tells you the value.
- **Patterns**: `add_linear_pattern`, `add_circular_pattern` (the axis is the
  cylindrical face nearest the centre you give). There is **no mirror**: place
  features symmetrically yourself.
- **Assemblies**: `insert_component` puts a part's origin at a point;
  transforms are read back, mates are measured back after the rebuild, and
  `check_interference` reports overlapping pairs with their volume.

## 5. 3D printing

- Leave a gap of about 0.2-0.4 mm between printed parts that slide together,
  more for ASA/ABS than for PLA. Print a small fit test of a critical interface
  before the whole part.
- Printed holes come out 0.1-0.2 mm small. A hole that a part is pushed through
  must pass the part's widest section, e.g. the diagonal of a flat blade (a
  6.35 x 0.81 mm spade tab needs more than 6.4 mm), not just its width.
- Heat-set inserts: follow the insert maker's hole size. For common M3 x 5.7 mm
  inserts that is about 4.0 mm, at least 0.5 mm deeper than the insert: the
  displaced plastic needs room, or it bulges at the mouth and the mating part no
  longer seats flat.
- Export for slicing with `export(..., quality="fine")` as 3MF or STL. **STL
  output is moved into positive space** by SolidWorks: compare geometry in the
  model frame, not in raw STL coordinates.

## 6. Reverse-engineering from a mesh (STL/3MF)

When a part must mate with something that only exists as a mesh, for example
a proven battery socket:

1. Slice the mesh across the axis the parts slide along, at small steps, and
   note where the cross-section changes: those heights are your feature
   boundaries.
2. Use the sections as polygon profiles. **Do not simplify them loosely**:
   remove only exactly collinear points. Mesh walls are rarely perfectly
   straight, so two sections of the same wall differ by a few micrometres.
3. **Build every section from one master**: take the master's points wherever a
   section shares its walls and keep the section's own points only where it
   really differs. Successive cuts along nearly (not exactly) coincident walls
   leave sliver faces, and the next cut then fails with `FeatureCut4 failed`.
4. Order the cuts so each one starts from a face you can select (outermost or
   innermost). Cut undercuts, such as latch recesses, from the floor of the
   narrower region above them.
5. Compare: export your part, slice it at the same heights and compare the
   section areas and extents with the reference. Equal area with different
   extents means a shifted frame; a very different area means a misread
   feature, for example a solid wall where the reference is hollow.

## 7. Limits and housekeeping

- Not available: importing meshes as bodies, mirror, drawings, sketches on
  arbitrary planes, arcs inside polygon profiles (approximate them with enough
  points, or use revolves, splines and holes).
- SolidWorks' memory grows over long sessions. If it warns about low memory,
  save your work and restart SolidWorks. Never restart it during a run: the COM
  connection breaks and every later call fails.
- Close documents you no longer need; saving over a file that is still open in
  SolidWorks fails.
