"""Integration tests for working in a part a person drew: their sketches and planes, by name.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
A person's sketch is drawn here straight through the API and left without
dimensions, as a quick hand sketch often is; a person's plane is an offset
plane. Volumes are in mm^3, positions in mm.
"""

import math

import pytest

from solidworks_mcp import binding
from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

BOX = 40 * 20 * 10


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def box(result):
    return result["mass_properties"]["bounding_box_mm"]


@pytest.fixture
def drawn(sw):
    """A fresh part that may keep under-defined sketches: a person's, not a tool's."""
    sw.new_part()
    yield sw
    try:
        sw.close_part()
    except SolidWorksError:
        pass


def last_feature(session):
    return session.list_features()["features"][-1]["name"]


def person_rectangle(session, corner1, corner2):
    """An undimensioned rectangle on the Front plane (z = 0); returns the sketch name.

    Four lines, not CreateCornerRectangle: on SOLIDWORKS 2026 SP4.0 that call
    crashed SolidWorks every time, also with its window kept still."""
    model = session._model
    front, _ = session._named_plane("front")
    model.ClearSelection2(True)
    assert front.Select2(False, 0)
    sk = binding.wrap(model.SketchManager, binding.module().ISketchManager)
    sk.InsertSketch(True)
    (x1, y1), (x2, y2) = ((c[0] / 1000, c[1] / 1000) for c in (corner1, corner2))
    sk.AddToDB = True
    for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
        assert sk.CreateLine(*a, 0, *b, 0)
    sk.AddToDB = False
    sk.InsertSketch(True)
    return last_feature(session)


def person_circle(session, face, centre_mm, radius_mm):
    """An undimensioned circle on the face through centre_mm; returns the sketch name."""
    sk, sketch = session._open_sketch_on_face(face, centre_mm)
    u, v = session._model_to_sketch_uv(sketch, *(c / 1000 for c in centre_mm), face)
    sk.AddToDB = True
    sk.CreateCircleByRadius(u, v, 0, radius_mm / 1000)
    sk.AddToDB = False
    sk.InsertSketch(True)
    return last_feature(session)


# --- read_sketch ----------------------------------------------------------------


def test_read_sketch_gives_a_face_sketch_in_model_coordinates(part):
    """A sketch on the +x face has axes of its own; read back, it must sit
    where it is in the part (y and z differ, so swapped axes would show)."""
    part.add_box(40, 20, 10)
    boss = part.add_boss_on_face(6, "+x", 40, 12, 4, 5)
    read = part.read_sketch(boss["dimensions"]["diameter"].split("@")[1])

    assert read["fully_defined"] is True and read["status"] == "fully defined"
    [circle] = read["segments"]
    assert circle["type"] == "circle" and circle["radius_mm"] == pytest.approx(3)
    assert circle["centre_mm"] == pytest.approx([40, 12, 4], abs=1e-4), f"not in model coordinates: {circle}"
    assert boss["dimensions"]["diameter"] in {d["name"] for d in read["dimensions"]}


def test_read_sketch_gives_rounded_corners_as_arcs(part):
    part.add_extruded_profile([[0, 0], [30, 0], [30, 20], [0, 20]], 5, corner_radii_mm=4)
    read = part.read_sketch(part.list_features()["features"][0]["name"])

    arcs = [s for s in read["segments"] if s["type"] == "arc"]
    assert len(arcs) == 4 and all(a["radius_mm"] == pytest.approx(4) for a in arcs), arcs
    centres = sorted(tuple(round(c, 4) for c in a["centre_mm"]) for a in arcs)
    assert centres == [(4, 4, 0), (4, 16, 0), (26, 4, 0), (26, 16, 0)]


def test_read_sketch_reports_a_persons_sketch_as_drawn(drawn):
    drawn.add_box(40, 20, 10)
    name = person_rectangle(drawn, (30, 0), (50, 20))
    read = drawn.read_sketch(name)

    assert read["fully_defined"] is False and read["status"] == "under defined"
    assert read["dimensions"] == []
    edges = sorted(tuple(sorted([tuple(s["start_mm"]), tuple(s["end_mm"])]))
                   for s in read["segments"] if s["type"] == "line" and not s["construction"])
    assert edges == [((30, 0, 0), (30, 20, 0)), ((30, 0, 0), (50, 0, 0)),
                     ((30, 20, 0), (50, 20, 0)), ((50, 0, 0), (50, 20, 0))]


