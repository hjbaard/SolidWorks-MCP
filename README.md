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
| M4 | revolve (cylinder/cone), shell, hole, fillet/chamfer, linear pattern, geometry inspection | 🚧 ongoing |

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
| `add_cylinder(diameter_mm, height_mm, name)` | Cylinder by revolving a profile 360° about an axis |
| `add_cone(bottom_diameter_mm, top_diameter_mm, height_mm, name)` | Cone/frustum by revolve (top Ø = 0 → full cone) |
| `add_hole(diameter_mm, x_mm, y_mm, name)` | Cut a circular through-hole at (x, y) through the depth axis |
| `add_fillet(radius_mm, edges, name)` | Round edges (`edges`: `all`, axis `x`/`y`/`z`, or indices `"2,5"`) |
| `add_chamfer(distance_mm, edges, name)` | Chamfer edges at 45° (`edges`: `all`, axis, or indices) |
| `add_shell(thickness_mm, open_face)` | Hollow to a wall thickness; open a face (`+z`/…) or `none` |
| `add_linear_pattern(count, spacing_mm, direction, feature_name)` | Repeat a feature N times along `+x`/`-x`/… |
| `set_dimension(dimension_name, value_mm)` | Change a named driving dim (e.g. `D1@BlockExtrude`), rebuild, remeasure |
| `rebuild(top_only)` | Force rebuild, report errors |
| `get_mass_properties` | Volume, mass, surface area, centre of mass, bounding box |
| `get_bounding_box` | Tight part bounding box (min/max/size, mm) |
| `list_faces` / `list_edges` | Inspect faces (normal/area/centre) and edges (type/axis/length) by index |
| `export(path, file_format)` | STEP/STL/IGES/Parasolid/3MF (silent; verifies file on disk) |
| `screenshot(path)` | Isometric, zoom-to-fit PNG/BMP/JPG |
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

- Geometry so far: **boxes**, **cylinders/cones** (revolve), **through-holes**,
  **fillets**, **chamfers**, **shells**, **linear patterns**, plus geometry
  **inspection** (`list_faces`/`list_edges`). Next: circular pattern + mirror
  (need a centre axis / plane), holes on any face (sketch-frame transform),
  equations, generic sketch primitives.
- Selection: plane walk, face-by-normal/direction (`_planar_face_by_normal`,
  `+z`/…), and edge selection by axis **or explicit index** (`_select_edges`).
  `list_faces`/`list_edges` let an agent inspect geometry before selecting;
  choosing the hole face (beyond +Z) is still open.
- Assemblies, interference detection, drawings and Simulation (FEA) are out of
  scope for v0 (M5).
