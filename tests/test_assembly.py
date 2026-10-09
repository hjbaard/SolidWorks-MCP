"""Integration tests for the assembly tools (M6), each verified against geometry.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The components are two blocks built and saved by the `blocks` fixture, so every
expected position, box and interference volume follows from a hand calculation.
Distances are in mm, volumes in mm^3.
"""

import os

import pytest

from solidworks_mcp import binding
from solidworks_mcp.constants import SW_DOC_ASSEMBLY, SW_OPEN_DOC_SILENT
from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.mesh_tools import load_mesh

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


def test_list_faces_reads_a_components_hole_in_its_own_frame(sw, tmp_path):
    """For an imported servo: its horn's hole circle must be readable without
    opening the part, and the same wherever the component sits."""
    sw.new_part()
    sw.add_box(40, 20, 10)
    sw.add_hole(6, 10, 12)
    path = sw.save_part(str(tmp_path / "holed.sldprt"))["path"]
    sw.close_part()
    sw.new_assembly()
    try:
        sw.insert_component(path, 100, 50, 0)
        sw.set_component_transform("holed", 100, 50, 0, rz_deg=90)

        holes = [f["cylinder"] for f in sw.list_faces(component="holed")["faces"] if "cylinder" in f]

        assert len(holes) == 1 and holes[0]["radius_mm"] == pytest.approx(3), holes
        assert holes[0]["point_mm"] == pytest.approx([10, 12, 5], abs=1e-4), "not in the component's own frame"
    finally:
        sw.close_part()
        sw._sw.CloseDoc(path)


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
    with pytest.raises(SolidWorksError, match="Unknown mate type"):
        assembly.add_mate("block_a", "+x", "block_b", "-x", mate_type="glue")


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


@pytest.fixture(scope="module")
def disc(sw, tmp_path_factory):
    """A saved disc, 20 across and 10 thick: turning it about its axis must not grow its box."""
    sw.new_part()
    sw.add_disc(20, 10)
    path = sw.save_part(str(tmp_path_factory.mktemp("disc") / "disc.sldprt"))["path"]
    sw.close_part()
    return path


def test_a_turned_round_part_keeps_its_own_box(sw, disc):
    """SolidWorks' component box turns with the component and grows: this disc
    came out 28.3 wide, and an arm in a real assembly reached 4.7 mm further
    than its box said. Every box the tools report must be the part's own."""
    sw.new_assembly()
    try:
        sw.insert_component(disc, 0, 0, 0)
        turned = sw.set_component_transform("disc", 0, 0, 0, rz_deg=45)
        listed = only(sw, "disc")
        whole = sw.get_assembly_bounding_box()["bounding_box_mm"]
    finally:
        sw.close_part()
        sw._sw.CloseDoc(disc)
    for label, box in (("set_component_transform", turned["bounding_box_mm"]),
                       ("list_components", listed["bounding_box_mm"]), ("get_assembly_bounding_box", whole)):
        assert box["min_mm"] == pytest.approx([-10, -10, 0], abs=1e-4), f"{label} boxes the turned disc too big: {box}"
        assert box["size_mm"] == pytest.approx([20, 20, 10], abs=1e-4), f"{label} boxes the turned disc too big: {box}"


def test_a_sub_assembly_is_measured_through_its_parts(sw, blocks, tmp_path):
    """A person's assembly or an imported STEP nests parts in sub-assemblies,
    which have no body of their own: box and distance must reach the parts."""
    sw.new_assembly()
    sw.insert_component(blocks["block_a"], 10, 0, 0)
    sub = sw.save_assembly(str(tmp_path / "sub.sldasm"))["path"]
    sw.close_part()
    sw.new_assembly()
    try:
        # insert_component takes parts only, so the sub-assembly goes in by hand
        top = sw._model.GetTitle()
        sw._sw.OpenDoc6(sub, SW_DOC_ASSEMBLY, SW_OPEN_DOC_SILENT, "", 0, 0)
        sw._sw.ActivateDoc3(top, True, 0, 0)
        assert sw._require_assembly().AddComponent5(sub, 0, "", False, "", 0.0, 0.0, 0.0) is not None
        sw.set_component_transform("sub", 0, 100, 0, rz_deg=90)
        box = only(sw, "sub")["bounding_box_mm"]
        measured = sw.measure_distance("sub", point_mm=[-10, 160, 5])
    finally:
        sw.close_part()
        sw._sw.CloseDoc(sub)
        sw._sw.CloseDoc(blocks["block_a"])
    # block_a (x 0..40, y 0..20) sits at x = 10 in the sub-assembly, which is
    # turned 90 degrees about Z and moved to y = 100: x -20..0, y 110..150
    assert box is not None, "the sub-assembly got no box: its parts were not searched"
    assert box["min_mm"] == pytest.approx([-20, 110, 0], abs=1e-4) and \
        box["max_mm"] == pytest.approx([0, 150, 10], abs=1e-4), f"wrong box for the nested part: {box}"
    assert measured["distance_mm"] == pytest.approx(10, abs=1e-4), measured
    assert measured["nearest_mm"] == pytest.approx([-10, 150, 5], abs=1e-4), measured