def test_read_sketch_names_the_sketches_there_are(part):
    part.add_box(40, 20, 10)
    with pytest.raises(SolidWorksError, match="there are: Sketch1"):
        part.read_sketch("Sketch9")


# --- extrude_sketch / cut_sketch ------------------------------------------------


def test_extrude_sketch_builds_on_a_persons_sketch(drawn):
    # the rectangle x 30..50 overlaps the 40-long box by 10: 10 x 20 x 5 is new
    drawn.add_box(40, 20, 10)
    name = person_rectangle(drawn, (30, 0), (50, 20))
    result = drawn.extrude_sketch(name, 5)
    assert vol(result) == pytest.approx(BOX + 10 * 20 * 5)
    assert box(result)["max_mm"] == pytest.approx([50, 20, 10])

    # the depth stays a dimension a person can change
    deeper = drawn.set_dimension(result["dimensions"]["depth"], 8)
    assert vol(deeper) == pytest.approx(BOX + 10 * 20 * 8)


def test_extrude_sketch_reverse_goes_against_the_normal(drawn):
    # under the box: z -5..0 below the whole rectangle, 20 x 20 x 5 new
    drawn.add_box(40, 20, 10)
    name = person_rectangle(drawn, (30, 0), (50, 20))
    result = drawn.extrude_sketch(name, 5, reverse=True)
    assert vol(result) == pytest.approx(BOX + 20 * 20 * 5)
    assert box(result)["min_mm"] == pytest.approx([0, 0, -5])


def test_cut_sketch_goes_into_the_part_from_a_face(drawn):
    drawn.add_box(40, 20, 10)
    name = person_circle(drawn, "+z", [20, 10, 10], 4)
    result = drawn.cut_sketch(name, 3)
    assert vol(result) == pytest.approx(BOX - math.pi * 4 ** 2 * 3, abs=1e-3)
    assert box(result)["max_mm"] == pytest.approx([40, 20, 10]), "the cut went out of the part"


def test_cut_sketch_from_a_plane_tells_when_it_needs_reverse(drawn):
    """From the Front plane the cut's own direction points away from the box:
    the refusal must say how to fix it, and reverse=True then cuts."""
    drawn.add_box(40, 20, 10)
    name = person_rectangle(drawn, (5, 5), (15, 15))
    with pytest.raises(SolidWorksError, match="reverse=True"):
        drawn.cut_sketch(name)
    result = drawn.cut_sketch(name, reverse=True)
    assert vol(result) == pytest.approx(BOX - 10 * 10 * 10)


# --- planes by name -------------------------------------------------------------


def test_mirror_about_a_persons_plane_moved_along_its_normal(part):
    # the person's plane at x = 15, moved 5 further: the hole at x = 10 lands at 30
    part.add_box(40, 20, 10)
    part.add_hole(6, 10, 10, name="Hole")
    plane, _ = part._plane_at("right", 15)
    part.add_mirror(plane.Name, offset_mm=5, features=["Hole"])
    holes = sorted(f["cylinder"]["point_mm"][0] for f in part.list_faces()["faces"] if "cylinder" in f)
    assert holes == pytest.approx([10, 30], abs=1e-4), f"mirrored about the wrong place: holes at x = {holes}"


def test_cut_through_a_persons_plane(part):
    # the person's plane at z = 5: the square cuts through the box both ways
    part.add_box(40, 20, 10)
    plane, _ = part._plane_at("front", 5)
    result = part.cut_profile_through_plane([[10, 5, 5], [20, 5, 5], [20, 15, 5], [10, 15, 5]], plane.Name)
    assert vol(result) == pytest.approx(BOX - 10 * 10 * 10)


def test_an_unknown_plane_names_the_planes_of_the_part(part):
    part.add_box(40, 20, 10)
    plane, _ = part._plane_at("front", 5)
    with pytest.raises(SolidWorksError, match=f"No plane 'side'.*{plane.Name}"):
        part.add_mirror("side")
    with pytest.raises(SolidWorksError, match=f"No plane 'side'.*{plane.Name}"):
        part.cut_profile_through_plane([[0, 0, 0], [0, 1, 0], [0, 0, 1]], "side")
