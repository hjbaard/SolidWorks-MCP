"""M4 - fillet (round all edges), standalone.

Builds a 40x20x10 block and rounds every edge with a constant radius, verifying
that the feature builds, all 12 edges are filleted, and material is removed
(volume drops). For the default r=2 mm the volume is anchored to the value
SolidWorks itself produced (7770.36 mm^3) as a regression check.

    .venv\\Scripts\\python.exe scripts\\m4_fillet.py
    .venv\\Scripts\\python.exe scripts\\m4_fillet.py --keep-open --radius 3
"""

import argparse
import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: fillet all edges of a block.")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--radius", type=float, default=2.0)
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    session.new_part()

    box = session.add_box(40, 20, 10)
    v_box = box["mass_properties"]["volume_mm3"]
    print(f"OK: block 40x20x10 mm -> {v_box:.2f} mm^3")

    fil = session.add_fillet(args.radius)
    v = fil["mass_properties"]["volume_mm3"]
    edges = fil["edges_filleted"]
    print(f"OK: fillet r{args.radius} mm on {edges} edges, feature '{fil['feature']}'")
    print(f"  volume    : {v:.2f} mm^3  (removed: {v_box - v:.2f} mm^3)")

    if not args.keep_open:
        session.close_part()
        print("OK: document closed (not saved)")

    ok = edges == 12 and 0 < (v_box - v) < v_box * 0.1
    if args.radius == 2.0:
        ok = ok and abs(v - 7770.36) < 1.0  # regression anchor for r=2
    if ok:
        print("\nM4 fillet PASS: all edges filleted, material removed as expected.")
        return 0
    print("\nM4 fillet FAIL.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
