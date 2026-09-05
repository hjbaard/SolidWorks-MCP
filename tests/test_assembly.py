"""Integration tests for the assembly tools (M6), each verified against geometry.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The components are two blocks built and saved by the `blocks` fixture, so every
expected position, box and interference volume follows from a hand calculation.
Distances are in mm, volumes in mm^3.
"""

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

BLOCK_A = [40.0, 20.0, 10.0]
BLOCK_B = [20.0, 20.0, 20.0]


def only(session, name):
    """The one component whose name starts with `name`."""
    matches = [c for c in session.list_components()["components"]
               if c["name"].lower().startswith(name)]
    assert len(matches) == 1, f"expected one '{name}', got {[c['name'] for c in matches]}"
    return matches[0]


def two_blocks(assembly, blocks, b_at=(100.0, 0.0, 0.0)):
    """block_a fixed at the origin plus a free block_b at `b_at`."""
    assembly.insert_component(blocks["block_a"], 0, 0, 0)
    assembly.insert_component(blocks["block_b"], *b_at)
    return assembly


# --- documents ----------------------------------------------------------------


def test_new_assembly_is_empty(assembly):
    listed = assembly.list_components()
    assert listed["count"] == 0 and listed["components"] == []


def test_save_and_open_assembly_round_trip(assembly, blocks, tmp_path):
    two_blocks(assembly, blocks)
    saved = assembly.save_assembly(str(tmp_path / "two_blocks.sldasm"))
    assert saved["bytes"] > 0
    box = assembly.get_assembly_bounding_box()["bounding_box_mm"]
    assembly.close_part()
    assembly.open_assembly(saved["path"])
    assert assembly.list_components()["count"] == 2
    assert assembly.get_assembly_bounding_box()["bounding_box_mm"] == box


def test_assembly_tools_reject_a_part(part):
    with pytest.raises(SolidWorksError):
        part.list_components()


def test_part_tools_reject_an_assembly(assembly):
    # a part builder must not sketch into an assembly and fail later in the dark
    with pytest.raises(SolidWorksError):
        assembly.add_box(10, 10, 10)


# --- inserting and placing ----------------------------------------------------


def test_first_component_is_fixed_at_the_origin(assembly, blocks):
    comp = assembly.insert_component(blocks["block_a"], 0, 0, 0)["component"]
    assert comp["fixed"] is True
    assert comp["position_mm"] == [0.0, 0.0, 0.0]
    assert comp["bounding_box_mm"]["size_mm"] == BLOCK_A


def test_insert_places_the_part_origin_not_the_box_centre(assembly, blocks):
    # AddComponent5's own X/Y/Z drop the component with its bounding-box CENTRE
    # on the point; insert_component must land the part's ORIGIN there instead.
    comp = assembly.insert_component(blocks["block_a"], 100, 50, 25)["component"]
    assert comp["bounding_box_mm"]["min_mm"] == [100.0, 50.0, 25.0]
    assert comp["bounding_box_mm"]["max_mm"] == [140.0, 70.0, 35.0]


def test_second_component_is_free_by_default(assembly, blocks):
    two_blocks(assembly, blocks)
    assert only(assembly, "block_a")["fixed"] is True
    assert only(assembly, "block_b")["fixed"] is False


def test_insert_component_missing_file_raises(assembly, tmp_path):
    with pytest.raises(SolidWorksError):
        assembly.insert_component(str(tmp_path / "nope.sldprt"))


def test_set_component_transform_moves_and_reads_back(assembly, blocks):
    two_blocks(assembly, blocks)
    moved = assembly.set_component_transform("block_b", 200, 30, -15)
    assert moved["position_mm"] == [200.0, 30.0, -15.0]
    assert only(assembly, "block_b")["bounding_box_mm"]["min_mm"] == [200.0, 30.0, -15.0]


def test_set_component_transform_rotates(assembly, blocks):
    two_blocks(assembly, blocks)
    turned = assembly.set_component_transform("block_a", 0, 0, 0, rz_deg=90)
    assert turned["rotation_deg"] == pytest.approx([0.0, 0.0, 90.0], abs=1e-6)
    # a 90-degree turn about Z swaps the block's X and Y extents
    assert turned["bounding_box_mm"]["size_mm"] == pytest.approx(
        [BLOCK_A[1], BLOCK_A[0], BLOCK_A[2]], abs=1e-6)


def test_set_component_transform_unknown_name_raises(assembly, blocks):
    two_blocks(assembly, blocks)
    with pytest.raises(SolidWorksError):
        assembly.set_component_transform("block_c", 0, 0, 0)


# --- mates --------------------------------------------------------------------


def test_distance_mate_moves_the_component(assembly, blocks):
    # block_a spans x 0..40; a 5 mm gap to block_b's -X face puts block_b at x=45
    two_blocks(assembly, blocks, b_at=(100.0, 0.0, 0.0))
    mate = assembly.add_mate("block_b", "-x", "block_a", "+x",
                             mate_type="distance", distance_mm=5)
    assert mate["distance_mm"] == pytest.approx(5.0, abs=1e-3)
    assert only(assembly, "block_b")["position_mm"][0] == pytest.approx(45.0, abs=1e-3)


def test_coincident_mate_puts_the_faces_together(assembly, blocks):
    two_blocks(assembly, blocks, b_at=(100.0, 0.0, 0.0))
    mate = assembly.add_mate("block_b", "-x", "block_a", "+x", mate_type="coincident")
    assert mate["distance_mm"] == pytest.approx(0.0, abs=1e-3)
    assert only(assembly, "block_b")["position_mm"][0] == pytest.approx(40.0, abs=1e-3)


