"""MCP server exposing SolidWorks part-modelling tools over stdio.

All tools delegate to a single SolidWorksSession that runs on one dedicated COM
thread (ComWorker). Failures are returned as {"ok": false, "error": "..."} so
the agent can read and react to them in the build -> measure -> correct loop,
rather than getting an opaque stack trace.
"""

import pythoncom
from mcp.server.fastmcp import FastMCP

from .com_worker import ComWorker
from .errors import SolidWorksError
from .session import SolidWorksSession

mcp = FastMCP("solidworks-mcp")
_worker = ComWorker()
_session = SolidWorksSession()


async def _call(fn, *args, **kwargs) -> dict:
    """Run a session method on the COM thread and normalise errors to a result dict."""
    try:
        return await _worker.call(lambda: fn(*args, **kwargs))
    except SolidWorksError as exc:
        return {"ok": False, "error": str(exc)}
    except pythoncom.com_error as exc:
        # Extract the human-readable description if SolidWorks supplied one;
        # raw HRESULT tuples are useless as a correction-loop signal.
        info = getattr(exc, "excepinfo", None)
        desc = info[2] if info and len(info) > 2 and info[2] else getattr(exc, "strerror", None)
        return {"ok": False, "error": f"SolidWorks COM-fout: {desc or exc}"}
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the agent
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def get_status() -> dict:
    """Report whether SolidWorks is reachable, its revision, and the active/current part."""
    return await _call(_session.get_status)


@mcp.tool()
async def new_part() -> dict:
    """Create a new empty part document; it becomes the current part."""
    return await _call(_session.new_part)


@mcp.tool()
async def add_box(width_mm: float, height_mm: float, depth_mm: float,
                  name: str = "BlockExtrude") -> dict:
    """Add a rectangular block: sketch width x height on the first plane, extrude by depth.

    Dimensions are in millimetres. Returns the created feature name, the
    addressable depth dimension ('D1@<name>'), and the resulting mass properties.
    """
    return await _call(_session.add_box, width_mm, height_mm, depth_mm, name)


@mcp.tool()
async def add_extruded_profile(points_mm: list, depth_mm: float, name: str = "Extrude") -> dict:
    """Extrude a closed polygon into a solid: points_mm = [[x,y], ...] in mm.

    The polygon (first-plane coordinates, same as add_box) is auto-closed and
    extruded by depth_mm. Unlocks arbitrary prismatic shapes (brackets, profiles,
    polygons). Returns mass properties (volume = polygon area * depth).
    """
    return await _call(_session.add_extruded_profile, points_mm, depth_mm, name)


@mcp.tool()
async def add_disc(diameter_mm: float, thickness_mm: float, name: str = "Disc") -> dict:
    """Create a disc/puck/flange: a circle extruded along +Z, centred at the origin.

    Flat faces are +Z/-Z, so add_hole and add_circular_pattern compose with it
    (round-flange bolt circles). Returns mass properties. Use new_part first.
    """
    return await _call(_session.add_disc, diameter_mm, thickness_mm, name)


@mcp.tool()
async def add_cylinder(diameter_mm: float, height_mm: float, name: str = "Revolve") -> dict:
    """Create a cylinder by revolving a profile 360° about an axis.

    The first revolve-based primitive. Returns the resulting mass properties
    (volume = π · r² · h). Use new_part first.
    """
    return await _call(_session.add_cylinder, diameter_mm, height_mm, name)


@mcp.tool()
async def add_cone(bottom_diameter_mm: float, top_diameter_mm: float,
                   height_mm: float, name: str = "Revolve") -> dict:
    """Create a cone/frustum by revolving a trapezoidal profile 360°.

    top_diameter_mm = 0 gives a full cone. Returns mass properties
    (volume = π·h/3 · (rb² + rb·rt + rt²)). Use new_part first.
    """
    return await _call(_session.add_cone, bottom_diameter_mm, top_diameter_mm, height_mm, name)


