"""Pure unit tests for unit conversions (no SolidWorks)."""

import math

from solidworks_mcp.units import deg_to_rad, m_to_mm, mm_to_m


def test_mm_to_m():
    assert mm_to_m(1000) == 1.0
    assert mm_to_m(0) == 0.0
    assert mm_to_m(40) == 0.04


def test_m_to_mm():
    assert m_to_mm(1.0) == 1000.0
    assert m_to_mm(0.04) == 40.0


def test_length_roundtrip():
    for v in (0, 1, 12.5, 8000):
        assert abs(m_to_mm(mm_to_m(v)) - v) < 1e-9


def test_deg_to_rad():
    assert deg_to_rad(0) == 0.0
    assert abs(deg_to_rad(180) - math.pi) < 1e-12
    assert abs(deg_to_rad(360) - 2 * math.pi) < 1e-12
    assert abs(deg_to_rad(45) - math.pi / 4) < 1e-12
