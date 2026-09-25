"""M4 - linear pattern of a hole, standalone.

Box + one hole, then a linear pattern of `count` holes along +x. Verifies the
volume against block - count * cylinder.

    .venv\\Scripts\\python.exe scripts\\m4_pattern.py
    .venv\\Scripts\\python.exe scripts\\m4_pattern.py --keep-open
"""

import argparse
import math
import os
import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: linear pattern.")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--spacing", type=float, default=10.0)
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    session.new_part()
    session.add_box(40, 20, 10)
    session.add_hole(8, 10, 10)

    pat = session.add_linear_pattern(args.count, args.spacing, "+x")
    v = pat["mass_properties"]["volume_mm3"]
    cyl = math.pi * 16 * 10
    expected = 8000 - args.count * cyl
    rel = abs(v - expected) / expected
    print(f"OK: linear pattern {args.count}x along +x, spacing {args.spacing} mm")
    print(f"  instances : {pat['instances']}, seed '{pat['seed']}'")
    print(f"  volume    : {v:.3f} mm^3  (expected {expected:.3f}, = block - {args.count} holes), rel {rel:.1e}")

    if args.keep_open:
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "out", "pattern.png")
        session.screenshot(out)
        print(f"  screenshot -> {out}")
    else:
        session.close_part()

    if rel < 1e-4:
        print("\nM4 pattern PASS: the linear pattern volume is correct.")
        return 0
    print("\nM4 pattern FAIL.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
