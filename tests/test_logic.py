"""Pure unit tests for SolidWorksSession's no-COM helpers.

These cover the fiddly logic added during the toolset build (selector parsing,
direction parsing, axis classification, polygon cleaning) without SolidWorks.
"""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.session import SolidWorksSession


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
