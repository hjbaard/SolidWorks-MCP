# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `add_chamfer(angle_deg=60, from_face="-z")` chamfers at another angle than
  45 degrees: the distance runs along the named face and the angle is
  measured from it, round an arc too (a cone). SolidWorks picks one of an
  edge's two faces by itself; the chamfer is checked and turned round when
  it took the other.
- `set_appearance(rgb, transparency, component)` colours the current part,
  or one component in an assembly (the part file keeps its own colour), and
  makes it see-through or solid again.

### Fixed
- An assembly left editing one of its parts (a double click in SolidWorks)
  showed every other component see-through, and `insert_component` failed
  with "AddComponent5 returned None". Every tool now edits the assembly as a
  whole again first.
- A fillet refused on an edge that "fails even alone" said nothing about how
  large a round would fit; the refusal now names the largest radius each
  such edge takes (to 0.01 mm), found by trial.

## [0.14.0] — 2026-10-09

### Added
- `reorder_feature(name, before)` moves a feature, with its sketch, before
  another one: a boss added last fills the holes cut before it, and moved
  before them it is cut through again. It refuses, naming the feature it
  depends on, when that would come after it.
- `delete_feature(dry_run=True)` lists what a delete would take along
  (`would_delete`) and leaves the part as it is.
- `extrude_sketch(up_to="@x,y,z")` ends on the face through that point. Up
  to next fails when a side of the profile lies on a face of the part (a
  strip against a boss); its refusal now says so and names a point on the
  face ahead to end on instead.
- An angle mate's dimension turns its joint below 0 in `set_dimension`,
  `check_motion` and `swept_region`: -30 is 30 the other way from 0. Past
  180 it already turned on; a joint of -35..115 needs no face pair picked
  to keep it inside 0..180 any more.

### Changed
- STL exports keep the part's or assembly's own coordinates, also one file
  per component, where SolidWorks moved them into positive space; the
  result names the `frame`. A mesh measured back now lines up with the
  model, and slicers place the part on the bed anyway.

### Fixed
- `delete_feature` named only the features built directly on the one to
  delete, while `with_children=True` also took what was built on those; the
  refusal now names everything that goes.
- A STEP import that SolidWorks refused ("error 1") could leave the parts it
  had made open, hidden and unsaved; a failed or refused import now closes
  every document it opened.
- SolidWorks now and then ignores a dimension's new value at random (about
  1 in 25 on a fresh part), so `set_dimension` refused a plain value and
  `check_motion` stopped at a step. The value is now written once more when
  the old one reads back; a value SolidWorks refuses is still refused.

## [0.13.0] — 2026-10-06

### Added
- `add_mate` picks a face by a point on it, `"@x,y,z"` in the component's own
  coordinates: a hole by a point on its wall, whatever the face numbers.
- A part inside a sub-assembly by its path, such as `"Leg-1/Thigh-1"`, in
  every assembly tool; a joint inside one steps as `"D1@Angle1@Leg-1"`.
- `add_fillet(tangent_propagation=True)` carries a round on along the edges
  that run on smoothly from the given ones, as SolidWorks does by default.

### Fixed
- An equation replaced in place that used a variable added below it made the
  Equations folder warn, and a new value of the variable took two rebuilds:
  SolidWorks solved the equations as listed. `set_equations` now lets
  SolidWorks order them; `list_equations` reports `automatic_solve_order`.
- `set_dimension` on a point's coordinate from the origin: -5 put the point
  across the origin but read back 5, "not applied", and a later 3 kept it on
  that side, at -3, reported as applied. A coordinate now takes its sign;
  any other length below 0 is refused, the part as it was.
- A component's face numbers could come in another order after a mate, and
  `add_mate` took the wrong faces; they are now ordered by part, type, centre
  and area.
- `check_motion` stepped a joint while a mate was broken, a concentric mate
  on a hole made again, and the part slid off its axis. It and
  `swept_region` now refuse and name the mate; `list_components` also flags a
  mate that lost a face without an error from SolidWorks, and `rebuild` of an
  assembly names such mates in `mate_errors`.
