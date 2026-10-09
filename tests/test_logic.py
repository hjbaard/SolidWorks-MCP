"""Pure unit tests for SolidWorksSession's no-COM helpers.

These cover the fiddly logic added during the toolset build (selector parsing,
direction parsing, axis classification, polygon cleaning) without SolidWorks.
"""

import math

import pythoncom
import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.session import SolidWorksSession
from solidworks_mcp.sketch_constraints import MAX_DIMENSIONED_VERTICES


@pytest.fixture
def s():
    return SolidWorksSession()  # not connected; only pure helpers are exercised


def test_parse_edge_indices_axis_and_all(s):
    assert s._parse_edge_indices("all") is None
    assert s._parse_edge_indices("x") is None
    assert s._parse_edge_indices("z") is None


def test_parse_edge_indices_lists(s):
    assert s._parse_edge_indices("2,5") == [2, 5]
    assert s._parse_edge_indices("2 5 7") == [2, 5, 7]
    assert s._parse_edge_indices([1, 3]) == [1, 3]


def test_parse_direction(s):
    assert s._parse_direction("+z") == (0.0, 0.0, 1.0)
    assert s._parse_direction("-x") == (-1.0, 0.0, 0.0)
    assert s._parse_direction("+Y") == (0.0, 1.0, 0.0)


def test_parse_direction_invalid(s):
    with pytest.raises(SolidWorksError):
        s._parse_direction("up")


def test_axis_of(s):
    assert s._axis_of(10, 0, 0, 10) == "x"
    assert s._axis_of(0, 5, 0, 5) == "y"
    assert s._axis_of(0, 0, -5, 5) == "z"
    assert s._axis_of(1, 1, 0, 2 ** 0.5) is None   # diagonal
    assert s._axis_of(0, 0, 0, 0) is None           # zero length


def test_clean_polygon_open_ring():
    pts = SolidWorksSession._clean_polygon([[0, 0], [40, 0], [40, 20]])
    assert pts == [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0)]


def test_clean_polygon_drops_explicit_closing_point():
    pts = SolidWorksSession._clean_polygon([[0, 0], [40, 0], [40, 20], [0, 0]])
    assert pts == [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0)]


def test_clean_polygon_dedupes_consecutive():
    pts = SolidWorksSession._clean_polygon([[0, 0], [0, 0], [40, 0], [40, 20]])
    assert pts == [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0)]


def test_clean_polygon_too_few_distinct():
    with pytest.raises(SolidWorksError):
        SolidWorksSession._clean_polygon([[0, 0], [40, 0]])
    with pytest.raises(SolidWorksError):
        SolidWorksSession._clean_polygon([[0, 0], [0, 0], [0, 0]])


def test_clean_polygon_takes_3d_points_too():
    # the on-face tools check their corners on the 3D points, before a sketch opens
    pts = SolidWorksSession._clean_polygon([[0, 0, 5], [40, 0, 5], [40, 0, 5], [40, 20, 5], [0, 0, 5]])
    assert pts == [(0.0, 0.0, 5.0), (40.0, 0.0, 5.0), (40.0, 20.0, 5.0)]


def test_corner_groups_share_one_dimension_per_radius():
    groups = SolidWorksSession._corner_groups
    assert groups(None, 4) == []
    assert groups(5, 4) == [(5.0, [0, 1, 2, 3])]
    assert groups([5, 0, 3, 5], 4) == [(5.0, [0, 3]), (3.0, [2])], "same radius, same group; 0 stays sharp"


def test_corner_groups_reject_a_list_that_does_not_fit_the_profile():
    with pytest.raises(SolidWorksError, match="3 values for 4 corners"):
        SolidWorksSession._corner_groups([5, 5, 5], 4)
    with pytest.raises(SolidWorksError, match=">= 0"):
        SolidWorksSession._corner_groups([5, -1, 5, 5], 4)
    with pytest.raises(SolidWorksError, match=str(MAX_DIMENSIONED_VERTICES)):  # such profiles are fixed, not dimensioned
        SolidWorksSession._corner_groups(1, MAX_DIMENSIONED_VERTICES + 6)


def test_corner_radii_must_leave_some_straight_edge():
    check = SolidWorksSession._check_corner_radii
    rectangle = [(0, 0), (40, 0), (40, 20), (0, 20)]
    check(rectangle, [(9.9, [0, 1, 2, 3])])  # the 20 mm sides keep 0.2 mm straight
    with pytest.raises(SolidWorksError, match="Edge 1-2 is 20 mm long"):
        check(rectangle, [(10, [0, 1, 2, 3])])  # two R10 arcs eat the whole 20 mm side


def test_radii_whose_arcs_would_meet_point_to_the_disc_and_slot_tools():
    """R10 on a 20 mm square was refused without a way out; SolidWorks merges
    arcs that meet, which the corners cannot keep, but a disc or a slot is
    exactly that shape."""
    check = SolidWorksSession._check_corner_radii
    rectangle = [(0, 0), (40, 0), (40, 20), (0, 20)]
    with pytest.raises(SolidWorksError, match="add_disc, for round ends add_extruded_slot, for any outline add_sketch"):
        check(rectangle, [(10, [0, 1, 2, 3])])
    with pytest.raises(SolidWorksError, match=r"Use smaller radii\.$"):
        check(rectangle, [(12, [0, 1, 2, 3])])  # past meeting: just too big


def test_corner_radius_setback_follows_the_corner_angle():
    # a 60 degree corner sets the arc back r / tan(30 deg) = 1.732 r along each edge
    triangle = [(0, 0), (10, 0), (5, 5 * 3 ** 0.5)]
    SolidWorksSession._check_corner_radii(triangle, [(2.8, [0, 1, 2])])  # 2 * 4.85 < 10
    with pytest.raises(SolidWorksError, match="Edge 0-1"):
        SolidWorksSession._check_corner_radii(triangle, [(3, [0, 1, 2])])  # 2 * 5.196 > 10


