"""Integration tests for shaping from outlines and views: offset pockets and keep-inside cuts.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The block is 60 x 20 x 10 with R5 corners, so the outline has arcs to follow:
an inward offset by `rim` leaves a rounded rectangle 60 - 2 rim by 20 - 2 rim
with R(5 - rim) corners. Volumes are in mm^3.
"""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

BLOCK = 60 * 20 * 10 - (4 - math.pi) * 5 ** 2 * 10


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def box(result):
    return result["mass_properties"]["bounding_box_mm"]


def inner_area(rim):
    """The face inside the rim: a rounded rectangle with R(5 - rim) corners."""
    return (60 - 2 * rim) * (20 - 2 * rim) - (4 - math.pi) * (5 - rim) ** 2


def rounded_block(session):
    return session.add_extruded_profile([[0, 0], [60, 0], [60, 20], [0, 20]], 10, corner_radii_mm=5)


def test_offset_pocket_leaves_a_rim_along_a_rounded_outline(part):
    rounded_block(part)
    pocket = part.cut_offset_pocket("+z", 30, 10, 10, rim_mm=2, depth_mm=3)
    assert vol(pocket) == pytest.approx(BLOCK - inner_area(2) * 3, abs=1e-3)
    assert set(pocket["dimensions"]) == {"rim", "depth"} and pocket["fully_defined"] is True
    assert pocket["dimensions"]["rim"].startswith("rim@"), "a person sees the rim as D1 in SolidWorks"

    # the rim stays a dimension a person can change; the pocket follows it
    wider = part.set_dimension(pocket["dimensions"]["rim"], 4)
    assert vol(wider) == pytest.approx(BLOCK - inner_area(4) * 3, abs=1e-3)


def test_offset_pocket_through_all_leaves_a_frame(part):
    rounded_block(part)
    frame = part.cut_offset_pocket("+z", 30, 10, 10, rim_mm=2)
    assert vol(frame) == pytest.approx(BLOCK - inner_area(2) * 10, abs=1e-3)


def test_offset_pocket_keeps_the_holes_in_the_face(part):
    # the hole was empty already: the pocket takes the rest of the inner area
    rounded_block(part)
    part.add_hole(6, 30, 10)
    pocket = part.cut_offset_pocket("+z", 10, 10, 10, rim_mm=2, depth_mm=3)
    hole_area = math.pi * 3 ** 2
    assert vol(pocket) == pytest.approx(BLOCK - hole_area * 10 - (inner_area(2) - hole_area) * 3, abs=1e-3)


def test_offset_pocket_with_a_rim_too_wide_leaves_the_part_as_it_was(sw):
    """Run guarded, as every MCP call is: the refused pocket leaves no sketch behind."""
    sw.new_part()
    try:
        rounded_block(sw)
        features = sw.list_features()["features"]
        with pytest.raises(SolidWorksError, match="rim wider than half the face"):
            sw.run_guarded(sw.cut_offset_pocket, "+z", 30, 10, 10, 11, 3)
        assert sw.list_features()["features"] == features
    finally:
        sw.close_part()


def test_keep_inside_turns_a_side_view_into_the_shape(part):
    # the front view is the 40 x 20 block; seen from the side only the triangle
    # y 0..20, z 0..6 is kept (a cut of the triangle would leave 5600 instead)
    part.add_box(40, 20, 10)
    wedge = part.cut_profile_through_plane([[0, 0, 0], [0, 20, 0], [0, 0, 6]], "right", keep_inside=True)
    assert vol(wedge) == pytest.approx(40 * 20 * 6 / 2, abs=1e-3), "the triangle was cut away, not kept"
    assert wedge["mass_properties"]["bounding_box_mm"]["max_mm"][2] == pytest.approx(6, abs=1e-4)


# --- revolving about any axis; rounding one feature's edges ---------------------


def test_a_revolve_about_a_slanted_axis_follows_pappus(part):
    """A right triangle with one leg on the axis y = x turns into a cone of
    radius and height sqrt(200): pi r^2 h / 3."""
    cone = part.add_revolved_profile([[0, 0], [10, 10], [20, 0]], axis_mm=[[0, 0], [10, 10]])
    side = math.sqrt(200)
    assert vol(cone) == pytest.approx(math.pi * side ** 2 * side / 3, rel=1e-6)
    assert cone["fully_defined"] is True


