"""MCP server exposing SolidWorks part-modelling tools over stdio.

All tools delegate to a single SolidWorksSession that runs on one dedicated COM
thread (ComWorker). Failures are returned as {"ok": false, "error": "..."} so
the agent can read and react to them in the build -> measure -> correct loop,
rather than getting an opaque stack trace.
"""

import argparse
import sys
from importlib import resources

from mcp.server.fastmcp import FastMCP

from .errors import SolidWorksError
from .selftest import main as run_selftest

# Shown to the model by every MCP client at connect time, so keep it short; the
# long version is the solidworks://guide resource.
INSTRUCTIONS = """\
Builds, measures and verifies parametric parts and assemblies in the user's running SolidWorks.

Conventions
- Millimetres and degrees. New geometry starts on the Front plane (XY) and extrudes along +Z from z = 0. add_box spans x 0..w, y 0..h; add_disc is centred on the origin; revolves turn about Y.
- Faces are picked by direction: '+z', '-x', ... *_on_face tools take 3D points on the face and use the face facing that way through them (the top, a pocket floor, a step); '+z:outer' / '+z:inner' force the outermost / innermost one. Other tools use the outermost face.
- Every sketch is fully defined. Tools return `dimensions` ({role: 'width@Sketch1', ...}): change one with set_dimension, or drive several from a global variable via set_equation.
- Other orientations: add_plane (an offset, or turned about x/y/z) and build on it with add_extruded_profile_on_plane or cut_profile_through_plane.
- In a part a person drew, their sketches and planes work by name: read_sketch, extrude_sketch, cut_sketch; plane='Plane1' in add_mirror and cut_profile_through_plane.
- Flowing outlines (an S-bend, a hub running into a beam): add_sketch with lines, tangent arcs and splines, then extrude_sketch / cut_sketch.
- A joint: concentric mate (cylinders by list_faces index, '#5') + coincident + angle mate; the angle mate's dimension turns it (set_dimension, check_motion).

Work in small verified steps
- Every modelling call returns volume, mass and bounding box: check them against your own hand calculation after each step, and fix the first mismatch before adding more.
- Look before selecting by index: list_faces / list_edges; screenshot to see the shape.
- Undo a wrong step with delete_feature; list_features shows the history, also of a part opened from disk.
- {ok: false, error} means the tool refused or SolidWorks failed; the error names the cause, and the part is left as it was.

Pitfalls
- Walls shared by successive polygon cuts must use identical points: sections a few micrometres apart leave sliver faces and the next cut fails.
- Round polygon corners with corner_radii_mm (real sketch fillets; equal radii share one dimension). Other curves: revolves, splines, slots and holes.
- add_mirror needs its plane where the copies land in material: add_box spans x 0..w, so mirror about offset_mm = w / 2, not x = 0.
- SolidWorks' memory grows in long sessions: on a low-memory warning, save and restart SolidWorks (never during a run).

Read the resource solidworks://guide for the full guide: recipes (holes, ribs, threads, patterns, assemblies), 3D printing, and reverse-engineering a part from a mesh.
"""

mcp = FastMCP("solidworks-mcp", instructions=INSTRUCTIONS)


@mcp.resource("solidworks://guide", name="guide", mime_type="text/markdown",
              description="Modelling guide: conventions, verification, recipes, 3D printing, "
                          "reverse-engineering from a mesh, limits.")
def guide() -> str:
    return resources.files("solidworks_mcp").joinpath("guide.md").read_text(encoding="utf-8")


class _NoSolidWorks:
    """Stand-in session off Windows: every method fails with a readable error.

    The server still starts and lists its tools there, because MCP directories
    introspect servers in a Linux container before listing them.
    """

    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise SolidWorksError(
                f"SolidWorks MCP only works on Windows with SolidWorks installed (this is {sys.platform})."
            )
        return unavailable


if sys.platform == "win32":
    import pythoncom

    from .com_worker import ComWorker
    from .session import SolidWorksSession

    _worker = ComWorker()
    _session = SolidWorksSession()
    _COM_ERROR = pythoncom.com_error
else:
    _worker = None
    _session = _NoSolidWorks()
    _COM_ERROR = ()  # an empty tuple matches no exception