- `set_dimension` answered ok with `applied: false` when SolidWorks ignored
  the value: an equation drives the dimension, or a size of 0, which also left
  its sketch "invalid solution". It now refuses with the reason (naming the
  equation) and puts the old value back.
- The distance to a whole sub-assembly, in `measure_distance` and in
  `check_motion(distances=...)`, failed: it is measured through its parts.
- `check_motion` on a joint inside a sub-assembly said nothing moved.
- A fillet edge that ends where it runs on into another tangentially, such as
  round a fillet, was refused as failing "even alone", with no reason; the
  refusal now says that it rounds with `tangent_propagation=True`. A single
  refused edge is looked into too.

## [0.12.0] — 2026-10-06

### Added
- `delete_feature` names the equations it leaves broken
  (`broken_equations`); `with_equations=True` deletes them along, keeping
  the global variables.
- `set_material(name, density_kg_m3=...)` assigns a material SolidWorks
  does not have, such as TPU, by its density. The part shows it by name and
  keeps it when opened elsewhere; SolidWorks' material settings stay as they
  were.
- `screenshot(from_dir=[x, y, z])` looks from any direction, such as from
  behind and below, which no standard view shows; the model's +y stays up.
- `extrude_sketch(up_to="next")` extrudes up to the next face of the part:
  a post into a curved wall ends on its shape and follows it when the wall
  changes, where no single depth fits.
- `swept_region` outlines what a component covers in a plane while a joint
  moves through its range, with a margin, in the coordinates of the part to
  cut from: an ordered outline per region, ready for a spline.

### Changed
- `screenshot` leaves reference planes and axes out, so they do not cut
  through the shape; `show_planes=True` keeps them.

### Fixed
- `screenshot` no longer offers .bmp, which SolidWorks did not write.
- `make_drawing` left its drawing open in SolidWorks, and with it the part
  after `close_part`: it closed the drawing by the title it had before it
  was saved.
- `add_hole_wizard` picked its face as seen on screen, so in another view
  (a screenshot leaves one behind) the hole failed with "has no position
  sketch to define", or silently went into the face seen first there. It
  now picks the face straight along its normal, whatever the view.
- `add_hole_wizard` at a point off the face, for instance inside an earlier
  hole's countersink, now says so, and by how much.
- `screenshot(zoom_mm=...)` in any view but the front zoomed onto another
  region, or showed only the background: the region is now turned into the
  view first.
- `add_mate` refused a coincident or distance mate on a slanted face that is
  not symmetric ("gives 0.0158 mm instead of 0 mm"): it measured from the
  face's box centre, which lies off such a face's plane.
- `add_sketch` refused concentric arcs whose chords share one bisector, a bent
  slot, as "under- or over-defined": their ends barely held the shared centre,
  and SolidWorks saw it free. A radius holds it now.
- After SolidWorks was restarted, every call failed with "the RPC server is
  unavailable" until the MCP server was restarted too. It now attaches to the
  running SolidWorks by itself.
- `check_printability(min_wall_mm=...)` on a part with curved faces could hang
  SolidWorks: the long slivers of their facets got samples by the square of
  their length, over a million rays in one batch. They now get as many as
  their size needs, at most 100 000 in all.
- `check_printability(min_wall_mm=...)` read the hollow side of a curved wall
  as 0.001 mm thin: it now measures to where a ray leaves the material.

## [0.11.0] — 2026-10-05

### Added
- `set_equation` replaces the equation that sets the same name, a global
  variable (in any case) or a dimension, instead of refusing it.
  `set_equations` takes a list with one rebuild at the end; a refused one
  undoes the list. `list_equations` lists them and marks the ones left
  broken by a deleted feature; `delete_equation` removes one. Their results
  name the features that fail to rebuild (`failing_features`).
- `{"arc": [u, v], "center": [u, v]}` in `add_sketch`: an arc round a given
  centre, the short way.
- `list_documents` and `activate_document`: back to an open document by its
  title, also a new one that was never saved.
- `delete_component` removes a component from an assembly, with its mates.
- `delete_mate` and `suppress_mate`; `add_mate` returns the new mate's name
  (`mate`).
- `check_motion` lists the components that moved (`moving`) and fails when
  nothing moves: the mate it steps holds nothing any more.
