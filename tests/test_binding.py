"""Pure unit tests for binding.py -- the COM boundary is faked, no SolidWorks needed."""

import types

import pythoncom
import pytest

from solidworks_mcp import binding
from solidworks_mcp.errors import SolidWorksError


class _FakeSldWorks:
    def __init__(self, oleobj):
        self.oleobj = oleobj
        self.Visible = False


@pytest.fixture
def running_solidworks(monkeypatch):
    """Fake a running SolidWorks that reports `revision`; returns the list of
    (major, minor) typelib versions gencache is asked to load."""

    def start(revision):
        requested = []
        raw = types.SimpleNamespace(_oleobj_=object())
        monkeypatch.setattr(binding.win32com.client, "GetActiveObject", lambda progid: raw)
        monkeypatch.setattr(
            binding.win32com.client.dynamic,
            "DumbDispatch",
            lambda oleobj: types.SimpleNamespace(RevisionNumber=revision),
        )

        def ensure_module(guid, lcid, major, minor):
            requested.append((major, minor))
            return types.SimpleNamespace(ISldWorks=_FakeSldWorks, loaded_for=major)

        monkeypatch.setattr(binding.gencache, "EnsureModule", ensure_module)
        monkeypatch.setattr(binding, "_mod", None)
        return requested

    return start


@pytest.mark.parametrize("revision, major", [("34.3.0", 34), ("33.1.0", 33), ("31.5.0", 31)])
def test_connect_loads_the_typelib_of_the_running_solidworks(running_solidworks, revision, major):
    """Regression: the typelib version was hard-coded to 34 (SW 2026), so on any
    other release the first call failed with 'Library not registered'."""
    requested = running_solidworks(revision)

    binding.connect()

    assert requested == [(major, 0)], (
        f"SolidWorks {revision} is running but typelib {requested} was loaded -- "
        "the server only works on the one release whose version is hard-coded"
    )
    assert binding.module().loaded_for == major, (
        "module() must hand the session the wrappers for the SolidWorks it connected to"
    )


def test_connect_unregistered_typelib_is_a_readable_error(running_solidworks, monkeypatch):
    running_solidworks("33.1.0")

    def not_registered(*args):
        raise pythoncom.com_error(-2147319779, "Library not registered.", None, None)

    monkeypatch.setattr(binding.gencache, "EnsureModule", not_registered)

    with pytest.raises(SolidWorksError, match="33"):
        binding.connect()


def test_connect_rejects_an_unparseable_revision(running_solidworks):
    running_solidworks("")

    with pytest.raises(SolidWorksError):
        binding.connect()


def test_module_before_connect_fails_loud(monkeypatch):
    monkeypatch.setattr(binding, "_mod", None)

    with pytest.raises(SolidWorksError):
        binding.module()