async def _call(fn, *args, **kwargs) -> dict:
    """Run a session method on the COM thread and normalise errors to a result dict.

    It runs guarded: a call that fails leaves the current part as it was.
    """
    try:
        if _worker is None:
            return fn(*args, **kwargs)
        return await _worker.call(lambda: _session.run_guarded(fn, *args, **kwargs))
    except SolidWorksError as exc:
        return {"ok": False, "error": str(exc)}
    except _COM_ERROR as exc:
        # Extract the human-readable description if SolidWorks supplied one;
        # raw HRESULT tuples are useless as a correction-loop signal.
        info = getattr(exc, "excepinfo", None)
        desc = info[2] if info and len(info) > 2 and info[2] else getattr(exc, "strerror", None)
        return {"ok": False, "error": f"SolidWorks COM error: {desc or exc}"}
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the agent
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def get_status() -> dict:
    """Report whether SolidWorks is reachable, its revision, this server's version, and the active/current part."""
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
    addressable depth dimension ('D1@<name>'), `dimensions` (width, height,
    depth; for set_dimension) and the resulting mass properties.
    """
    return await _call(_session.add_box, width_mm, height_mm, depth_mm, name)


@mcp.tool()
async def add_extruded_profile(points_mm: list, depth_mm: float, name: str = "Extrude",
                               corner_radii_mm: float | list | None = None, rotate_deg: float = 0.0,
                               about_mm: list | None = None, draft_deg: float = 0.0, merge: bool = True) -> dict:
    """Extrude a closed polygon into a solid: points_mm = [[x,y], ...] in mm.

    The polygon (first-plane coordinates, same as add_box) is auto-closed and
    extruded by depth_mm. Unlocks arbitrary prismatic shapes (brackets, profiles,
    polygons). corner_radii_mm rounds the corners with real sketch fillets: one
    radius for all, or one per vertex (0 = sharp); equal radii share one
    dimension ('radius', else 'r<i>'). rotate_deg turns the profile
    counterclockwise about about_mm ([x, y], the origin by default) first: draw
    a crank or a lug straight, then set it at its angle. draft_deg tapers the
    walls inwards as they rise (negative: outwards), a dimension 'draft' (a
    moulded boss, a printable taper). merge=False keeps the result a separate
    body: list_bodies, combine_bodies. Returns mass properties (volume = polygon
    area * depth; a right-angled corner of radius r loses r^2 (1 - pi/4), a
    concave one gains it).
    """
    return await _call(_session.add_extruded_profile, points_mm, depth_mm, name, corner_radii_mm, rotate_deg,
                       about_mm, draft_deg, merge)


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
async def add_disc(diameter_mm: float, thickness_mm: float, name: str = "Disc",
                   x_mm: float = 0.0, y_mm: float = 0.0) -> dict:
    """Create a disc/puck/flange: a circle extruded along +Z, centred at (x_mm, y_mm).

    Flat faces are +Z/-Z, so add_hole and add_circular_pattern compose with it
    (round-flange bolt circles). The centre defaults to the origin; off it, its
    x and y are dimensions. Returns mass properties. Use new_part first.
    """
    return await _call(_session.add_disc, diameter_mm, thickness_mm, name, x_mm, y_mm)


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
                               name: str = "Revolve", corner_radii_mm: float | list | None = None,
                               axis_mm: list | None = None) -> dict:
    """Revolve a closed profile on the Front plane about an axis in it.

    Without axis_mm: profile_mm = [[r, z], …] in mm, r = distance from the Y axis,
    z = position along it. With axis_mm = [[x1, y1], [x2, y2]]: profile_mm gives
    [x, y] points and the axis is the line through those two points, anywhere and
    at any angle (a hub turned about its own centre). Auto-closed and spun
    angle_deg (default 360°). Points on the axis give a solid (shafts, vases); a
    profile away from it gives a ring. The profile may not cross the axis.
    corner_radii_mm rounds corners as for add_extruded_profile (not those on the
    axis). Returns mass properties. Use new_part first.
    """
    return await _call(_session.add_revolved_profile, profile_mm, angle_deg, name, corner_radii_mm, axis_mm)


@mcp.tool()
async def add_swept_pipe(path_mm: list, diameter_mm: float,
                         bend_radius_mm: float = 0.0, name: str = "Pipe", smooth: bool = False) -> dict:
    """Sweep a circular profile (pipe/tube/rod) along a 2D path on the Front plane.

    path_mm = [[x, y], …] in mm is the centreline. Interior corners are rounded
    with bend_radius_mm (required when the path turns; a 2-point straight path
    needs none); smooth=True runs a spline through the points instead, a
    flowing curve. diameter_mm = outer Ø; the round profile is auto-generated
    perpendicular to the path. Returns mass properties (pi r^2 x
    path_length_mm). Use new_part first.
    """
    return await _call(_session.add_swept_pipe, path_mm, diameter_mm, bend_radius_mm, name, smooth)


@mcp.tool()
async def add_swept_profile(profile_mm: list, path_mm: list,
                            bend_radius_mm: float = 0.0, name: str = "Sweep",
                            corner_radii_mm: float | list | None = None) -> dict:
    """Sweep an arbitrary closed PROFILE (cross-section) along a 2D PATH.

    profile_mm = [[u,v],…] in mm: the closed cross-section on the Right plane (u →
    world +Y, v → world +Z), centred near the origin. path_mm = [[x,y],…] in mm on
    the Front plane — MUST start at the origin heading +X (the profile is
    perpendicular to the path there). Path corners are rounded with bend_radius_mm;
    corner_radii_mm rounds the profile's corners as for add_extruded_profile.
    Volume = profile_area · path_length. For non-round extrusions along a path
    (rails, gaskets, trim, channels). Returns mass properties. Use new_part first.
    """
    return await _call(_session.add_swept_profile, profile_mm, path_mm, bend_radius_mm, name, corner_radii_mm)


@mcp.tool()
async def add_lofted_solid(profiles_mm: list, heights_mm: list, name: str = "Loft") -> dict:
    """Loft (blend) 2+ closed profiles on parallel planes stacked along +Z.

    profiles_mm: per profile a polygon [[x,y],…] (mm) in Front-plane coords, or a
    round section {"center_mm": [x, y], "diameter_mm": d} (thick at the knee,
    thin at the foot, centres free to wander). heights_mm = the +Z offset (mm)
    of each profile, same length, strictly increasing, starting at 0. A
    2-profile loft is a ruled transition; 3+ blend smoothly. Each profile
    starts at its first vertex (a round one on +x): give polygons in a
    consistent vertex order to avoid twist. Returns mass properties. Use
    new_part first.
    """
    return await _call(_session.add_lofted_solid, profiles_mm, heights_mm, name)


@mcp.tool()
async def cut_profile_through_plane(points_mm: list, plane: str, depth_mm: float | None = None,
                                    name: str = "Cut", corner_radii_mm: float | list | None = None,
                                    keep_inside: bool = False) -> dict:
    """Cut a polygon sketched on a reference plane, symmetric about that plane.

    plane: 'front' (z = 0), 'top' (y = 0), 'right' (x = 0) or the name of another
    plane in the part ('Plane1'); points_mm = 3D [x, y, z] points ON that plane
    (e.g. x = 0 for 'right'). Cuts through all in
    both directions (depth_mm omitted) or depth_mm in total, centred on the
    plane. For shapes seen from the side: wedges, windows, symmetric recesses.
    corner_radii_mm rounds the corners as for add_extruded_profile.
    keep_inside=True cuts away everything OUTSIDE the profile (through all):
    extrude the front view, cut the side view with keep_inside, and the part is
    the 3D shape both views agree on. Returns mass properties.
    """
    return await _call(_session.cut_profile_through_plane, points_mm, plane, depth_mm, name, corner_radii_mm,
                       keep_inside)


@mcp.tool()
async def add_plane(base: str = "front", offset_mm: float = 0.0, angle_deg: float = 0.0,
                    about: str | None = None, name: str | None = None) -> dict:
    """Make a reference plane to sketch on: an offset or a turned plane, its position a dimension.

    base moved offset_mm along its normal (base: front/top/right or a plane by
    name), or a default plane turned angle_deg about a model axis it holds,
    about='x'/'y'/'z' (front holds x and y, top x and z, right y and z), by the
    right-hand rule. For both, turn first, then offset that plane. Returns its
    name for plane= arguments, and origin_mm, normal, x_axis and y_axis: a point
    (u, v) on it is origin + u x_axis + v y_axis.
    """
    return await _call(_session.add_plane, base, offset_mm, angle_deg, about, name)


@mcp.tool()
async def add_extruded_profile_on_plane(points_mm: list, plane: str, depth_mm: float, reverse: bool = False,
                                        name: str = "Extrude",
                                        corner_radii_mm: float | list | None = None, draft_deg: float = 0.0,
                                        merge: bool = True) -> dict:
    """Extrude a polygon drawn on a reference plane, such as one add_plane made at an angle.

    points_mm = 3D [x, y, z] points ON the plane (origin + u x_axis + v y_axis
    from add_plane). depth_mm along the plane's normal, or the other way with
    reverse=True; merged with the part. corner_radii_mm, draft_deg and merge
    work as for add_extruded_profile. Returns mass properties.
    """
    return await _call(_session.add_extruded_profile_on_plane, points_mm, plane, depth_mm, reverse, name,
                       corner_radii_mm, draft_deg, merge)


@mcp.tool()
async def list_bodies() -> dict:
    """List the part's solid bodies: name, volume and bounding box.

    A part holds several after merge=False or split_body; combine_bodies joins
    them again.
    """
    return await _call(_session.list_bodies)


@mcp.tool()
async def combine_bodies(operation: str, main: str, tools: list | None = None, name: str = "Combine") -> dict:
    """Combine solid bodies of the part into `main` (a list_bodies name).

    operation 'add' joins the tools to main, 'subtract' cuts them out of it,
    'common' keeps only what main shares with them. tools: body names, every
    other body by default. Returns mass properties and the bodies left.
    """
    return await _call(_session.combine_bodies, operation, main, tools, name)


@mcp.tool()
async def split_body(plane: str, name: str = "Split") -> dict:
    """Split the part's body in two along a plane (front/top/right or a plane by name).

    The two bodies come back by name: combine them differently, or check each
    with list_bodies. Use add_plane first for a cut anywhere else.
    """
    return await _call(_session.split_body, plane, name)


@mcp.tool()
async def add_thread(size: str, x_mm: float, y_mm: float, z_mm: float, length_mm: float,
                     internal: bool = False, name: str = "Thread") -> dict:
    """Cut a real, printable ISO metric thread (SolidWorks' own Thread feature).

    size: e.g. 'M10x1.5', 'M3x0.5', 'M10x1.0' -- checked against SolidWorks'
    thread profiles. (x_mm, y_mm, z_mm) = centre of the circular edge where the
    thread starts: the end face of a rod, or the mouth of a hole (internal=True).
    It runs length_mm into the material, right-handed. External: the rod must
    have the nominal diameter (M10 -> Ø10). Internal: drill the ISO basic minor
    diameter first, D - 1.0825*P (M10x1.5 -> Ø8.376). Returns mass properties
    and the thread's size, pitch and diameters.
    """
    return await _call(_session.add_thread, size, x_mm, y_mm, z_mm, length_mm, internal, name)


@mcp.tool()
async def add_rib(start_mm: list, end_mm: list, toward_mm: list, thickness_mm: float,
                  z_mm: float, name: str = "Rib") -> dict:
    """Add a straight stiffening rib / gusset in a plane parallel to the Front plane.

    start_mm/end_mm = [x, y] ends of the rib's free edge (add_extruded_profile
    coordinates); the plane sits at height z_mm. The rib grows toward toward_mm
    (any [x, y] point on the side to fill, e.g. the inner corner of an L-bracket)
    until it meets the part, thickness_mm thick, centred on the plane. Returns mass
    properties: a triangular gusset with legs a and b adds a*b/2 * thickness.
    """
    return await _call(_session.add_rib, start_mm, end_mm, toward_mm, thickness_mm, z_mm, name)


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
async def add_hole_on_face(diameter_mm: float, face: str, x_mm: float, y_mm: float, z_mm: float,
                           depth_mm: float | None = None, name: str = "Hole") -> dict:
    """Drill a round hole on ANY planar face, centred at 3D point (x, y, z) mm.

    face is "+x"/"-x"/"+y"/"-y"/"+z"/"-z": the face facing that way through the
    point is drilled, so a pocket floor or a step works too. Through all, or
    depth_mm deep (blind: heat-set inserts, screw pilots). Returns mass
    properties and `dimensions` (diameter, x, y, depth).
    """
    return await _call(_session.add_hole_on_face, diameter_mm, face, x_mm, y_mm, z_mm, depth_mm, name)


@mcp.tool()
async def add_hole_wizard(kind: str, size: str, face: str, x_mm: float, y_mm: float, z_mm: float,
                          depth_mm: float | None = None, fit: str = "normal", thread: str = "cosmetic",
                          name: str | None = None) -> dict:
    """An ISO hole from SolidWorks' Hole Wizard, sized by its standard tables.

    kind: 'clearance' (ISO 273), 'counterbore' (socket head cap screw),
    'countersink' (socket countersunk screw) or 'tapped'; size e.g. 'M3'.
    Centred at (x, y, z) mm on the face through that point; through all, or
    depth_mm deep. fit: 'close' / 'normal' / 'loose'. For tapped holes
    thread='modeled' cuts a real, printable ISO thread instead of SolidWorks'
    cosmetic one. Returns the standard's sizes in `hole` and mass properties.
    """
    return await _call(_session.add_hole_wizard, kind, size, face, x_mm, y_mm, z_mm,
                       depth_mm, fit, thread, name)


@mcp.tool()
async def add_boss_on_face(diameter_mm: float, face: str, x_mm: float, y_mm: float, z_mm: float,
                           height_mm: float, name: str = "Boss") -> dict:
    """Grow a round boss (standoff, peg) height_mm out of ANY planar face, centred at (x, y, z) mm.

    The face is found as for add_hole_on_face. For a PCB standoff, add a blind
    hole in its top. Returns mass properties and `dimensions`.
    """
    return await _call(_session.add_boss_on_face, diameter_mm, face, x_mm, y_mm, z_mm, height_mm, name)


@mcp.tool()
async def add_extruded_profile_on_face(points_mm: list, face: str, depth_mm: float,
                                       name: str = "Boss", corner_radii_mm: float | list | None = None) -> dict:
    """Grow a polygon boss depth_mm out of ANY planar face: points_mm = [[x,y,z], ...] in mm.

    The 3D points lie on the face (found as for add_hole_on_face). For pads,
    ledges and mounting blocks on an existing part. corner_radii_mm rounds the
    corners as for add_extruded_profile. Returns mass properties.
    """
    return await _call(_session.add_extruded_profile_on_face, points_mm, face, depth_mm, name, corner_radii_mm)


@mcp.tool()
async def add_text_on_face(text: str, face: str, x_mm: float, y_mm: float, z_mm: float,
                           height_mm: float, depth_mm: float, emboss: bool = False,
                           font: str | None = None, name: str = "Text") -> dict:
    """Engrave text into ANY planar face, or emboss it with emboss=True (labels, version numbers).

    (x, y, z) is the text's lower-left corner on the face; the text runs along
    the face sketch's horizontal axis: upright along +x on the top (+z) and
    front (-y) faces, turned on the others (vertical on +/-x, upside down on
    +y), so put labels on the top or front. height_mm is the
    character height, depth_mm the engraving depth or embossing height. The
    position is two dimensions ('x', 'y'); font must be installed (default:
    SolidWorks' own). Returns text_area_mm2, since letters have no hand calculation.
    """
    return await _call(_session.add_text_on_face, text, face, x_mm, y_mm, z_mm,
                       height_mm, depth_mm, emboss, font, name)


@mcp.tool()
async def cut_profile(points_mm: list, depth_mm: float | None = None, name: str = "Cut",
                      corner_radii_mm: float | list | None = None, rotate_deg: float = 0.0,
                      about_mm: list | None = None) -> dict:
    """Cut a polygonal pocket/slot from the +Z face: points_mm = [[x,y], ...] in mm.

    Auto-closed polygon, cut blind by depth_mm or all the way through when depth_mm
    is omitted. For pockets, slots, cutouts. corner_radii_mm rounds the corners
    and rotate_deg turns the profile about about_mm, as for add_extruded_profile
    (notches at +/-60 degrees around a pivot). Returns mass properties.
    """
    return await _call(_session.cut_profile, points_mm, depth_mm, name, corner_radii_mm, rotate_deg, about_mm)


@mcp.tool()
async def cut_profile_on_face(points_mm: list, face: str,
                             depth_mm: float | None = None, name: str = "Cut",
                             corner_radii_mm: float | list | None = None) -> dict:
    """Cut a polygon pocket/slot on ANY planar face: points_mm = [[x,y,z], ...] in mm.

    The 3D points must lie on one face facing `face` ("+x"/"-x"/...), which is
    found through them; cut blind by depth_mm or through when omitted. For side
    pockets/cutouts. corner_radii_mm rounds the corners as for
    add_extruded_profile. Returns mass properties.
    """
    return await _call(_session.cut_profile_on_face, points_mm, face, depth_mm, name, corner_radii_mm)


@mcp.tool()
async def cut_offset_pocket(face: str, x_mm: float, y_mm: float, z_mm: float, rim_mm: float,
                            depth_mm: float | None = None, name: str = "Pocket") -> dict:
    """Pocket a planar face, leaving a rim rim_mm wide along its outline.

    The recessed web of an I-beam that follows a curved link, a tray, or a frame
    (depth_mm omitted: through all). The face is the one facing `face`
    ('+z', ...) through the point (x, y, z). The rim follows the outline, arcs
    and splines included; holes in the face stay holes. rim and depth come back
    as dimensions, and the pocket follows the outline when it changes. Returns
    mass properties.
    """
    return await _call(_session.cut_offset_pocket, face, x_mm, y_mm, z_mm, rim_mm, depth_mm, name)


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
async def add_extruded_slot(start_mm: list, end_mm: list, width_mm: float, depth_mm: float,
                            name: str = "Slot") -> dict:
    """Extrude a stadium (rounded tab, lug or link) from point start_mm to end_mm ([x, y] mm).

    The half-round ends are centred on the two points themselves; width_mm is
    the tab's width, depth_mm the extrusion along +Z. Volume = (width * length
    + pi * (width/2)^2) * depth. Returns mass properties.
    """
    return await _call(_session.add_extruded_slot, start_mm, end_mm, width_mm, depth_mm, name)


@mcp.tool()
async def extrude_sketch(sketch: str, depth_mm: float | None = None, reverse: bool = False,
                         name: str = "Extrude", up_to: str | None = None) -> dict:
    """Extrude an existing sketch of the current part by name, e.g. one a person drew.

    depth_mm along the sketch's normal (reverse=True: the other way), merged
    with the body. Or up_to='next': up to the next face of the part, ending on
    its shape (a post into a curved wall) and following it when it changes;
    or up_to='@x,y,z': up to the face through that point, also where 'next'
    fails (a side of the profile on a face: the refusal names a point ahead).
    The sketch keeps its own dimensions and relations, so the person's design
    stays in charge; read_sketch shows it first. Returns mass properties.
    """
    return await _call(_session.extrude_sketch, sketch, depth_mm, reverse, name, up_to)


@mcp.tool()
async def add_sketch(plane: str, start_mm: list, segments: list, name: str | None = None) -> dict:
    """Draw a sketch of lines, arcs and splines on a plane, fully defined, to extrude or cut.

    plane: "front", "top", "right" or a plane by name (add_plane). Points are
    [u, v] in the plane's own axes, which the result gives (origin_mm + u
    x_axis + v y_axis): front is (x, y), top (x, -z), right (-z, y).
    The outline starts at start_mm; each segment runs on from where the last
    ended: {"line": [u, v]}; {"arc": [u, v], "through": [u, v]} (an arc through
    a middle point); {"arc": [u, v], "tangent": true} (an arc flowing on from
    the segment before); {"spline": [[u, v], ..., [u, v]]} (through the points,
    ending at the last). "tangent": true on a line checks that it flows on.
    End on start_mm to close the outline. Segments that meet smoothly get a
    tangent relation, the rest is dimensioned from the origin: x<i>/y<i> for
    the i-th point given (points_mm lists them; start_mm is 0), r<k> for the
    radius of segment k. Then extrude_sketch / cut_sketch the returned sketch.
    """
    return await _call(_session.add_sketch, plane, start_mm, segments, name)


@mcp.tool()
async def cut_sketch(sketch: str, depth_mm: float | None = None, reverse: bool = False,
                     name: str = "Cut") -> dict:
    """Cut an existing sketch of the current part by name into the body.

    depth_mm deep, or through all when depth_mm is omitted, against the
    sketch's normal: into the part from a face. From a plane with the part in
    front of it (a box on the Front plane) give reverse=True. Returns mass
    properties.
    """
    return await _call(_session.cut_sketch, sketch, depth_mm, reverse, name)


@mcp.tool()
async def add_fillet(radius_mm: float, edges: str = "all", name: str = "Fillet",
                     radii_at_mm: list | None = None, skip_shorter_mm: float | None = None,
                     tangent_propagation: bool = False) -> dict:
    """Round edges of the current part with one constant radius (mm).

    edges: "all" (default), "x"/"y"/"z" for edges parallel to that world axis,
    a face outline like "+z:outline" (the outer edges of the top face, not those
    of holes in it), every edge of one feature like "feature:Boss" (its top and
    where it meets the part: a moulded look), or explicit indices like "2,5" from
    list_edges. radii_at_mm = [[x, y, z, r], ...] makes the radius vary: r at
    the edge end at each point (list_edges gives the ends), radius_mm at the
    other ends, straight in between; 'vertex_radii' gives each end's dimension.
    skip_shorter_mm leaves out edges shorter than that. tangent_propagation=True
    carries the round on along edges that run on smoothly from the given ones,
    as SolidWorks does by default: an edge ending where it runs into another
    tangentially, such as round a fillet, only rounds so. A refusal names the
    edges the radius does not fit (each tried alone, and with propagation).
    Returns the number of edges filleted and the mass properties.
    """
    return await _call(_session.add_fillet, radius_mm, edges, name, radii_at_mm, skip_shorter_mm,
                       tangent_propagation)


@mcp.tool()
async def add_full_round(face: str, x_mm: float, y_mm: float, z_mm: float, name: str = "FullRound") -> dict:
    """Round a rib's top off completely: a full round fillet, as on a moulded part.

    The face facing `face` ("+z", ...) through the point (x, y, z) is the top;
    the two opposite flat side faces along it that lie closest together are the
    sides, so the round runs across the rib, not along it. The radius is half
    their distance (width_mm in the result) and follows the rib. Hand
    calculation: a rib w wide and L long loses L w^2 (1/2 - pi/8).
    """
    return await _call(_session.add_full_round, face, x_mm, y_mm, z_mm, name)


@mcp.tool()
async def add_chamfer(distance_mm: float, edges: str = "all", name: str = "Chamfer",
                      angle_deg: float = 45.0, from_face: str | None = None) -> dict:
    """Chamfer edges of the current part: distance_mm back along a face, at angle_deg (default 45°) to it.

    edges: "all" (default), "x"/"y"/"z", a face outline like "+z:outline", a
    feature's edges like "feature:Boss", or explicit indices like "2,5" from
    list_edges. Another angle than 45° needs from_face, which way the face that
    the distance runs along faces: "-z", angle_deg=60 makes an underside edge
    printable without support (round an arc the chamfer is a cone). Returns the
    number of edges chamfered and the mass properties.
    """
    return await _call(_session.add_chamfer, distance_mm, edges, name, angle_deg, from_face)


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
async def add_mirror(plane: str, offset_mm: float = 0.0, features: list | None = None,
                     name: str = "Mirror") -> dict:
    """Mirror features, or the whole body, about the 'front'/'top'/'right' plane moved offset_mm.

    Mirrors about z / y / x = offset_mm; plane may also name another plane in
    the part ('Plane1'), moved offset_mm along its normal. features: list_features names; the
    copies follow their seeds. Without features the body is mirrored and
    merged: model half of a symmetric part, mirror it about the face where the
    halves meet. The plane position comes back as dimension 'plane_offset'.
    Fails, leaving the part as it was, when a copy would land outside the part.
    """
    return await _call(_session.add_mirror, plane, offset_mm, features, name)


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

    This is the parametric edit at the heart of the correction loop. Every
    modelling tool returns its dimensions by role in `dimensions`. An angle (a
    revolve, an angle mate) takes degrees; list_dimensions shows the unit. A
    point's coordinate from the origin (a sketch's 'x3', 'y2') takes a sign,
    -5 being across the origin; any other length is a size, refused below 0.
    A value SolidWorks does not take (a dimension an equation drives, a size
    of 0) is refused with the reason, and the part is left as it was.
    """
    return await _call(_session.set_dimension, dimension_name, value_mm)