@mcp.tool()
async def add_hole(diameter_mm: float, x_mm: float, y_mm: float, name: str = "Hole") -> dict:
    """Cut a circular through-hole at (x_mm, y_mm), through the part's depth axis.

    The hole runs straight through the thickness (the add_box extrude direction),
    perpendicular to the width x height profile face. Coordinates share add_box's
    system (the centre of a 40x20 profile is x=20, y=10). Returns mass properties.
    """
    return await _call(_session.add_hole, diameter_mm, x_mm, y_mm, name)


@mcp.tool()
async def add_hole_on_face(diameter_mm: float, face: str,
                           x_mm: float, y_mm: float, z_mm: float, name: str = "Hole") -> dict:
    """Drill a through-hole on ANY planar face, centred at 3D point (x, y, z) mm.

    face is "+x"/"-x"/"+y"/"-y"/"+z"/"-z" (the face to drill); (x,y,z) is the
    centre in global coordinates and must lie on that face. Enables side holes and
    bolt circles on cylinder end-faces. Returns mass properties.
    """
    return await _call(_session.add_hole_on_face, diameter_mm, face, x_mm, y_mm, z_mm, name)


@mcp.tool()
async def cut_profile(points_mm: list, depth_mm: float | None = None, name: str = "Cut") -> dict:
    """Cut a polygonal pocket/slot from the +Z face: points_mm = [[x,y], ...] in mm.

    Auto-closed polygon, cut blind by depth_mm or all the way through when depth_mm
    is omitted. For pockets, slots, cutouts. Returns mass properties.
    """
    return await _call(_session.cut_profile, points_mm, depth_mm, name)


@mcp.tool()
async def cut_profile_on_face(points_mm: list, face: str,
                             depth_mm: float | None = None, name: str = "Cut") -> dict:
    """Cut a polygon pocket/slot on ANY planar face: points_mm = [[x,y,z], ...] in mm.

    The 3D points must lie on `face` ("+x"/"-x"/...); cut blind by depth_mm or
    through when omitted. For side pockets/cutouts. Returns mass properties.
    """
    return await _call(_session.cut_profile_on_face, points_mm, face, depth_mm, name)


@mcp.tool()
async def cut_slot(length_mm: float, width_mm: float, x_mm: float, y_mm: float,
                   angle_deg: float = 0.0, depth_mm: float | None = None,
                   name: str = "Slot") -> dict:
    """Cut a straight slotted hole (obround) on the +Z face.

    Centred at (x_mm, y_mm); length_mm is centre-to-centre of the rounded ends,
    width_mm the slot width, angle_deg its orientation in the +Z plane (0 = +X).
    Cut blind by depth_mm or through when omitted. Returns mass properties.
    """
    return await _call(_session.cut_slot, length_mm, width_mm, x_mm, y_mm,
                       angle_deg, depth_mm, name)


@mcp.tool()
async def add_fillet(radius_mm: float, edges: str = "all", name: str = "Fillet") -> dict:
    """Round edges of the current part with one constant radius (mm).

    edges: "all" (default), "x"/"y"/"z" for edges parallel to that world axis, or
    explicit indices like "2,5" from list_edges. Returns the number of edges
    filleted and the resulting mass properties.
    """
    return await _call(_session.add_fillet, radius_mm, edges, name)


@mcp.tool()
async def add_chamfer(distance_mm: float, edges: str = "all", name: str = "Chamfer") -> dict:
    """Chamfer edges of the current part at 45° with the given distance (mm).

    edges: "all" (default), "x"/"y"/"z", or explicit indices like "2,5" from
    list_edges. Returns the number of edges chamfered and the resulting mass
    properties.
    """
    return await _call(_session.add_chamfer, distance_mm, edges, name)


@mcp.tool()
async def add_linear_pattern(count: int, spacing_mm: float, direction: str = "+x",
                             feature_name: str | None = None) -> dict:
    """Repeat a feature `count` times, `spacing_mm` apart, along a direction.

    direction: "+x"/"-x"/"+y"/... feature_name: the feature to repeat (e.g.
    "Hole"); defaults to the most recently added feature. Returns mass properties.
    """
    return await _call(_session.add_linear_pattern, count, spacing_mm, direction, feature_name)


