"""M4 - circular pattern (bolt circle), standalone.

A 40x40x10 plate, a centre hole at (20,20) whose wall is the rotation axis, and
one bolt hole at (10,20) patterned `count` times around the centre. Verifies the
volume against plate - centre hole - count * bolt hole.

    .venv\\Scripts\\python.exe scripts\\m4_circular.py
    .venv\\Scripts\\python.exe scripts\\m4_circular.py --keep-open --count 6
"""

import argparse
import math
import os
import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: circular pattern / bolt circle.")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--count", type=int, default=4)
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    session.new_part()
    session.add_box(40, 40, 10)
    session.add_hole(8, 20, 20, name="CenterHole")
    session.add_hole(6, 10, 20, name="BoltHole")

    pat = session.add_circular_pattern(args.count, 20, 20)
    v = pat["mass_properties"]["volume_mm3"]
    expected = 40 * 40 * 10 - math.pi * 16 * 10 - args.count * math.pi * 9 * 10
    rel = abs(v - expected) / expected
    print(f"OK: bout-cirkel {args.count}x rond (20,20), seed '{pat['seed']}'")
    print(f"  volume    : {v:.3f} mm^3  (verwacht {expected:.3f}), rel {rel:.1e}")

    if args.keep_open:
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "out", "circular.png")
        session.screenshot(out)
        print(f"  screenshot -> {out}")
    else:
        session.close_part()

    if rel < 1e-4:
        print("\nM4 circular PASS: bout-cirkel-volume klopt.")
        return 0
    print("\nM4 circular FAIL.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
