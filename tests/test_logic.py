"""Pure unit tests for SolidWorksSession's no-COM helpers.

These cover the fiddly logic added during the toolset build (selector parsing,
direction parsing, axis classification, polygon cleaning) without SolidWorks.
"""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.session import SolidWorksSession


@pytest.fixture
def s():
    return SolidWorksSession()  # not connected; only pure helpers are exercised


def test_parse_edge_indices_axis_and_all(s):
    assert s._parse_edge_indices("all") is None
    assert s._parse_edge_indices("x") is None
    assert s._parse_edge_indices("z") is None


def test_parse_edge_indices_lists(s):
    assert s._parse_edge_indices("2,5") == [2, 5]
    assert s._parse_edge_indices("2 5 7") == [2, 5, 7]
    assert s._parse_edge_indices([1, 3]) == [1, 3]


def test_parse_direction(s):
    assert s._parse_direction("+z") == (0.0, 0.0, 1.0)
    assert s._parse_direction("-x") == (-1.0, 0.0, 0.0)
    assert s._parse_direction("+Y") == (0.0, 1.0, 0.0)


def test_parse_direction_invalid(s):
    with pytest.raises(SolidWorksError):
        s._parse_direction("up")


def test_axis_of(s):
    assert s._axis_of(10, 0, 0, 10) == "x"
    assert s._axis_of(0, 5, 0, 5) == "y"
    assert s._axis_of(0, 0, -5, 5) == "z"
    assert s._axis_of(1, 1, 0, 2 ** 0.5) is None   # diagonal
    assert s._axis_of(0, 0, 0, 0) is None           # zero length


def test_clean_polygon_open_ring():
    pts = SolidWorksSession._clean_polygon([[0, 0], [40, 0], [40, 20]])
    assert pts == [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0)]


def test_clean_polygon_drops_explicit_closing_point():
    pts = SolidWorksSession._clean_polygon([[0, 0], [40, 0], [40, 20], [0, 0]])
    assert pts == [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0)]


def test_clean_polygon_dedupes_consecutive():
    pts = SolidWorksSession._clean_polygon([[0, 0], [0, 0], [40, 0], [40, 20]])
    assert pts == [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0)]


def test_clean_polygon_too_few_distinct():
    with pytest.raises(SolidWorksError):
        SolidWorksSession._clean_polygon([[0, 0], [40, 0]])
    with pytest.raises(SolidWorksError):
        SolidWorksSession._clean_polygon([[0, 0], [0, 0], [0, 0]])
