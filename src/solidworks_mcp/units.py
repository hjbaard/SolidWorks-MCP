"""Unit helpers.

The SolidWorks API works in METRES internally, regardless of the document's
display units. Length values crossing the COM boundary are converted here so the
rest of the code can think in millimetres -- this is one of the most common
silent bug sources in SolidWorks automation. (Angle helpers will be added next
to their first caller when an angle-taking feature like revolve lands.)
"""


def mm_to_m(value_mm: float) -> float:
    return value_mm / 1000.0


def m_to_mm(value_m: float) -> float:
    return value_m * 1000.0
