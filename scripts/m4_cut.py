"""M4 - cut_profile (polygonal pocket/slot), standalone.

Cuts a 20x10 rectangular pocket into a 40x20x10 block, blind and through, and
verifies the removed volume = pocket area * cut depth.

    .venv\\Scripts\\python.exe scripts\\m4_cut.py
    .venv\\Scripts\\python.exe scripts\\m4_cut.py --keep-open
"""

import argparse
import os
import sys

from solidworks_mcp.session import SolidWorksSession

POCKET = [[10, 5], [30, 5], [30, 15], [10, 15]]  # 20 x 10 mm
AREA = 20 * 10


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: cut_profile pocket.")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    failures = []

    cases = [("blind 4mm", 4.0, 8000 - AREA * 4), ("doorlopend", None, 8000 - AREA * 10)]
    for i, (label, depth, expected) in enumerate(cases):
        session.new_part()
        session.add_box(40, 20, 10)
        res = session.cut_profile(POCKET, depth)
        v = res["mass_properties"]["volume_mm3"]
        rel = abs(v - expected) / expected
        print(f"{label}: {v:.2f} mm^3 (verwacht {expected}), rel {rel:.1e}")
        if rel >= 1e-4:
            failures.append(label)
        if args.keep_open and i == 0:
            out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "out", "cut.png")
            session.screenshot(out)
            print(f"  screenshot -> {out}")
        else:
            session.close_part()

    if not failures:
        print("\nM4 cut PASS: pocket-volumes (blind + doorlopend) kloppen.")
        return 0
    print(f"\nM4 cut FAIL: {failures}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
