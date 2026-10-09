# SolidWorks MCP

[![PyPI](https://img.shields.io/pypi/v/solidworks-mcp)](https://pypi.org/project/solidworks-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/hjbaard/SolidWorks-MCP/blob/main/LICENSE)

Let an AI agent (Claude, or any other MCP client) model real parametric parts and
assemblies in **your own SolidWorks** — and **check its own work**. Every
modelling call returns the measured volume, mass and bounding box, so the agent
can compare the result with the spec and correct itself instead of guessing that
it "looks about right".

<!-- mcp-name: io.github.hjbaard/solidworks-mcp -->

<p align="center">
  <img src="https://raw.githubusercontent.com/hjbaard/SolidWorks-MCP/main/Docs/images/bracket.png" width="32%" alt="3D-print mounting bracket with counterbored holes, bolt circle and cable slot">
  <img src="https://raw.githubusercontent.com/hjbaard/SolidWorks-MCP/main/Docs/images/thread.png" width="32%" alt="M10 bolt with a real, printable ISO metric thread">
  <img src="https://raw.githubusercontent.com/hjbaard/SolidWorks-MCP/main/Docs/images/vase.png" width="32%" alt="Revolved and shelled vase">
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/hjbaard/SolidWorks-MCP/main/Docs/images/pipe.png" width="32%" alt="Swept pipe with rounded bends">
  <img src="https://raw.githubusercontent.com/hjbaard/SolidWorks-MCP/main/Docs/images/loft.png" width="32%" alt="Lofted and shelled horn">
</p>
<p align="center"><sub>Built by the tools themselves: a mounting bracket (every step checked against a hand calculation), an M10 bolt with a real thread, a revolved + shelled vase, a swept pipe, a lofted + shelled horn.</sub></p>

## Why this server

- **It verifies, not just generates.** Features report measured geometry;
  dimensions and mates are measured back after the rebuild.
- **Fully defined sketches.** Every sketch is constrained the way a designer
  would: dimensions from the origin, relations only where the geometry is
  exactly horizontal, vertical or on the origin. Tools return their dimensions
  by role (`width@Sketch1`), so the part stays editable, in SolidWorks or
  through the agent.
- **Real CAD, not just primitives.** Extrude, revolve, sweep, loft and splines;
  holes, counterbores, slots and pockets on any face; rounded polygon corners; ISO holes from the Hole Wizard; real ISO metric threads;
  fillets, chamfers, shells, patterns, mirrors, ribs, equations and materials. Assemblies
  with mates and interference checks. STEP/STL/3MF export and screenshots.
  Engraved and embossed text. Work on existing parts: list, delete, suppress and reorder features; read, extrude and cut a person's sketches; import STEP. Colours per part or component. Joints with concentric and angle mates, checked over their range. Printability checks and dimensioned drawings. Planes at any offset or angle. Several bodies per part, combined and split. Variable fillets and full rounds. Free sketches with tangent arcs and splines. 86 tools in total.
- **It fails loud.** A call that cannot do what was asked returns
  `{ok: false, error}` with the cause, never silently wrong geometry.
- **A fixed, typed tool surface.** There is no "run arbitrary code" tool; the
  agent can only do what the tools allow.
- **Tested against real SolidWorks.** 681 tests; each feature's integration test
  compares the result with a hand calculation.
- **Local.** It talks to your running SolidWorks over COM; the server itself
  makes no network calls.

## Quickstart

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. **Start SolidWorks** and leave it open (the server attaches to the running
   instance — it does not launch one).
3. Register the server with your MCP client.

   **Claude Code:**

   ```bash
   claude mcp add solidworks -- uvx solidworks-mcp
   ```

   **Claude Desktop** (`claude_desktop_config.json`) or any other client:

   ```json
   {
     "mcpServers": {
       "solidworks": {
         "command": "uvx",
         "args": ["solidworks-mcp"]
       }
     }
   }
   ```

   It is also listed in the official
   [MCP Registry](https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.hjbaard/solidworks-mcp)
   as `io.github.hjbaard/solidworks-mcp`.

4. Ask for a part, for example:

   > Design a 100 × 80 × 8 mm mounting plate with a Ø16 mm centre bore, four
   > counterbored M5 holes 12 mm from the corners and R5 corners. Check the
   > volume against your own calculation, then export a fine STL.

## Guidelines for agents

The server hands every MCP client short modelling guidelines when it connects
(conventions, verify each step, known pitfalls). The full guide is the resource
`solidworks://guide`: recipes for holes, ribs, threads and assemblies, 3D-print
advice, and how to reverse-engineer a part from a mesh (STL/3MF). The same text
is in [`src/solidworks_mcp/guide.md`](https://github.com/hjbaard/SolidWorks-MCP/blob/main/src/solidworks_mcp/guide.md).

## Requirements and compatibility

- Windows, with SolidWorks installed, licensed and **running**.
- Python 3.11+ (uv fetches one if needed).
- **Tested on SOLIDWORKS 2026** (3DEXPERIENCE R2026x). The API calls it uses
  exist since SOLIDWORKS 2020 SP2, so 2020–2025 should work, but that is
  **untested**. To check your installation, run this with SolidWorks open:

  ```bash
  uvx solidworks-mcp --selftest
  ```

  It builds a few small parts, compares each with a hand calculation and
  closes them unsaved. Please paste its output in a
  [compatibility report](https://github.com/hjbaard/SolidWorks-MCP/issues/new?template=compatibility.yml),
  whether it worked or not.

**Status: early (0.x).** It works end-to-end, but tool names and conventions
may still change. See [CHANGELOG.md](https://github.com/hjbaard/SolidWorks-MCP/blob/main/CHANGELOG.md).

## Troubleshooting

- **Start with the selftest**: `uvx solidworks-mcp --selftest` shows your
  SolidWorks release, language and templates, and which tool areas work. Add
  its output to a [bug report](https://github.com/hjbaard/SolidWorks-MCP/issues/new?template=bug_report.yml).
- **"No running SolidWorks found" / connection fails** — SolidWorks must be
  *running* before you start the server or run a script; it attaches to the active
  instance via `GetActiveObject` and does not launch one.
- **First call is slow or `EnsureModule` errors** — the first COM call generates the
  makepy typelib wrappers under your temp `gen_py` folder. Let it finish; if it gets
  into a bad state, delete the `gen_py` cache and retry. Early binding is mandatory on
  this build (see [Architecture](#architecture)).
- **A feature returns `{ok: false, error: ...}`** — that is by design: every tool
  fails loud with a readable message rather than silently producing wrong
  geometry. Read the message; it names the likely cause.
- **Only tested against SOLIDWORKS 2026 (3DEXPERIENCE R2026x).** On other builds the
  verified enum values or method signatures may differ — re-run
  `scripts/introspect_api.py` to inspect your installed typelib.

## Development

Clone the repository, then install it editable into a venv:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .[dev]
```

To run the MCP server from this checkout instead of via uvx, point your client
at the venv's Python:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "C:\\path\\to\\SolidWorks-MCP\\.venv\\Scripts\\python.exe",
      "args": ["-m", "solidworks_mcp.server"]
    }
  }
}
```

### Run the verification scripts

With SolidWorks open:

```powershell
.\.venv\Scripts\python.exe scripts\probe_connection.py     # M0
.\.venv\Scripts\python.exe scripts\m1_block.py             # M1
.\.venv\Scripts\python.exe scripts\m2_parametric.py        # M2
.\.venv\Scripts\python.exe scripts\test_mcp_server.py      # M3 (full MCP loop over stdio)
.\.venv\Scripts\python.exe scripts\m5_demo_bracket.py      # M5 (3D-print bracket, every step verified)
```

`scripts/introspect_api.py` regenerates/inspects the installed typelib and prints
verified enum values — run it if SolidWorks is upgraded and signatures change.

### Tests

```powershell
.\.venv\Scripts\python.exe -m pytest                 # all tests
.\.venv\Scripts\python.exe -m pytest -m "not solidworks"   # fast unit layer, no SolidWorks
```

Two layers: **pure unit tests** (units, selector/direction parsing, polygon
cleaning, the component-placement maths, and that every MCP tool forwards its
arguments to the right session method) run anywhere; **integration tests**
(`solidworks` marker) drive a running SolidWorks and verify each feature's
volume — or each component's placement — against a hand calc. They auto-skip if
SolidWorks isn't reachable.

## Tools

The server speaks MCP over **stdio**.

### Part tools

| Tool | Purpose |
|---|---|
| `get_status` | Is SolidWorks reachable? revision, server version, active/current part |
| `new_part` | Create a new empty part (becomes current) |
| `add_box(width_mm, height_mm, depth_mm, name)` | Sketch rectangle + extrude; returns mass properties |
| `add_cylinder(diameter_mm, height_mm, name)` | Cylinder by revolving a profile 360° about an axis (Y axis) |
| `add_disc(diameter_mm, thickness_mm, name, x_mm, y_mm)` | Disc/puck/flange: circle extruded along +Z, centred at (x, y) (holes/patterns compose) |
| `add_cone(bottom_diameter_mm, top_diameter_mm, height_mm, name)` | Cone/frustum by revolve (top Ø = 0 → full cone) |
| `add_revolved_profile(profile_mm, angle_deg, name, corner_radii_mm, axis_mm)` | Revolve any closed `(radius, height)` profile about the Y axis (shafts, vases, rings), or `[x, y]` points about any line `axis_mm` on the Front plane; `corner_radii_mm` rounds its edges |
| `add_swept_pipe(path_mm, diameter_mm, bend_radius_mm, name, smooth)` | Sweep a round profile along a 2D path with rounded bends (pipes, tubes, rods), or along a spline through the points with `smooth=True` |
| `add_swept_profile(profile_mm, path_mm, bend_radius_mm, name, corner_radii_mm)` | Sweep any closed cross-section along a 2D path (rails, gaskets, trim, channels); `corner_radii_mm` rounds the cross-section |
| `add_lofted_solid(profiles_mm, heights_mm, name)` | Loft/blend 2+ polygon or round profiles on stacked parallel planes (transitions, adapters, a leg thick at the knee) |
| `add_rib(start_mm, end_mm, toward_mm, thickness_mm, z_mm, name)` | Straight rib / gusset in a plane parallel to Front at `z_mm`, grown toward `toward_mm` until it meets the part (L-bracket gussets) |
| `add_extruded_profile(points_mm, depth_mm, name, corner_radii_mm, rotate_deg, about_mm, draft_deg, merge)` | Extrude any closed polygon `[[x,y],…]` (brackets, sections); `corner_radii_mm` rounds its corners with real sketch fillets, each radius a dimension; `rotate_deg` turns it about a pivot; `draft_deg` tapers its walls; `merge=False` keeps a separate body |
| `add_plane(base, offset_mm, angle_deg, about, name)` | A reference plane offset from a plane, or turned about a model axis; its offset or angle a dimension; gives its origin and axes |
| `add_extruded_profile_on_plane(points_mm, plane, depth_mm, reverse, name, corner_radii_mm, draft_deg, merge)` | Extrude a polygon drawn on any plane (3D points on it), merged with the part |
| `list_bodies()` | The part's solid bodies: name, volume, bounding box |
| `combine_bodies(operation, main, tools, name)` | Add, subtract or intersect bodies of the part (`add` / `subtract` / `common`) |
| `split_body(plane, name)` | Split the part into two bodies along a plane |
| `add_extruded_spline(points_mm, depth_mm, name)` | Extrude a smooth closed spline through points (free-form/organic outlines) |
| `add_hole(diameter_mm, x_mm, y_mm, name)` | Cut a circular through-hole at (x, y) through the depth axis |
| `add_counterbore_hole(clearance_diameter_mm, cbore_diameter_mm, cbore_depth_mm, x_mm, y_mm, name)` | Counterbored screw hole (flush cap-head / heat-set insert) on +Z |
| `add_thread(size, x_mm, y_mm, z_mm, length_mm, internal, name)` | Real, printable ISO metric thread (e.g. `M10x1.5`) from a rod's end edge or a hole's mouth, via SolidWorks' Thread feature; the size is checked against the thread profiles. Internal: drill the basic minor diameter first (M10x1.5 → Ø8.376) |
| `add_hole_on_face(diameter_mm, face, x_mm, y_mm, z_mm, depth_mm, name)` | Round hole on ANY planar face at a 3D point, through or blind (side holes, heat-set insert holes); the face through the point is used |
| `add_hole_wizard(kind, size, face, x_mm, y_mm, z_mm, depth_mm, fit, thread, name)` | ISO hole from SolidWorks' Hole Wizard tables: clearance (ISO 273 fits), counterbore, countersink or tapped; `thread="modeled"` cuts a real, printable thread |
| `add_boss_on_face(diameter_mm, face, x_mm, y_mm, z_mm, height_mm, name)` | Round boss (standoff, peg) grown out of ANY planar face |
| `add_extruded_profile_on_face(points_mm, face, depth_mm, name, corner_radii_mm)` | Polygon pad/ledge grown out of ANY planar face (3D points on the face) |
| `add_text_on_face(text, face, x_mm, y_mm, z_mm, height_mm, depth_mm, emboss, font, name)` | Engrave text into ANY planar face, or emboss it: labels, version numbers; the position is two dimensions |
| `cut_profile(points_mm, depth_mm, name, corner_radii_mm, rotate_deg, about_mm)` | Cut a polygon pocket/slot from the +Z face (blind or through), optionally turned about a pivot |
| `cut_profile_on_face(points_mm, face, depth_mm, name, corner_radii_mm)` | Cut a polygon pocket on ANY face (3D points on the face) |
| `cut_profile_through_plane(points_mm, plane, depth_mm, name, corner_radii_mm)` | Cut a polygon drawn on the Front/Top/Right plane or a plane by name, through all both ways or `depth_mm` centred on the plane (wedges, side windows, symmetric recesses); `keep_inside` keeps the profile instead, so two views make a 3D shape |
| `cut_offset_pocket(face, x_mm, y_mm, z_mm, rim_mm, depth_mm, name)` | Pocket a face leaving a rim along its outline (I-beam web, tray, frame); rim and depth are dimensions |
| `cut_slot(length_mm, width_mm, x_mm, y_mm, angle_deg, depth_mm, name)` | Cut a straight slotted hole (obround) on the +Z face at any angle |
| `add_extruded_slot(start_mm, end_mm, width_mm, depth_mm, name)` | Extrude a stadium (rounded tab, lug, link) between two points, the round ends centred on them |
| `add_fillet(radius_mm, edges, name, radii_at_mm, skip_shorter_mm)` | Round edges (`edges`: `all`, axis `x`/`y`/`z`, a face outline `"+z:outline"`, one feature's edges `"feature:Boss"`, or indices `"2,5"`); `radii_at_mm` varies the radius from end to end; a refusal names the edges that do not fit |
| `add_full_round(face, x_mm, y_mm, z_mm, name)` | Round a rib's top off completely between its two closest opposite sides |
| `add_chamfer(distance_mm, edges, name)` | Chamfer edges at 45° (`edges`: `all`, axis, a face outline, a feature's edges, or indices) |
| `add_shell(thickness_mm, open_face)` | Hollow to a wall thickness; open a face (`+z`/…) or `none` |
| `add_linear_pattern(count, spacing_mm, direction, feature_name)` | Repeat a feature N times along `+x`/`-x`/… |
| `add_circular_pattern(count, center_x_mm, center_y_mm, feature_name)` | Repeat a feature N times around an axis (bolt circle) |
| `add_mirror(plane, offset_mm, features, name)` | Mirror features (copies follow their seeds) or the whole body about the Front/Top/Right plane, or a plane by name, moved `offset_mm`; fails when a copy would land outside the part |
| `set_dimension(dimension_name, value_mm)` | Change a named driving dim (e.g. `D1@BlockExtrude`, or any name a tool returned in `dimensions`), rebuild, remeasure; an angle in degrees |
| `set_equation(equation)` | Add a global equation or variable linking dims (e.g. `"W" = 40`, then `"width@Sketch1" = "W"`), or replace the one that sets the same name; names the features that fail to rebuild |
| `set_equations(equations)` | Add or replace a list of equations with one rebuild |
| `list_equations` / `delete_equation(equation)` | List the equations (value, global, broken) / delete one by name or index |
| `slice_mesh(path, axis, heights_mm, frame)` | Cross-sections of an STL/3MF mesh as polygon loops, ready to use as profiles |
| `compare_with_mesh(path, axis, heights_mm, frame, offset_mm)` | Compare the part's cross-sections with a reference mesh (area and extent differences) |
| `read_sketch(name)` | A sketch read back in model coordinates: lines, arcs, circles, splines, its dimensions, fully defined or not |
| `add_sketch(plane, start_mm, segments, name)` | Sketch an outline of lines, arcs and splines on any plane, fully defined: tangent where it flows, dimensioned from the origin |
| `extrude_sketch(sketch, depth_mm, reverse, name, up_to)` / `cut_sketch(sketch, depth_mm, reverse, name)` | Build on an existing sketch by name (one a person drew): extrude it blind or up to the next face, or cut it blind or through all |
| `list_dimensions()` | Every dimension in the part: name (for `set_dimension`), feature, value, unit |
| `list_features()` | The part's history in tree order (name, type, suppressed); flags features that fail to rebuild and sketches that are not fully defined |
| `delete_feature(name, with_children, with_equations)` | Undo a step: delete a feature with its sketch; refuses (and names them) while other features depend on it, unless `with_children`; names the equations it breaks, or deletes them with `with_equations` |
| `suppress_feature(name, suppress)` | Take a feature out but keep it and its dimensions (try a variant); `suppress=False` brings it back with its dependents |
| `set_material(name, database, density_kg_m3)` | Assign a material (e.g. `6061 Alloy`) so mass/density are real; one SolidWorks lacks (e.g. `TPU`) comes with its density |
| `rebuild(top_only)` | Force rebuild, report errors |
| `get_mass_properties` | Volume, mass, density, surface area, centre of mass, bounding box |
| `get_bounding_box` | Tight part bounding box (min/max/size, mm) |
| `list_faces(component)` / `list_edges(face, feature, within_mm, min_length_mm)` | Inspect faces (normal/area/centre; a cylinder's axis, radius and centre, also of a component in an assembly) and edges (type/length/ends/axis) by index, narrowed to a face, a feature, a box or a length |
| `check_printability(up, overhang_deg, min_wall_mm)` | For a print direction: overhanging faces (area, worst lean, centre), bed contact, height; with `min_wall_mm` the walls thinner than that |
| `export(path, file_format, quality, deviation_mm, angle_deg, per_component)` | STEP/STL/IGES/Parasolid/3MF (silent; verifies file). STL/3MF tessellation: `quality` `coarse`/`fine`, or explicit `deviation_mm`+`angle_deg`; an assembly's STL as one file or one per component |
| `screenshot(path, view, zoom_mm, show_planes, from_dir)` | PNG/JPG/TIF from a standard view (`iso`, `front`, `top`, …) or any direction, zoomed to fit or onto a region; planes and axes left out unless asked for |
| `make_drawing(path)` | 2D drawing as PDF or editable `.slddrw`: front, top, right (first angle) and isometric views on A4 with the model's own dimensions, each once |
| `save_part(path)` / `open_part(path)` | Save to / open a native `.sldprt`; `open_part` also imports STEP, IGES and Parasolid files as a part to build on |
| `close_part(save)` | Close the current part or assembly; without one, SolidWorks' active document if it is saved |
| `list_documents` / `activate_document(title)` | The open documents (path, type, unsaved changes) / make one current again, also one never saved |

### Assembly tools

| Tool | Purpose |
|---|---|
| `new_assembly` | Create a new empty assembly (becomes the current document) |
| `open_assembly(path)` / `save_assembly(path)` | Open / save a native `.sldasm`; `open_assembly` also imports STEP, IGES and Parasolid assemblies, parts as components in place |
| `insert_component(path, x_mm, y_mm, z_mm, fixed)` | Insert a part, or a `.sldasm` as a sub-assembly, with its **origin** at (x, y, z); the first component is fixed by default |
| `list_components` | Name, path, fixed, position, rotation and bounding box of every component (its own extent, also when turned), and the mates with their dimensions and errors |
| `delete_component(component)` | Remove a component from the assembly, with its mates |
| `set_component_transform(name, x_mm, y_mm, z_mm, rx_deg, ry_deg, rz_deg)` | Move/rotate a component; the transform is read back and verified |
| `add_mate(comp_a, face_a, comp_b, face_b, mate_type, distance_mm, angle_deg, flip)` | Mate two faces (by direction, or `#index` from `list_faces`): `coincident`, `distance`, `parallel`, `perpendicular`, `angle`, or `concentric` between cylinders — measured back afterwards; a refused mate is removed. A distance or angle mate returns its dimension: a joint angle as one number |
| `check_motion(dimension_name, values, distances)` | Step a joint through its range: overlapping pairs and chosen distances per step, the smallest distance and where it occurs, the components that moved |
| `swept_region(component, dimension, values, heights_mm, axis, margin_mm, tolerance_mm, frame)` | What a component covers in a plane over a joint's range: its outline, with a margin, in the coordinates of the part to cut from |
| `delete_mate(name)` / `suppress_mate(name, suppress)` | Delete a mate, or suppress it (and bring it back) |
| `check_interference` | Component pairs whose solids overlap, with the volume in mm³ (touching faces don't count) |
| `measure_distance(component_a, component_b, point_mm, axis_mm)` | Smallest distance between two components, to a point (with the nearest point, and whether the point is in the material), or to an axis |
| `get_assembly_bounding_box` | Tight bounding box of the whole assembly (min/max/size, mm) |

`export` and `screenshot` work on assemblies too.

Faces are selected by direction in the component's **own** frame (`+x`, `-z`, …),
so a selector keeps meaning the same face however the component is turned. Add
`:inner` (e.g. `+y:inner`) for the cavity side of a hollow part — the inside of a
room wall instead of its outer skin.

All linear dimensions are **millimetres**; the server converts to/from the
SolidWorks-internal metre/radian units at the boundary.

## Architecture

```
src/solidworks_mcp/
  binding.py     early-binding plumbing (wrap raw dispatches in generated classes)
  com_worker.py  one dedicated STA thread; all COM calls serialised through it
  session.py     SolidWorks operations (must run on the COM thread)
  server.py      FastMCP tools that delegate to session via the worker
  constants.py   enum values read from the installed typelib (verified)
  units.py       mm<->m, deg<->rad
  errors.py      SolidWorksError -> agent-facing {ok:false,error}
```

Two non-obvious design decisions, both load-bearing:

1. **Early binding is mandatory.** On this build `GetActiveObject` returns a
   dispatch whose `GetTypeInfo()` fails, so `EnsureDispatch`/`CastTo` cannot infer
   types and pure late binding breaks (`IModelDoc2.FirstFeature` →
   `DISP_E_MEMBERNOTFOUND`). We generate makepy wrappers from the installed
   typelib and wrap each raw dispatch in the right interface class; calls then go
   by dispid via `InvokeTypes`, bypassing name resolution. See `binding.py`.

2. **A dedicated COM thread.** COM is STA and thread-affine. The MCP server runs
   on asyncio, so all COM work is pinned to one worker thread (`com_worker.py`)
   that handlers post to and await — actively enforcing the "one COM session,
   single-threaded" rule that does not hold automatically in an async server.

## Status and roadmap

Proven end-to-end against **SOLIDWORKS 2026 (3DEXPERIENCE R2026x)**:

| Milestone | What it proves | State |
|---|---|---|
| M0 | COM connection to a running SolidWorks | ✅ |
| M1 | new part → sketch rectangle → extrude → mass properties (volume matches hand calc) | ✅ |
| M2 | change a named dimension → rebuild → volume changes predictably | ✅ |
| M3 | full agent loop via the MCP server: build → measure → correct → export STEP/STL + screenshot | ✅ |
| M4 | revolve, sweep, loft, profiles, holes/pockets/counterbores, slots, fillet/chamfer, shell, patterns, equations, materials, save/open | 🚧 ongoing |
| M5 | end-to-end 3D-print part: build a functional mounting bracket through the full loop → verify every dimension → export a fine STL ([scripts/m5_demo_bracket.py](https://github.com/hjbaard/SolidWorks-MCP/blob/main/scripts/m5_demo_bracket.py)) | ✅ |
| M6 | assemblies: insert and position components, mate them, check interference — every placement and mate measured back ([tests/test_assembly.py](https://github.com/hjbaard/SolidWorks-MCP/blob/main/tests/test_assembly.py)) | ✅ |

See [Docs/PROGRESS.md](https://github.com/hjbaard/SolidWorks-MCP/blob/main/Docs/PROGRESS.md) for the detailed log and roadmap.
Feedback and contributions are welcome.

## Known limitations

- Geometry so far: **boxes**, **cylinders/cones** (revolve), **arbitrary
  extruded profiles**, **holes**, **polygon pockets/slots** (`cut_profile`),
  **fillets**, **chamfers**, **shells**, **linear + circular patterns** (bolt
  circles); plus **equations**, **materials**, geometry **inspection**, and
  **save/open** of `.sldprt`, **holes + pockets on any planar face**
  (model→sketch transform), **round flanges** (disc + bore + bolt circle), and
  **slotted holes** (`cut_slot`, obround at any angle — the first arc-based sketch),
  **general revolves** (`add_revolved_profile`: any `(r,z)` profile → shafts,
  vases, rings), **swept pipes/tubes** (`add_swept_pipe`: a round profile along
  a rounded 2D path), and **lofts** (`add_lofted_solid`: blend stacked polygon
  profiles → transitions/adapters), **free-form extrusions**
  (`add_extruded_spline`: a smooth closed spline → organic/aesthetic outlines), and
  **non-circular sweeps** (`add_swept_profile`: any cross-section along a path →
  rails, gaskets, trim), and **mirrors** (`add_mirror`: features or the whole
  body about a plane through the part).
- Selection: plane walk, face-by-normal/direction (`_planar_face_by_normal`,
  `+z`/…, with `:inner` for the cavity side of a hollow part), and edge selection
  by axis **or explicit index** (`_select_edges`). `list_faces`/`list_edges` let
  an agent inspect geometry before selecting.
- Assemblies (M6): components, transforms, mates and interference detection.
  Component patterns, in-context features, configurations, drawings and
  Simulation (FEA) are out of scope.
