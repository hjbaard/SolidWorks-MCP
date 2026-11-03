"""Integration tests for the history tools: list, delete and suppress features.

An agent needs these to correct a step without starting over, and to work on a
part it did not build itself. Marked `solidworks` -- they need a running
SolidWorks (auto-skipped otherwise). Volumes are in mm^3.
"""

import math

import pytest

from solidworks_mcp import binding
from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

BLOCK = 40 * 20 * 10
HOLE = math.pi * 3 ** 2 * 10                  # Ø6 through the 10 mm block
ROUNDS = 4 * (1 - math.pi / 4) * 2 ** 2 * 10  # R2 on the four vertical edges


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def sketch_of(result, role):
    """The sketch a tool drew, read from a dimension it returned ('width@Sketch1')."""
    return result["dimensions"][role].split("@")[1]


def block_with_hole_and_rounds(part):
    """A 40x20x10 block 'Base', a Ø6 through hole 'Hole' at (10, 10), and R2
    'Round' on the vertical edges; returns the names of the two sketches."""
    box = part.add_box(40, 20, 10, name="Base")
    hole = part.add_hole(6, 10, 10, name="Hole")
    part.add_fillet(2, edges="z", name="Round")
    return sketch_of(box, "width"), sketch_of(hole, "diameter")


def leave_loose_sketch(session):
    """A sketch as a person may leave it: one line, without relations or dimensions."""
    session._first_ref_plane().Select2(False, 0)
    sketcher = binding.wrap(session._model.SketchManager, binding.module().ISketchManager)
    sketcher.InsertSketch(True)
    sketcher.CreateLine(0.0, 0.0, 0.0, 0.01, 0.005, 0.0)
    sketcher.InsertSketch(True)
    return session.list_features()["features"][-1]["name"]


def delete_keeping_dependents(session, name):
    """Delete a feature the way the SolidWorks UI lets a person: what depends on it stays, broken."""
    feature = next(f for f in session._iter_features() if f.Name == name)
    session._model.ClearSelection2(True)
    feature.Select2(False, 0)
    extension = binding.wrap(session._model.Extension, binding.module().IModelDocExtension)
    assert extension.DeleteSelection2(2)  # swDelete_Absorbed: its sketch goes, its dependents stay
    session._model.ForceRebuild3(False)


def test_reference_planes_move_along_with_what_is_built_on_them(part):
    """A plane made at the end of the tree would not move earlier ('did not
    move'): it was built on a reference axis and another plane made after the
    place. Eleven later features were thrown away and made again. The
    reference geometry it stands on now moves along."""
    part.add_box(40, 20, 10)
    part.add_hole(4, 10, 10)
    slant = part.add_plane("front", angle_deg=30, about="y", name="Slant")
    part.add_plane("Slant", offset_mm=5, name="SlantOff")
    moved = part.reorder_feature("SlantOff", "Hole")
    order = moved["features"]
    assert order.index("SlantOff") < order.index("Hole"), f"the plane did not move: {order}"
    assert all(order.index(ref) < order.index("SlantOff") for ref in ("Axis Y", "Slant")), order
    assert sorted(moved["moved_along"]) == ["Axis Y", "Slant"]
    assert slant["plane"] == "Slant"


def test_a_feature_built_on_a_later_solid_feature_still_refuses(part):
    part.add_box(40, 20, 10)
    hole = part.add_hole_on_face(4, "+z", 20, 10, 10, depth_mm=3)
    with pytest.raises(SolidWorksError, match="built on"):
        part.reorder_feature(hole["feature"], "BlockExtrude")


def test_list_features_gives_the_history_in_tree_order(part):
    box_sketch, hole_sketch = block_with_hole_and_rounds(part)

    listed = part.list_features()

    history = [(f["name"], f["type"]) for f in listed["features"]]
    assert history == [(box_sketch, "ProfileFeature"), ("Base", "Extrusion"),
                       (hole_sketch, "ProfileFeature"), ("Hole", "ICE"), ("Round", "Fillet")], (
        "list_features must give what was modelled, in tree order, without SolidWorks' folders, "
        f"default planes and origin; got {history}"
    )
    assert not any(f["suppressed"] or "error" in f for f in listed["features"]), listed["features"]
    assert listed["under_defined_sketches"] == []


def test_list_features_names_sketches_that_can_still_move(part):
    """A part drawn by hand may hold sketches that are not fully defined; an
    agent continuing on it must see which, before it trusts their dimensions."""
    part.add_box(40, 20, 10, name="Base")
    loose = leave_loose_sketch(part)

    assert part.list_features()["under_defined_sketches"] == [f"{loose} (under defined)"]

    part.delete_feature(loose)
    assert part.list_features()["under_defined_sketches"] == [], "the loose sketch survived its delete"


def test_list_features_flags_what_fails_to_rebuild(part):
    block_with_hole_and_rounds(part)
    delete_keeping_dependents(part, "Base")

    flagged = {f["name"]: f.get("error") for f in part.list_features()["features"]}

    assert flagged["Hole"] and flagged["Hole"]["code"] and not flagged["Hole"]["warning"], (
        f"the hole has no block left to cut, so it must show as failing: {flagged}"
    )
    part.delete_feature("Hole")
    assert part.list_features()["features"] == [], "deleting the broken hole must clear the part"


