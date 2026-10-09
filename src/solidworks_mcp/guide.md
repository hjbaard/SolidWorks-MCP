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
    `add_revolved_profile` revolve about the **Y** axis; give
    `add_revolved_profile` an `axis_mm` (two points) to turn [x, y] points about
    any line on the Front plane instead (Pappus still holds: area x 2 pi x
    the centroid's distance from that line).
  - Sweeps: the path lies on the Front plane. For `add_swept_profile` it **must
    start at the origin heading +X**, and the profile is drawn on the Right
    plane (u along +Y, v along +Z).
- Faces are selected by the direction they face: `+x`, `-z`, ... The `*_on_face`
  tools take **3D points on the face** and use the planar face facing that way
  **through the point**: the top, a pocket floor, a step in between. A point on
  no such face is refused instead of being projected, and the error lists the
  levels that do exist. `+z:outer` / `+z:inner` force the outermost / innermost
  face. Tools without a 3D point (`cut_profile`, `add_hole`, `cut_slot`) use
  the outermost +Z face.

## 2. Work in small, verified steps

- Every modelling call returns the measured volume, mass and bounding box.
  **Compare with your own hand calculation after every step** and stop at the
  first mismatch. A wrong feature under ten correct ones is expensive to find.
- Hand calculations that work: prism = area x depth; revolve = area x 2 pi x
  centroid radius (Pappus); sweep = profile area x path length; a helical
  thread groove = area x 2 pi x centroid radius / pitch per mm of thread.
- Look before you select: `list_faces` / `list_edges` give indices, normals,
  areas and axes; `screenshot` shows the shape, from any standard view
  (`view="front"`, `"top"`, ...) or any direction (`from_dir=[-1, 1, -1]` sees
  what iso hides underneath, behind), and zoomed onto a detail with
  `zoom_mm=[[x1, y1, z1], [x2, y2, z2]]`. A cylindrical face comes with
  its axis, radius and a point on the axis, so holes and hole circles can be
  measured; `list_faces(component=...)` does that for a part in an assembly,
  in the part's own frame.
- **Undo a step** with `delete_feature(name)`: the feature goes together with
  its sketch. It refuses while other features are built on it (a fillet on its
  edges, a sketch on its face) and names them, and what is built on those;
  `with_children=True` deletes them all, `dry_run=True` only lists them. A
  loft or rib keeps its hidden helper plane, as in SolidWorks itself: delete
  that plane as well (`list_features` shows it as a RefPlane).
  Equations that named its dimensions break and come back as
  `broken_equations`; `with_equations=True` deletes them.
  `suppress_feature` takes a feature out but keeps it and its
  dimensions, to try a variant; `suppress=False` brings it back together with
  what depends on it.
- **A boss fills the cuts made before it**: every tool adds its feature at the
  end of the history, so a plate drawn over earlier holes closes them, and
  only the volume shows it. `reorder_feature("Plate", before="Hole1")` moves
  it before the first of them, and they cut through it again.
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
- Rounded corners (`corner_radii_mm`) are sketch fillets: each corner keeps its
  `x<i>` / `y<i>` as a virtual sharp, and corners with the same radius share one
  dimension, `radius` when all rounded corners share it, else `r<i>` named after
  the first corner with that radius.
- Profiles with more than 24 points (typically traced from a mesh), sweep paths
  and splines are **fixed** instead: dimensioning them takes minutes and gives
  nobody a usable handle. Rebuild them to change them.
- `list_dimensions` names every dimension in the part with its value, e.g. for
  a part opened from disk.
- `list_features` gives a part's history in tree order (name, SolidWorks type,
  suppressed), flags features that fail to rebuild, and names the sketches that
  are not fully defined. A part drawn by hand may have those: their geometry
  can still move, so say so before you build on it.
- Drive several dimensions from one number with a global variable:
  `set_equation('"W" = 40')`, then `set_equation('"width@Sketch1" = "W"')`;
  `set_equation('"W" = 45')` changes it again. `set_equations([...])` sets a
  list with one rebuild, and every result names the features that fail to
  rebuild (`failing_features`): set a variable over a range to find where a
  part breaks. `list_equations` marks equations left broken by a deleted
  feature; `delete_equation` removes them. An
  equation-driven dimension refuses `set_dimension` and names its equation. An
  angle (a revolve's, an angle mate's) is set in degrees; `list_dimensions`
  shows each dimension's unit.

