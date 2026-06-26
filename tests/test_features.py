"""Integration tests: each feature verified against a hand calculation.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
This is the automated regression suite that replaces running the m4_*.py scripts
by hand. Volumes are in mm^3.
"""

import math

import pytest

pytestmark = pytest.mark.solidworks


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def test_box(part):
    assert abs(vol(part.add_box(40, 20, 10)) - 8000) < 0.01


def test_set_dimension(part):
    box = part.add_box(40, 20, 10)
    assert abs(vol(part.set_dimension(box["depth_dimension"], 25)) - 20000) < 0.01


def test_cylinder(part):
    assert abs(vol(part.add_cylinder(20, 20)) - math.pi * 100 * 20) < 0.1


def test_cone_frustum(part):
    rb, rt, h = 10, 5, 20
    expected = math.pi * h / 3 * (rb * rb + rb * rt + rt * rt)
    assert abs(vol(part.add_cone(20, 10, 20)) - expected) < 0.1


def test_disc(part):
    assert abs(vol(part.add_disc(40, 10)) - math.pi * 20 ** 2 * 10) < 0.1


def test_revolve_cylinder(part):
    # general revolve reproduces a cylinder: r=10, h=20
    got = vol(part.add_revolved_profile([[0, 0], [10, 0], [10, 20], [0, 20]]))
    assert abs(got - math.pi * 100 * 20) < 0.1


def test_revolve_ring(part):
    # profile offset from the axis -> annular ring: outer 10, inner 5, height 2
    got = vol(part.add_revolved_profile([[5, 0], [10, 0], [10, 2], [5, 2]]))
    assert abs(got - math.pi * (10 ** 2 - 5 ** 2) * 2) < 0.1


def test_revolve_partial(part):
    # 180 deg revolve removes exactly half the volume
    got = vol(part.add_revolved_profile([[0, 0], [10, 0], [10, 20], [0, 20]], 180))
    assert abs(got - math.pi * 100 * 20 / 2) < 0.1


def test_swept_pipe_straight(part):
    # straight 50 mm path, Ø10 -> cylinder r=5: pi*25*50 (Pappus)
    got = vol(part.add_swept_pipe([[0, 0], [50, 0]], 10))
    assert abs(got - math.pi * 25 * 50) < 0.5


def test_swept_pipe_L_bend(part):
    # L path with 10 mm bend: length = 20 + 20 + (pi/2)*10; Ø10 -> pi*25*length
    length = 20 + 20 + math.pi / 2 * 10
    got = vol(part.add_swept_pipe([[0, 0], [30, 0], [30, 30]], 10, 10))
    assert abs(got - math.pi * 25 * length) < 0.5


def test_round_flange(part):
    # disc + centre bore + bolt hole + 6x circular pattern = a round flange
    part.add_disc(80, 15)
    part.add_hole(20, 0, 0, name="Bore")
    part.add_hole(10, 30, 0, name="Bolt")
    r = part.add_circular_pattern(6, 0, 0)
    expected = math.pi * 15 * (40 ** 2 - 10 ** 2 - 6 * 5 ** 2)
    assert abs(vol(r) - expected) < 0.5


def test_extruded_profile(part):
    # L-bracket, shoelace area 1800 mm^2
    pts = [[0, 0], [60, 0], [60, 20], [20, 20], [20, 50], [0, 50]]
    assert abs(vol(part.add_extruded_profile(pts, 10)) - 18000) < 0.1


def test_extruded_profile_explicitly_closed(part):
    # repeating the first point must give the same result (ring normalised)
    pts = [[0, 0], [40, 0], [40, 20], [0, 0]]  # triangle, area 400
    assert abs(vol(part.add_extruded_profile(pts, 10)) - 4000) < 0.1


def test_hole(part):
    part.add_box(40, 20, 10)
    assert abs(vol(part.add_hole(8, 20, 10)) - (8000 - math.pi * 16 * 10)) < 0.1


def test_hole_off_center_frame(part):
    # (x,y) must be add_box coordinates: COM shifts away from a (10,6) hole
    part.add_box(40, 20, 10)
    com = part.add_hole(8, 10, 6)["mass_properties"]["center_of_mass_mm"]
    assert com[0] > 20 and com[1] > 10


def test_hole_on_x_face(part):
    # +X face at x=40 (20x10); centre (40,10,5); hole runs through the 40 mm length
    part.add_box(40, 20, 10)
    r = part.add_hole_on_face(8, "+x", 40, 10, 5)
    assert abs(vol(r) - (8000 - math.pi * 16 * 40)) < 0.1


