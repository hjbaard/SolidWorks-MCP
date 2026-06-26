"""M4 - generic extruded profile, standalone.

Extrudes an L-bracket outline and verifies the volume against polygon area (via
the shoelace formula) * depth. Proves arbitrary 2D profiles.

    .venv\\Scripts\\python.exe scripts\\m4_profile.py
    .venv\\Scripts\\python.exe scripts\\m4_profile.py --keep-open
"""

import argparse
import os
import sys

from solidworks_mcp.session import SolidWorksSession

L_BRACKET = [[0, 0], [60, 0], [60, 20], [20, 20], [20, 50], [0, 50]]
DEPTH = 10.0


def shoelace(pts):
    n = len(pts)
    s = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))
    return abs(s) / 2.0


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: generic extruded profile.")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    session.new_part()

    res = session.add_extruded_profile(L_BRACKET, DEPTH)
    v = res["mass_properties"]["volume_mm3"]
    expected = shoelace(L_BRACKET) * DEPTH
    rel = abs(v - expected) / expected
    print(f"OK: L-beugel ({len(L_BRACKET)} punten) geëxtrudeerd {DEPTH} mm, feature '{res['feature']}'")
    print(f"  volume    : {v:.3f} mm^3  (verwacht {expected:.3f} = opp {shoelace(L_BRACKET):.0f} x {DEPTH}), rel {rel:.1e}")

    if args.keep_open:
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "out", "profile.png")
        session.screenshot(out)
        print(f"  screenshot -> {out}")
    else:
        session.close_part()

    if rel < 1e-4:
        print("\nM4 profile PASS: willekeurig profiel-volume klopt met de shoelace-berekening.")
        return 0
    print("\nM4 profile FAIL.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