def test_a_straight_corner_cannot_be_rounded():
    with pytest.raises(SolidWorksError, match="Corner 1 .*in line"):
        SolidWorksSession._check_corner_radii([(0, 0), (20, 0), (40, 0), (40, 20)], [(2, [1])])


def test_corner_radii_work_on_3d_points():
    # the same rectangle on the x = 40 face: lengths and angles do not change
    side = [(40, 0, 0), (40, 20, 0), (40, 20, 10), (40, 0, 10)]
    with pytest.raises(SolidWorksError, match="Edge 1-2 is 10 mm long"):
        SolidWorksSession._check_corner_radii(side, [(5, [0, 1, 2, 3])])


def test_round_polyline_straight_is_single_line():
    segs = SolidWorksSession._round_polyline([[0, 0], [50, 0]], 0)
    assert segs == [("line", (0.0, 0.0), (50.0, 0.0))]


def test_round_polyline_corner_needs_radius():
    with pytest.raises(SolidWorksError):
        SolidWorksSession._round_polyline([[0, 0], [30, 0], [30, 30]], 0)


def test_round_polyline_radius_too_large():
    with pytest.raises(SolidWorksError):
        SolidWorksSession._round_polyline([[0, 0], [10, 0], [10, 10]], 50)


def _close(a, b):
    return all(abs(x - y) < 1e-9 for x, y in zip(a, b))


def test_round_polyline_L_bend_geometry():
    # 90-degree corner at (30,0): tangent points at (20,0) and (30,10), arc centre (20,10)
    segs = SolidWorksSession._round_polyline([[0, 0], [30, 0], [30, 30]], 10)
    assert segs[0][0] == "line" and _close(segs[0][1], (0, 0)) and _close(segs[0][2], (20, 0))
    kind, c, p1, p2, direction = segs[1]
    assert kind == "arc" and direction == 1
    assert _close(c, (20, 10)) and _close(p1, (20, 0)) and _close(p2, (30, 10))
    assert segs[2][0] == "line" and _close(segs[2][1], (30, 10)) and _close(segs[2][2], (30, 30))


def test_round_polyline_collinear_points_no_arc():
    # a straight run expressed as 3 collinear points -> no fillet, just lines
    segs = SolidWorksSession._round_polyline([[0, 0], [25, 0], [50, 0]], 10)
    assert all(s[0] == "line" for s in segs)


def test_round_polyline_R_bend_geometry():
    # 90-degree RIGHT turn at (30,0): pins the direction == -1 (CW) branch
    segs = SolidWorksSession._round_polyline([[0, 0], [30, 0], [30, -30]], 10)
    assert segs[0][0] == "line" and _close(segs[0][2], (20, 0))
    kind, c, p1, p2, direction = segs[1]
    assert kind == "arc" and direction == -1
    assert _close(c, (20, -10)) and _close(p1, (20, 0)) and _close(p2, (30, -10))
    assert segs[2][0] == "line" and _close(segs[2][1], (30, -10)) and _close(segs[2][2], (30, -30))


def test_round_polyline_obtuse_corner_geometry():
    # 135-degree corner: setback = r/tan(67.5deg) = 4.14 != r, pins the angle convention
    segs = SolidWorksSession._round_polyline([[0, 0], [100, 0], [200, 100]], 10)
    assert segs[0][0] == "line" and _close(segs[0][2], (95.857864376269, 0.0))
    kind, c, p1, p2, direction = segs[1]
    assert kind == "arc" and direction == 1
    assert _close(c, (95.857864376269, 10.0))
    assert _close(p1, (95.857864376269, 0.0))
    assert _close(p2, (102.928932188135, 2.928932188135))


def test_round_polyline_acute_corner_geometry():
    # sharp ~26.6-degree corner: setback = 42.36 > r, exposes half-angle errors
    segs = SolidWorksSession._round_polyline([[0, 0], [100, 0], [0, 50]], 10)
    kind, c, p1, p2, direction = segs[1]
    assert kind == "arc" and direction == 1
    assert _close(c, (57.639320225002, 10.0))
    assert _close(p1, (57.639320225002, 0.0))
    assert _close(p2, (62.111456180002, 18.944271909999))


def test_round_polyline_S_shape_chains_two_fillets():
    # Z/S path with two opposite 90-deg corners: proves consecutive fillets chain,
    # the connecting straight carries the previous t_out, and both arc dirs appear.
    segs = SolidWorksSession._round_polyline([[0, 0], [40, 0], [40, 40], [80, 40]], 10)
    assert [s[0] for s in segs] == ["line", "arc", "line", "arc", "line"]
    assert _close(segs[0][1], (0, 0)) and _close(segs[0][2], (30, 0))
    _, c1, p1a, p1b, d1 = segs[1]
    assert d1 == 1 and _close(c1, (30, 10)) and _close(p1a, (30, 0)) and _close(p1b, (40, 10))
    assert _close(segs[2][1], (40, 10)) and _close(segs[2][2], (40, 30))
    _, c2, p2a, p2b, d2 = segs[3]
    assert d2 == -1 and _close(c2, (50, 30)) and _close(p2a, (40, 30)) and _close(p2b, (50, 40))
    assert _close(segs[4][1], (50, 40)) and _close(segs[4][2], (80, 40))


def test_round_polyline_two_points_radius_ignored():
    # a straight 2-point path ignores a positive radius (no corner -> no guard)
    segs = SolidWorksSession._round_polyline([[0, 0], [50, 0]], 10)
    assert segs == [("line", (0.0, 0.0), (50.0, 0.0))]


def test_round_polyline_overlapping_fillets_rejected():
    # two 90-deg corners share a 5mm segment; setback=3 fits each leg alone but
    # 3+3 > 5 -> the fillets overlap and must fail fast (not reach the sweep).
    with pytest.raises(SolidWorksError):
        SolidWorksSession._round_polyline([[0, 0], [10, 0], [10, 5], [0, 5]], 3)