def test_delete_feature_takes_its_sketch_along(part):
    _, hole_sketch = block_with_hole_and_rounds(part)

    result = part.delete_feature("Hole")

    assert abs(vol(result) - (BLOCK - ROUNDS)) < 0.01, "deleting the hole must give the rounded block back"
    assert result["deleted"] == [hole_sketch, "Hole"], f"the hole must go with its sketch: {result['deleted']}"
    assert hole_sketch not in [f["name"] for f in part.list_features()["features"]], (
        "the hole's sketch stayed behind: an orphan sketch the person has to clean up by hand"
    )


def test_delete_feature_refuses_to_break_what_depends_on_it(part):
    block_with_hole_and_rounds(part)

    with pytest.raises(SolidWorksError, match="with_children") as refusal:
        part.delete_feature("Base")

    assert "Hole" in str(refusal.value) and "Round" in str(refusal.value), (
        f"the refusal must name the features that would break: {refusal.value}"
    )
    assert abs(vol(part.get_mass_properties()) - (BLOCK - HOLE - ROUNDS)) < 0.01, "a refused delete changed the part"

    everything = part.delete_feature("Base", with_children=True)
    assert abs(vol(everything)) < 1e-9 and part.list_features()["features"] == [], (
        f"with_children must delete the dependents too; left: {part.list_features()['features']}"
    )


def test_delete_feature_names_everything_that_goes_along(part):
    """The refusal named only the direct dependents ('Bridge'), yet
    with_children deleted what was built on those too. A 40x20x10 block, a
    Ø10 x 5 boss on its top, a Ø6 x 4 post on the boss, a Ø2 x 3 hole in the
    post: the hole is built on the post, not on the boss."""
    part.add_box(40, 20, 10, name="Base")
    part.add_boss_on_face(10, "+z", 20, 10, 10, 5, name="Boss")
    part.add_boss_on_face(6, "+z", 20, 10, 15, 4, name="Post")
    part.add_hole_on_face(2, "+z", 20, 10, 19, depth_mm=3, name="PostHole")
    whole = BLOCK + math.pi * (25 * 5 + 9 * 4 - 1 * 3)

    with pytest.raises(SolidWorksError, match="with_children") as refusal:
        part.delete_feature("Boss")
    assert "PostHole" in str(refusal.value), (
        f"the refusal must name everything with_children deletes, not only the direct dependents: {refusal.value}"
    )
    planned = part.delete_feature("Boss", with_children=True, dry_run=True)
    assert abs(vol(planned) - whole) < 0.01, "a dry run changed the part"

    done = part.delete_feature("Boss", with_children=True)
    assert planned["would_delete"] == done["deleted"], "the dry run listed other features than the delete took"
    assert [f["name"] for f in part.list_features()["features"]][-1] == "Base"


def test_reorder_feature_moves_a_boss_before_the_cuts_it_filled(part):
    """A boss added last filled the holes made before it, without a word;
    moved before them, they cut through it again. A 40x20x10 block, a Ø6
    through hole at (10, 10), then a 10 x 10 web over the hole, 10 high."""
    box = part.add_box(40, 20, 10, name="Base")
    hole = part.add_hole(6, 10, 10, name="Hole")
    web = part.add_extruded_profile([[5, 5], [15, 5], [15, 15], [5, 15]], 10, name="Web")
    assert abs(vol(web) - BLOCK) < 0.01, "the web should fill the hole while it comes last"

    moved = part.reorder_feature("Web", before="Hole")

    assert abs(vol(moved) - (BLOCK - HOLE)) < 0.01, "the hole does not cut through the moved web"
    order = [sketch_of(box, "width"), "Base", sketch_of(web, "x1"), "Web", sketch_of(hole, "diameter"), "Hole"]
    assert moved["features"] == order, "the web and its sketch belong before the hole's sketch"


def test_reorder_feature_refuses_to_move_a_feature_before_what_it_is_built_on(part):
    part.add_box(40, 20, 10, name="Base")
    part.add_hole(6, 10, 10, name="Hole")  # sketched on the block's top face

    with pytest.raises(SolidWorksError, match="built on Base"):
        part.reorder_feature("Hole", before="Base")
    assert abs(vol(part.get_mass_properties()) - (BLOCK - HOLE)) < 0.01


def test_suppress_feature_round_trips_with_its_dependents(part):
    block_with_hole_and_rounds(part)

    no_hole = part.suppress_feature("Hole")
    assert no_hole["changed"] == ["Hole"] and abs(vol(no_hole) - (BLOCK - ROUNDS)) < 0.01, no_hole
    assert abs(vol(part.suppress_feature("Hole", suppress=False)) - (BLOCK - HOLE - ROUNDS)) < 0.01

    nothing = part.suppress_feature("Base")
    assert {"Base", "Hole", "Round"} <= set(nothing["changed"]) and abs(vol(nothing)) < 1e-9, nothing
    restored = part.suppress_feature("Base", suppress=False)
    assert abs(vol(restored) - (BLOCK - HOLE - ROUNDS)) < 0.01, (
        "unsuppressing the block must bring back the hole and rounds it took along; "
        f"still suppressed: {[f['name'] for f in part.list_features()['features'] if f['suppressed']]}"
    )


def test_an_unknown_feature_name_lists_the_real_ones(part):
    part.add_box(40, 20, 10, name="Base")

    with pytest.raises(SolidWorksError, match="Base"):
        part.suppress_feature("Hole")