def test_hole_on_y_face(part):
    # +Y face at y=20 (40x10); centre (20,20,5); hole runs through the 20 mm width
    part.add_box(40, 20, 10)
    r = part.add_hole_on_face(8, "+y", 20, 20, 5)
    assert abs(vol(r) - (8000 - math.pi * 16 * 20)) < 0.1


def test_cut_profile_blind(part):
    part.add_box(40, 20, 10)
    pts = [[10, 5], [30, 5], [30, 15], [10, 15]]  # 20x10 pocket
    assert abs(vol(part.cut_profile(pts, 4)) - (8000 - 200 * 4)) < 0.1


def test_cut_profile_through(part):
    part.add_box(40, 20, 10)
    pts = [[10, 5], [30, 5], [30, 15], [10, 15]]
    assert abs(vol(part.cut_profile(pts, None)) - (8000 - 200 * 10)) < 0.1


def test_cut_profile_on_side_face(part):
    # 10(y) x 6(z) pocket on the +X face (x=40), 5 mm deep
    part.add_box(40, 20, 10)
    pts = [[40, 5, 2], [40, 15, 2], [40, 15, 8], [40, 5, 8]]
    r = part.cut_profile_on_face(pts, "+x", 5)
    assert abs(vol(r) - (8000 - 10 * 6 * 5)) < 0.1


def test_cut_slot_blind(part):
    # L=20 (centre-to-centre), W=10 obround at (20,10), 5 mm deep.
    # area = L*W + pi*(W/2)^2 = 200 + 25*pi
    part.add_box(40, 20, 10)
    area = 20 * 10 + math.pi * 5 ** 2
    assert abs(vol(part.cut_slot(20, 10, 20, 10, 0, 5)) - (8000 - area * 5)) < 0.5


def test_cut_slot_through(part):
    part.add_box(40, 20, 10)
    area = 20 * 10 + math.pi * 5 ** 2
    assert abs(vol(part.cut_slot(20, 10, 20, 10, 0, None)) - (8000 - area * 10)) < 1.0


def test_cut_slot_angled_same_volume(part):
    # A 90-degree slot fits the 20-wide plate and removes the same volume.
    part.add_box(40, 20, 10)
    area = 10 * 8 + math.pi * 4 ** 2
    assert abs(vol(part.cut_slot(10, 8, 20, 10, 90, 5)) - (8000 - area * 5)) < 0.5


def test_fillet_all_edges(part):
    part.add_box(40, 20, 10)
    r = part.add_fillet(2)
    assert r["edges_filleted"] == 12 and vol(r) < 8000


def test_fillet_one_axis(part):
    part.add_box(40, 20, 10)
    assert part.add_fillet(2, edges="z")["edges_filleted"] == 4


def test_chamfer(part):
    part.add_box(40, 20, 10)
    assert vol(part.add_chamfer(2)) < 8000


def test_shell_open(part):
    part.add_box(40, 20, 10)
    assert abs(vol(part.add_shell(2, "+z")) - (8000 - 36 * 16 * 8)) < 0.1


def test_linear_pattern(part):
    part.add_box(40, 20, 10)
    part.add_hole(8, 10, 10)
    r = part.add_linear_pattern(3, 10, "+x")
    assert abs(vol(r) - (8000 - 3 * math.pi * 16 * 10)) < 0.1


def test_circular_pattern(part):
    part.add_box(40, 40, 10)
    part.add_hole(8, 20, 20, name="CenterHole")
    part.add_hole(6, 10, 20, name="BoltHole")
    r = part.add_circular_pattern(4, 20, 20)
    expected = 40 * 40 * 10 - math.pi * 16 * 10 - 4 * math.pi * 9 * 10
    assert abs(vol(r) - expected) < 0.1


def test_equation(part):
    part.add_box(40, 20, 10)
    assert abs(vol(part.set_equation('"D1@BlockExtrude" = 2 * 12.5')) - 20000) < 0.01


def test_material(part):
    part.add_box(40, 20, 10)
    r = part.set_material("6061 Alloy")
    assert abs(r["mass_properties"]["density_kg_m3"] - 2700) < 50


def test_material_bad_name_fails(part):
    part.add_box(40, 20, 10)
    with pytest.raises(Exception):
        part.set_material("Definitely Not A Material 1234")


def test_inspect_counts(part):
    part.add_box(40, 20, 10)
    assert part.list_faces()["count"] == 6
    assert part.list_edges()["count"] == 12


def test_save_open_roundtrip(part, tmp_path):
    part.add_box(40, 20, 10)
    path = str(tmp_path / "rt.sldprt")
    part.save_part(path)
    part.close_part()
    part.open_part(path)
    assert abs(vol(part.get_mass_properties()) - 8000) < 0.01