@mcp.tool()
async def slice_mesh(path: str, axis: str, heights_mm: list, frame: str = "object") -> dict:
    """Cross-sections of an STL/3MF mesh at the given heights along axis 'x'/'y'/'z'.

    For modelling a part that must fit something that only exists as a mesh.
    Returns each section's closed loops, largest first, as polygon points (mm)
    ready to use as profiles. frame (3MF): 'object' (the mesh's own frame) or
    'build' (as placed on the slicer's plate).
    """
    return await _call(_session.slice_mesh, path, axis, heights_mm, frame)


@mcp.tool()
async def compare_with_mesh(path: str, axis: str, heights_mm: list, frame: str = "object",
                            offset_mm: list | None = None) -> dict:
    """Compare the current part's cross-sections with a reference mesh's at the same heights.

    offset_mm [dx, dy, dz] moves the mesh into the part's frame. A different
    area means a misread feature; equal areas with different extents a
    shifted frame. Returns per-section pairs and the worst differences.
    """
    return await _call(_session.compare_with_mesh, path, axis, heights_mm, frame, offset_mm)


@mcp.tool()
async def list_dimensions() -> dict:
    """List every dimension in the current part: name (for set_dimension), feature, value, unit.

    For a part opened from disk, or when the names the tools returned are gone.
    """
    return await _call(_session.list_dimensions)


