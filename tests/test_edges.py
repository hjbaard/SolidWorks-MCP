"""Integration tests for edges on bigger parts: lists narrowed to what matters,
fillets that say which edges do not fit, outlines dimensioned point by point.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The L-plate is 40 x 2 with a 0.5 mm wall rising at x 39.5..40 to y = 10, 10 deep:
two R1 rounds cannot both sit on that wall.
"""

import math
import re

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

L_PLATE = [[0, 0], [40, 0], [40, 10], [39.5, 10], [39.5, 2], [0, 2]]


def test_edges_narrow_to_a_face_a_feature_a_box_and_a_length(part):
    """A 40 x 20 x 10 block with a 4 mm hole through it."""
    part.add_box(40, 20, 10)
    part.add_hole(4, 10, 10, name="Bore")
    every = {e["index"]: e for e in part.list_edges()["edges"]}
    [top] = [f["index"] for f in part.list_faces()["faces"] if f.get("normal") == [0, 0, 1]]
    on_top = part.list_edges(face=f"#{top}")["edges"]
    assert sorted(e["type"] for e in on_top) == ["circle"] + ["line"] * 4
    assert all(e == every[e["index"]] for e in on_top), "a filtered edge keeps the index and data of the whole list"
    bore = part.list_edges(feature="Bore")["edges"]
    assert bore and all(e["type"] == "circle" and e["length_mm"] == pytest.approx(4 * math.pi, abs=1e-3) for e in bore)
    assert len(part.list_edges(within_mm=[[41, 21, 11], [-1, -1, 9]])["edges"]) == 5, "the top's lines and the rim"
    assert sorted(e["length_mm"] for e in part.list_edges(min_length_mm=15)["edges"]) == [20] * 4 + [40] * 4


def test_a_refused_fillet_names_the_edges_and_offers_the_ones_that_round(part):
    part.add_extruded_profile(L_PLATE, 10)
    with pytest.raises(SolidWorksError, match=r'round together: edges="[\d,]+"') as refused:
        part.add_fillet(1, "+z:outline")
    assert [f["name"] for f in part.list_features()["features"]] == ["Sketch1", "Extrude"], \
        "every trial fillet should be gone again"
    keep = re.search(r'edges="([\d,]+)"', str(refused.value)).group(1)
    assert part.add_fillet(1, keep)["edges_filleted"] == len(keep.split(","))


def test_a_refused_fillet_names_the_largest_radius_that_fits(part):
    """'Fails even alone' said nothing about how large a round would fit. A
    Ø4 x 3 boss on a block: its top rim takes a round up to its own radius, 2;
    the rim where it meets the block takes 2.5."""
    part.add_box(40, 20, 10)
    part.add_boss_on_face(4, "+z", 20, 10, 10, 3, name="Boss")
    rims = ",".join(str(e["index"]) for e in part.list_edges(feature="Boss")["edges"])

    with pytest.raises(SolidWorksError, match="fail even alone") as refusal:
        part.add_fillet(2.5, edges=rims)

    largest = re.search(r"largest round it takes: R(\d+(?:\.\d+)?)", str(refusal.value))
    assert largest, f"the refusal does not say how large a round fits: {refusal.value}"
    assert 1.95 <= float(largest.group(1)) <= 2.0, refusal.value


def test_an_edge_that_runs_on_into_a_fillet_rounds_with_tangent_propagation(part):
    """An L block with its inner corner rounded R2: the top edge along y = 10
    ends where it runs on smoothly round that round. Alone it could not be
    rounded, 'fail even alone' without a reason; carried on along the tangent
    edges, as SolidWorks does by default, it can."""
    part.add_extruded_profile([[0, 0], [40, 0], [40, 10], [10, 10], [10, 40], [0, 40]], 10)
    [corner] = part.list_edges(within_mm=[[9.9, 9.9, -0.1], [10.1, 10.1, 10.1]])["edges"]
    part.add_fillet(2, str(corner["index"]), name="Corner")
    [top] = part.list_edges(within_mm=[[11.9, 9.9, 9.9], [40.1, 10.1, 10.1]])["edges"]
    with pytest.raises(SolidWorksError, match="round with tangent_propagation=True"):
        part.add_fillet(1, str(top["index"]))
    rounded = part.add_fillet(1, str(top["index"]), tangent_propagation=True)
    assert rounded["edges_filleted"] == 1 and rounded["rebuild_ok"]


def test_short_edges_can_be_left_out(part):
    """The wall's 0.5 mm end is the only outline edge shorter than 1 mm."""
    part.add_extruded_profile(L_PLATE, 10)
    rounded = part.add_fillet(0.2, "+z:outline", skip_shorter_mm=1)
    assert rounded["edges_skipped"] == 1 and rounded["edges_filleted"] == 5


def test_a_forty_point_outline_gets_its_dimensions(part):
    """Up to 60 points a profile is dimensioned point by point, not fixed."""
    points = [[30 + 20 * math.cos(2 * math.pi * k / 40), 30 + 15 * math.sin(2 * math.pi * k / 40)] for k in range(40)]
    outline = part.add_extruded_profile(points, 5)
    assert outline["fully_defined"] and len(outline["dimensions"]) >= 60


def test_a_tool_call_puts_command_in_progress_back(sw):
    """Tool calls run with CommandInProgress set (a hundred times faster COM
    calls) and must leave it as they found it, also for the person at the screen."""
    before = sw._command_in_progress(False)
    sw.new_part()
    try:
        sw.run_guarded(sw.add_box, 10, 10, 10)
        assert sw._sw.CommandInProgress is False
    finally:
        sw.close_part()
        sw._command_in_progress(before)


def test_command_in_progress_is_counted_so_a_call_adds_nothing(sw):
    """SolidWorks counts CommandInProgress: every True needs its own False. A
    tool call made while it is already set must not add to the count, or one
    False no longer clears it and SolidWorks stops redrawing for the person."""
    before = sw._command_in_progress(False)
    sw.new_part()
    try:
        sw._command_in_progress(True)
        sw.run_guarded(sw.add_box, 10, 10, 10)
        sw._command_in_progress(False)
        assert sw._sw.CommandInProgress is False, "the tool call left a count behind"
    finally:
        sw.close_part()
        sw._command_in_progress(before)