- `export(..., per_component=True)` writes an assembly's STL as one file per
  component; every export lists its `files`.
- `list_edges` takes `face`, `feature`, `within_mm` and `min_length_mm`, and
  gives every edge's length.
- `add_fillet(skip_shorter_mm=...)` leaves out short edges. A refused fillet
  names the edges that do not fit and offers the ones that round together.
- Profiles up to 60 points are dimensioned point by point (24 before).
- Feature and mate errors come with their cause in words.

### Changed
- Tool calls run with SolidWorks' CommandInProgress set, which makes its COM
  calls up to a hundred times faster: `list_edges` on 144 edges took 18 s,
  now 0.6 s.
- An assembly exported to STL is one file, whatever SolidWorks' own setting
  says (with one file per component the expected file never appeared).

### Fixed
- Holes and discs placed just off an axis snapped onto it: SolidWorks' sketch
  inference moved a hole 0.75 mm below the x axis onto the axis, and its
  sketch got no y dimension. Circles are now drawn without inference.
- `add_sketch` refused arcs whose centres lie a hair apart, or a hair off the
  origin ("SolidWorks did not draw the points"): SolidWorks merges points
  closer than a tenth of a micrometre. Such points are one point now, and a
  centre that close to the origin goes onto it, as a relation.
- A part's bounding box hugs its curved faces: SolidWorks' own part box ran
  loose round lofts and splines.

## [0.10.0] — 2026-10-04

### Added
- `add_revolved_profile(axis_mm=[[x1, y1], [x2, y2]])` turns a profile on the
  Front plane about any line in it, not only the Y axis: a hub turned about its
  own centre. The axis ends are dimensions.
- `edges="feature:Boss"` for `add_fillet` and `add_chamfer`: every edge of one
  feature, its top and where it meets the part, for a moulded look.
- `insert_component` takes a `.sldasm` as a sub-assembly, such as a servo
  imported from STEP; `list_faces(component=...)` lists its parts' faces in the
  sub-assembly's frame, each with its part, and `add_mate` mates them.
- `add_plane`: a reference plane at an offset from a plane, or turned about a
  model axis by the right-hand rule, its offset or angle a dimension; it gives
  the plane's origin and axes. `add_extruded_profile_on_plane` extrudes a
  polygon drawn on any plane, such as that one.
- `rotate_deg` and `about_mm` on `add_extruded_profile` and `cut_profile` turn
  the profile about a pivot first: a crank at 150 degrees, notches at +/-60.
- `measure_distance(axis_mm=[[x, y, z], [dx, dy, dz]])`: the distance from a
  component to an endless line, such as a bolt's axis.
- `draft_deg` on `add_extruded_profile` and `add_extruded_profile_on_plane`:
  the walls lean inwards as they rise (negative: outwards); the angle is a
  dimension, `draft`.
- `add_lofted_solid` takes round sections, `{"center_mm": [x, y],
  "diameter_mm": d}`, their centres free to wander: a leg thick at the knee
  and thin at the foot.
- `add_swept_pipe(smooth=True)` runs the pipe along a spline through the
  points; the result gives `path_length_mm`.
- Several bodies in one part: `merge=False` on `add_extruded_profile` and
  `add_extruded_profile_on_plane` keeps a body of its own; `list_bodies` lists
  them; `combine_bodies` adds, subtracts or intersects them; `split_body`
  splits the part along a plane.
- `add_fillet(radii_at_mm=[[x, y, z, r], ...])`: a variable fillet. Its radius
  runs straight from one edge end to the other, and each end's radius is a
  dimension, listed in `vertex_radii`.
- `add_full_round`: rounds a rib's top off between its two closest opposite
  sides, as on a moulded part.
- `list_edges` gives each edge's ends (`ends_mm`).
- `add_sketch`: a sketch of lines, arcs (through a point, or flowing on from
  the segment before) and splines on any plane, fully defined. Segments that
  meet smoothly get tangent relations and the rest is dimensioned from the
  origin, so a changed dimension keeps the shape's flow. `extrude_sketch` and
  `cut_sketch` build on it. A radius of half an edge, which polygon corners
  refuse, is a pair of tangent arcs here.

