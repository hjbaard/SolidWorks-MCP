"""Unit helpers.

The SolidWorks API works in METRES and RADIANS internally, regardless of the
document's display units. Values crossing the COM boundary are converted here so
the rest of the code can think in millimetres and degrees -- this is one of the
most common silent bug sources in SolidWorks automation.
"""

import math


def mm_to_m(value_mm: float) -> float:
    return value_mm / 1000.0


def m_to_mm(value_m: float) -> float:
    return value_m * 1000.0


def deg_to_rad(value_deg: float) -> float:
    return math.radians(value_deg)