def test_parallel_mate_aligns_the_faces(assembly, blocks):
    two_blocks(assembly, blocks, b_at=(100.0, 0.0, 0.0))
    assembly.set_component_transform("block_b", 100, 0, 0, rz_deg=30)
    mate = assembly.add_mate("block_b", "+z", "block_a", "+z", mate_type="parallel")
    assert mate["angle_deg"] == pytest.approx(0.0, abs=1e-2)


def test_flip_puts_the_distance_on_the_other_side(assembly, blocks):
    # the same 5 mm distance mate has two solutions: block_b clear of block_a
    # (x=45) or reaching 5 mm into it (x=35). flip picks the other one, and the
    # measured perpendicular distance is 5 mm either way.
    two_blocks(assembly, blocks, b_at=(100.0, 0.0, 0.0))
    mate = assembly.add_mate("block_b", "-x", "block_a", "+x",
                             mate_type="distance", distance_mm=5, flip=True)
    assert mate["distance_mm"] == pytest.approx(5.0, abs=1e-3)
    assert only(assembly, "block_b")["position_mm"][0] == pytest.approx(35.0, abs=1e-3)


def test_mate_unknown_type_raises(assembly, blocks):
    two_blocks(assembly, blocks)
    with pytest.raises(SolidWorksError):
        assembly.add_mate("block_a", "+x", "block_b", "-x", mate_type="concentric")


def test_mate_needs_two_different_components(assembly, blocks):
    two_blocks(assembly, blocks)
    with pytest.raises(SolidWorksError):
        assembly.add_mate("block_a", "+x", "block_a", "-x")


def test_mate_unknown_face_direction_raises(assembly, blocks):
    two_blocks(assembly, blocks)
    with pytest.raises(SolidWorksError):
        assembly.add_mate("block_a", "up", "block_b", "-x")


# --- interference -------------------------------------------------------------


def test_no_interference_when_apart(assembly, blocks):
    two_blocks(assembly, blocks, b_at=(100.0, 0.0, 0.0))
    assert assembly.check_interference()["count"] == 0


def test_touching_faces_are_not_an_interference(assembly, blocks):
    # block_b's -X face flush against block_a's +X face: contact, not a clash
    two_blocks(assembly, blocks, b_at=(40.0, 0.0, 0.0))
    assert assembly.check_interference()["count"] == 0


def test_overlap_is_reported_with_its_volume(assembly, blocks):
    # block_b pushed 5 mm into block_a: overlap = 5 x 20 x 10 (block_a is only
    # 10 deep, block_b 20, so the shared depth is 10) = 1000 mm^3
    two_blocks(assembly, blocks, b_at=(35.0, 0.0, 0.0))
    clashes = assembly.check_interference()
    assert clashes["count"] == 1
    clash = clashes["interferences"][0]
    assert sorted(clash["components"]) == ["block_a-1", "block_b-1"]
    assert clash["volume_mm3"] == pytest.approx(5 * 20 * 10, abs=0.01)


# --- measurement --------------------------------------------------------------


def test_assembly_bounding_box_spans_all_components(assembly, blocks):
    two_blocks(assembly, blocks, b_at=(100.0, 0.0, 0.0))
    box = assembly.get_assembly_bounding_box()["bounding_box_mm"]
    assert box["min_mm"] == [0.0, 0.0, 0.0]
    assert box["max_mm"] == [120.0, 20.0, 20.0]
    assert box["size_mm"] == [120.0, 20.0, 20.0]


def test_get_assembly_bounding_box_rejects_a_part(part):
    part.add_box(*BLOCK_A)
    with pytest.raises(SolidWorksError):
        part.get_assembly_bounding_box()


def test_screenshot_and_export_work_on_an_assembly(assembly, blocks, tmp_path):
    two_blocks(assembly, blocks)
    assert assembly.screenshot(str(tmp_path / "asm.png"))["bytes"] > 0
    assert assembly.export(str(tmp_path / "asm.step"))["bytes"] > 0


# --- outer vs inner face selection (the shelled-box bug) ----------------------


def test_shelled_box_has_two_faces_per_normal(part):
    # A closed 2 mm shell of a 40x20x10 block has an OUTER +Z face at z=10 and an
    # INNER one (the cavity floor) at z=2. Picking whichever the API listed first
    # returned the wrong one; the side must decide.
    part.add_box(*BLOCK_A)
    part.add_shell(2, open_face="none")
    faces = part._solid_body().GetFaces()
    assert part._pick_planar_face(faces, (0.0, 0.0, 1.0), "outer")[1] == pytest.approx(10.0)
    assert part._pick_planar_face(faces, (0.0, 0.0, 1.0), "inner")[1] == pytest.approx(2.0)


def test_inner_face_selector_reaches_the_cavity(part):
    # the on-face guard proves WHICH face was selected: (20,10,2) lies on the
    # cavity floor, so '+z' (the outer face, 8 mm away) must reject it and
    # '+z:inner' must accept it and drill through the 2 mm wall.
    part.add_box(*BLOCK_A)
    closed = part.add_shell(2, open_face="none")["mass_properties"]["volume_mm3"]
    with pytest.raises(SolidWorksError):
        part.add_hole_on_face(4, "+z", 20, 10, 2)
    drilled = part.add_hole_on_face(4, "+z:inner", 20, 10, 2)
    import math
    removed = closed - drilled["mass_properties"]["volume_mm3"]
    assert removed == pytest.approx(math.pi * 2 ** 2 * 2, abs=0.01)