### Fixed
- `add_lofted_solid` starts every profile at its first vertex. SolidWorks
  started each at the corner nearest the origin, which twisted a loft whose
  profiles lie off-centre.

## [0.9.0] — 2026-10-03

### Added
- `screenshot` takes a `view` (`front`, `back`, `left`, `right`, `top`,
  `bottom` or `iso`) and `zoom_mm`, the corners of a region to fill the image,
  to judge a detail.
- `measure_distance`: the smallest distance between two components, or from a
  component to a point, with the nearest point and whether the point lies in
  the material. 0 means they touch or overlap.
- `add_mirror` and `cut_profile_through_plane` take the name of any plane in
  the part (`plane="Plane1"`), such as one a person made; an unknown name lists
  the planes there are.
- `read_sketch` reads a sketch back in model coordinates: its lines, arcs,
  circles and splines, its dimensions and whether it is fully defined.
  `extrude_sketch` and `cut_sketch` build on an existing sketch by name, such as
  one a person drew; it keeps its own dimensions and relations.
- `add_mate` takes `concentric` (two cylindrical faces: a pin in a hole, a
  hinge) and `angle` (`angle_deg`), and a face by its `list_faces(component=...)`
  index, `"#5"`. A distance or angle mate returns its dimension, so a joint
  angle is one number for `set_dimension`. A refused mate is removed again,
  naming the mate it contradicts; `list_components` lists the mates.
- `check_motion` steps a joint through its range and gives per step the
  overlapping pairs and the distances between chosen pairs, with each pair's
  smallest distance and the angle where it occurs.
- `cut_offset_pocket` pockets a face and leaves a rim along its outline, arcs
  and splines included: the recessed web of an I-beam, a tray, or a frame when
  cut through. The rim and the depth are dimensions.
- `cut_profile_through_plane(keep_inside=True)` cuts away everything outside
  the profile: extrude a front view, cut a side view this way, and the part is
  the shape both views agree on.
- `check_printability(up, overhang_deg, min_wall_mm)`: for a print direction,
  the faces that overhang more than the limit (area, worst lean, centre; the
  faces on the bed excluded), the bed contact and the print height; with
  `min_wall_mm` also the walls thinner than that, measured through the
  material from points spread over every face.
- `make_drawing(path)`: a 2D drawing of the part as PDF, or as an editable
  .slddrw: front, top and right views in first angle projection plus an
  isometric view on A4, with the model's own dimensions, each shown once.

### Changed
- Corner radii whose arcs would meet (R10 on a 20 mm edge) are still refused,
  as SolidWorks merges such arcs, but the error now points at `add_disc` for a
  circle and `add_extruded_slot` for round ends.

### Fixed
- The bounding box of a turned component was SolidWorks' turned outer box, too
  big for a round or slanted part (a 20 mm disc turned 45 degrees came out
  28.3 mm wide). `list_components`, `set_component_transform` and
  `get_assembly_bounding_box` now give each part's own extent. Sub-assemblies
  are boxed by their parts; a lightweight component is refused, as its geometry
  is not loaded.
- `set_dimension` wrote an angle as millimetres: 180 degrees landed as 0.18
  radians, about 10 degrees. An angle (a revolve, an angle mate) now takes
  degrees, and the result's keys end in `_deg`.

## [0.8.0] — 2026-10-03

### Added
- `add_disc` takes a centre (`x_mm`, `y_mm`), its position two dimensions;
  until now a disc could only sit on the origin.
- `edges="+z:outline"` for `add_fillet` and `add_chamfer`: the outer edges of a
  face, without the edges of holes and pockets inside it.
