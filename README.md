# SolidWorks MCP

An MCP server that drives a **locally running SolidWorks** instance over the COM
API (pywin32), so an AI agent can build, measure and export parametric parts —
and run a closed **build → measure → verify → correct** loop.

The point isn't just "make geometry". Parametric CAD gives *hard, verifiable
signals* (rebuild status, mass properties, measurements, bounding box), which
makes an agentic correction loop realistic instead of "it looks about right".

## Status (v0)

Proven end-to-end against **SOLIDWORKS 2026 (3DEXPERIENCE R2026x)**:

| Milestone | What it proves | State |
|---|---|---|
| M0 | COM connection to a running SolidWorks | ✅ |
| M1 | new part → sketch rectangle → extrude → mass properties (volume matches hand calc) | ✅ |
| M2 | change a named dimension → rebuild → volume changes predictably | ✅ |
| M3 | full agent loop via the MCP server: build → measure → correct → export STEP/STL + screenshot | ✅ |
| M4 | revolve, profiles, holes/pockets, fillet/chamfer, shell, patterns, equations, materials, save/open | 🚧 ongoing |

See [Docs/PROGRESS.md](Docs/PROGRESS.md) for the detailed log and roadmap.

## Requirements

- Windows, with SolidWorks installed and a valid licence.
- SolidWorks **running** (the server attaches to the active instance; it does not
  launch one).
- Python 3.11+.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

This installs `pywin32` + the `mcp` SDK and the `solidworks-mcp` package
(editable). The first COM call generates the SolidWorks typelib wrappers
automatically.

## Run the verification scripts

With SolidWorks open:

```powershell
.\.venv\Scripts\python.exe scripts\probe_connection.py     # M0
.\.venv\Scripts\python.exe scripts\m1_block.py             # M1
.\.venv\Scripts\python.exe scripts\m2_parametric.py        # M2
.\.venv\Scripts\python.exe scripts\test_mcp_server.py      # M3 (full MCP loop over stdio)
```

`scripts/introspect_api.py` regenerates/inspects the installed typelib and prints
verified enum values — run it if SolidWorks is upgraded and signatures change.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest                 # all tests
.\.venv\Scripts\python.exe -m pytest -m "not solidworks"   # fast unit layer, no SolidWorks
```

Two layers: **pure unit tests** (units, selector/direction parsing, polygon
cleaning) run anywhere; **integration tests** (`solidworks` marker) drive a
running SolidWorks and verify each feature's volume against a hand calc — they
auto-skip if SolidWorks isn't reachable. `pip install -e .[dev]` for pytest.

## Use as an MCP server

The server speaks MCP over **stdio**. Register it with an MCP client (e.g. Claude
Desktop / Claude Code) using the venv's Python:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "D:\\Ontwikkeling\\Solidworks-MCP\\.venv\\Scripts\\python.exe",
      "args": ["-m", "solidworks_mcp.server"]
    }
  }
}
```

### Tools