def test_the_axis_of_a_revolve_is_a_dimension(part):
    # a ring about the vertical line x = 50: the square x 60..70 turns at radius 15
    ring = part.add_revolved_profile([[60, 0], [70, 0], [70, 10], [60, 10]], axis_mm=[[50, 0], [50, 10]])
    assert vol(ring) == pytest.approx(100 * 2 * math.pi * 15, rel=1e-6)
    [axis_x] = [d["name"] for d in part.list_dimensions()["dimensions"] if d["value"] == pytest.approx(50)]
    moved = part.set_dimension(axis_x, 45)  # the axis moves away: radius 20
    assert vol(moved) == pytest.approx(100 * 2 * math.pi * 20, rel=1e-6)


def test_fillet_rounds_every_edge_of_one_feature(part):
    """The boss's top edge loses a spandrel of r^2 (1 - pi/4) at radius
    R - 0.2234 r and the edge where it meets the block gains one at
    R + 0.2234 r (Pappus). 'all' would round the block's edges too."""
    part.add_box(40, 20, 10)
    before = vol(part.add_boss_on_face(10, "+z", 20, 10, 10, 5, name="Boss"))
    rounded = part.add_fillet(2, edges="feature:Boss")
    spandrel, centroid = (1 - math.pi / 4) * 2 ** 2, (10 - 3 * math.pi) / (12 - 3 * math.pi) * 2
    assert rounded["edges_filleted"] == 2
    assert vol(rounded) - before == pytest.approx(spandrel * 2 * math.pi * 2 * centroid, abs=1e-3)


# --- planes to build on; profiles at an angle -------------------------------------


def on_plane(frame, u, v):
    """The model point (u, v) on a plane add_plane described."""
    return [o + u * x + v * y for o, x, y in zip(frame["origin_mm"], frame["x_axis"], frame["y_axis"])]


def test_an_offset_plane_carries_an_extrusion_either_way(part):
    part.add_box(40, 20, 10)
    plane = part.add_plane("front", offset_mm=15)
    assert plane["normal"] == pytest.approx([0, 0, 1], abs=1e-6) and plane["origin_mm"][2] == pytest.approx(15)
    square = [on_plane(plane, u, v) for u, v in ((0, 0), (10, 0), (10, 10), (0, 10))]
    up = part.add_extruded_profile_on_plane(square, plane["plane"], 3)  # z 15..18, apart from the block
    assert box(up)["max_mm"][2] == pytest.approx(18) and vol(up) == pytest.approx(40 * 20 * 10 + 10 * 10 * 3)
    down = part.add_extruded_profile_on_plane(square, plane["plane"], 5, reverse=True)  # z 10..15 joins them
    assert box(down)["min_mm"][2] == pytest.approx(0) and vol(down) == pytest.approx(8000 + 300 + 500)


def test_points_on_a_plane_may_be_given_as_u_v(part):
    """[u, v] in the plane's own frame, as add_sketch takes them, cut the same
    window through a turned plane as the [x, y, z] points on it."""
    part.add_box(40, 20, 10)
    plane = part.add_plane("right", angle_deg=30, about="y", name="Cheek")
    # a 6 x 6 window around where the plane meets the box's middle, y 10, z 5
    u, v = (sum((c - o) * a for c, o, a in zip((0, 10, 5), plane["origin_mm"], plane[axis]))
            for axis in ("x_axis", "y_axis"))
    window = ((u - 3, v - 3), (u + 3, v - 3), (u + 3, v + 3), (u - 3, v + 3))
    by_uv = vol(part.cut_profile_through_plane([list(p) for p in window], "Cheek"))
    part.delete_feature(part.list_features()["features"][-1]["name"])
    by_xyz = vol(part.cut_profile_through_plane([on_plane(plane, u, v) for u, v in window], "Cheek"))
    assert by_uv == pytest.approx(by_xyz) and by_uv < 8000, (by_uv, by_xyz)


def test_a_turned_plane_holds_a_turned_block(part):
    """Front turned 30 degrees about y faces (sin 30, 0, cos 30); a 10 x 10
    square on it extruded 4 is a 400 mm^3 block leaning with it. Its angle is a
    dimension: at 90 the block stands along x."""
    plane = part.add_plane("front", angle_deg=30, about="y", name="Slant")
    assert plane["plane"] == "Slant"
    # SolidWorks first turns it the other way; that try must be gone again
    assert [f["name"] for f in part.list_features()["features"] if f["type"] == "RefPlane"] == ["Slant"]
    assert plane["normal"] == pytest.approx([0.5, 0, math.sqrt(3) / 2], abs=1e-6)
    square = [on_plane(plane, u, v) for u, v in ((0, 0), (10, 0), (10, 10), (0, 10))]
    block = part.add_extruded_profile_on_plane(square, "Slant", 4)
    assert vol(block) == pytest.approx(400) and block["fully_defined"] is True
    upright = part.set_dimension(plane["dimensions"]["angle"], 90)
    assert vol(upright) == pytest.approx(400)
    assert box(upright)["size_mm"][0] == pytest.approx(4, abs=1e-4), "the block did not turn with its plane"