def test_round_polyline_foldback_raises():
    # path doubling back over itself (180-deg fold) must fail fast, not be flattened
    with pytest.raises(SolidWorksError):
        SolidWorksSession._round_polyline([[0, 0], [50, 0], [10, 0]], 5)


def test_path_starts_along_x_ok():
    # origin + first segment heading +X -> no raise (collinear extra point allowed)
    SolidWorksSession._require_path_starts_along_x([[0, 0], [40, 0], [40, 30]])


def test_path_starts_along_x_not_origin_raises():
    with pytest.raises(SolidWorksError):
        SolidWorksSession._require_path_starts_along_x([[5, 0], [40, 0]])


def test_path_starts_along_x_wrong_direction_raises():
    with pytest.raises(SolidWorksError):
        SolidWorksSession._require_path_starts_along_x([[0, 0], [0, 40]])  # heads +Y


def test_path_starts_along_x_too_few_points_raises():
    with pytest.raises(SolidWorksError):
        SolidWorksSession._require_path_starts_along_x([[0, 0]])


def test_rib_material_side_follows_the_toward_point():
    """SolidWorks grows a rib to the RIGHT of start->end unless reversed; the
    toward point must decide, whichever order the agent gives the ends in."""
    corner = (5, 5)
    assert SolidWorksSession._rib_material_reversed((5, 35), (35, 5), corner) is False
    assert SolidWorksSession._rib_material_reversed((35, 5), (5, 35), corner) is True


def test_rib_toward_point_on_the_line_raises():
    with pytest.raises(SolidWorksError):
        SolidWorksSession._rib_material_reversed((0, 0), (10, 10), (5, 5))


def test_rib_zero_length_raises():
    with pytest.raises(SolidWorksError):
        SolidWorksSession._rib_material_reversed((3, 3), (3, 3), (0, 0))


def test_mirror_with_an_empty_feature_list_raises(s):
    # an empty list is not "the body": mirroring the whole part by accident
    # would double it without the agent having asked for that
    with pytest.raises(SolidWorksError, match="leave it out"):
        s.add_mirror("right", features=[])


def test_thread_size_parses_diameter_and_pitch():
    assert SolidWorksSession._parse_thread_size("M10x1.5") == (10.0, 1.5)
    assert SolidWorksSession._parse_thread_size("M3x0.5") == (3.0, 0.5)
    assert SolidWorksSession._parse_thread_size("M10x1.0") == (10.0, 1.0)


@pytest.mark.parametrize("size", ["M10", "10x1.5", "M10x", "M10 x 1.5", ""])
def test_thread_size_malformed_raises(size):
    with pytest.raises(SolidWorksError):
        SolidWorksSession._parse_thread_size(size)


def test_thread_minor_diameter_is_the_iso_basic_value():
    # ISO 724: D1 = D - 1.082532 * P, so M10x1.5 -> 8.376 and M3x0.5 -> 2.459
    assert abs(SolidWorksSession._thread_minor_diameter(10, 1.5) - 8.376202) < 1e-5
    assert abs(SolidWorksSession._thread_minor_diameter(3, 0.5) - 2.458734) < 1e-5


# --- assembly placement maths (M6) -------------------------------------------


def test_parse_face_selector_defaults_to_outer(s):
    assert s._parse_face_selector("+z") == ((0.0, 0.0, 1.0), "outer")
    assert s._parse_face_selector("-X") == ((-1.0, 0.0, 0.0), "outer")


def test_parse_face_selector_inner_suffix(s):
    assert s._parse_face_selector("+y:inner") == ((0.0, 1.0, 0.0), "inner")
    assert s._parse_face_selector(" -z : OUTER ") == ((0.0, 0.0, -1.0), "outer")


def test_parse_face_selector_without_side_can_leave_the_choice_to_a_point(s):
    # tools that get a point on the face pick the face through that point
    assert s._parse_face_selector("+z", default_side=None) == ((0.0, 0.0, 1.0), None)
    assert s._parse_face_selector("+z:inner", default_side=None) == ((0.0, 0.0, 1.0), "inner")


def test_wizard_values_follow_what_solidworks_accepts():
    """HoleWizard5's twelve values were found by trial; these pin the findings.
    A through plain hole fails with -1 in the unused slots (it needs 0), a
    tapped hole given zeros cuts garbage, and a tapped hole without its
    cosmetic thread mills the threaded length at the major diameter."""
    values = SolidWorksSession._wizard_values
    assert values("clearance", 1, True) == [1.0] + [0.0] * 11
    assert values("clearance", 2, False) == [2.0] + [-1.0] * 11
    assert values("counterbore", 0, True)[3] == 0.0, "the screw fit is Value4 for a counterbore"
    tapped = values("tapped", 1, True)
    assert tapped[6] == 1.0, "a tapped hole must keep its cosmetic thread (Value7)"
    assert (tapped[7], values("tapped", 1, False)[7]) == (1.0, 0.0), "thread end follows the hole's end"
    assert all(len(values(k, 1, t)) == 12 for k in ("clearance", "counterbore", "countersink", "tapped")
               for t in (True, False))


def test_parse_face_selector_rejects_unknown_side(s):
    with pytest.raises(SolidWorksError):
        s._parse_face_selector("+z:middle")


def test_parse_face_selector_rejects_unknown_direction(s):
    with pytest.raises(SolidWorksError):
        s._parse_face_selector("up:inner")


def test_rotation_columns_identity():
    assert SolidWorksSession._rotation_columns(0, 0, 0) == pytest.approx(
        [1, 0, 0, 0, 1, 0, 0, 0, 1], abs=1e-12)


def test_rotation_columns_are_column_major():
    # Ry(+90) maps (x,y,z) -> (z,y,-x). SolidWorks reads ArrayData COLUMN-major,
    # so the array is the TRANSPOSE of the matrix written out row by row -- this
    # is the value SolidWorks actually returned for a 90-degree turned component.
    assert SolidWorksSession._rotation_columns(0, 90, 0) == pytest.approx(
        [0, 0, -1, 0, 1, 0, 1, 0, 0], abs=1e-12)


