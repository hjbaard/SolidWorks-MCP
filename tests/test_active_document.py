"""Integration tests: a tool that had to make its document SolidWorks' active
one gives the front back to the document that had it, so a person or another
session working in SolidWorks finds their window again after the call.

Each test makes another document the active one, calls the tool the way the
server does (through run_guarded), and checks which document is active after.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
"""

import pytest

from solidworks_mcp.errors import SolidWorksError
from test_read_only import _active_title, _other_document

pytestmark = pytest.mark.solidworks


def test_a_tool_that_writes_gives_the_front_back(part):
    other = _other_document(part)
    try:
        made = part.run_guarded(part.add_box, 40, 20, 10)
        assert _active_title(part) == other, "the part the server built in kept the front"
        assert made["mass_properties"]["volume_mm3"] == pytest.approx(8000), "the box was not built in the server's part"
    finally:
        part._sw.CloseDoc(other)


def test_a_refused_tool_gives_the_front_back_too(part):
    part.add_box(40, 20, 10)
    other = _other_document(part)
    try:
        with pytest.raises(SolidWorksError):
            # drawn beside the block: refused only once its sketch is complete
            part.run_guarded(part.cut_profile, [[100, 100], [110, 100], [110, 110], [100, 110]], 2)
        assert _active_title(part) == other, "a refused call kept its part in front"
    finally:
        part._sw.CloseDoc(other)


def test_a_tool_whose_document_is_in_front_leaves_it_there(part):
    part.run_guarded(part.add_box, 40, 20, 10)
    assert _active_title(part) == part._model.GetTitle()


def test_activate_document_keeps_its_document_in_front(part):
    mine = part._model.GetTitle()
    other = _other_document(part)
    try:
        part.run_guarded(part.activate_document, mine)
        assert _active_title(part) == mine, "activate_document was undone at the end of the call"
    finally:
        part._sw.CloseDoc(other)
