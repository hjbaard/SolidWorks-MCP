"""Pure unit tests for the sketch-definition planner (no SolidWorks).

Every tool sketch must end up fully defined WITHOUT moving any geometry. So the
planner may only add relations the input already satisfies exactly, and must
give every remaining degree of freedom exactly one constraint: one too few
leaves the sketch under-defined (a stray drag moves it), one too many
over-defines it and SolidWorks flags the sketch.
"""

import math
import random

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.sketch_constraints import MAX_DIMENSIONED_VERTICES, plan_profile, plan_sketch


def constraint_count(plan):
    """Degrees of freedom the plan removes; must equal 2 per vertex."""
    return (2 * len(plan.coincident) + len(plan.horizontal) + len(plan.vertical)
            + (2 if plan.at_origin is not None else 0)
            + len(plan.origin_x) + len(plan.origin_y) + len(plan.x_dims) + len(plan.y_dims))


def test_rectangle_at_origin_gets_width_and_height_only():
    plan = plan_profile([(0, 0), (40, 0), (40, 20), (0, 20)], closed=True)
    assert plan.horizontal == (0, 2)
    assert plan.vertical == (1, 3)
    assert plan.at_origin == 0
    assert plan.x_dims == ((1, 40.0),), "the width must be the only x dimension"
    assert plan.y_dims == ((2, 20.0),), "the height must be the only y dimension"
    assert plan.origin_x == () and plan.origin_y == ()
    assert constraint_count(plan) == 8


def test_l_bracket_dimensions_each_distinct_edge_position_once():
    pts = [(0, 0), (40, 0), (40, 5), (5, 5), (5, 30), (0, 30)]
    plan = plan_profile(pts, closed=True)
    assert plan.horizontal == (0, 2, 4)
    assert plan.vertical == (1, 3, 5)
    assert plan.x_dims == ((1, 40.0), (3, 5.0))
    assert plan.y_dims == ((2, 5.0), (4, 30.0))
    assert constraint_count(plan) == 2 * len(pts)


def test_coordinates_on_the_origin_axes_get_relations_not_zero_dimensions():
    # SolidWorks cannot make a 0 mm dimension: x = 0 or y = 0 must be a relation
    plan = plan_profile([(0, 10), (20, 0), (30, 20)], closed=True)
    assert plan.at_origin is None
    assert plan.origin_x == (0,)
    assert plan.origin_y == (1,)
    assert plan.x_dims == ((1, 20.0), (2, 30.0))
    assert plan.y_dims == ((0, 10.0), (2, 20.0))
    assert constraint_count(plan) == 6


def test_nearly_vertical_mesh_wall_is_dimensioned_not_straightened():
    # A wall 1 nm off vertical is real input (sliced meshes are full of them); a
    # vertical relation would move a point -- the snapping bug AddToDB fixed.
    plan = plan_profile([(0, 0), (10, 0), (10.000001, 10), (0, 10)], closed=True)
    assert 1 not in plan.vertical, "a nearly vertical segment must not get a vertical relation"
    assert (1, 10.0) in plan.x_dims and (2, 10.000001) in plan.x_dims
    assert constraint_count(plan) == 8


def test_collinear_vertical_segments_share_one_x_dimension():
    plan = plan_profile([(0, 0), (10, 0), (10, 5), (10, 10), (0, 10)], closed=True)
    assert plan.vertical == (1, 2, 4)
    assert plan.x_dims == ((1, 10.0),), "one x for the whole straight right-hand wall"
    assert constraint_count(plan) == 10


def test_negative_coordinates_are_dimensioned_by_distance_from_the_origin():
    plan = plan_profile([(-10, -5), (10, -5), (10, 5), (-10, 5)], closed=True)
    assert plan.at_origin is None
    assert plan.x_dims == ((0, -10.0), (1, 10.0))
    assert plan.y_dims == ((0, -5.0), (2, 5.0))
    assert constraint_count(plan) == 8


def test_open_diagonal_line_dimensions_both_ends():
    plan = plan_profile([(5, 5), (25, 20)], closed=False)
    assert plan.horizontal == () and plan.vertical == ()
    assert plan.x_dims == ((0, 5.0), (1, 25.0))
    assert plan.y_dims == ((0, 5.0), (1, 20.0))


def test_open_horizontal_line_is_not_closed_back_to_its_start():
    plan = plan_profile([(0, 5), (20, 5)], closed=False)
    assert plan.horizontal == (0,)
    assert plan.origin_x == (0,)
    assert plan.x_dims == ((1, 20.0),)
    assert plan.y_dims == ((0, 5.0),)
    assert constraint_count(plan) == 4


def test_single_point_like_a_circle_centre():
    assert plan_profile([(10, 5)], closed=False).x_dims == ((0, 10.0),)
    assert plan_profile([(10, 5)], closed=False).y_dims == ((0, 5.0),)
    assert plan_profile([(0, 0)], closed=False).at_origin == 0


def test_closed_profile_needs_three_points():
    with pytest.raises(SolidWorksError):
        plan_profile([(0, 0), (10, 0)], closed=True)


def test_revolve_axis_drawn_onto_profile_vertices_is_tied_to_them():
    # A cylinder profile plus its centreline, whose end points SolidWorks may
    # keep as separate points at the profile's axis corners: they must become
    # coincident, and the centreline then needs no vertical relation of its own.
    pts = [(0, 0), (10, 0), (10, 20), (0, 20), (0, 0), (0, 20)]
    plan = plan_sketch(pts, [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5)])
    assert plan.coincident == ((4, 0), (5, 3))
    assert plan.vertical == (1, 3), "the centreline's vertical relation would over-define"
    assert plan.x_dims == ((1, 10.0),)
    assert plan.y_dims == ((2, 20.0),)
    assert constraint_count(plan) == 2 * len(pts)


def test_revolve_axis_of_a_ring_is_defined_on_its_own():
    # ring cross-section away from the axis; the centreline starts on the origin
    pts = [(5, 0), (10, 0), (10, 2), (5, 2), (0, 0), (0, 2)]
    plan = plan_sketch(pts, [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5)])
    assert plan.coincident == ()
    assert plan.at_origin == 4
    assert 4 in plan.vertical, "the centreline must be held vertical"
    assert plan.origin_y == (0,)
    assert plan.x_dims == ((0, 5.0), (1, 10.0))
    assert plan.y_dims == ((2, 2.0), (5, 2.0))
    assert constraint_count(plan) == 2 * len(pts)


def test_profiles_beyond_the_limit_are_fixed_not_dimensioned():
    ring = [(10 + i, (i % 2) * 3) for i in range(MAX_DIMENSIONED_VERTICES)]
    assert not plan_profile(ring, closed=True).fixed
    assert plan_profile(ring + [(5, 20)], closed=True).fixed


@pytest.mark.parametrize("seed", range(20))
def test_every_degree_of_freedom_gets_exactly_one_constraint(seed):
    # distinct grid points around their centre: simple polygons with plenty of
    # axis-aligned segments and points on the origin's axes
    rng = random.Random(seed)
    cells = rng.sample([(x, y) for x in range(-3, 4) for y in range(-3, 4)], rng.randint(3, 12))
    pts = [(x * 5.0, y * 5.0) for x, y in cells]
    cx, cy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    pts.sort(key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
    area = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))
    if area == 0:
        pytest.skip("collinear random points")
    plan = plan_profile(pts, closed=True)
    assert constraint_count(plan) == 2 * len(pts), f"under/over-defined plan for {pts}"
