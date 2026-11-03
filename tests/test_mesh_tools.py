"""Pure unit tests for mesh reading and slicing (no SolidWorks).

Reverse-engineering a part from a mesh starts with its cross-sections; these
tests use meshes whose sections are known exactly.
"""

import math
import struct
import zipfile

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.mesh_tools import compare_sections, load_mesh, section, simplify

# a 40 x 20 x 10 box from the origin, as 12 triangles
_BOX_V = [(0, 0, 0), (40, 0, 0), (40, 20, 0), (0, 20, 0), (0, 0, 10), (40, 0, 10), (40, 20, 10), (0, 20, 10)]
_BOX_T = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
          (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
BOX = [tuple(_BOX_V[i] for i in t) for t in _BOX_T]


def _area(points):
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]))) / 2


def _write_binary_stl(path, triangles):
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(triangles)))
        for tri in triangles:
            f.write(struct.pack("<12fH", 0, 0, 0, *[c for v in tri for c in v], 0))


def test_box_section_is_its_footprint():
    loops = section(BOX, "z", 5.0)
    assert len(loops) == 1
    assert _area(loops[0]) == pytest.approx(800)


def test_section_axes_give_the_other_two_coordinates():
    # across y the box shows its x-z face: 40 x 10
    (loop,) = section(BOX, "y", 7.0)
    xs, zs = [p[0] for p in loop], [p[1] for p in loop]
    assert (min(xs), max(xs), min(zs), max(zs)) == pytest.approx((0, 40, 0, 10))


def _square_ring(outer, inner, height):
    """A closed (manifold) mesh of a square ring centred on the origin."""
    def square(s, z):
        h = s / 2
        return [(-h, -h, z), (h, -h, z), (h, h, z), (-h, h, z)]
    o0, o1, i0, i1 = square(outer, 0), square(outer, height), square(inner, 0), square(inner, height)
    tris = []
    for k in range(4):
        n = (k + 1) % 4
        for a, b, c, d in ((o1[k], o1[n], i1[n], i1[k]), (o0[k], i0[k], i0[n], o0[n]),  # top, bottom
                           (o0[k], o0[n], o1[n], o1[k]), (i0[k], i1[k], i1[n], i0[n])):  # outer, inner wall
            tris += [(a, b, c), (a, c, d)]
    return tris


def test_section_through_a_hole_has_an_outer_and_an_inner_loop():
    loops = section(_square_ring(30, 10, 5), "z", 2.5)
    assert sorted(round(_area(l), 6) for l in loops) == [100, 900]


def test_sections_come_largest_first():
    loops = section(_square_ring(30, 10, 5), "z", 2.5)
    assert _area(loops[0]) > _area(loops[1])


def test_section_outside_the_mesh_is_empty():
    assert section(BOX, "z", 20.0) == []


def test_simplify_removes_only_exactly_collinear_points():
    square = [(0, 0), (5, 0), (10, 0), (10, 10), (0, 10)]
    assert simplify(square) == [(0, 0), (10, 0), (10, 10), (0, 10)]
    bent = [(0, 0), (5, 0.001), (10, 0), (10, 10), (0, 10)]  # 1 um off: a real wall
    assert len(simplify(bent)) == 5


def test_unknown_axis_raises():
    with pytest.raises(SolidWorksError):
        section(BOX, "w", 1.0)


def test_binary_stl_round_trip(tmp_path):
    path = tmp_path / "box.stl"
    _write_binary_stl(path, BOX)
    assert _area(section(load_mesh(str(path)), "z", 5.0)[0]) == pytest.approx(800)


def test_ascii_stl(tmp_path):
    path = tmp_path / "box_ascii.stl"
    lines = ["solid box"]
    for tri in BOX:
        lines += ["facet normal 0 0 0", "outer loop"] + [f"vertex {x} {y} {z}" for x, y, z in tri]
        lines += ["endloop", "endfacet"]
    path.write_text("\n".join(lines + ["endsolid box"]), encoding="ascii")
    assert len(load_mesh(str(path))) == 12


def test_3mf_component_transform_is_applied(tmp_path):
    # an object that references the box through a component moved +100 in x,
    # the way slicers store a mesh in a separate model file
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in _BOX_V)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in _BOX_T)
    ns = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
    part = f'<model {ns}><resources><object id="1" type="model"><mesh><vertices>{verts}</vertices>' \
           f'<triangles>{tris}</triangles></mesh></object></resources></model>'
    main = (f'<model {ns} xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
            '<resources><object id="2" type="model"><components>'
            '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 100 0 0"/>'
            '</components></object></resources><build><item objectid="2"/></build></model>')
    path = tmp_path / "box.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", main)
        z.writestr("3D/Objects/object_1.model", part)
    (loop,) = section(load_mesh(str(path)), "z", 5.0)
    assert min(p[0] for p in loop) == pytest.approx(100) and _area(loop) == pytest.approx(800)


def test_unreadable_file_raises(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="ascii")
    with pytest.raises(SolidWorksError):
        load_mesh(str(path))


def test_compare_pairs_loops_by_position_not_by_size():
    # the part's hole is smaller than the reference's: the pairing must still
    # pair outer with outer and hole with hole, then report the difference
    reference = section(_square_ring(30, 10, 5), "z", 2.5)
    part = section(_square_ring(30, 8, 5), "z", 2.5)
    result = compare_sections(reference, part)
    by_ref = {round(p["reference_area_mm2"]): p for p in result["pairs"]}
    assert by_ref[900]["area_diff_mm2"] == pytest.approx(0), "the outer loops are the same"
    assert by_ref[900]["part_area_mm2"] == pytest.approx(900)
    assert by_ref[100]["part_area_mm2"] == pytest.approx(64)
    assert by_ref[100]["max_extent_diff_mm"] == pytest.approx(1.0), "a hole 1 mm smaller per side"
    assert result["unmatched_reference"] == result["unmatched_part"] == 0


def test_compare_counts_a_missing_loop():
    reference = section(_square_ring(30, 10, 5), "z", 2.5)
    result = compare_sections(reference, section(BOX, "z", 5.0))
    assert result["unmatched_reference"] == 1 and result["unmatched_part"] == 0


def test_session_slices_a_mesh_without_solidworks(tmp_path):
    from solidworks_mcp.session import SolidWorksSession
    path = tmp_path / "box.stl"
    _write_binary_stl(path, BOX)
    result = SolidWorksSession().slice_mesh(str(path), "z", [5.0, 20.0])
    first, empty = result["sections"]
    assert first["loops"][0]["area_mm2"] == pytest.approx(800)
    assert first["loops"][0]["point_count"] == 4 and len(first["loops"][0]["points_mm"]) == 4
    assert empty["loops"] == []


def test_the_slice_mesh_tool_works_while_solidworks_is_gone(tmp_path, monkeypatch):
    """SolidWorks dropped its link three times; slice_mesh then said 'No running
    SolidWorks found', though it only reads an STL."""
    import asyncio

    from solidworks_mcp import server

    class GoneSolidWorks:
        async def call(self, fn):
            raise SolidWorksError("No running SolidWorks found.")

    monkeypatch.setattr(server, "_worker", GoneSolidWorks())
    path = tmp_path / "box.stl"
    _write_binary_stl(path, BOX)
    result = asyncio.run(server.slice_mesh(str(path), "z", [5.0]))
    assert result.get("ok", True) is not False, f"slicing a mesh needed SolidWorks: {result}"
    assert result["sections"][0]["loops"][0]["area_mm2"] == pytest.approx(800)