@mcp.tool()
async def read_sketch(name: str) -> dict:
    """Read a sketch of the current part back, e.g. one a person drew (a list_features name).

    Gives its lines, arcs, circles and splines in MODEL coordinates (mm),
    construction geometry marked, its dimensions (for set_dimension) and
    whether it is fully defined: check a design against fixed points before
    building on it with extrude_sketch / cut_sketch.
    """
    return await _call(_session.read_sketch, name)


@mcp.tool()
async def list_features() -> dict:
    """List the current part's modelling history in tree order: name, type, suppressed.

    For a part opened from disk, or to find the step to undo. Flags features
    that fail to rebuild and names sketches that are not fully defined.
    """
    return await _call(_session.list_features)


@mcp.tool()
async def delete_feature(name: str, with_children: bool = False, with_equations: bool = False,
                         dry_run: bool = False) -> dict:
    """Delete a feature (a list_features name) with its sketch, then rebuild and remeasure.

    The way to undo a step. Refuses when other features are built on it and
    names them, and what is built on those; with_children=True deletes them
    all. dry_run=True only lists what would go (would_delete). Equations that
    named its dimensions break and come back as broken_equations;
    with_equations=True deletes them (global variables stay).
    """
    return await _call(_session.delete_feature, name, with_children, with_equations, dry_run)