def test_a_profile_turns_about_a_pivot(part):
    # a 30 x 10 slot from the left edge, turned 90 degrees about the centre,
    # opens on the bottom edge instead: x 15..25, y 0..30
    part.add_box(40, 40, 10)
    cut = part.cut_profile([[0, 15], [30, 15], [30, 25], [0, 25]], rotate_deg=90, about_mm=[20, 20])
    assert vol(cut) == pytest.approx(40 * 40 * 10 - 30 * 10 * 10)
    x, y, _ = cut["mass_properties"]["center_of_mass_mm"]
    assert x == pytest.approx(20, abs=1e-3) and y > 20, f"the slot was not turned: centre of mass at {x}, {y}"


# --- drafts, round lofts, smooth pipes ---------------------------------------------


@pytest.mark.parametrize("draft", [5, -5], ids=["inwards", "outwards"])
def test_a_draft_tapers_the_walls(part, draft):
    """A 20 x 10 rectangle 10 deep: every wall leans in (or out) by tan(draft)
    per mm of height, so the area at height z is (20 - 2zt)(10 - 2zt)."""
    t = math.tan(math.radians(draft))
    expected = 20 * 10 * 10 - (20 + 10) * t * 10 ** 2 + 4 / 3 * t ** 2 * 10 ** 3
    tapered = part.add_extruded_profile([[0, 0], [20, 0], [20, 10], [0, 10]], 10, draft_deg=draft)
    assert vol(tapered) == pytest.approx(expected, rel=1e-9)
    straighter = part.set_dimension(tapered["dimensions"]["draft"], 2)
    t2 = math.tan(math.radians(2 if draft > 0 else -2))
    assert vol(straighter) == pytest.approx(20 * 10 * 10 - 30 * t2 * 100 + 4 / 3 * t2 ** 2 * 1000, rel=1e-9)


@pytest.mark.parametrize("foot,rel", [([5, 0], 1e-4), ([-4, -9], 1e-2)], ids=["along x", "anywhere"])
def test_a_round_loft_makes_a_frustum(part, foot, rel):
    """Thick at the knee, thin at the foot: d 20 at z 0 to d 10 at z 30, the
    narrow end moved. Shifted circles hold the same volume (Cavalieri):
    pi h (R^2 + Rr + r^2) / 3. A steep slant comes out ~0.7% fuller in
    SolidWorks (measured), hence the wider rel there."""
    loft = part.add_lofted_solid([{"center_mm": [0, 0], "diameter_mm": 20}, {"center_mm": foot, "diameter_mm": 10}],
                                 [0, 30])
    assert vol(loft) == pytest.approx(math.pi * 30 * (10 ** 2 + 10 * 5 + 5 ** 2) / 3, rel=rel),         "a loft whose circles start at different angles twists, pinches in and loses volume"
    assert {"profile0_diameter", "profile1_diameter", "profile1_x"} <= set(loft["dimensions"])
    assert box(loft)["max_mm"][0] == pytest.approx(10, abs=1e-4),         "the box should hug the lofted face (SolidWorks' own part box reads 10.004)"


def test_a_loft_starts_each_polygon_at_its_first_vertex(part):
    """Square 20 round the origin to square 10 off to the lower left: the corner
    nearest the origin is not the first one, so lining the profiles up by that
    corner would twist the loft half a turn. Untwisted: h (a^2 + ab + b^2) / 3."""
    loft = part.add_lofted_solid([[[-10, -10], [10, -10], [10, 10], [-10, 10]],
                                  [[-12, -6], [-2, -6], [-2, 4], [-12, 4]]], [0, 30])
    assert vol(loft) == pytest.approx(30 * (20 ** 2 + 20 * 10 + 10 ** 2) / 3), "a twisted loft pinches in"


def test_a_smooth_pipe_follows_a_spline(part):
    # Pappus for tubes: area x path length, near enough for a gentle curve
    pipe = part.add_swept_pipe([[0, 0], [20, 10], [40, 0], [60, 10]], 4, smooth=True)
    assert vol(pipe) == pytest.approx(math.pi * 2 ** 2 * pipe["path_length_mm"], rel=1e-3)
    assert 60 < pipe["path_length_mm"] < 80