def test_rotation_columns_apply_x_then_y_then_z():
    # R = Rz*Ry*Rx: rotating 90 deg about X then 90 deg about Z sends
    # (1,0,0) -> (0,1,0) and (0,1,0) -> (0,0,1), which pins the order.
    columns = SolidWorksSession._rotation_columns(90, 0, 90)
    rows = [[columns[c * 3 + r] for c in range(3)] for r in range(3)]

    def apply(v):
        return [sum(rows[r][c] * v[c] for c in range(3)) for r in range(3)]

    assert apply([1, 0, 0]) == pytest.approx([0, 1, 0], abs=1e-12)
    assert apply([0, 1, 0]) == pytest.approx([0, 0, 1], abs=1e-12)


@pytest.mark.parametrize("angles", [(0, 0, 0), (90, 0, 0), (0, 0, -45),
                                    (10, 20, 30), (-120, 35, 170)])
def test_euler_round_trip(angles):
    columns = SolidWorksSession._rotation_columns(*angles)
    assert SolidWorksSession._euler_from_columns(columns) == pytest.approx(angles, abs=1e-6)


def test_euler_gimbal_lock_still_reproduces_the_matrix():
    # at ry = 90 deg the X and Z rotations are the same motion; we report rz = 0
    # and fold everything into rx, which must rebuild the identical matrix.
    columns = SolidWorksSession._rotation_columns(30, 90, 20)
    rx, ry, rz = SolidWorksSession._euler_from_columns(columns)
    assert rz == 0.0 and ry == pytest.approx(90.0, abs=1e-9)
    assert SolidWorksSession._rotation_columns(rx, ry, rz) == pytest.approx(columns, abs=1e-9)


# The session below is not connected: a check that came after the first COM
# call would fail with "No active document" instead of naming the bad input.


def test_screenshot_rejects_an_unknown_view(s):
    with pytest.raises(SolidWorksError, match="Unknown view 'side'"):
        s.screenshot("shot.png", view="side")


def test_screenshot_refuses_bmp_which_solidworks_does_not_write(s):
    """SaveAs3 to .bmp returned 256 and wrote nothing: say so before trying."""
    with pytest.raises(SolidWorksError, match="'bmp' is not supported"):
        s.screenshot("shot.bmp")


@pytest.mark.parametrize("zoom", [[[0, 0, 0]], [[0, 0], [1, 1]], [[0, 0, 0], [1, 1, 1], [2, 2, 2]]])
def test_screenshot_zoom_needs_two_3d_corners(s, zoom):
    with pytest.raises(SolidWorksError, match="two corners"):
        s.screenshot("shot.png", zoom_mm=zoom)


def test_a_material_of_its_own_is_one_material_with_its_density():
    import xml.etree.ElementTree as ET
    root = ET.fromstring(SolidWorksSession._material_xml('TPU "95A" & more', 1210.5))
    [material] = root.iter("material")
    assert material.get("name") == 'TPU "95A" & more', "the name must survive XML intact"
    assert material.find("physicalproperties/DENS").get("value") == "1210.5"


def test_set_material_takes_a_database_or_a_density_not_both(s):
    with pytest.raises(SolidWorksError, match="not both"):
        s.set_material("TPU", database="mine.sldmat", density_kg_m3=1210)


class _GoneSolidWorks:
    """A link into a SolidWorks that was restarted: every call fails."""

    def __init__(self, hresult):
        self.hresult = hresult

    def __getattr__(self, name):
        raise pythoncom.com_error(self.hresult, "De RPC-server is niet beschikbaar.", None, None)


class _RunningSolidWorks:
    CommandInProgress = False


def test_a_restarted_solidworks_is_attached_again(s, monkeypatch):
    """After SolidWorks was restarted the old link answered every call with
    'RPC server unavailable', until the MCP server itself was restarted. The
    call now runs on the new SolidWorks; the old document went with the old one."""
    s._sw, s._model = _GoneSolidWorks(-2147023174), _GoneSolidWorks(-2147023174)
    monkeypatch.setattr(s, "connect", lambda: setattr(s, "_sw", _RunningSolidWorks()))
    assert s.run_guarded(lambda: "ran") == "ran"
    assert isinstance(s._sw, _RunningSolidWorks) and s._model is None


def test_other_com_errors_do_not_attach_again(s, monkeypatch):
    s._sw = _GoneSolidWorks(-2147467259)  # E_FAIL: SolidWorks is there, the call failed
    monkeypatch.setattr(s, "connect", lambda: pytest.fail("attached again on an ordinary COM error"))
    with pytest.raises(pythoncom.com_error):
        s.run_guarded(lambda: "ran")


@pytest.mark.parametrize("from_dir,orientation", [
    ((1, 1, 1), (0.70711, -0.40825, 0.57735, 0, 0.8165, 0.57735, -0.70711, -0.40825, 0.57735)),  # SolidWorks' iso
    ((0, -3, 0), (1, 0, 0, 0, 0, -1, 0, 1, 0)),  # its bottom view: +z up
    ((0, 1, 0), (1, 0, 0, 0, 0, 1, 0, -1, 0)),  # its top view: -z up
], ids=["iso", "bottom", "top"])
def test_a_view_from_a_direction_matches_solidworks_own(from_dir, orientation):
    """The columns are the screen's x, y and z; the model's +y stays up where it can."""
    assert SolidWorksSession._view_rotation(from_dir) == pytest.approx(orientation, abs=1e-5)


def test_a_view_from_a_free_direction_looks_from_it_with_y_up():
    x, y, z = (SolidWorksSession._view_rotation((-1, 1, -1))[col::3] for col in range(3))
    assert z == pytest.approx([-3 ** -0.5, 3 ** -0.5, -3 ** -0.5]), "the screen's z must point at the viewer"
    assert y[1] > 0 and sum(a * b for a, b in zip(x, y)) == pytest.approx(0, abs=1e-12)


