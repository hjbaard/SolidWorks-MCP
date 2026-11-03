"""Integration tests for free sketches: lines, tangent arcs and splines, fully defined.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The `part` fixture also fails any test that leaves a sketch under-defined.
Volumes are in mm^3.
"""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

STADIUM = [{"line": [40, 0]}, {"arc": [40, 20], "tangent": True}, {"line": [0, 20]}, {"arc": [0, 0], "tangent": True}]


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def box(result):
    return result["mass_properties"]["bounding_box_mm"]


def segment_area(radius, chord):
    """The area between a chord and its arc (the smaller side)."""
    d = math.sqrt(radius ** 2 - (chord / 2) ** 2)
    return radius ** 2 * math.acos(d / radius) - d * chord / 2


def _square(half):
    return [{"line": [half, -half]}, {"line": [half, half]}, {"line": [-half, half]}, {"line": [-half, -half]}]


def test_sketches_on_any_planes_loft_into_one_solid(part):
    """add_lofted_solid lofts only along +Z from the Front plane; a head that
    narrows towards the side needed a loft along x. A 20 mm square on the
    Right plane lofted to a 10 mm square 30 mm along x is a frustum:
    h / 3 (A1 + A2 + sqrt(A1 A2)) = 10 (400 + 100 + 200) = 7000 mm^3."""
    wide = part.add_sketch("right", [-10, -10], _square(10))["sketch"]
    side = part.add_plane("right", offset_mm=30, name="Cheek")
    narrow = part.add_sketch(side["plane"], [-5, -5], _square(5))["sketch"]
    loft = part.loft_sketches([wide, narrow])
    assert vol(loft) == pytest.approx(7000, rel=1e-6), "the loft between the two squares is not the frustum"
    box = loft["mass_properties"]["bounding_box_mm"]
    assert box["min_mm"][0] == pytest.approx(0) and box["max_mm"][0] == pytest.approx(30)


def test_a_loft_needs_two_sketches(part):
    sketch = part.add_sketch("front", [-10, -10], _square(10))["sketch"]
    with pytest.raises(SolidWorksError, match="at least 2"):
        part.loft_sketches([sketch])


def test_a_stadium_is_two_dimensions_and_stays_a_stadium(part):
    """Tangent arcs make half circles on a 40 x 20 rectangle; a taller stadium
    keeps half circles, because the tangents hold, not the coordinates."""
    sketch = part.add_sketch("front", [0, 0], STADIUM)
    assert sketch["fully_defined"] and sketch["closed"]
    assert set(sketch["dimensions"]) == {"x1", "y2"}
    slab = part.extrude_sketch(sketch["sketch"], 10)
    assert vol(slab) == pytest.approx((40 * 20 + math.pi * 10 ** 2) * 10)
    assert box(slab)["min_mm"][0] == pytest.approx(-10, abs=1e-3) and box(slab)["max_mm"][0] == pytest.approx(50, abs=1e-3), \
        "the ends should bulge outwards"
    taller = part.set_dimension(sketch["dimensions"]["y2"], 30)
    assert vol(taller) == pytest.approx((40 * 30 + math.pi * 15 ** 2) * 10), \
        "the ends should stay half circles tangent to the sides"


def test_a_free_arc_keeps_its_ends_when_its_radius_changes(part):
    """An arc through (45, 10) from (40, 0) to (40, 20) has R12.5 and bulges 5 mm;
    at R15 the same chord holds a flatter segment. A spline closes the top."""
    sketch = part.add_sketch("front", [0, 0], [{"line": [40, 0]}, {"arc": [40, 20], "through": [45, 10]},
                                               {"spline": [[20, 25], [0, 20]]}, {"line": [0, 0]}])
    assert sketch["fully_defined"] and sketch["points_mm"][3] == [20, 25]
    before = vol(part.extrude_sketch(sketch["sketch"], 5))
    after = vol(part.set_dimension(sketch["dimensions"]["r1"], 15))
    assert before - after == pytest.approx((segment_area(12.5, 20) - segment_area(15, 20)) * 5, rel=1e-6), \
        "only the arc's bulge should change: its ends and the spline stay put"


def test_a_sketch_on_the_top_plane_follows_its_frame(part):
    """On the Top plane u runs along x and v along -z; the extrusion goes up +y."""
    sketch = part.add_sketch("top", [0, 0], [{"line": [40, 0]}, {"line": [40, 20]}, {"line": [0, 20]},
                                             {"line": [0, 0]}], name="Footprint")
    assert sketch["sketch"] == "Footprint" and sketch["dimensions"]["x1"].endswith("@Footprint")
    assert sketch["x_axis"] == [1, 0, 0] and sketch["y_axis"] == [0, 0, -1]
    block = part.extrude_sketch("Footprint", 5)
    assert box(block)["min_mm"] == pytest.approx([0, 0, -20]) and box(block)["max_mm"] == pytest.approx([40, 5, 0])


