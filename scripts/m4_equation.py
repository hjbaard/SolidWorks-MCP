"""M4 - equations, standalone.

Drives a dimension via a global equation (with an expression) and verifies the
volume follows. Proves IEquationMgr wiring + expression evaluation.

    .venv\\Scripts\\python.exe scripts\\m4_equation.py
"""

import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    session = SolidWorksSession()
    session.connect()
    session.new_part()

    box = session.add_box(40, 20, 10)
    v0 = box["mass_properties"]["volume_mm3"]
    res = session.set_equation('"D1@BlockExtrude" = 2 * 12.5')  # depth -> 25 mm
    v = res["mass_properties"]["volume_mm3"]
    expected = 40 * 20 * 25
    rel = abs(v - expected) / expected
    print(f"OK: block starts at {v0:.1f} mm^3; equation '{res['equation']}' (index {res['index']})")
    print(f"  volume after equation: {v:.1f} mm^3 (expected {expected}), rebuild_ok={res['rebuild_ok']}, rel {rel:.1e}")

    session.close_part()

    if abs(v0 - 8000) < 1e-6 and rel < 1e-6:
        print("\nM4 equation PASS: the equation drives the dimension (expression evaluated).")
        return 0
    print("\nM4 equation FAIL.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