def test_measure_distance_between_two_components(assembly, blocks):
    # block_a spans x 0..40 and block_b starts at x = 100: 60 mm of air
    two_blocks(assembly, blocks, b_at=(100.0, 0.0, 0.0))
    assert assembly.measure_distance("block_a", "block_b")["distance_mm"] == pytest.approx(60, abs=1e-4)


def test_measure_distance_follows_a_turned_component(assembly, blocks):
    # block_b turned 90 degrees about Z at x = 100 spans x 80..100: 40 mm, not 60
    two_blocks(assembly, blocks, b_at=(100.0, 0.0, 0.0))
    assembly.set_component_transform("block_b", 100, 0, 0, rz_deg=90)
    assert assembly.measure_distance("block_a", "block_b")["distance_mm"] == pytest.approx(40, abs=1e-4)


@pytest.mark.parametrize("b_x", [40.0, 35.0], ids=["touching", "overlapping"])
def test_components_that_meet_are_0_apart(assembly, blocks, b_x):
    # SolidWorks measures no distance between bodies that meet
    two_blocks(assembly, blocks, b_at=(b_x, 0.0, 0.0))
    assert assembly.measure_distance("block_a", "block_b")["distance_mm"] == 0


def test_measure_distance_to_a_point_gives_the_nearest_point(assembly, blocks):
    # block_a is turned 90 degrees about Z: x -20..0, y 0..40. The point is
    # 10 mm past its far end; unturned, the block would be 31.6 mm away.
    two_blocks(assembly, blocks)
    assembly.set_component_transform("block_a", 0, 0, 0, rz_deg=90)
    measured = assembly.measure_distance("block_a", point_mm=[-10, 50, 5])
    assert measured["distance_mm"] == pytest.approx(10, abs=1e-4), measured
    assert measured["nearest_mm"] == pytest.approx([-10, 40, 5], abs=1e-4), measured
    assert measured["inside"] is False


def test_a_point_in_the_material_is_flagged_inside(assembly, blocks):
    """A positive distance alone would read as clearance; 2 mm deep is not."""
    # 2 mm under block_a's top face (z = 10); every other face is further away
    two_blocks(assembly, blocks)
    measured = assembly.measure_distance("block_a", point_mm=[20, 10, 8])
    assert measured["inside"] is True, f"a point in the material reads as clearance: {measured}"
    assert measured["distance_mm"] == pytest.approx(2, abs=1e-4)
    assert measured["nearest_mm"] == pytest.approx([20, 10, 10], abs=1e-4)


def test_get_assembly_bounding_box_rejects_a_part(part):
    part.add_box(*BLOCK_A)
    with pytest.raises(SolidWorksError):
        part.get_assembly_bounding_box()


def test_screenshot_and_export_work_on_an_assembly(assembly, blocks, tmp_path):
    two_blocks(assembly, blocks)
    assert assembly.screenshot(str(tmp_path / "asm.png"))["bytes"] > 0
    assert assembly.export(str(tmp_path / "asm.step"))["bytes"] > 0


def test_an_assembly_goes_to_one_stl_or_one_per_component(assembly, blocks, tmp_path):
    """SolidWorks' own setting decides by default, and per component the
    expected file never appears: both ways are asked for explicitly."""
    two_blocks(assembly, blocks)
    whole = assembly.export(str(tmp_path / "whole.stl"))
    assert whole["bytes"] > 0 and whole["files"] == [whole["path"]]
    apart = assembly.export(str(tmp_path / "apart" / "asm.stl"), per_component=True)
    names = sorted(os.path.basename(f).lower() for f in apart["files"])
    assert len(names) == 2 and "block_a" in names[0] and "block_b" in names[1], names
    assert all(os.path.getsize(f) > 0 for f in apart["files"])


def test_stls_of_an_assembly_keep_its_coordinates(assembly, blocks, tmp_path):
    """Moved into positive space, the files per component no longer lined up
    with the assembly. block_b (20 cube) at x = -50, block_a at the origin."""
    two_blocks(assembly, blocks, b_at=(-50.0, 0.0, 0.0))
    whole = assembly.export(str(tmp_path / "whole.stl"))
    assert min(p[0] for t in load_mesh(whole["path"]) for p in t) == pytest.approx(-50, abs=1e-3)
    apart = assembly.export(str(tmp_path / "apart" / "asm.stl"), per_component=True)
    [b_file] = [f for f in apart["files"] if "block_b" in os.path.basename(f).lower()]
    xs = [p[0] for t in load_mesh(b_file) for p in t]
    assert (min(xs), max(xs)) == pytest.approx((-50, -30), abs=1e-3), "block_b's file is not where it sits"
    assert whole["frame"] == apart["frame"] == "assembly"


