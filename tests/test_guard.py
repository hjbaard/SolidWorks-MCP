"""A failed tool call leaves the part as it was.

Every MCP tool runs through SolidWorksSession.run_guarded: on a failure it
removes what the call added to the current part. Without it a refused call left
its sketch behind, which also counted in the bounding box.
"""

import asyncio

import pytest

from solidworks_mcp.constants import SW_DOC_PART
from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.session import SolidWorksSession


class RecordingSession(SolidWorksSession):
    """Records the guard's steps instead of touching SolidWorks."""

    def __init__(self, left=()):
        super().__init__()
        self.events = []
        self.left = list(left)

    def _history_snapshot(self):
        self.events.append("snapshot")
        return "Part1", {"Sketch1", "Base"}

    def _roll_back(self, snapshot):
        self.events.append(("roll back", snapshot))
        return self.left


def test_a_failed_call_is_rolled_back_to_the_snapshot_taken_before_it():
    session = RecordingSession()

    def fails():
        session.events.append("call")
        raise SolidWorksError("FeatureCut4 failed (None).")

    with pytest.raises(SolidWorksError, match="FeatureCut4"):
        session.run_guarded(fails)

    assert session.events == ["snapshot", "call", ("roll back", ("Part1", {"Sketch1", "Base"}))]


def test_a_successful_call_is_left_alone():
    session = RecordingSession()

    assert session.run_guarded(lambda: "done") == "done"
    assert session.events == ["snapshot"], "a call that worked must not be rolled back"


def test_what_could_not_be_removed_is_named_in_the_error():
    session = RecordingSession(left=["Sketch5"])

    def fails():
        raise SolidWorksError("Point is not on the +z face.")

    with pytest.raises(SolidWorksError, match="not on the .z face. Could not remove what the call added: Sketch5"):
        session.run_guarded(fails)


@pytest.mark.solidworks
def test_a_failed_tool_call_through_the_server_leaves_the_part_as_it_was():
    """End to end, the way an MCP client calls: a pocket drawn beside the block
    fails only after its sketch is complete, which then stayed in the tree and
    stretched the bounding box to x = 110. (A sketch left empty SolidWorks drops
    by itself, so that case proves nothing.)"""
    from solidworks_mcp import server

    def call(tool, *args):
        return asyncio.run(tool(*args))

    call(server.new_part)
    try:
        block = call(server.add_box, 40, 20, 10)
        before = call(server.list_features)["features"]

        refused = call(server.cut_profile, [[100, 100], [110, 100], [110, 110], [100, 110]], 2)

        assert refused["ok"] is False, refused
        assert call(server.list_features)["features"] == before, "the failed call left its sketch in the tree"
        assert call(server.get_bounding_box)["bounding_box_mm"] == block["mass_properties"]["bounding_box_mm"]
    finally:
        call(server.close_part)


# --- the active document comes back -----------------------------------------
#
# A tool that writes makes its document SolidWorks' active one (_require_model).
# When that took the front from another document, the call gives it back at the
# end, also when it failed; a call that found its document in front switches
# nothing. A fake SolidWorks records every activation.

class FakeDocument:
    def __init__(self, title):
        self.title = title

    def GetTitle(self):
        return self.title

    def GetType(self):
        return SW_DOC_PART

    def GetPathName(self):
        return ""

    def GetSaveFlag(self):
        return False

    Visible = True


class FakeSolidWorks:
    def __init__(self, *titles):
        self.documents = [FakeDocument(t) for t in titles]
        self.ActiveDoc = self.documents[0]
        self.CommandInProgress = False
        self.activations = []

    def GetDocuments(self):
        return list(self.documents)

    def ActivateDoc3(self, title, *_):
        self.activations.append(title)
        self.ActiveDoc = next(d for d in self.documents if d.title == title)

    def close(self, title):
        self.documents = [d for d in self.documents if d.title != title]
        if self.ActiveDoc.title == title:
            self.ActiveDoc = self.documents[0] if self.documents else None


class _Unwrapped:
    """binding.wrap(obj, cls) returns cls(obj): here, the object itself."""
    IModelDoc2 = staticmethod(lambda obj: obj)


class FrontSession(RecordingSession):
    """The server's own part is 'Part1'; 'Theirs' is in front when a call starts."""

    def __init__(self, active="Theirs"):
        super().__init__()
        self._sw = FakeSolidWorks(active, *(t for t in ("Theirs", "Part1") if t != active))
        self._mod = _Unwrapped
        self._model = next(d for d in self._sw.documents if d.title == "Part1")

    def _quiet(self, model):
        pass

    def writes(self):
        self._require_model()
        return self._sw.ActiveDoc.title  # in front while the tool works


def test_a_call_that_took_the_front_gives_it_back():
    session = FrontSession()

    assert session.run_guarded(session.writes) == "Part1", "the tool did not work in its own document"
    assert session._sw.ActiveDoc.title == "Theirs"
    assert session._sw.activations == ["Part1", "Theirs"]


def test_a_call_whose_document_was_in_front_switches_nothing():
    session = FrontSession(active="Part1")

    session.run_guarded(session.writes)

    assert session._sw.activations == [], "a call that needed no switch must not switch back either"


def test_a_failed_call_gives_the_front_back_too():
    session = FrontSession()

    def fails():
        session._require_model()
        raise SolidWorksError("FeatureCut4 failed (None).")

    with pytest.raises(SolidWorksError, match="FeatureCut4"):
        session.run_guarded(fails)

    assert session._sw.ActiveDoc.title == "Theirs"


def test_a_document_closed_during_the_call_is_not_brought_back():
    session = FrontSession()

    def closes_theirs():
        session._require_model()
        session._sw.close("Theirs")

    session.run_guarded(closes_theirs)

    assert session._sw.activations == ["Part1"]
    assert session._sw.ActiveDoc.title == "Part1"


def test_activating_switches_back_once_after_several_steps():
    session = FrontSession()

    def two_steps():
        session._require_model()
        session._require_model()

    session.run_guarded(two_steps)

    assert session._sw.activations == ["Part1", "Theirs"]


def test_activate_document_keeps_the_document_it_was_asked_for_in_front():
    session = FrontSession()

    session.run_guarded(session.activate_document, "Part1")

    assert session._sw.ActiveDoc.title == "Part1"
    assert session._sw.activations == ["Part1"]
