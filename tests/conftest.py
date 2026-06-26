"""Shared pytest fixtures.

Two layers of tests live under tests/:
- pure unit tests (no SolidWorks) -- always run, CI-friendly;
- integration tests marked `solidworks` -- they use the `part` fixture below,
  which connects to a running SolidWorks and SKIPS the whole suite if it can't.

Run only the fast layer with:  pytest -m "not solidworks"
"""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.session import SolidWorksSession


@pytest.fixture(scope="session")
def sw():
    """A connected SolidWorksSession shared across integration tests.

    Skips the integration suite when SolidWorks is not reachable.
    """
    session = SolidWorksSession()
    try:
        session.connect()
    except SolidWorksError as exc:
        pytest.skip(f"SolidWorks niet bereikbaar: {exc}")
    return session


@pytest.fixture
def part(sw):
    """A fresh empty part for one test; closed afterwards."""
    sw.new_part()
    yield sw
    try:
        sw.close_part()
    except SolidWorksError:
        pass