## 4. Recipes

- **Holes**: `add_hole` (through, along Z), `add_counterbore_hole` (on +Z),
  `add_hole_on_face` (any planar face, through or blind with `depth_mm`: a
  heat-set insert hole, a screw pilot).
- **Standard holes**: `add_hole_wizard` sizes a hole from SolidWorks' own ISO
  tables: `clearance` (ISO 273; M3 is 3.2 / 3.4 / 3.6 for a close / normal /
  loose fit), `counterbore` (socket head cap screw), `countersink` (socket
  countersunk screw) and `tapped` (ISO tap drill plus a cosmetic thread). The
  sizes it used come back in `hole`, ready for your hand calculation. A blind
  hole ends in a 118 degree drill point; its depth counts from the surface.
  The Hole Wizard only makes *cosmetic* threads; `thread="modeled"` drills the
  ISO basic minor diameter and cuts a real, printable thread with the Thread
  feature, over the standard depth (2 x D) or all the way through.
- **Adding material to a part**: `add_boss_on_face` (a round boss: standoff,
  peg) and `add_extruded_profile_on_face` (a polygon pad or ledge) grow out of
  any planar face. A PCB standoff is a boss plus a blind hole in its top.
- **Pockets and slots**: `cut_profile` (+Z face), `cut_profile_on_face` (any
  face), `cut_slot` (obround on +Z). A rounded tab or lug between two points
  is `add_extruded_slot(start, end, width, depth)`: the round ends are centred
  on the points, so nothing needs working out. `cut_offset_pocket(face, x, y,
  z, rim_mm, depth_mm)` pockets a whole face and leaves a rim along its
  outline, whatever its shape: the recessed web of an I-beam along a curved
  link, a tray, or a frame when cut through. Hand calculation: the face's area
  shrunk by the rim (a corner of radius R keeps radius R - rim).
- **Rounded outlines**: give `corner_radii_mm` to `add_extruded_profile`,
  `cut_profile`, the `*_on_face` profile tools, `cut_profile_through_plane`,
  `add_revolved_profile` (rounded edges of a turned part; not corners on the
  axis) or `add_swept_profile`: one radius for every corner, or one per vertex
  (0 = sharp). Hand calculation: a right-angled corner of radius r loses
  r^2 (1 - pi/4) of area, a concave one gains it; in a revolve that area sits
  0.2234 r from the corner along both edges (Pappus). A radius too big for its
  edges is refused before anything is sketched, with the edge that is too
  short; so are arcs that would meet, as SolidWorks merges them: a circle is
  `add_disc`, round ends are `add_extruded_slot`, and any other outline is
  `add_sketch` with tangent arcs. Profiles over 24 points cannot be
  rounded (they are fixed, not dimensioned). For a plain rounded block,
  `add_box` plus `add_fillet(edges="z")` works as well.
- **Flowing outlines** (an S-bend in a leg, a hub running into a beam):
  `add_sketch(plane, start_mm, segments)` draws a chain on any plane:
  `{"line": [u, v]}`, `{"arc": [u, v], "through": [u, v]}`, `{"arc": [u, v],
  "center": [u, v]}` (the short way round), `{"arc": [u, v], "tangent": true}`
  (flowing on from the segment before) and `{"spline": [[u, v], ...]}`; end on
  the start to close it. A centre on the origin or on another arc's centre is
  tied to it, also when rounded points put it a hair beside it. Points are [u, v] in the plane's
  axes, which the result gives: front (x, y), top (x, -z), right (-z, y).
  Where segments flow into each other the sketch gets tangent relations, and
  the rest is dimensioned from the origin (`x<i>`/`y<i>` for the i-th point,
  `r<k>` for the radius of a free arc), so a changed dimension keeps the
  flow. Then `extrude_sketch` or `cut_sketch` the returned sketch. A tangent
  arc turns towards its end point: from (30, 0) heading +x, an arc to (50, 20)
  is a quarter circle round (30, 20).
