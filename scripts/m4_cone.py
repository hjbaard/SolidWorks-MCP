"""M4 - cone / frustum via revolve, standalone.

Verifies the revolved volume against the frustum formula
V = pi*h/3 * (rb^2 + rb*rt + rt^2), for both a frustum and a full cone (rt=0).

    .venv\\Scripts\\python.exe scripts\\m4_cone.py
    .venv\\Scripts\\python.exe scripts\\m4_cone.py --keep-open
"""

import argparse
import math
import os
import sys

from solidworks_mcp.session import SolidWorksSession


def frustum_volume(bottom_d, top_d, h):
    rb, rt = bottom_d / 2, top_d / 2
    return math.pi * h / 3 * (rb * rb + rb * rt + rt * rt)


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: cone/frustum via revolve.")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    failures = []

    cases = [("frustum Ø20->Ø10 h20", 20.0, 10.0, 20.0),
             ("volle kegel Ø20 h20", 20.0, 0.0, 20.0)]
    for i, (label, bd, td, h) in enumerate(cases):
        session.new_part()
        cone = session.add_cone(bd, td, h)
        v = cone["mass_properties"]["volume_mm3"]
        expected = frustum_volume(bd, td, h)
        rel = abs(v - expected) / expected
        print(f"{label}: {v:.3f} mm^3 (verwacht {expected:.3f}), rel {rel:.1e}")
        if rel >= 1e-4:
            failures.append(label)
        if args.keep_open and i == 0:
            out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "out", "cone.png")
            session.screenshot(out)
            print(f"  screenshot -> {out}")
        else:
            session.close_part()

    if not failures:
        print("\nM4 cone PASS: frustum- en kegel-volume kloppen met de formule.")
        return 0
    print(f"\nM4 cone FAIL: {failures}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