@mcp.tool()
async def suppress_feature(name: str, suppress: bool = True) -> dict:
    """Suppress a feature, or bring it back with suppress=False; rebuild and remeasure.

    Keeps the feature and its dimensions, unlike delete_feature, so it suits
    trying a variant. Features that depend on it follow both ways.
    """
    return await _call(_session.suppress_feature, name, suppress)


@mcp.tool()
async def reorder_feature(name: str, before: str) -> dict:
    """Move a feature (a list_features name), with its sketch, to just before another one; rebuild and remeasure.

    For a boss added last that filled holes cut earlier: build it at the end
    as usual, then move it before the first of those cuts, and they cut
    through it again. Refused, the part as it was, when the feature is built
    on something at or after that place. Returns the new order (features)
    and the features that fail to rebuild (failing_features).
    """
    return await _call(_session.reorder_feature, name, before)


@mcp.tool()
async def set_material(name: str, database: str = "", density_kg_m3: float | None = None) -> dict:
    """Assign a material by name (e.g. "6061 Alloy", "AISI 1020", "ABS").

    Makes mass and density reflect a real material instead of the 1000 kg/m³
    default. One SolidWorks does not have (say "TPU") comes with its density:
    density_kg_m3=1210 makes it, by that name, carrying only the density.
    Returns mass properties including density.
    """
    return await _call(_session.set_material, name, database, density_kg_m3)