def test_an_assembly_left_editing_a_part_is_edited_as_a_whole_again(assembly, blocks, tmp_path):
    """Left editing one of its parts (a double click in SolidWorks), the
    assembly showed every other component see-through, and a component would
    not go in: 'AddComponent5 returned None'."""
    assembly.insert_component(blocks["block_a"], 0, 0, 0)
    assembly.save_assembly(str(tmp_path / "pair.sldasm"))  # SolidWorks edits a part in context of a saved one
    model = assembly._model
    title = model.GetTitle().rsplit(".", 1)[0]
    extension = binding.wrap(model.Extension, binding.module().IModelDocExtension)
    assert extension.SelectByID2(f"{only(assembly, 'block_a')['name']}@{title}", "COMPONENT", 0, 0, 0, False, 0, None, 0)
    assert binding.wrap(model, binding.module().IAssemblyDoc).EditPart2(True, False, 0) == 0
    assert not model.IsEditingSelf(), "the setup did not leave the assembly editing a part"

    assembly.insert_component(blocks["block_b"], 100, 0, 0)

    assert model.IsEditingSelf(), "the assembly is still editing one of its parts"
    assert len(assembly.list_components()["components"]) == 2


def test_components_take_a_colour_and_see_through_of_their_own(assembly, blocks):
    """A black frame and light grey covers had to be set by hand, and a
    component left see-through stayed so. block_a dark, block_b see-through
    and solid again; the part files keep their own colour."""
    two_blocks(assembly, blocks)
    dark = assembly.set_appearance(rgb=[30, 30, 30], component="block_a")
    assert dark["rgb"] == [30, 30, 30] and dark["transparency"] == 0
    assert assembly.set_appearance(transparency=0.6, component="block_b")["transparency"] == pytest.approx(0.6)
    solid = assembly.set_appearance(transparency=0, component="block_b")
    assert solid["transparency"] == 0
    comp = assembly._component_by_name(assembly._require_assembly(), "block_a")
    own = binding.wrap(comp.GetModelDoc2(), binding.module().IModelDoc2).MaterialPropertyValues
    assert [round(c * 255) for c in own[:3]] != [30, 30, 30], "the colour went into the part file, not the assembly"
    with pytest.raises(SolidWorksError, match="name the component"):
        assembly.set_appearance(rgb=[30, 30, 30])


def test_one_stl_per_component_is_for_assemblies(part, tmp_path):
    part.add_box(10, 10, 10)
    with pytest.raises(SolidWorksError, match="per_component is for an assembly"):
        part.export(str(tmp_path / "block.stl"), per_component=True)


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


def test_face_selector_reaches_the_cavity(part):
    # the on-face guard proves WHICH face was selected: (20,10,2) lies on the
    # cavity floor, so '+z:outer' (the outer face, 8 mm away) must reject it,
    # while '+z' (the face through the point) and '+z:inner' reach it and drill
    # through the 2 mm wall.
    import math
    part.add_box(*BLOCK_A)
    closed = part.add_shell(2, open_face="none")["mass_properties"]["volume_mm3"]
    with pytest.raises(SolidWorksError):
        part.add_hole_on_face(4, "+z:outer", 20, 10, 2)
    drilled = part.add_hole_on_face(4, "+z", 20, 10, 2)
    assert closed - drilled["mass_properties"]["volume_mm3"] == pytest.approx(math.pi * 2 ** 2 * 2, abs=0.01)
    inner = part.add_hole_on_face(4, "+z:inner", 10, 10, 2)
    assert closed - inner["mass_properties"]["volume_mm3"] == pytest.approx(2 * math.pi * 2 ** 2 * 2, abs=0.01)


@pytest.mark.parametrize("axis,expected", [([[60, 0, 5], [0, 1, 0]], 20), ([[20, 10, 0], [0, 0, 1]], 0)],
                         ids=["beside", "through"])
def test_measure_distance_to_an_axis(assembly, blocks, axis, expected):
    # block_a spans x 0..40: a line along y at x = 60 runs 20 mm off; one along z
    # through its middle runs through it
    two_blocks(assembly, blocks, b_at=(100.0, 0.0, 0.0))
    measured = assembly.measure_distance("block_a", axis_mm=axis)
    assert measured["distance_mm"] == pytest.approx(expected, abs=1e-4)
    helpers = [f.Name for f in assembly._iter_features() if f.GetTypeName2() == "3DProfileFeature"]
    assert helpers == [], f"the helper line was left in the tree: {helpers}"
