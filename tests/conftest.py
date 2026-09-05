"""Shared pytest fixtures.

Two layers of tests live under tests/:
- pure unit tests (no SolidWorks) -- always run, CI-friendly;
- integration tests marked `solidworks` -- they use the `part` fixture below,
  which connects to a running SolidWorks and SKIPS the whole suite if it can't.

Run only the fast layer with:  pytest -m "not solidworks"
"""

import pytest

from solidworks_mcp import binding
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


# --- assemblies ---------------------------------------------------------------

BLOCK_A = (40.0, 20.0, 10.0)   # saved as block_a.sldprt
BLOCK_B = (20.0, 20.0, 20.0)   # saved as block_b.sldprt


@pytest.fixture(scope="session")
def blocks(sw, tmp_path_factory):
    """Two saved .sldprt blocks to assemble, built with the part tools.

    Assembly tests need components on disk. Building them here keeps the suite
    self-contained (no machine-specific sample files) and hand-calculable.
    """
    directory = tmp_path_factory.mktemp("blocks")
    paths = {}
    for name, size in (("block_a", BLOCK_A), ("block_b", BLOCK_B)):
        sw.new_part()
        sw.add_box(*size)
        paths[name] = sw.save_part(str(directory / f"{name}.sldprt"))["path"]
        sw.close_part()
    return paths


@pytest.fixture
def assembly(sw, blocks):
    """A fresh empty assembly for one test; it and its components close afterwards."""
    sw.new_assembly()
    yield sw
    _close_documents(sw, {"block_a.sldprt", "block_b.sldprt"})


def _close_documents(sw, component_files):
    """Close the current assembly plus the component windows it opened.

    Inserting a component opens its part document, so without this the suite
    would accumulate open documents across tests.
    """
    try:
        sw.close_part()
    except SolidWorksError:
        pass
    for document in (sw._sw.GetDocuments() or []):
        title = binding.wrap(document, binding.module().IModelDoc2).GetTitle()
        if title.lower() in component_files:
            sw._sw.CloseDoc(title)