def test_an_s_bend_is_two_quarter_rings_between_straight_bars(part):
    """A bar 10 wide bends left round (30, 20) and right round (70, 20): quarter
    rings of R10-R20 and R20-R30 between two 30 x 10 bars, so the area is
    600 + 200 pi. Every bend flows on from the segment before."""
    s_bend = [{"line": [30, 0]}, {"arc": [50, 20], "tangent": True}, {"arc": [70, 40], "tangent": True},
              {"line": [100, 40]}, {"line": [100, 50]}, {"line": [70, 50]}, {"arc": [40, 20], "tangent": True},
              {"arc": [30, 10], "tangent": True}, {"line": [0, 10]}, {"line": [0, 0]}]
    sketch = part.add_sketch("front", [0, 0], s_bend)
    assert sketch["fully_defined"] and sketch["closed"]
    leg = part.extrude_sketch(sketch["sketch"], 5)
    assert vol(leg) == pytest.approx((600 + 200 * math.pi) * 5)


def test_a_ring_sector_from_rounded_points_shares_its_centre_with_the_origin(part):
    """Through points rounded to 5 decimals: both arcs end up centred on the
    origin, one quarter of a R10-R20 ring."""
    c, d = 7.07107, 14.14214
    sketch = part.add_sketch("front", [10, 0], [{"arc": [0, 10], "through": [c, c]}, {"line": [0, 20]},
                                                {"arc": [20, 0], "through": [d, d]}, {"line": [10, 0]}])
    ring = part.extrude_sketch(sketch["sketch"], 5)
    assert vol(ring) == pytest.approx(math.pi * (20 ** 2 - 10 ** 2) / 4 * 5)


def test_a_wedge_round_the_origin_takes_its_centre_as_given(part):
    """An 8 degree wedge of R30 with its end rounded to 3 decimals."""
    sketch = part.add_sketch("front", [0, 0], [{"line": [30, 0]}, {"arc": [29.708, 4.175], "center": [0, 0]},
                                               {"line": [0, 0]}])
    wedge = part.extrude_sketch(sketch["sketch"], 5)
    assert vol(wedge) == pytest.approx(30 ** 2 * math.atan2(4.175, 29.708) / 2 * 5, rel=1e-9)


def test_a_bent_slot_of_concentric_arcs_is_fully_defined(part):
    """Arcs of R14.5 and R9.7 round one centre, joined by lines on one straight
    line 2.2 mm beside it: the outer arc's segment less the inner one's. Both
    chords share a bisector, which once left the centre free in SolidWorks."""
    sketch = part.add_sketch("front", [97.8839, 9.2771], [
        {"line": [99.4276, 13.9118]}, {"arc": [90.3697, -13.2832], "through": [107.31, 1.01]},
        {"line": [91.9134, -8.6485]}, {"arc": [97.8839, 9.2771], "through": [102.51, 1.01]}])
    assert "r1" in sketch["dimensions"]
    slot = part.extrude_sketch(sketch["sketch"], 5)
    d = 2.2015  # the line's distance from the centre
    outer, inner = (segment_area(r, 2 * math.sqrt(r ** 2 - d ** 2)) for r in (14.5, 9.7))
    assert vol(slot) == pytest.approx((outer - inner) * 5, rel=1e-4)


def test_a_post_up_to_the_next_face_ends_on_its_curve(part):
    """A 5 x 10 post from a plane at z = 30 down onto a lying R10 cylinder: its
    end follows the curve, from 10 high at the crown to 9.68 at its sides,
    which no single depth can."""
    part.add_cylinder(20, 40)  # its axis along y, y 0..40
    plane = part.add_plane("front", offset_mm=30)["plane"]
    sketch = part.add_sketch(plane, [-2.5, 15], [{"line": [2.5, 15]}, {"line": [2.5, 25]}, {"line": [-2.5, 25]},
                                                 {"line": [-2.5, 15]}])
    before = vol(part.get_mass_properties())
    post = part.extrude_sketch(sketch["sketch"], up_to="next", reverse=True)
    under = 2 * (1.25 * math.sqrt(100 - 2.5 ** 2) + 50 * math.asin(0.25))  # the circle's height over x -2.5..2.5
    assert vol(post) - before == pytest.approx(10 * (30 * 5 - under), rel=1e-6)
    assert "depth" not in post["dimensions"], "an end up to a face has no depth to change"


def test_a_post_ends_on_the_face_through_a_point(part):
    """Up to next failed for a strip with a side on a boss; an end on a
    chosen face does not depend on what lies next. A 40 x 20 x 10 block with
    a step up to z = 20 over x 20..40; a 5 x 5 post from z = 30 over the low
    half ends on the step's plane (z = 20), not on the block below (z = 10)."""
    part.add_box(40, 20, 10)
    part.add_extruded_profile([[20, 0], [40, 0], [40, 20], [20, 20]], 20, name="Step")
    plane = part.add_plane("front", offset_mm=30)["plane"]
    sketch = part.add_sketch(plane, [5, 5], [{"line": [10, 5]}, {"line": [10, 10]}, {"line": [5, 10]},
                                             {"line": [5, 5]}])
    before = vol(part.get_mass_properties())
    post = part.extrude_sketch(sketch["sketch"], up_to="@30, 10, 20", reverse=True)
    assert vol(post) - before == pytest.approx(25 * 10), "the post did not end on the chosen face"
    with pytest.raises(SolidWorksError, match="nearest lies"):
        part.extrude_sketch(sketch["sketch"], up_to="@30, 10, 21", reverse=True)