def test_screenshot_needs_a_direction_to_look_from(s):
    with pytest.raises(SolidWorksError, match="from_dir"):
        s.screenshot("shot.png", from_dir=[0, 0, 0])


@pytest.mark.parametrize("kwargs,match", [
    ({"depth_mm": 5, "up_to": "next"}, "not both"),
    ({}, "depth_mm or up_to"),
    ({"up_to": "wall"}, "Unknown up_to 'wall'"),
], ids=["both", "neither", "unknown"])
def test_extrude_sketch_ends_at_a_depth_or_the_next_face(s, kwargs, match):
    with pytest.raises(SolidWorksError, match=match):
        s.extrude_sketch("Sketch1", **kwargs)


def test_a_face_by_a_point_on_it_reads_three_numbers():
    assert SolidWorksSession._point_selector("@0, -2.5, 20.2") == [0.0, -2.5, 20.2]
    for wrong in ("@1,2", "@a,b,c", "@"):
        with pytest.raises(SolidWorksError, match="@x,y,z"):
            SolidWorksSession._point_selector(wrong)


def test_an_appearance_changes_only_what_is_given():
    """Making a component see-through must not reset the colour set before,
    nor the shine SolidWorks keeps in the same nine values."""
    current = [0.1, 0.2, 0.3, 0.9, 0.8, 0.5, 0.3, 0.0, 0.0]
    clear = SolidWorksSession._appearance_values(current, None, 0.6)
    assert clear == [0.1, 0.2, 0.3, 0.9, 0.8, 0.5, 0.3, 0.6, 0.0]
    grey = SolidWorksSession._appearance_values(current, [204, 204, 204], None)
    assert grey == pytest.approx([0.8, 0.8, 0.8, 0.9, 0.8, 0.5, 0.3, 0.0, 0.0])
    for rgb, transparency in (([300, 0, 0], None), ([1, 2], None), (None, 1.5)):
        with pytest.raises(SolidWorksError):
            SolidWorksSession._appearance_values(current, rgb, transparency)


def test_a_zoom_region_turns_into_the_bottom_view():
    # from below the screen's x is the model's x, its y the model's z, and the
    # viewer looks along +y; the bottom view's Orientation3, as SolidWorks gives it
    bottom = (1, 0, 0, 0, 0, -1, 0, 1, 0)
    low, high = SolidWorksSession._box_along_screen([[70, -100, -24], [112, -70, 24]], bottom)
    assert (low, high) == ([70, -24, 70], [112, 24, 100])


class _FakePart:
    """Just enough of an IComponent2 part for the suppression checks."""

    Name2 = "Arm-1"

    def __init__(self, state):
        self._state = state

    def GetChildren(self):
        return ()

    def GetSuppression2(self):
        return self._state

    def GetBodies2(self, body_type):
        return None  # lightweight or suppressed: no geometry loaded


@pytest.mark.parametrize("state", [1, 4])  # swComponentLightweight, swComponentFullyLightweight
def test_a_lightweight_component_is_not_measured_as_empty(s, state):
    """Its geometry is not loaded, so it has no bodies: skipping it would
    shrink the assembly's box without a word."""
    with pytest.raises(SolidWorksError, match="'Arm-1' is lightweight"):
        s._component_box(_FakePart(state))


def test_a_suppressed_component_has_no_box(s):
    assert s._component_box(_FakePart(0)) is None


@pytest.mark.parametrize("target", [{}, {"component_b": "B-1", "point_mm": [0, 0, 0]},
                                    {"point_mm": [0, 0, 0], "axis_mm": [[0, 0, 0], [0, 0, 1]]}])
def test_measure_distance_needs_exactly_one_target(s, target):
    with pytest.raises(SolidWorksError, match="Give one of component_b, point_mm or axis_mm"):
        s.measure_distance("A-1", **target)


@pytest.mark.parametrize("axis", [[[0, 0, 0], [0, 0, 0]], [[0, 0], [0, 0, 1]], "z"])
def test_an_axis_is_a_point_and_a_direction(s, axis):
    with pytest.raises(SolidWorksError, match="axis_mm needs"):
        s.measure_distance("A-1", axis_mm=axis)


def test_measure_distance_point_needs_three_coordinates(s):
    with pytest.raises(SolidWorksError, match=r"\[x, y, z\]"):
        s.measure_distance("A-1", point_mm=[0, 0])


@pytest.mark.parametrize("call", [lambda s: s.extrude_sketch("Sketch1", 0),
                                  lambda s: s.cut_sketch("Sketch1", -2)])
def test_sketch_features_need_a_positive_depth(s, call):
    with pytest.raises(SolidWorksError, match="depth must be > 0"):
        call(s)


@pytest.mark.parametrize("selector,index", [("#5", 5), ("5", 5), (" #12 ", 12), ("+x", None),
                                            ("-z:inner", None), ("#x", None), ("#-1", None)])
def test_a_face_is_picked_by_index_or_by_direction(selector, index):
    assert SolidWorksSession._face_index(selector) == index


@pytest.mark.parametrize("angle", [0, 180, -30, 200])
def test_an_angle_mate_takes_an_angle_strictly_between_0_and_180(s, angle):
    with pytest.raises(SolidWorksError, match="between 0 and 180"):
        s.add_mate("Thigh-1", "-y", "Shin-1", "-y", mate_type="angle", angle_deg=angle)


@pytest.mark.parametrize("values,distances,message", [([], None, "values to step through"),
                                                      ([30, 60], [["Rod-1"]], r"pairs \[\[a, b\]")])
def test_check_motion_needs_values_and_pairs(s, values, distances, message):
    with pytest.raises(SolidWorksError, match=message):
        s.check_motion("D1@Angle1", values, distances)