- **Side-view shapes** (wedges, windows, symmetric recesses):
  `cut_profile_through_plane` on the Front/Top/Right plane or another plane by
  name, through all or a depth centred on the plane.
- **Planes at an angle**: `add_plane("front", angle_deg=30, about="y")` turns a
  default plane about a model axis it holds (right-hand rule: positive turns
  counterclockwise seen from the axis's positive end); `add_plane("Plane1", offset_mm=5)`
  moves any plane. Both return `origin_mm`, `x_axis` and `y_axis`: the point
  (u, v) on the plane is origin + u x_axis + v y_axis, which is what
  `add_extruded_profile_on_plane` and `cut_profile_through_plane` take. The
  angle or offset is a dimension, and what is built on the plane follows it.
- **Parts at an angle on the Front plane**: draw the profile straight and give
  `rotate_deg` and `about_mm` (the pivot) to `add_extruded_profile` or
  `cut_profile`; the turned points are dimensioned like any profile.
- **Tapered walls**: `draft_deg` on `add_extruded_profile` (and
  `add_extruded_profile_on_plane`) leans every wall inwards as it rises
  (negative: outwards); the angle is a dimension, `draft`. Hand calculation
  for a w x h rectangle extruded d with t = tan(draft): w h d - (w + h) t d^2
  + 4/3 t^2 d^3.
- **Organic shapes**: `add_lofted_solid` takes round sections,
  `{"center_mm": [x, y], "diameter_mm": d}`: a leg thick at the knee and thin
  at the foot, the centres free to wander. Two sections hold
  pi h (R^2 + Rr + r^2) / 3 wherever the centres lie (a steep slant comes out
  up to 1% fuller). Every profile starts at its first vertex (a round one on
  +x), so give polygons in the same vertex order. `add_swept_pipe(smooth=True)`
  runs a pipe along a spline through the points: pi r^2 x `path_length_mm`.
- **Several bodies**: `merge=False` keeps a new extrusion a body of its own;
  `list_bodies` names them, with volume and box. `combine_bodies("subtract",
  main, [tool])` cuts one out of another, `add` joins them and `common` keeps
  their overlap; `split_body(plane)` splits the part along a plane, such as
  one from `add_plane`. Faces, edges, fillets and the printability check see
  the first body only: combine the bodies before you work on their faces.
- **A shape from two views**: extrude the front view, then cut the side view
  with `cut_profile_through_plane(..., keep_inside=True)`: everything outside
  the side outline goes, and what is left matches both views.
- **Fillets and chamfers**: edges by axis (`x`/`y`/`z`), by index, `all`, or a
  face's outline (`+z:outline`: the outer edges of the top face, not the rims of
  holes in it), or every edge of one feature (`feature:Boss`: a boss's top and
  the edge where it meets the part, for a moulded look). On a big part,
  `list_edges(face="#5")`, `feature=`, `within_mm=` or `min_length_mm=` narrow
  the list; the indices stay those of the whole part. A refused fillet names
  the edges the radius does not fit and gives the ones that round together;
  `skip_shorter_mm` leaves out slivers. A chamfer is 45 degrees unless
  `angle_deg` says otherwise, measured from the face `from_face` names:
  `add_chamfer(1, "-z:outline", angle_deg=60, from_face="-z")` leaves the
  underside at 60 degrees, printable without support (1 mm in, tan 60 up).
  `radii_at_mm=[[x, y, z, r]]` lets the radius vary: r at the edge end at
  that point (`list_edges` gives the ends), `radius_mm` at the others, straight
  in between. Hand calculation for a right-angled edge of length L:
  (1 - pi/4) L (r1^2 + r1 r2 + r2^2) / 3 comes off.
  `add_full_round(face, x, y, z)` rounds a rib's top off completely: the point
  lies on the top, the round runs across the rib between its closest opposite
  sides, and a rib w wide and L long loses L w^2 (1/2 - pi/8).
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
  cylindrical face nearest the centre you give).
