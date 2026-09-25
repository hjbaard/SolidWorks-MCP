"""M4 - cylinder via revolve, standalone.

Revolves a rectangular profile 360 deg into a cylinder and verifies the volume
against pi * r^2 * h -- the first revolve-based primitive.

    .venv\\Scripts\\python.exe scripts\\m4_cylinder.py
    .venv\\Scripts\\python.exe scripts\\m4_cylinder.py --keep-open --diameter 30 --height 15
"""

import argparse
import math
import os
import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: cylinder via revolve.")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--diameter", type=float, default=20.0)
    parser.add_argument("--height", type=float, default=20.0)
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    session.new_part()

    cyl = session.add_cylinder(args.diameter, args.height)
    props = cyl["mass_properties"]
    v = props["volume_mm3"]
    expected = math.pi * (args.diameter / 2) ** 2 * args.height
    rel = abs(v - expected) / expected
    size = props["bounding_box_mm"]["size_mm"]
    print(f"OK: cilinder Ø{args.diameter} x h{args.height} mm, feature '{cyl['feature']}'")
    print(f"  volume    : {v:.3f} mm^3  (expected {expected:.3f}), rel {rel:.1e}")
    print(f"  bbox size : {size} mm  (expected [{args.diameter}, {args.height}, {args.diameter}])")

    if args.keep_open:
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "out", "cylinder.png")
        session.screenshot(out)
        print(f"OK: screenshot -> {out}")
    else:
        session.close_part()
        print("OK: document closed")

    # bbox should be diameter x height x diameter (axis along Y)
    bbox_ok = (abs(size[0] - args.diameter) < 1e-3 and abs(size[1] - args.height) < 1e-3
               and abs(size[2] - args.diameter) < 1e-3)
    if rel < 1e-4 and bbox_ok:
        print("\nM4 cylinder PASS: revolve volume and bounding box are correct.")
        return 0
    print("\nM4 cylinder FAIL.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