@mcp.tool()
async def set_equation(equation: str) -> dict:
    """Add an equation, or replace the one that sets the same name; rebuild and remeasure.

    A SolidWorks equation string, e.g. '"D1@BlockExtrude" = 25' or
    '"D1@BlockExtrude" = 2 * "D1@Sketch1"'. A global variable is '"W" = 40';
    link a dimension to it with '"width@Sketch1" = "W"', and set '"W" = 45'
    again to change it. Persists a relation (unlike set_dimension). Returns
    the index, whether it replaced one, the features that fail to rebuild
    (failing_features) and mass properties.
    """
    return await _call(_session.set_equation, equation)


@mcp.tool()
async def set_equations(equations: list) -> dict:
    """Add or replace a list of equations at once, with one rebuild at the end.

    As set_equation for each, in order (later ones may use earlier ones). A
    refused equation undoes the whole list. Returns per equation its index and
    whether it replaced one, failing_features and mass properties.
    """
    return await _call(_session.set_equations, equations)


@mcp.tool()
async def list_equations() -> dict:
    """List the equations and global variables: index, equation, name it sets, value, global.

    `broken` marks an equation that names a dimension or variable that is gone,
    as one left behind by delete_feature; delete_equation removes it.
    automatic_solve_order False means SolidWorks solves them as listed: one
    above the line that sets its variable then warns and lags a rebuild. Any
    set_equation turns it on.
    """
    return await _call(_session.list_equations)


@mcp.tool()
async def delete_equation(equation: str) -> dict:
    """Delete an equation by the name it sets ("L_thigh", "D1@Boss") or its list_equations index.

    Rebuilds and returns what was deleted, failing_features and mass properties.
    """
    return await _call(_session.delete_equation, equation)


@mcp.tool()
async def list_documents() -> dict:
    """List the documents open in SolidWorks: title, path (empty until saved), type, unsaved changes.

    `current` marks the one the tools work on, `active` SolidWorks' own. Parts
    an open assembly loaded come along, with visible false.
    """
    return await _call(_session.list_documents)


@mcp.tool()
async def activate_document(title: str) -> dict:
    """Make an open document current again by its title, also a new one that was never saved.

    For after open_part / close_part on another document; list_documents gives
    the titles.
    """
    return await _call(_session.activate_document, title)


@mcp.tool()
async def delete_mate(name: str) -> dict:
    """Delete a mate of the current assembly by name (list_components, or add_mate's 'mate').

    Its components are free to move again. Returns the mates left.
    """
    return await _call(_session.delete_mate, name)


@mcp.tool()
async def suppress_mate(name: str, suppress: bool = True) -> dict:
    """Suppress a mate so it stops holding its components (it stays in the tree), or bring it back.

    suppress=False brings it back. Returns whether it is suppressed now.
    """
    return await _call(_session.suppress_mate, name, suppress)


@mcp.tool()
async def delete_component(component: str) -> dict:
    """Remove a component from the current assembly, with the mates that hold it.

    component: its name in list_components ("Bracket-1", or "Bracket" while
    there is one). Returns the mates deleted, the components left and the
    assembly's mass properties.
    """
    return await _call(_session.delete_component, component)