- **Mirror**: `add_mirror(plane, offset_mm, features)` mirrors features about
  the Front/Top/Right plane moved `offset_mm` (z / y / x = offset). Put the
  plane where the copies land in material: a box spans x 0..w, so mirror about
  `offset_mm = w / 2`; a copy outside the part fails. The copies follow their
  seeds. To keep the plane in the middle when the part grows, tie it to the
  width: `set_equation('"<plane_offset>" = "<width>" / 2')` with the names the
  tools returned. Without `features` the whole body is mirrored and merged:
  model half of a symmetric part and mirror it about the face where the
  halves meet. `plane` may also name another plane of the part (`"Plane1"`),
  moved `offset_mm` along its normal.
- **Text**: `add_text_on_face(text, face, x, y, z, height_mm, depth_mm)`
  engraves a label into any planar face; `emboss=True` raises it instead. The
  point is the text's lower-left corner, and the text runs along the face
  sketch's horizontal axis. It reads upright on the top (+z) and front (-y)
  faces, along +x; SolidWorks turns it on the others (vertical on +x and -x,
  upside down on +y), so put labels on the top or front. Letters have no hand calculation:
  the result's `text_area_mm2` is what the next steps change by (area x depth).
  `font` must be an installed font; an unknown one is refused, because Windows
  would quietly draw another.
- **A supplier's model**: `open_part` imports a STEP (`.step`/`.stp`), IGES or
  Parasolid (`.x_t`/`.x_b`) file as a new part. The imported body has no
  history, but the tools work on it: holes, pockets and bosses on its faces,
  fillets. The result has `solid_bodies` and mass properties: read the bounding
  box to see where the model lies before you place anything. With several
  bodies the tools act on the first. A file holding an assembly opens with
  `open_assembly` instead: its parts come in as components at their places,
  ready for `list_components` and `add_mate`.
- **Drawings**: `make_drawing("part.pdf")` puts front, top and right views
  (first angle) and an isometric view on A4 with the model's own dimensions,
  each shown once, so a fully dimensioned part gives a complete sketch to
  print or send; `.slddrw` keeps it editable. Save the part first.
- **Open documents**: `list_documents` lists what SolidWorks has open;
  `activate_document(title)` makes one current again, also a new part that was
  never saved (after `open_part` and `close_part` on another).
- **Assemblies**: `insert_component` puts a part's origin at a point; a
  `.sldasm` goes in whole as a sub-assembly (save an imported servo first).
  `delete_component` takes one out again, with its mates.
  `list_faces(component=...)` then lists every part's faces in the
  sub-assembly's own frame, naming the part, and `add_mate` takes them;
  transforms are read back, mates are measured back after the rebuild, and
  `check_interference` reports overlapping pairs with their volume.
  Component boxes are the parts' own extent, also when turned.
  `measure_distance` gives the clearance between two components (0 when
  they touch or overlap), or from a component to a point such as a pivot,
  with the nearest point; `inside: true` means the point lies in the material.
  `axis_mm=[[x, y, z], [dx, dy, dz]]` measures to an endless line instead,
  such as the axis of a bolt that is not modelled.
- **Joints**: a hinge is a `concentric` mate between the two holes (pick a
  cylinder by its `list_faces(component=...)` index, `"#5"`), a `coincident`
  mate between the faces that slide on each other, and an `angle` mate between
  two side faces. The angle mate returns its `dimension`: `set_dimension`
  turns the joint, and `check_motion(dimension, [30, 60, 90, 120, 150],
  distances=[["Rod", "Bolt"]])` checks overlaps and clearances at every step
  and puts the joint back. The mate is made at 0..180 degrees, but its
  dimension turns the joint the whole way round: a hip of -35..115 steps as
  `[-35, 0, 60, 115]` from any pair of side faces (a negative angle is the
  turn the other way from 0). `flip` turns an angle mate the other way. A mate
  that contradicts the others is refused and removed, naming the one it
  fights; `list_components` lists the mates and their errors, in words.
  `check_motion` fails when nothing moves: then the mate lost a face, or is
  suppressed. `delete_mate` and `suppress_mate` take the name add_mate returns.
  What a moving part sweeps over, to keep another part clear of it:
  `swept_region(part, dimension, values, heights_mm, margin_mm=..., frame=
  "Cover-1")` outlines it in a plane, in the cover's coordinates, as points for
  an `add_sketch` spline to cut with.