- `list_faces` gives a cylindrical face's axis, radius and a point on the axis
  (a hole's centre), and lists a component's faces in its own frame with
  `component`, so a hole circle on an imported part can be read off.
- `close_part` without a current document closes SolidWorks' active one, as
  long as that has no unsaved changes (closing never asks, so they would be
  lost). `get_status` names the server's version.
- `add_extruded_slot`: a stadium (rounded tab, lug or link) from one point to
  another, with the half-round ends centred on the points themselves.

### Fixed
- Every sketch was refused ("SolidWorks refused a horizontal relation") in a
  part that `open_part` returned while it was already open behind another
  document: it became the server's current part without becoming SolidWorks'
  active document. Tools now bring the current document to the front first,
  which also covers a window switched by hand in SolidWorks.
- A failed tool call left its sketch in the tree, where it also counted in the
  bounding box. Every call now runs guarded: when it fails, whatever it added
  to the current part is removed again.

## [0.7.0] — 2026-10-02

### Added
- `corner_radii_mm` on `add_extruded_profile`, `cut_profile`,
  `add_extruded_profile_on_face`, `cut_profile_on_face`,
  `cut_profile_through_plane`, `add_revolved_profile` (not on the axis) and
  `add_swept_profile`: round a polygon's corners with real sketch
  fillets, one radius for all corners or one per vertex (0 = sharp). The
  corners keep their dimensions as virtual sharps and equal radii share one
  dimension (`radius`, else `r<i>`), so the profile stays fully defined and
  editable. Until now a rounded outline meant many points, and over 24 points
  a profile is fixed rather than dimensioned. A radius too big for its edges
  is refused before anything is sketched, naming the edge.
- `add_text_on_face`: engrave text into any planar face, or emboss it, for
  labels and version numbers. The text's position is two dimensions and the
  sketch is fully defined; the result gives `text_area_mm2`, since letters have
  no hand calculation. A font that is not installed is refused: Windows would
  quietly draw another one. Text reads upright on the top (+z) and front (-y)
  faces; SolidWorks turns it on the others.
- The selftest checks text and a STEP export and import too (15 checks).
- `open_part` imports STEP, IGES and Parasolid files as a new part, so a
  supplier's model can get holes, pockets and bosses. The result reports the
  solid bodies and mass properties. `open_assembly` imports such assemblies,
  their parts as components in place, ready to list and mate. Each refuses the
  other kind of file and closes it again, components and all.

### Fixed
- `cut_profile` with a depth of 0 or less sketched the profile before refusing,
  leaving a stray sketch in the tree; the depth is checked first now.

## [0.6.0] — 2026-10-02

### Added
- `list_features`, `delete_feature` and `suppress_feature`: work on a part's
  history, also of a part opened from disk. `list_features` gives the features
  in tree order (without SolidWorks' folders, default planes and origin), flags
  the ones that fail to rebuild, and names sketches that are not fully defined.
  `delete_feature` undoes a step together with its sketch, and refuses while
  other features are built on it, naming them, unless `with_children=True`.
  `suppress_feature` keeps the feature and its dimensions; unsuppressing brings
  back what depends on it too, so the two calls round-trip.
- `add_mirror`: mirror features, or the whole body, about the Front, Top or
  Right plane moved `offset_mm`. The copies follow their seeds, and the plane's
  position is a dimension, so an equation can keep it at half the width. Mirror
  was listed as unsupported since 0.1.0 on a wrong diagnosis: the API works, but
  SolidWorks silently builds an empty mirror when the copy lands outside the
  part (a box spans x 0..w, so mirroring about x = 0 misses it). That now fails
  loud and leaves the part as it was.
- `solidworks-mcp --selftest`: checks your installation before you report an
  issue. It prints the SolidWorks release, language, templates and units, then
  runs thirteen checks on small parts and an assembly (holes, fillet, revolve,
  Hole Wizard, thread, material, mirror, delete, STL export), compares each with a hand
  calculation and closes them unsaved. GitHub issue forms for bug and
  compatibility reports ask for its output.

### Changed
- Sketches that are not fully defined are reported as 'Sketch3 (under
  defined)' instead of 'Sketch3 (status 2)'.

### Fixed
- An edge index out of range (`add_fillet` / `add_chamfer` with `edges="12"`)
  was the last error message still in Dutch.

## [0.5.0] — 2026-09-27

### Added
- `add_hole_wizard`: ISO holes from SolidWorks' Hole Wizard, sized by its standard
  tables: clearance (ISO 273 close/normal/loose), counterbore for socket head
  cap screws, countersink for socket countersunk screws, and tapped holes. The
  Hole Wizard only makes cosmetic threads, so `thread="modeled"` drills the ISO
  basic minor diameter and cuts a real, printable thread with the Thread
  feature. The hole lands exactly on the given point (the Hole Wizard itself
  lands ~0.04 mm off) and its position sketch is fully defined.
- `slice_mesh` and `compare_with_mesh`: slice an STL or 3MF (object or build
  frame, component transforms applied) into polygon loops ready to use as
  profiles, and compare the part's cross-sections with it. The part is
  measured in its own frame, not SolidWorks' STL shift to positive space.
- `add_boss_on_face` and `add_extruded_profile_on_face`: grow a round boss
  (standoff, peg) or a polygon pad out of any planar face. Until now all
  material had to start on the Front plane.
- `add_hole_on_face(..., depth_mm)`: blind round holes on any face (heat-set
  inserts, screw pilots) as real circles, so diameter and depth stay editable
  dimensions. The old workaround, a many-sided polygon, is fixed, not dimensioned.
- `list_dimensions`: every dimension in the part with its name, value and unit.

### Changed
- The `*_on_face` tools use the face through the given point, so a pocket floor
  or a step between the outermost and innermost face can be sketched on;
  `:outer` / `:inner` still force the extremes. A point on no face facing that
  way still fails, and the error now lists the levels there are.

## [0.4.0] — 2026-09-26

### Fixed
- Polygon outlines are drawn exactly. SolidWorks' automatic relations snapped a
  nearly horizontal or vertical segment: a middle one silently moved a point,
  a closing one failed with 'Could not create line segment'. Real outlines
  (e.g. sliced from a mesh) are full of such segments.
- `solidworks_mcp.__version__` said 0.2.0 since 0.2.0; it now matches the
  release, and a test keeps it that way.

### Added
- **Fully defined sketches.** Every tool leaves its sketches fully defined, the
  way a designer would: a relation only where the input is exactly horizontal,
  vertical or on the origin, and one dimension from the origin for the rest, so
  nothing moves. Tools return `dimensions` by role (`width@Sketch1`,
  `diameter@Sketch3`, ...) and `fully_defined`; change them with `set_dimension`
  or drive them from a global variable with `set_equation`. Profiles with more
  than 24 points, sweep paths and splines are fixed instead: SolidWorks slows
  down quadratically with the number of dimensions, and such geometry has no
  useful handles. Every integration test now fails on an under-defined sketch,
  and changing a returned dimension gives the hand-calculated volume.
- Modelling guidelines for agents: short connect-time `instructions` (conventions,
  verify-each-step, pitfalls) and the `solidworks://guide` resource with the full
  guide, including how to reverse-engineer a part from a mesh.
- `cut_profile_through_plane`: cut a polygon sketched on the Front, Top or Right
  plane, through all in both directions or a given depth centred on the plane.

## [0.3.0] — 2026-09-25

### Added
- `add_thread`: real, printable ISO metric threads (external on a rod, internal in
  a hole) via SolidWorks' own Thread feature. The size is checked against the
  thread-profile library, because SolidWorks silently cuts a meaningless groove
  for an unknown size. Verified against a hand calculation of the ISO groove.
- `add_rib`: a straight rib / gusset in a plane parallel to the Front plane,
  grown toward a given point until it meets the part. Verified against a hand
  calculation (an L-bracket gusset adds exactly a*b/2 * thickness).

### Changed
- All error messages and script output are English now (they were Dutch).
- The server now starts and lists its tools on any OS, so MCP directories
  (Glama) can introspect it in a Linux container. Off Windows every tool call
  fails loud: SolidWorks still needs Windows. `pywin32` is a Windows-only
  dependency now.

### Fixed
- `add_lofted_solid` left its helper offset planes visible, cluttering every
  screenshot; they are hidden after the loft now.

## [0.2.1] — 2026-09-25

### Fixed
- **Only SOLIDWORKS 2026 could connect**: the typelib version was hard-coded to
  34 (2026), so on any other release the first call failed with "Library not
  registered". The version is now read from the running SolidWorks.
- **Fresh installs crashed on start-up**: `mcp>=1.0` now resolves to mcp 2.x,
  which renamed `FastMCP`. Pinned to `mcp>=1.26,<2` (1.26–1.30 verified).

### Added
- `server.json` for the MCP Registry (`io.github.hjbaard/solidworks-mcp`) and
  PyPI-ready package metadata.

### Changed
- README rewritten: what it does and why, a gallery built by the tools
  themselves, a one-line install via `uvx`, and an honest compatibility note.

### Removed
- `scripts/m6_demo_kamer.py`: it needed private sample parts, so nobody else
  could run it. Assemblies stay covered by `tests/test_assembly.py`.

## [0.2.0] — 2026-09-05

### Added
- **Assemblies (M6)**: `new_assembly`, `open_assembly`, `save_assembly`,
  `insert_component`, `list_components`, `set_component_transform`, `add_mate`
  (coincident / distance / parallel / perpendicular between planar faces),
  `check_interference` and `get_assembly_bounding_box`. `export`, `screenshot`,
  `rebuild`, `get_mass_properties` and `close_part` now serve assemblies too.
- Every placement is verified against the geometry: a written component
  transform is read back and compared, and a mate is measured back after the
  rebuild (perpendicular distance, or the angle between the faces) and rejected
  if it did not deliver what was asked.
- Face selectors take an optional `:inner` suffix (`+y:inner`) to address the
  cavity side of a hollow part.
- `scripts/m6_demo_kamer.py`: assembles a furnished bedroom from three parts and
  asserts the bounding box, the floor contact, a 50 mm distance mate and a
  clash-free interference check.

### Fixed
- **Face selection on hollow parts**: `_planar_face_by_normal` returned whichever
  face the API listed first when several shared the same normal, so on a shelled
  box it could pick the inner face of the opposite wall instead of the outer
  skin. It now picks the extreme along the direction — outermost by default,
  innermost with `:inner`.
- A sketch left open by a rejected on-face point (`add_hole_on_face`,
  `cut_profile_on_face`) broke the *next* operation; the sketch is now always
  closed, and a missing active sketch fails at its own root cause.
- `get_bounding_box` on an assembly returned `null` instead of failing, and
  `IAssemblyDoc.GetBox` reports stale extents until the assembly is rebuilt —
  both now give a live box or a readable error.

## [0.1.0] — 2026-06-26

**First public release — an early, experimental first draft.** It works
end-to-end against SOLIDWORKS 2026 (3DEXPERIENCE R2026x) on the author's machine,
but the API surface and conventions may still change. Treat it as a starting
point, not a stable product.

### Added
- MCP server (stdio) that drives a locally running SolidWorks instance over the
  COM API, with all calls serialised on one dedicated STA worker thread.
- **Build**: `add_box`, `add_cylinder`, `add_cone`, `add_disc`,
  `add_extruded_profile`, `add_extruded_spline` (free-form), `add_revolved_profile`
  (general revolve), `add_swept_pipe` (round profile along a path),
  `add_swept_profile` (any cross-section along a path), `add_lofted_solid`.
- **Cut**: `add_hole`, `add_hole_on_face` (any planar face), `add_counterbore_hole`,
  `cut_profile`, `cut_profile_on_face`, `cut_slot`.
- **Modify**: `add_fillet`, `add_chamfer`, `add_shell`, `add_linear_pattern`,
  `add_circular_pattern`, `set_dimension`, `set_equation`, `set_material`, `rebuild`.
- **Inspect/measure**: `get_status`, `get_mass_properties`, `get_bounding_box`,
  `list_faces`, `list_edges`.
- **I/O**: `export` (STEP/STL/IGES/Parasolid/3MF, with STL/3MF tessellation
  resolution control), `screenshot`, `save_part`, `open_part`, `new_part`,
  `close_part`.
- Two-layer test suite (pytest): pure unit tests + SolidWorks integration tests
  that verify each feature's volume against a hand calculation.
- `scripts/m5_demo_bracket.py`: an end-to-end demo that builds a functional
  3D-print mounting bracket through the full build → measure → verify loop.

### Known limitations
- **Mirror** is not supported (both SolidWorks API routes fail on this build).
- Swept profiles require the path to start at the origin heading +X.
- 3D (non-planar) sweep paths, countersink holes, and embossed text are not yet
  implemented.
- Verified only against SOLIDWORKS 2026 (3DEXPERIENCE R2026x).