def test_keep_inside_cuts_through_all(s):
    with pytest.raises(SolidWorksError, match="keep_inside cuts through all"):
        s.cut_profile_through_plane([[0, 0, 0], [0, 1, 0], [0, 0, 1]], "right", depth_mm=5, keep_inside=True)


@pytest.mark.parametrize("rim,depth,message", [(0, 3, "rim must be > 0"), (2, -1, "depth must be > 0")])
def test_an_offset_pocket_needs_a_rim_and_a_depth(s, rim, depth, message):
    with pytest.raises(SolidWorksError, match=message):
        s.cut_offset_pocket("+z", 10, 10, 10, rim, depth)


class _FakeDimension:
    def __init__(self, param_type):
        self._type = param_type

    def GetType(self):
        return self._type


def test_an_angle_dimension_is_written_in_radians():
    """set_dimension wrote every value as millimetres: 180 degrees landed as
    0.18 radians, about 10 degrees."""
    unit, to_system, from_system = SolidWorksSession._dimension_unit(_FakeDimension(1))  # angular
    assert (unit, to_system(180)) == ("deg", pytest.approx(math.pi))
    unit, to_system, from_system = SolidWorksSession._dimension_unit(_FakeDimension(0))  # linear
    assert (unit, to_system(25)) == ("mm", pytest.approx(0.025))


# --- MCP wiring ---------------------------------------------------------------


def _tool_delegations():
    """Every MCP tool as (tool name, its parameter names, the _call arguments).

    Parsed from the source rather than imported: importing the server module
    would start a COM worker thread, which this pure layer must not need.
    """
    import ast
    import pathlib

    source = pathlib.Path("src/solidworks_mcp/server.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if not isinstance(node, ast.AsyncFunctionDef) or not node.decorator_list:
            continue
        call = next(n for n in ast.walk(node)
                    if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_call")
        target, *passed = call.args
        yield (node.name,
               [a.arg for a in node.args.args],
               target.attr,
               [getattr(a, "id", None) for a in passed])


@pytest.mark.parametrize("tool,params,target,passed", list(_tool_delegations()))
def test_tool_forwards_its_arguments_in_order(tool, params, target, passed):
    """A tool must hand the session method its own parameters, in order.

    _call forwards positionally, so a reordered or dropped argument would send
    the wrong value to SolidWorks and only show up as strange geometry.
    """
    import inspect

    method = getattr(SolidWorksSession, target, None)
    assert method is not None, f"{tool} delegates to a session method that does not exist"
    assert passed == params, f"{tool} forwards {passed} but takes {params}"
    accepted = list(inspect.signature(method).parameters)[1:]  # drop self
    assert params == accepted[:len(params)], f"{tool} does not match {target}{tuple(accepted)}"


# --- 3D printing and drawings (pure parts) --------------------------------------


@pytest.mark.parametrize("spacing", [30, 7.5, 2])
def test_triangle_samples_cover_the_whole_triangle(spacing):
    a, b, c = (0, 0, 0), (10, 0, 0), (0, 10, 0)
    points = SolidWorksSession._triangle_samples(a, b, c, spacing)
    assert all(x >= 0 and y >= 0 and x + y <= 10 + 1e-9 and z == 0 for x, y, z in points), "a sample left the triangle"
    if spacing < 10:  # every spot, corners too, lies within the spacing of a sample
        spots = [(x / 4, y / 4, 0) for x in range(41) for y in range(41 - x)]
        assert max(min(math.dist(s, p) for p in points) for s in spots) <= spacing


def test_a_sliver_triangle_gets_samples_for_its_size():
    """A curved face tessellates into long slivers; this one is 67.6 mm long and
    0.2 mm wide. Divided by its longest edge it got 7225 samples, and the thin-
    wall check on a cable cover fired over a million rays and hung SolidWorks.
    It needs one row along its length."""
    points = SolidWorksSession._triangle_samples((0, 0, 0), (67.6, 0, 0), (30, 0.2, 0), 0.8)
    assert len(points) <= 67.6 / 0.8 + 1, f"{len(points)} samples for a 67.6 x 0.2 mm sliver"
    assert max(p[0] for p in points) - min(p[0] for p in points) > 60, "the row does not run its length"


def test_wall_samples_never_exceed_the_ray_limit():
    # 400 000 tiny triangles: one sample each would already be four times the limit
    tiny = [(0, (x, 0, 0), (x + 0.01, 0, 0), (x, 0.01, 0), [0, 0, 1]) for x in range(400_000)]
    samples, spacing = SolidWorksSession._wall_samples(tiny, 0.8)
    assert len(samples) <= SolidWorksSession._MAX_WALL_SAMPLES
    assert spacing > 0.8, "fewer samples than asked for must show in the spacing reported"


def _quad(index, corners, normal):
    """A flat quadrilateral as two (index, a, b, c, normal) triangles."""
    return [(index, corners[0], corners[k], corners[k + 1], list(normal)) for k in (1, 2)]


def test_a_ceiling_overhangs_and_the_bed_does_not(s):
    """The underside of an arm at z = 5 faces down, like the bed face at z = 0:
    only the arm needs support."""
    triangles = (_quad(0, [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)], (0, 0, -1))
                 + _quad(1, [(10, 0, 5), (14, 0, 5), (14, 10, 5), (10, 10, 5)], (0, 0, -1))
                 + _quad(2, [(0, 0, 10), (14, 0, 10), (14, 10, 10), (0, 10, 10)], (0, 0, 1)))
    found = s._overhangs(triangles, (0, 0, 1), 45)
    assert found["bed_contact_mm2"] == pytest.approx(100) and found["height_mm"] == pytest.approx(10)
    [arm] = found["overhang"]["faces"]
    assert (arm["index"], arm["area_mm2"], arm["worst_deg"]) == (1, pytest.approx(40), pytest.approx(90))
    assert arm["center_mm"] == pytest.approx([12, 5, 5])
    # built the other way up, the top rests on the bed and the old bed faces up
    assert s._overhangs(triangles, (0, 0, -1), 45)["overhang"]["faces"] == []


@pytest.mark.parametrize("arguments,message", [({"up": "up"}, "Unknown direction"),
                                               ({"overhang_deg": 90}, "between 0 and 90"),
                                               ({"min_wall_mm": 0}, "min_wall_mm must be > 0")])
def test_check_printability_checks_its_input(s, arguments, message):
    with pytest.raises(SolidWorksError, match=message):
        s.check_printability(**arguments)


def test_a_drawing_is_a_pdf_or_a_slddrw(s):
    with pytest.raises(SolidWorksError, match="pdf or slddrw"):
        s.make_drawing("part.png")


# --- revolving about any axis ---------------------------------------------------


def test_a_revolve_axis_is_two_distinct_points():
    assert SolidWorksSession._revolve_axis([[0, 0], [10, 10]]) == ((0.0, 0.0), (10.0, 10.0))
    with pytest.raises(SolidWorksError, match="coincide"):
        SolidWorksSession._revolve_axis([[5, 5], [5, 5]])
    with pytest.raises(SolidWorksError, match="two points"):
        SolidWorksSession._revolve_axis([[0, 0, 0]])


def test_the_offset_from_a_slanted_axis_tells_side_and_distance():
    offset = SolidWorksSession._offset_from_line
    assert offset((0, 0), (10, 10), (15, 5)) == pytest.approx(-math.sqrt(50))
    assert offset((0, 0), (10, 10), (5, 15)) == pytest.approx(math.sqrt(50))
    assert offset((0, 0), (10, 10), (20, 20)) == pytest.approx(0)


@pytest.mark.parametrize("profile,message", [
    ([[10, 0], [20, 0], [20, 10], [0, 10]], "crosses the axis"),   # (0, 10) lies across y = x
    ([[0, 0], [5, 5], [10, 10]], "entirely on the axis"),
    ([[10, 0], [20, 0], [20, 10], [10, 10]], r"Point\(s\) \[3\] touch the axis on their own"),  # a pinch
])
def test_a_profile_must_stay_on_one_side_of_its_axis(s, profile, message):
    with pytest.raises(SolidWorksError, match=message):
        s.add_revolved_profile(profile, axis_mm=[[0, 0], [10, 10]])


def test_a_corner_on_a_slanted_axis_cannot_be_rounded(s):
    # the cone's corner (10, 10) lies on the axis y = x, on the edge it turns about
    with pytest.raises(SolidWorksError, match=r"Corner\(s\) \[1\] lie on the axis"):
        s.add_revolved_profile([[0, 0], [10, 10], [20, 0]], axis_mm=[[0, 0], [10, 10]], corner_radii_mm=[0, 2, 0])


# --- sub-assemblies --------------------------------------------------------------

_IDENTITY = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
_TURN_Z_90 = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]  # rows: (x, y) -> (-y, x)