@mcp.tool()
async def rebuild(top_only: bool = False) -> dict:
    """Force a rebuild of the current part or assembly and report whether it rebuilt without errors.

    In an assembly, mate_errors names each mate SolidWorks flags, and each
    mate that lost a face or edge it used (it rebuilds fine, but holds nothing).
    """
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
async def list_faces(component: str | None = None) -> dict:
    """List the part's faces (index, planar?, normal, area, centre) for inspection.

    A cylindrical face also gives its axis, radius and a point on the axis (for
    a hole: its centre), so hole patterns can be read off. component: a
    component of the current assembly, listed in its own frame (e.g. an
    imported servo); for a sub-assembly, every part's faces in the
    sub-assembly's frame, each naming its part. A part's indices shift as
    features are added; a component's are ordered by part, type, centre and
    area, so they stay while its shape does. add_mate takes them as '#5'.
    """
    return await _call(_session.list_faces, component)


@mcp.tool()
async def list_edges(face: str | None = None, feature: str | None = None, within_mm: list | None = None,
                     min_length_mm: float | None = None) -> dict:
    """List the part's edges: index, type, length, ends; lines also axis and midpoint.

    Narrow it on a big part: face (a list_faces index, "#5"), feature (the edges
    of the faces it made), within_mm ([[x1, y1, z1], [x2, y2, z2]]: both ends
    inside), min_length_mm. Indices are always those of the whole part: use them
    with add_fillet/add_chamfer edges="2,5".
    """
    return await _call(_session.list_edges, face, feature, within_mm, min_length_mm)


@mcp.tool()
async def check_printability(up: str = "+z", overhang_deg: float = 45.0,
                             min_wall_mm: float | None = None) -> dict:
    """Check the current part for 3D printing, built along `up` ('+z', '-y', ...).

    Overhangs: downward faces leaning more than overhang_deg from vertical (90 =
    a flat ceiling) need support; the faces on the bed do not count. Gives the
    overhanging area per face (list_faces index) with its worst lean and centre,
    the bed contact area and the print height. With min_wall_mm (e.g. two
    nozzle widths) also the walls thinner than that, measured straight through
    the material from points spread over every face, with where each is thinnest.
    A spot right beside an edge where faces meet at a sharp angle reads as thin
    as the wedge of material there is: that is the edge, not a wall.
    """
    return await _call(_session.check_printability, up, overhang_deg, min_wall_mm)


@mcp.tool()
async def export(path: str, file_format: str | None = None, quality: str = "fine",
                 deviation_mm: float | None = None, angle_deg: float | None = None,
                 per_component: bool = False) -> dict:
    """Export the current part or assembly to STEP/STL/IGES/Parasolid/3MF (format from extension).

    Silent (no prompts). Verifies the file appears on disk and reports its size.
    For STL/3MF, tessellation resolution is set first: quality 'coarse'|'fine'
    (default 'fine' for print quality), or pass deviation_mm (+ optional angle_deg)
    for a reproducible custom resolution (overrides quality). Ignored for other formats.
    An assembly goes to STL as one file, or one file per component with
    per_component=True (SolidWorks names them); `files` lists what was written.
    STL/3MF keep the part's or assembly's own coordinates (`frame`), also per
    component, so a mesh measures back where the model is; slicers place it anyway.
    """
    return await _call(_session.export, path, file_format, quality, deviation_mm, angle_deg, per_component)


@mcp.tool()
async def screenshot(path: str, view: str = "iso", zoom_mm: list | None = None,
                     show_planes: bool = False, from_dir: list | None = None) -> dict:
    """Save a screenshot of the current part or assembly (PNG/JPG/TIF).

    view: 'iso' (default), 'front', 'back', 'left', 'right', 'top' or 'bottom';
    or from_dir = [x, y, z], the direction to look from, e.g. [-1, 1, -1] to
    see from behind and below what iso hides (the model's +y stays up).
    Zoomed to fit, or onto a detail: zoom_mm = [[x1, y1, z1], [x2, y2, z2]], the
    corners of the region to fill the image (model mm, in any view). Reference
    planes and axes are left out, so they do not cut through the shape;
    show_planes=True keeps them.
    """
    return await _call(_session.screenshot, path, view, zoom_mm, show_planes, from_dir)


@mcp.tool()
async def make_drawing(path: str) -> dict:
    """Make a 2D drawing of the current part: a PDF, or an editable .slddrw.

    Front, top and right views in European (first angle) projection plus an
    isometric view, on an A4 sheet at the scale SolidWorks picks, with the
    model's own dimensions, each shown once: a dimensioned sketch to print or
    send. Save the part first (save_part): the drawing refers to its file.
    """
    return await _call(_session.make_drawing, path)


@mcp.tool()
async def close_part(save: bool = False) -> dict:
    """Close the current part or assembly without saving (export/save first if needed).

    With no current document it closes SolidWorks' active one, but only when
    that has no unsaved changes.
    """
    return await _call(_session.close_part, save)


@mcp.tool()
async def save_part(path: str) -> dict:
    """Save the current part to a native .sldprt file (so it can be reopened/edited)."""
    return await _call(_session.save_part, path)


@mcp.tool()
async def open_part(path: str) -> dict:
    """Open a .sldprt, or import a STEP/IGES/Parasolid file as a new part; it becomes the current part.

    list_features shows its history, list_dimensions its dimensions. An
    imported body has no history, but the tools work on it (holes and pockets
    on its faces, bosses, fillets); an import returns its solid body count and
    mass properties. A file holding an assembly is refused.
    """
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
    """Open a .sldasm, or import a STEP/IGES/Parasolid assembly; it becomes the current document.

    An imported assembly's parts arrive as components at their places, ready
    for list_components and add_mate; the result gives the component count.
    A file holding a single part is refused (use open_part).
    """
    return await _call(_session.open_assembly, path)


@mcp.tool()
async def save_assembly(path: str) -> dict:
    """Save the current assembly to a native .sldasm file (so it can be reopened)."""
    return await _call(_session.save_assembly, path)


