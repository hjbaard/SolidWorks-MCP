"""M4 - chamfer (45 deg on all edges), standalone.

Builds a 40x20x10 block and chamfers every edge, verifying the feature builds,
all 12 edges are chamfered, and material is removed (more than a fillet of the
same size, since a chamfer cuts a flat triangle rather than a rounded quarter).

    .venv\\Scripts\\python.exe scripts\\m4_chamfer.py
    .venv\\Scripts\\python.exe scripts\\m4_chamfer.py --keep-open --distance 3
"""

import argparse
import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: chamfer all edges of a block.")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--distance", type=float, default=2.0)
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    session.new_part()

    box = session.add_box(40, 20, 10)
    v_box = box["mass_properties"]["volume_mm3"]
    print(f"OK: block 40x20x10 mm -> {v_box:.2f} mm^3")

    cham = session.add_chamfer(args.distance)
    v = cham["mass_properties"]["volume_mm3"]
    edges = cham["edges_chamfered"]
    print(f"OK: chamfer {args.distance} mm on {edges} edges, feature '{cham['feature']}'")
    print(f"  volume    : {v:.2f} mm^3  (removed: {v_box - v:.2f} mm^3)")

    if not args.keep_open:
        session.close_part()
        print("OK: document closed (not saved)")

    ok = edges == 12 and 0 < (v_box - v) < v_box * 0.2
    if ok:
        print("\nM4 chamfer PASS: all edges chamfered, material removed as expected.")
        return 0
    print("\nM4 chamfer FAIL.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