| Tool | Purpose |
|---|---|
| `get_status` | Is SolidWorks reachable? revision + active/current part |
| `new_part` | Create a new empty part (becomes current) |
| `add_box(width_mm, height_mm, depth_mm, name)` | Sketch rectangle + extrude; returns mass properties |
| `add_cylinder(diameter_mm, height_mm, name)` | Cylinder by revolving a profile 360° about an axis (Y axis) |
| `add_disc(diameter_mm, thickness_mm, name)` | Disc/puck/flange: circle extruded along +Z (holes/patterns compose) |
| `add_cone(bottom_diameter_mm, top_diameter_mm, height_mm, name)` | Cone/frustum by revolve (top Ø = 0 → full cone) |
| `add_revolved_profile(profile_mm, angle_deg, name)` | Revolve any closed `(radius, height)` profile about the axis (shafts, vases, rings) |
| `add_swept_pipe(path_mm, diameter_mm, bend_radius_mm, name)` | Sweep a round profile along a 2D path with rounded bends (pipes, tubes, rods) |
| `add_extruded_profile(points_mm, depth_mm, name)` | Extrude any closed polygon `[[x,y],…]` (brackets, sections) |
| `add_hole(diameter_mm, x_mm, y_mm, name)` | Cut a circular through-hole at (x, y) through the depth axis |
| `add_hole_on_face(diameter_mm, face, x_mm, y_mm, z_mm, name)` | Through-hole on ANY planar face at a 3D point (side holes, etc.) |
| `cut_profile(points_mm, depth_mm, name)` | Cut a polygon pocket/slot from the +Z face (blind or through) |
| `cut_profile_on_face(points_mm, face, depth_mm, name)` | Cut a polygon pocket on ANY face (3D points on the face) |
| `cut_slot(length_mm, width_mm, x_mm, y_mm, angle_deg, depth_mm, name)` | Cut a straight slotted hole (obround) on the +Z face at any angle |
| `add_fillet(radius_mm, edges, name)` | Round edges (`edges`: `all`, axis `x`/`y`/`z`, or indices `"2,5"`) |
| `add_chamfer(distance_mm, edges, name)` | Chamfer edges at 45° (`edges`: `all`, axis, or indices) |
| `add_shell(thickness_mm, open_face)` | Hollow to a wall thickness; open a face (`+z`/…) or `none` |
| `add_linear_pattern(count, spacing_mm, direction, feature_name)` | Repeat a feature N times along `+x`/`-x`/… |
| `add_circular_pattern(count, center_x_mm, center_y_mm, feature_name)` | Repeat a feature N times around an axis (bolt circle) |
| `set_dimension(dimension_name, value_mm)` | Change a named driving dim (e.g. `D1@BlockExtrude`), rebuild, remeasure |
| `set_equation(equation)` | Add a global equation linking dims (e.g. `"D1@BlockExtrude" = 25`) |
| `set_material(name, database)` | Assign a material (e.g. `6061 Alloy`) so mass/density are real |
| `rebuild(top_only)` | Force rebuild, report errors |
| `get_mass_properties` | Volume, mass, density, surface area, centre of mass, bounding box |
| `get_bounding_box` | Tight part bounding box (min/max/size, mm) |
| `list_faces` / `list_edges` | Inspect faces (normal/area/centre) and edges (type/axis/length) by index |
| `export(path, file_format)` | STEP/STL/IGES/Parasolid/3MF (silent; verifies file on disk) |
| `screenshot(path)` | Isometric, zoom-to-fit PNG/BMP/JPG |
| `save_part(path)` / `open_part(path)` | Save to / open a native `.sldprt` |
| `close_part(save)` | Close the current part |

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

## Known limitations / roadmap

- Geometry so far: **boxes**, **cylinders/cones** (revolve), **arbitrary
  extruded profiles**, **holes**, **polygon pockets/slots** (`cut_profile`),
  **fillets**, **chamfers**, **shells**, **linear + circular patterns** (bolt
  circles); plus **equations**, **materials**, geometry **inspection**, and
  **save/open** of `.sldprt`, **holes + pockets on any planar face**
  (model→sketch transform), **round flanges** (disc + bore + bolt circle), and
  **slotted holes** (`cut_slot`, obround at any angle — the first arc-based sketch),
  **general revolves** (`add_revolved_profile`: any `(r,z)` profile → shafts,
  vases, rings), and **swept pipes/tubes** (`add_swept_pipe`: a round profile along
  a rounded 2D path). Next: loft, non-circular sweep profiles, sketch splines.
  Mirror is shelved — both routes fail
  on this build; an AI mirrors by placing features symmetrically.
- Selection: plane walk, face-by-normal/direction (`_planar_face_by_normal`,
  `+z`/…), and edge selection by axis **or explicit index** (`_select_edges`).
  `list_faces`/`list_edges` let an agent inspect geometry before selecting;
  choosing the hole face (beyond +Z) is still open.
- Assemblies, interference detection, drawings and Simulation (FEA) are out of
  scope for v0 (M5).
