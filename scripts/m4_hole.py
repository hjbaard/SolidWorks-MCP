"""M4 - prove cut-extrude (a through-hole), standalone.

Builds the kickoff M3 spec: a 40x20x10 block with a centred Ø8 through-hole, and
verifies the measured volume against the hand calculation
(block - cylinder = 40*20*10 - pi*4^2*10).

    .venv\\Scripts\\python.exe scripts\\m4_hole.py
    .venv\\Scripts\\python.exe scripts\\m4_hole.py --keep-open
"""

import argparse
import math
import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: block with a centred through-hole.")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--diameter", type=float, default=8.0)
    args = parser.parse_args()

    w, h, d = 40.0, 20.0, 10.0
    session = SolidWorksSession()
    session.connect()
    session.new_part()

    box = session.add_box(w, h, d)
    v_box = box["mass_properties"]["volume_mm3"]
    print(f"OK: blok {w}x{h}x{d} mm -> {v_box:.1f} mm^3")

    hole = session.add_hole(args.diameter, w / 2, h / 2)
    v_hole = hole["mass_properties"]["volume_mm3"]
    expected = w * h * d - math.pi * (args.diameter / 2) ** 2 * d
    rel_err = abs(v_hole - expected) / expected
    print(f"OK: Ø{args.diameter} mm gat centraal, feature '{hole['feature']}'")
    print(f"  volume    : {v_hole:.3f} mm^3  (verwacht {expected:.3f})")
    print(f"  rel. error: {rel_err:.2e}")

    if not args.keep_open:
        session.close_part()
        print("OK: document gesloten (niet opgeslagen)")

    if rel_err < 1e-5:
        print("\nM4 PASS: cut-extrude (gat) klopt met de handberekening.")
        return 0
    print("\nM4 FAIL: volume wijkt af.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
