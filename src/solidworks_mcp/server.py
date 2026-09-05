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
async def add_extruded_spline(points_mm: list, depth_mm: float, name: str = "Spline") -> dict:
    """Extrude a smooth CLOSED spline through points: points_mm = [[x,y], ...] in mm.

    Like add_extruded_profile but the outline is a smooth curve through the points
    (free-form/organic shapes: cams, rounded outlines, aesthetic bosses), auto-closed
    and extruded by depth_mm. A spline's area is not analytic, so the returned volume
    is the measured value. Returns mass properties. Use new_part first.
    """
    return await _call(_session.add_extruded_spline, points_mm, depth_mm, name)


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
async def add_revolved_profile(profile_mm: list, angle_deg: float = 360.0,
                               name: str = "Revolve") -> dict:
    """Revolve a closed (radius, height) profile about the axis at radius 0.

    profile_mm = [[r, z], …] in mm: r = distance from the axis, z = position along
    it. Auto-closed and spun angle_deg (default 360°). Points at r=0 give a solid
    (turned shafts, vases); a profile offset from the axis gives a ring/torus. The
    profile may not cross the axis. Returns mass properties. Use new_part first.
    """
    return await _call(_session.add_revolved_profile, profile_mm, angle_deg, name)


@mcp.tool()
async def add_swept_pipe(path_mm: list, diameter_mm: float,
                         bend_radius_mm: float = 0.0, name: str = "Pipe") -> dict:
    """Sweep a circular profile (pipe/tube/rod) along a 2D path on the Front plane.

    path_mm = [[x, y], …] in mm is the centreline. Interior corners are rounded
    with bend_radius_mm (required when the path turns; a 2-point straight path
    needs none). diameter_mm = outer Ø; the round profile is auto-generated
    perpendicular to the path. Returns mass properties. Use new_part first.
    """
    return await _call(_session.add_swept_pipe, path_mm, diameter_mm, bend_radius_mm, name)


@mcp.tool()
async def add_swept_profile(profile_mm: list, path_mm: list,
                            bend_radius_mm: float = 0.0, name: str = "Sweep") -> dict:
    """Sweep an arbitrary closed PROFILE (cross-section) along a 2D PATH.

    profile_mm = [[u,v],…] in mm: the closed cross-section on the Right plane (u →
    world +Y, v → world +Z), centred near the origin. path_mm = [[x,y],…] in mm on
    the Front plane — MUST start at the origin heading +X (the profile is
    perpendicular to the path there). Path corners are rounded with bend_radius_mm.
    Volume = profile_area · path_length. For non-round extrusions along a path
    (rails, gaskets, trim, channels). Returns mass properties. Use new_part first.
    """
    return await _call(_session.add_swept_profile, profile_mm, path_mm, bend_radius_mm, name)


@mcp.tool()
async def add_lofted_solid(profiles_mm: list, heights_mm: list, name: str = "Loft") -> dict:
    """Loft (blend) 2+ closed polygon profiles on parallel planes stacked along +Z.

    profiles_mm = [[[x,y],…], …] (mm), one polygon per profile in Front-plane
    coords. heights_mm = the +Z offset (mm) of each profile, same length, strictly
    increasing, starting at 0. A 2-profile loft is a ruled transition; 3+ blend
    smoothly. Give profiles in a consistent vertex order to avoid twist. For
    non-rotational transitions (round shapes: use add_revolved_profile/add_cone).
    Returns mass properties. Use new_part first.
    """
    return await _call(_session.add_lofted_solid, profiles_mm, heights_mm, name)


@mcp.tool()
async def add_hole(diameter_mm: float, x_mm: float, y_mm: float, name: str = "Hole") -> dict:
    """Cut a circular through-hole at (x_mm, y_mm), through the part's depth axis.

    The hole runs straight through the thickness (the add_box extrude direction),
    perpendicular to the width x height profile face. Coordinates share add_box's
    system (the centre of a 40x20 profile is x=20, y=10). Returns mass properties.
    """
    return await _call(_session.add_hole, diameter_mm, x_mm, y_mm, name)


@mcp.tool()
async def add_counterbore_hole(clearance_diameter_mm: float, cbore_diameter_mm: float,
                               cbore_depth_mm: float, x_mm: float, y_mm: float,
                               name: str = "Counterbore") -> dict:
    """Cut a counterbored screw hole on the +Z face at (x_mm, y_mm).

    A clearance shank through the thickness plus a larger coaxial flat-bottom
    pocket of cbore_depth_mm from the top — so a cap-head screw or heat-set insert
    sits flush/recessed (common for 3D-printed parts). cbore_diameter must exceed
    clearance_diameter. Coordinates share add_box's system. Returns mass properties.
    """
    return await _call(_session.add_counterbore_hole, clearance_diameter_mm,
                       cbore_diameter_mm, cbore_depth_mm, x_mm, y_mm, name)


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
async def export(path: str, file_format: str | None = None, quality: str = "fine",
                 deviation_mm: float | None = None, angle_deg: float | None = None) -> dict:
    """Export the current part or assembly to STEP/STL/IGES/Parasolid/3MF (format from extension).

    Silent (no prompts). Verifies the file appears on disk and reports its size.
    For STL/3MF, tessellation resolution is set first: quality 'coarse'|'fine'
    (default 'fine' for print quality), or pass deviation_mm (+ optional angle_deg)
    for a reproducible custom resolution (overrides quality). Ignored for other formats.
    """
    return await _call(_session.export, path, file_format, quality, deviation_mm, angle_deg)


