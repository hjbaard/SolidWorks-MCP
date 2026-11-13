"""Integration tests: the tools that only read leave SolidWorks' active
document alone, so a person or another session working in SolidWorks keeps
their window while this server reads its own document.

Each test makes another document the active one, calls the tool the way the
server does (through run_guarded), and checks the active title did not move.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
"""

import os

import pytest

from solidworks_mcp import binding
from solidworks_mcp.constants import SW_PREF_DEFAULT_TEMPLATE_PART

pytestmark = pytest.mark.solidworks


def _active_title(sw):
    active = binding.wrap(sw._sw.ActiveDoc, sw._mod.IModelDoc2)
    return active.GetTitle() if active is not None else None


def _other_document(sw):
    """A new part opened behind the server's back: it becomes SolidWorks'
    active document while the server's current document stays its own."""
    template = sw._sw.GetUserPreferenceStringValue(SW_PREF_DEFAULT_TEMPLATE_PART)
    # a 3DEXPERIENCE install may name a template that is no file ('~BLANK_PART_TEMPLATE.prtdot'),
    # where NewDocument returns None: new_part falls back to NewPart as well
    made = sw._sw.NewDocument(template, 0, 0, 0) if os.path.isfile(template) else sw._sw.NewPart()
    other = binding.wrap(made, sw._mod.IModelDoc2)
    assert other is not None, "could not open a second document"
    title = other.GetTitle()
    assert _active_title(sw) == title
    return title


def _assert_leaves_active_document(sw, tool, *args, **kwargs):
    other = _other_document(sw)
    try:
        sw.run_guarded(getattr(sw, tool), *args, **kwargs)
        assert _active_title(sw) == other, f"{tool} made its own document the active one"
    finally:
        sw._sw.CloseDoc(other)


def _sketch_name(sw):
    return next(f["name"] for f in sw.list_features()["features"] if f["type"] == "ProfileFeature")


@pytest.mark.parametrize("tool", [
    "list_dimensions", "list_features", "list_equations", "get_mass_properties", "get_bounding_box",
    "list_faces", "list_edges", "list_bodies", "check_printability",
])
def test_a_part_read_leaves_the_active_document(part, tool):
    part.add_box(40, 20, 10)
    _assert_leaves_active_document(part, tool)


def test_read_sketch_leaves_the_active_document(part):
    part.add_box(40, 20, 10)
    _assert_leaves_active_document(part, "read_sketch", _sketch_name(part))


def _two_blocks(assembly, blocks):
    """block_a at the origin (x 0..40, y 0..20, z 0..10), block_b 100 mm along x."""
    assembly.insert_component(blocks["block_a"], 0, 0, 0)
    assembly.insert_component(blocks["block_b"], 100, 0, 0)


@pytest.mark.parametrize("tool, args, kwargs", [
    ("list_components", (), {}),
    ("check_interference", (), {}),
    ("measure_distance", ("block_a", "block_b"), {}),
    ("measure_distance", ("block_a",), {"point_mm": [-10, 50, 5]}),
    ("get_assembly_bounding_box", (), {}),
], ids=["list_components", "check_interference", "measure_distance", "measure_distance_to_a_point",
        "get_assembly_bounding_box"])
def test_an_assembly_read_leaves_the_active_document(assembly, blocks, tool, args, kwargs):
    _two_blocks(assembly, blocks)
    _assert_leaves_active_document(assembly, tool, *args, **kwargs)


def test_measure_distance_to_an_axis_still_measures_with_another_document_active(assembly, blocks):
    """The axis case draws a temporary sketch line, so it makes the assembly
    active as any tool that writes; it must still measure right and clean up."""
    _two_blocks(assembly, blocks)
    other = _other_document(assembly)
    try:
        measured = assembly.run_guarded(assembly.measure_distance, "block_a", axis_mm=[[60, 0, 5], [0, 1, 0]])
        # the line was drawn in the active, stilled assembly (bringing the other
        # document back afterwards is the next step of #10)
        assert _active_title(assembly) == assembly._model.GetTitle(), "the axis line was drawn in a document that is not active"
        assert measured["distance_mm"] == pytest.approx(20, abs=1e-4)
        helpers = [f.Name for f in assembly._iter_features() if f.GetTypeName2() == "3DProfileFeature"]
        assert helpers == [], f"the helper line was left in the tree: {helpers}"
    finally:
        assembly._sw.CloseDoc(other)


def test_a_part_read_still_answers_for_the_current_document(part):
    """Not activated, the tools still read the server's own document, not the active one."""
    part.add_box(40, 20, 10)
    other = _other_document(part)
    try:
        assert part.run_guarded(part.get_mass_properties)["mass_properties"]["volume_mm3"] == pytest.approx(8000)
        assert part.run_guarded(part.list_faces)["count"] == 6
    finally:
        part._sw.CloseDoc(other)


def test_an_assembly_read_still_answers_for_the_current_document(assembly, blocks):
    _two_blocks(assembly, blocks)
    other = _other_document(assembly)
    try:
        assert assembly.run_guarded(assembly.list_components)["count"] == 2
    finally:
        assembly._sw.CloseDoc(other)