- **A person's part**: `list_features` names their sketches and planes.
  `read_sketch` reads a sketch back in model coordinates, so you can check it
  against the points that must fit before building on it. `extrude_sketch` and
  `cut_sketch` build on it by name, and it keeps its own dimensions and
  relations. A cut goes against the sketch's normal: from a face that is into
  the part; from a plane with the part in front of it (a box standing on the
  Front plane) it needs `reverse=True`. A post or rib that must end in a
  curved wall: `extrude_sketch(up_to="next")` ends on the wall's shape and
  follows it, where no single depth fits; `up_to="@x,y,z"` ends on the face
  through that point instead, also where "next" fails because a side of the
  profile lies on a face. Change the person's dimensions with
  `set_dimension` (names from `read_sketch` or `list_dimensions`). Their
  planes work by name in `add_mirror` and `cut_profile_through_plane`.

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
- Text prints best large and bold: as a rule of thumb at least 5 mm high, with
  strokes wider than the nozzle (0.4 mm), engraved 0.4-0.6 mm or embossed about
  1 mm. On a face that prints facing down, engrave rather than emboss.
- Before printing, `check_printability(up="+z", overhang_deg=45, min_wall_mm=0.8)`
  names the faces that need support for that build direction (worst lean,
  area, centre) and the walls thinner than the minimum (two nozzle widths is
  a common floor), with where they are thinnest. Try another `up` to find the
  orientation with the least overhang; the bed contact area tells how well it
  sticks.
- Export for slicing with `export(..., quality="fine")` as 3MF or STL. Both
  keep the model's own coordinates (`frame` in the result), so a mesh
  measures back where the part is; the slicer places it on the bed.

## 6. Reverse-engineering from a mesh (STL/3MF)

When a part must mate with something that only exists as a mesh, for example
a proven battery socket:

1. Slice the mesh with `slice_mesh` across the axis the parts slide along, at
   small steps, and note where the cross-section changes: those heights are
   your feature boundaries. For a slicer's 3MF, `frame='object'` gives the
   designer's modelling frame, `frame='build'` the print orientation.
2. Use the sections as polygon profiles; `slice_mesh` already drops only
   exactly collinear points. **Do not simplify them further**: mesh walls are
   rarely perfectly straight, so two sections of the same wall differ by a few
   micrometres. Profiles over 24 points are fixed rather than dimensioned.
3. **Build every section from one master**: take the master's points wherever a
   section shares its walls and keep the section's own points only where it
   really differs. Successive cuts along nearly (not exactly) coincident walls
   leave sliver faces, and the next cut then fails with `FeatureCut4 failed`.
4. Start each cut from the face it belongs to (give a point on it). Cut
   undercuts, such as latch recesses, from the floor of the narrower region
   above them.
5. Compare with `compare_with_mesh` at the same heights (`offset_mm` moves the
   mesh into your part's frame). Equal area with different extents means a
   shifted frame; a very different area means a misread feature, for example
   a solid wall where the reference is hollow; an unmatched loop is a pocket
   or hole only one of them has.

## 7. Limits and housekeeping

- Not available: importing meshes as bodies (slice them instead), sketches on
  arbitrary planes (a person's planes work in `add_mirror` and
  `cut_profile_through_plane`), free arcs inside polygon profiles (round corners with
  `corner_radii_mm`; other curves need revolves, splines, slots or holes).
- SolidWorks' memory grows over long sessions. If it warns about low memory,
  save your work and restart SolidWorks. Never restart it during a run: the COM
  connection breaks and every later call fails.
- Close documents you no longer need; saving over a file that is still open in
  SolidWorks fails. With no current document, `close_part` closes SolidWorks'
  active one if it is saved; `get_status` shows which one that is.
- Tools failing in ways this guide does not explain? Ask the user to run
  `uvx solidworks-mcp --selftest` with SolidWorks open: it shows which tool
  areas work on their installation and ends with where to report it.