def test_a_part_in_a_turned_sub_assembly_is_seen_in_the_sub_assemblys_frame():
    """The sub-assembly sits turned 90 degrees at y = 100; its part sits 10 mm
    along the sub-assembly's x, so in the root it is turned too and at
    (0, 110, 0). Relative to the sub-assembly: no turn, 10 mm along x."""
    rotation, shift = SolidWorksSession._relative_frame((_TURN_Z_90, [0, 100, 0]), (_TURN_Z_90, [0, 110, 0]))
    assert sum(rotation, []) == pytest.approx(sum(_IDENTITY, [])) and shift == pytest.approx([10, 0, 0])


def test_a_turned_part_in_a_straight_sub_assembly_keeps_its_turn():
    rotation, shift = SolidWorksSession._relative_frame((_IDENTITY, [5, 5, 5]), (_TURN_Z_90, [5, 5, 5]))
    assert sum(rotation, []) == pytest.approx(sum(_TURN_Z_90, [])) and shift == pytest.approx([0, 0, 0])


def test_insert_component_takes_parts_and_sub_assemblies_only(s):
    with pytest.raises(SolidWorksError, match=r"\.sldprt or a \.sldasm"):
        s.insert_component("servo.step")


def test_a_profile_that_meets_the_y_axis_in_one_point_is_refused_too(s):
    """SolidWorks refuses a solid that pinches to a point on its axis; a whole
    edge on the axis (a cone) is fine."""
    with pytest.raises(SolidWorksError, match="touch the axis on their own"):
        s.add_revolved_profile([[0, 5], [5, 0], [10, 5], [5, 10]])  # a diamond touching r = 0 at (0, 5)


# --- planes at an angle, turned profiles ------------------------------------------


@pytest.mark.parametrize("normal,axis,angle,expected", [
    ((0, 0, 1), "y", 30, (0.5, 0, math.sqrt(3) / 2)),      # front turned about y: tips towards +x
    ((0, 0, 1), "x", 90, (0, -1, 0)),                       # front about x, a quarter turn: faces -y
    ((0, 1, 0), "z", 90, (-1, 0, 0)),                       # top about z
    ((1, 0, 0), "y", -90, (0, 0, 1)),                       # right about y, the other way
])
def test_a_turned_plane_follows_the_right_hand_rule(normal, axis, angle, expected):
    assert SolidWorksSession._turned_normal(normal, axis, angle) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("arguments,message", [
    ({}, "Give offset_mm or angle_deg"),
    ({"offset_mm": 5, "angle_deg": 30, "about": "y"}, "Give offset_mm or angle_deg"),
    ({"angle_deg": 30, "about": "w"}, "about must be"),
    ({"base": "front", "angle_deg": 30, "about": "z"}, "lies in the top and right planes"),
    ({"base": "top", "angle_deg": 180, "about": "x"}, "between -180 and 180"),
])
def test_add_plane_checks_its_input(s, arguments, message):
    with pytest.raises(SolidWorksError, match=message):
        s.add_plane(**arguments)


