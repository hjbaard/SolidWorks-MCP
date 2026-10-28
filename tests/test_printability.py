"""Integration tests for check_printability: overhangs and thin walls.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The tee has a stem x 0..10, y 0..20 and a crossbar x -20..30, y 20..25, 10 deep
along z: built along +y its arms' undersides are 2 x 20 x 10 of flat ceiling,
and the crossbar is a 5 mm wall. Areas are in mm^2, lengths in mm.
"""

import math

import pytest

pytestmark = pytest.mark.solidworks

TEE = [[0, 0], [10, 0], [10, 20], [30, 20], [30, 25], [-20, 25], [-20, 20], [0, 20]]


def test_the_arms_of_a_tee_overhang_when_it_stands_on_its_stem(part):
    part.add_extruded_profile(TEE, 10)
    found = part.check_printability(up="+y")
    overhang = found["overhang"]
    assert overhang["area_mm2"] == pytest.approx(2 * 20 * 10, abs=1e-3)
    assert len(overhang["faces"]) == 2 and all(f["worst_deg"] == pytest.approx(90) for f in overhang["faces"])
    assert sorted(f["center_mm"][0] for f in overhang["faces"]) == pytest.approx([-10, 20], abs=1e-3)
    assert found["bed_contact_mm2"] == pytest.approx(10 * 10, abs=1e-3) and found["height_mm"] == pytest.approx(25)


def test_a_downward_point_steep_enough_not_to_overhang_is_an_island(part):
    """Standing on its stem, the arm's underside is a V: its sides lean 31
    degrees, too little to overhang, and its point at y = 10 starts in the air.
    The check passed such a part, and the slicer showed it hanging."""
    part.add_extruded_profile([[0, 0], [10, 0], [10, 20], [16, 10], [22, 20], [22, 25], [0, 25]], 10)
    found = part.check_printability(up="+y")
    assert found["overhang"]["faces"] == []
    [island] = found["islands"]
    assert island["height_mm"] == pytest.approx(10, abs=1e-3), f"the V's point was not found in the air: {island}"
    assert island["at_mm"][:2] == pytest.approx([16, 10], abs=1e-3) and len(island["faces"]) == 2


def test_the_ends_of_an_edge_resting_only_in_the_middle_are_unsupported(part):
    """A pad's chamfered underside ended in an edge that rested on a tube in
    the middle; its ends hung 1.8 and 2.3 mm out in the air. No face leaned
    past 45 degrees and the edge touched material, so neither the overhang nor
    the island check saw it; the slicer did. Here a pedestal x 10..30 carries
    a pad x 0..40 whose 45-degree underside ends in an edge at y = 19, just
    inside the pedestal: its 10 mm ends hang free."""
    part.add_extruded_profile([[10, 0], [30, 0], [30, 20], [10, 20]], 12)
    part.add_extruded_profile_on_plane([[0, 19, 4], [0, 26, 4], [0, 26, 11]], "right", 40)
    found = part.check_printability(up="+y")
    assert found["overhang"]["faces"] == [] and found["islands"] == []
    ends = found["unsupported_edges"]
    assert len(ends) == 2, f"the free ends of the pad's edge were not both found: {ends}"
    assert all(e["length_mm"] == pytest.approx(10, abs=1) and e["height_mm"] == pytest.approx(19, abs=1e-3)
               for e in ends), ends
    assert sorted(e["at_mm"][0] for e in ends) == pytest.approx([5, 35], abs=1)


def test_a_tee_lying_flat_has_no_overhang(part):
    part.add_extruded_profile(TEE, 10)
    found = part.check_printability(up="+z")
    assert found["overhang"]["faces"] == [] and found["islands"] == [] and found["unsupported_edges"] == []
    assert found["bed_contact_mm2"] == pytest.approx(10 * 20 + 50 * 5, abs=1e-3)


def test_the_underside_of_a_sideways_peg_overhangs(part):
    """A horizontal cylinder leans more than 45 degrees over a quarter of its
    round: r x pi/2 x length, give or take the facets of the display mesh."""
    part.add_box(40, 20, 10)
    part.add_boss_on_face(6, "+x", 40, 10, 5, 10)
    [peg] = part.check_printability(up="+z")["overhang"]["faces"]
    assert peg["area_mm2"] == pytest.approx(3 * math.pi / 2 * 10, rel=0.2)
    assert peg["worst_deg"] > 80 and peg["center_mm"][0] == pytest.approx(45, abs=0.5)


def test_thin_walls_are_found_where_they_are_thin(part):
    # the crossbar is 5 thick: thin at a 6 mm minimum, fine at 4
    part.add_extruded_profile(TEE, 10)
    strict = part.check_printability(up="+y", min_wall_mm=6)["thin_walls"]
    assert strict["thinnest_mm"] == pytest.approx(5, abs=1e-3)
    assert len(strict["faces"]) == 3 and all(f["thinnest_mm"] == pytest.approx(5, abs=1e-3) for f in strict["faces"])
    loose = part.check_printability(up="+y", min_wall_mm=4)["thin_walls"]
    assert loose["faces"] == [] and loose["thinnest_mm"] == pytest.approx(5, abs=1e-3)


def test_a_hollow_face_is_measured_through_its_wall(part):
    """A ring 10 wide every way. Its hole's facets cut the curve, so samples on
    them start in the air: the first hit is where the ray enters the material,
    a hair away, and a curved cover read as walls of 0.001 mm. The wall runs to
    where the ray leaves the material."""
    part.add_disc(40, 10)
    part.add_hole(20, 0, 0)
    walls = part.check_printability(min_wall_mm=2)["thin_walls"]
    assert walls["faces"] == [], f"no wall of the ring is thin: {walls['faces']}"
    assert walls["thinnest_mm"] == pytest.approx(10, abs=0.1)


def test_a_thin_skin_over_a_pocket_is_found_inside_a_big_face(part):
    """Only a corner of the top face is thin: the samples must reach it, not
    just the centres of the two big triangles the face is drawn with (at x 20
    and 40, whichever diagonal splits it)."""
    part.add_box(60, 20, 10)
    part.cut_profile_on_face([[2, 3, 0], [14, 3, 0], [14, 9, 0], [2, 9, 0]], "-z", depth_mm=8.5)
    walls = part.check_printability(min_wall_mm=2)["thin_walls"]
    assert walls["thinnest_mm"] == pytest.approx(1.5, abs=1e-3)
    tops = [f for f in walls["faces"] if f["at_mm"][2] == pytest.approx(10, abs=1e-3)]
    assert tops, f"the 1.5 mm skin under the top face was missed: {walls['faces']}"
    x, y, _ = tops[0]["at_mm"]
    assert 2 <= x <= 14 and 3 <= y <= 9, "the thin spot is not over the pocket"