@mcp.tool()
async def insert_component(path: str, x_mm: float = 0.0, y_mm: float = 0.0,
                           z_mm: float = 0.0, fixed: bool | None = None) -> dict:
    """Insert a .sldprt, or a .sldasm as a sub-assembly, with its ORIGIN at (x, y, z) mm.

    The part's own origin lands exactly on that point, and the placement is read
    back and verified. fixed=True pins the component; fixed=False leaves it free
    for mates. The default fixes only the FIRST component, giving the assembly a
    ground to build against. Returns the component's name, placement and box.
    """
    return await _call(_session.insert_component, path, x_mm, y_mm, z_mm, fixed)


@mcp.tool()
async def list_components() -> dict:
    """List the assembly's components (name, path, fixed, position, rotation, bounding box) and mates.

    Positions are in mm and rotations in degrees, both in assembly coordinates;
    the bounding box of each component is in assembly coordinates too. Each
    mate gives its name, SolidWorks type, the dimension of a distance or angle
    mate, and an error when SolidWorks flags it.
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
                   angle_deg: float = 0.0, flip: bool = False) -> dict:
    """Mate a face of one component to a face of another.

    comp_a/comp_b are component names ('Bed' or 'Bed-1'; 'Leg-1/Thigh-1' inside
    a sub-assembly). face_a/face_b select a face in that component's OWN frame:
    by direction "+x"/"-x"/"+y"/... (optionally "+y:inner" for the cavity side
    of a hollow part), by its list_faces(component=...) index, "#5", or by a
    point on it, "@x,y,z" (a hole's wall: its centre plus the radius along x).
    mate_type between planar faces:
    "coincident", "distance" (distance_mm), "parallel", "perpendicular" or
    "angle" (angle_deg between the faces' normals, 0..180); "concentric" between
    two cylindrical faces (a pin in a hole, a hinge axis), leaving the turn
    about the axis free. flip takes the other solution: the mirror side of a
    distance, the other turning direction of an angle. A distance or angle
    mate returns its `dimension`: drive it with
    set_dimension, or step it with check_motion. An angle mate's dimension
    then turns the joint the whole way round, past 180 and below 0 (-30 is 30
    the other way, the same turn as 330). The result is measured back
    after the rebuild; a mate that does not hold is removed and the components
    are put back.
    """
    return await _call(_session.add_mate, comp_a, face_a, comp_b, face_b,
                       mate_type, distance_mm, angle_deg, flip)


@mcp.tool()
async def check_interference() -> dict:
    """Report component pairs whose solids overlap, with the volume in mm^3.

    Touching faces do not count (a bed standing on the floor is fine); only real
    overlapping material does. count == 0 means the assembly is clash-free.
    """
    return await _call(_session.check_interference)


@mcp.tool()
async def measure_distance(component_a: str, component_b: str | None = None,
                           point_mm: list | None = None, axis_mm: list | None = None) -> dict:
    """The smallest distance (mm) from a component to another one, a point or an axis.

    Give component_b; or point_mm = [x, y, z] in assembly mm, and the nearest
    point on the component comes back too, with inside: true when the point lies
    in its material; or axis_mm = [[x, y, z], [dx, dy, dz]], the endless line
    through that point along that direction, such as a bolt's axis. 0 means they
    touch or overlap; check_interference tells which. Sub-assemblies count with
    all their parts; one part inside is 'Leg-1/Thigh-1'.
    """
    return await _call(_session.measure_distance, component_a, component_b, point_mm, axis_mm)


@mcp.tool()
async def check_motion(dimension_name: str, values: list, distances: list | None = None) -> dict:
    """Step a joint through its range and check the assembly at every step.

    dimension_name: an angle or distance mate's dimension (add_mate returns it);
    values in degrees for an angle, mm for a distance, e.g. [30, 60, 90, 120,
    150]. Each step gives the overlapping pairs with their volume and the
    distance between each pair in distances ([["Rod", "Bolt"], ...]); the
    summary gives clash_free and each pair's smallest distance with the value
    where it occurs. The dimension goes back to its value afterwards. A joint
    inside a sub-assembly is 'D1@Angle1@Leg-1'. Refused while a mate is broken:
    the parts it should hold would drift and the result mean nothing.
    """
    return await _call(_session.check_motion, dimension_name, values, distances)


@mcp.tool()
async def swept_region(component: str, dimension: str, values: list, heights_mm: list, axis: str = "z",
                       margin_mm: float = 0.0, tolerance_mm: float = 0.2, frame: str | None = None) -> dict:
    """Outline what a component covers in a plane while a joint moves, to keep
    clear of it or cut it away.

    Steps the joint's dimension through values (as check_motion, e.g. every
    5 degrees over 30..150), cuts the component at axis = each of heights_mm
    (a few heights inside a layer, not on its faces) and joins all of it, made
    margin_mm wider, within about tolerance_mm. frame: the component to give the
    coordinates in, such as the part to cut from; else the assembly's. Each
    region's outline runs counter-clockwise in the plane's other two axes,
    (x, y) across z: give it to add_sketch as a spline. count says how many
    separate regions there are.
    """
    return await _call(_session.swept_region, component, dimension, values, heights_mm, axis,
                       margin_mm, tolerance_mm, frame)


@mcp.tool()
async def get_assembly_bounding_box() -> dict:
    """Get the bounding box of the whole assembly (min/max/size in mm)."""
    return await _call(_session.get_assembly_bounding_box)


def main() -> None:
    """Entry point: run the MCP server over stdio, or the selftest with --selftest."""
    parser = argparse.ArgumentParser(prog="solidworks-mcp", description="MCP server for a running SolidWorks.")
    parser.add_argument("--selftest", action="store_true",
                        help="check this machine's SolidWorks with a few small parts, print a report and exit")
    selftest = parser.parse_args().selftest
    try:
        if not selftest:
            mcp.run()
        elif _worker is None:
            sys.exit(run_selftest(_session))
        else:
            sys.exit(_worker.submit(lambda: run_selftest(_session)).result())
    finally:
        if _worker is not None:
            _worker.shutdown()


if __name__ == "__main__":
    main()