def test_points_turn_counterclockwise_about_a_pivot():
    turned = SolidWorksSession._turned_points([[30, 20], [20, 30]], 90, [20, 20])
    assert sum(turned, []) == pytest.approx([20, 30, 10, 20])
    assert SolidWorksSession._turned_points([[1, 2]], 0, None) == [[1, 2]]  # no turn, untouched
    with pytest.raises(SolidWorksError, match="about"):
        SolidWorksSession._turned_points([[1, 2]], 45, [5])


# --- drafts, round loft sections, bodies -------------------------------------------


@pytest.mark.parametrize("draft", [90, -90, 120])
def test_a_draft_stays_below_ninety_degrees(draft):
    with pytest.raises(SolidWorksError, match="between -90 and 90"):
        SolidWorksSession._check_draft(draft)


def test_a_round_loft_section_is_a_centre_and_a_diameter(s):
    assert s._loft_section({"center_mm": [5, -2], "diameter_mm": 20}) == {"center_mm": (5.0, -2.0), "diameter_mm": 20.0}
    with pytest.raises(SolidWorksError, match="diameter > 0"):
        s._loft_section({"center_mm": [0, 0], "diameter_mm": 0})
    with pytest.raises(SolidWorksError, match="A round section is"):
        s._loft_section({"centre": [0, 0], "diameter_mm": 10})
    assert len(s._loft_section([[0, 0], [10, 0], [10, 10]])) == 3  # a polygon stays a polygon


def test_a_loft_profile_starts_on_plus_x_or_at_its_first_vertex():
    """The start points line up across the profiles, so the loft does not twist."""
    assert SolidWorksSession._loft_start({"center_mm": (5.0, -2.0), "diameter_mm": 20.0}) == (15.0, -2.0)
    assert SolidWorksSession._loft_start([[3, 4], [10, 0], [10, 10]]) == (3, 4)


def test_combine_bodies_knows_add_subtract_and_common(s):
    with pytest.raises(SolidWorksError, match="operation must be one of"):
        s.combine_bodies("glue", "Body1")


# --- variable fillets, full rounds ---------------------------------------------------


def test_a_variable_fillet_gives_each_edge_end_its_radius():
    """Ends without a point keep the fillet's own radius."""
    ends = [(0.0, 0.0, 10.0), (40.0, 0.0, 10.0), (0.0, 20.0, 10.0)]
    assert SolidWorksSession._vertex_radii(ends, [[0, 20, 10, 6], [40, 0, 10, 4]], 2) == [2, 4, 6]


@pytest.mark.parametrize("radii_at,match", [
    ([[20, 0, 10, 5]], r"No end of the edges at \(20, 0, 10\).*\(0, 0, 10\), \(40, 0, 10\)"),
    ([[0, 0, 10, 5], [0, 0, 10.0001, 6]], "twice"),
    ([[0, 0, 10, 0]], "radius > 0"),
    ([[0, 0, 10]], r"\[x, y, z, radius\]"),
], ids=["off the ends", "twice", "zero", "short"])
def test_a_variable_fillet_refuses_radii_it_cannot_place(radii_at, match):
    with pytest.raises(SolidWorksError, match=match):
        SolidWorksSession._vertex_radii([(0.0, 0.0, 10.0), (40.0, 0.0, 10.0)], radii_at, 2)


def test_variable_fillet_radii_are_named_like_solidworks_does():
    """SolidWorks numbers them in the order of the radii it got (verified up to 12)."""
    assert [SolidWorksSession._vertex_radius_dimension(k) for k in (0, 1, 2, 10, 11)] == \
        ["D0", "D01", "D02", "D010", "D011"]


def test_a_full_round_takes_the_closest_pair_of_opposite_sides():
    """A rib 10 wide and 40 long: across its width, not along its length."""
    sides = [((0, -1, 0), (20, 0, 15)), ((0, 1, 0), (20, 40, 15)), ((-1, 0, 0), (15, 20, 15)), ((1, 0, 0), (25, 20, 15))]
    assert SolidWorksSession._full_round_sides(sides) == (2, 3, 10.0)


def test_a_full_round_needs_one_closest_pair_of_opposite_sides():
    square = [((-1, 0, 0), (0, 20, 5)), ((1, 0, 0), (40, 20, 5)), ((0, -1, 0), (20, 0, 5)), ((0, 1, 0), (20, 40, 5))]
    with pytest.raises(SolidWorksError, match="equally far apart"):
        SolidWorksSession._full_round_sides(square)
    with pytest.raises(SolidWorksError, match="two opposite flat side faces"):
        SolidWorksSession._full_round_sides([((-1, 0, 0), (0, 0, 0)), ((0, 1, 0), (0, 40, 0))])


# --- equations -----------------------------------------------------------------------


@pytest.mark.parametrize("equation,lhs", [('"L_thigh" = 85', "L_thigh"), ('"D1@Sketch1"=2*"L"', "D1@Sketch1"),
                                          ('  "shin_stretch" = ("L_shin" - 56) / 54', "shin_stretch")])
def test_an_equation_is_named_by_its_left_hand_side(equation, lhs):
    assert SolidWorksSession._equation_lhs(equation) == lhs


@pytest.mark.parametrize("equation", ["L = 85", '"L" 85', "", '"" = 3'])
def test_an_equation_needs_a_quoted_name_and_an_equals_sign(equation):
    with pytest.raises(SolidWorksError, match='"name" = expression'):
        SolidWorksSession._equation_lhs(equation)


def test_an_equation_names_what_it_uses():
    assert SolidWorksSession._equation_names('"D1@Boss" = ("L_shin" - 56) / "W"') == ["D1@Boss", "L_shin", "W"]


def test_equations_compare_without_spacing_or_case():
    """SolidWorks may respace an equation it keeps; a refused one keeps the old text."""
    assert SolidWorksSession._same_equation('"L"=90', ' "l" = 90 ')
    assert not SolidWorksSession._same_equation('"L" = 90', '"L" = 85')