@mcp.tool()
async def screenshot(path: str) -> dict:
    """Save an isometric, zoom-to-fit screenshot of the current part or assembly (PNG/BMP/JPG)."""
    return await _call(_session.screenshot, path)


@mcp.tool()
async def close_part(save: bool = False) -> dict:
    """Close the current part or assembly without saving (export/save first if needed)."""
    return await _call(_session.close_part, save)


@mcp.tool()
async def save_part(path: str) -> dict:
    """Save the current part to a native .sldprt file (so it can be reopened/edited)."""
    return await _call(_session.save_part, path)


@mcp.tool()
async def open_part(path: str) -> dict:
    """Open an existing .sldprt file; it becomes the current part."""
    return await _call(_session.open_part, path)


# --- assemblies ---------------------------------------------------------------


@mcp.tool()
async def new_assembly() -> dict:
    """Create a new empty assembly document; it becomes the current document.

    Assemblies compose saved parts: insert_component places each part, add_mate
    constrains them, check_interference proves nothing overlaps.
    """
    return await _call(_session.new_assembly)


@mcp.tool()
async def open_assembly(path: str) -> dict:
    """Open an existing .sldasm file; it becomes the current document."""
    return await _call(_session.open_assembly, path)


@mcp.tool()
async def save_assembly(path: str) -> dict:
    """Save the current assembly to a native .sldasm file (so it can be reopened)."""
    return await _call(_session.save_assembly, path)


@mcp.tool()
async def insert_component(path: str, x_mm: float = 0.0, y_mm: float = 0.0,
                           z_mm: float = 0.0, fixed: bool | None = None) -> dict:
    """Insert a .sldprt into the current assembly with its ORIGIN at (x, y, z) mm.

    The part's own origin lands exactly on that point, and the placement is read
    back and verified. fixed=True pins the component; fixed=False leaves it free
    for mates. The default fixes only the FIRST component, giving the assembly a
    ground to build against. Returns the component's name, placement and box.
    """
    return await _call(_session.insert_component, path, x_mm, y_mm, z_mm, fixed)


@mcp.tool()
async def list_components() -> dict:
    """List the assembly's components: name, path, fixed, position, rotation, bounding box.

    Positions are in mm and rotations in degrees, both in assembly coordinates;
    the bounding box of each component is in assembly coordinates too.
    """
    return await _call(_session.list_components)


@mcp.tool()
async def set_component_transform(name: str, x_mm: float, y_mm: float, z_mm: float,
                                  rx_deg: float = 0.0, ry_deg: float = 0.0,
                                  rz_deg: float = 0.0) -> dict:
    """Move/rotate a component: its origin to (x, y, z) mm, rotated rx/ry/rz degrees.

    name is the component name ('Bed' or 'Bed-1'); rotations apply X, then Y,
    then Z about the assembly axes, and work on a fixed component too. The
    transform is read back and compared, so a move SolidWorks ignored (e.g. one
    already pinned by mates) fails loudly instead of silently leaving the part
    where it was.
    """
    return await _call(_session.set_component_transform, name, x_mm, y_mm, z_mm,
                       rx_deg, ry_deg, rz_deg)


@mcp.tool()
async def add_mate(comp_a: str, face_a: str, comp_b: str, face_b: str,
                   mate_type: str = "coincident", distance_mm: float = 0.0,
                   flip: bool = False) -> dict:
    """Mate a planar face of one component to a planar face of another.

    comp_a/comp_b are component names ('Bed' or 'Bed-1'). face_a/face_b select a
    planar face by direction in that component's OWN frame: "+x"/"-x"/"+y"/...,
    optionally "+y:inner" for the cavity side of a hollow part (the inside of a
    room wall instead of its outer skin). mate_type: "coincident", "distance"
    (uses distance_mm), "parallel" or "perpendicular". flip swaps the solution
    if SolidWorks lands on the mirror side. The result is measured back from the
    geometry after the rebuild and rejected if it is not what was asked.
    """
    return await _call(_session.add_mate, comp_a, face_a, comp_b, face_b,
                       mate_type, distance_mm, flip)


@mcp.tool()
async def check_interference() -> dict:
    """Report component pairs whose solids overlap, with the volume in mm^3.

    Touching faces do not count (a bed standing on the floor is fine); only real
    overlapping material does. count == 0 means the assembly is clash-free.
    """
    return await _call(_session.check_interference)


@mcp.tool()
async def get_assembly_bounding_box() -> dict:
    """Get the bounding box of the whole assembly (min/max/size in mm)."""
    return await _call(_session.get_assembly_bounding_box)


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    try:
        mcp.run()
    finally:
        _worker.shutdown()


if __name__ == "__main__":
    main()