@mcp.tool()
async def add_circular_pattern(count: int, center_x_mm: float, center_y_mm: float,
                               feature_name: str | None = None) -> dict:
    """Repeat a feature `count` times evenly around 360° about an axis.

    The axis is the cylindrical face nearest (center_x_mm, center_y_mm) — e.g. a
    centre hole drilled there. feature_name defaults to the last feature. Bolt
    circle: drill a centre hole + one bolt hole, then pattern the bolt hole.
    """
    return await _call(_session.add_circular_pattern, count, center_x_mm, center_y_mm, feature_name)


@mcp.tool()
async def add_shell(thickness_mm: float, open_face: str = "+z") -> dict:
    """Hollow the current part to a wall of thickness_mm, opening one face.

    open_face: a direction "+z"/"-z"/"+x"/... removes that planar face (open
    shell); "none" makes a closed hollow. Returns the resulting mass properties.
    """
    return await _call(_session.add_shell, thickness_mm, open_face)


@mcp.tool()
async def set_dimension(dimension_name: str, value_mm: float) -> dict:
    """Set a named driving dimension (e.g. 'D1@BlockExtrude') in mm, rebuild, and remeasure.

    This is the parametric edit at the heart of the correction loop.
    """
    return await _call(_session.set_dimension, dimension_name, value_mm)


@mcp.tool()
async def set_material(name: str, database: str = "") -> dict:
    """Assign a material by name (e.g. "6061 Alloy", "AISI 1020", "ABS").

    Makes mass and density reflect a real material instead of the 1000 kg/m³
    default. Returns mass properties including density.
    """
    return await _call(_session.set_material, name, database)


@mcp.tool()
async def set_equation(equation: str) -> dict:
    """Add a global equation linking dimensions, then rebuild and remeasure.

    A SolidWorks equation string, e.g. '"D1@BlockExtrude" = 25' or
    '"D1@BlockExtrude" = 2 * "D1@Sketch1"'. Persists a relation (unlike
    set_dimension). Returns mass properties.
    """
    return await _call(_session.set_equation, equation)


@mcp.tool()
async def rebuild(top_only: bool = False) -> dict:
    """Force a rebuild of the current part and report whether it rebuilt without errors."""
    return await _call(_session.rebuild, top_only)


@mcp.tool()
async def get_mass_properties() -> dict:
    """Get volume (mm^3), mass (kg), surface area (mm^2), centre of mass, and bounding box."""
    return await _call(_session.get_mass_properties)


@mcp.tool()
async def get_bounding_box() -> dict:
    """Get the tight bounding box of the current part (min/max/size in mm)."""
    return await _call(_session.get_bounding_box)


@mcp.tool()
async def list_faces() -> dict:
    """List the part's faces (index, planar?, normal, area, centre) for inspection.

    Indices are positional and shift as features are added; call again after edits.
    """
    return await _call(_session.list_faces)


@mcp.tool()
async def list_edges() -> dict:
    """List the part's edges (index, type; lines give axis/length/midpoint).

    Use the index with add_fillet/add_chamfer edges="2,5" to target specific edges.
    """
    return await _call(_session.list_edges)


@mcp.tool()
async def export(path: str, file_format: str | None = None) -> dict:
    """Export the current part to STEP/STL/IGES/Parasolid/3MF (format inferred from extension).

    Silent (no prompts). Verifies the file appears on disk and reports its size.
    """
    return await _call(_session.export, path, file_format)


@mcp.tool()
async def screenshot(path: str) -> dict:
    """Save an isometric, zoom-to-fit screenshot of the current part (PNG/BMP/JPG)."""
    return await _call(_session.screenshot, path)


@mcp.tool()
async def close_part(save: bool = False) -> dict:
    """Close the current part without saving (export first if you need the geometry)."""
    return await _call(_session.close_part, save)


@mcp.tool()
async def save_part(path: str) -> dict:
    """Save the current part to a native .sldprt file (so it can be reopened/edited)."""
    return await _call(_session.save_part, path)


@mcp.tool()
async def open_part(path: str) -> dict:
    """Open an existing .sldprt file; it becomes the current part."""
    return await _call(_session.open_part, path)


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    try:
        mcp.run()
    finally:
        _worker.shutdown()


if __name__ == "__main__":
    main()
