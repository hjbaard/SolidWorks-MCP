"""Integration tests: the tools that only read leave SolidWorks' active
document alone, so a person or another session working in SolidWorks keeps
their window while this server reads its own document.

Each test makes another document the active one, calls the tool the way the
server does (through run_guarded), and checks the active title did not move.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
"""

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
    other = binding.wrap(sw._sw.NewDocument(template, 0, 0, 0), sw._mod.IModelDoc2)
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


@pytest.mark.parametrize("tool, args", [
    ("list_components", ()),
    ("check_interference", ()),
    ("measure_distance", ("block_a", "block_b")),
    ("get_assembly_bounding_box", ()),
])
def test_an_assembly_read_leaves_the_active_document(assembly, blocks, tool, args):
    assembly.insert_component(blocks["block_a"])
    assembly.insert_component(blocks["block_b"], 60, 0, 0)
    _assert_leaves_active_document(assembly, tool, *args)


def test_a_read_still_answers_for_the_current_document(part):
    """Not activated, the tool still reads the server's own document, not the active one."""
    part.add_box(40, 20, 10)
    other = _other_document(part)
    try:
        volume = part.run_guarded(part.get_mass_properties)["mass_properties"]["volume_mm3"]
        assert volume == pytest.approx(8000)
    finally:
        part._sw.CloseDoc(other)
