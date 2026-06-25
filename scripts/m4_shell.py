"""M4 - shell (hollow out), standalone.

Hollows a 40x20x10 block to a 2 mm wall and verifies the remaining volume:
- open +z (top removed): 8000 - 36*16*8 = 3392 mm^3
- closed (no opening):   8000 - 36*16*6 = 4544 mm^3

    .venv\\Scripts\\python.exe scripts\\m4_shell.py
    .venv\\Scripts\\python.exe scripts\\m4_shell.py --keep-open
"""

import argparse
import os
import sys

from solidworks_mcp.session import SolidWorksSession

T = 2.0  # wall thickness mm


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: shell a block.")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    failures = []

    cases = [("open +z", "+z", 8000 - 36 * 16 * 8),
             ("closed", "none", 8000 - 36 * 16 * 6)]
    for i, (label, face, expected) in enumerate(cases):
        session.new_part()
        session.add_box(40, 20, 10)
        sh = session.add_shell(T, open_face=face)
        v = sh["mass_properties"]["volume_mm3"]
        rel = abs(v - expected) / expected
        print(f"{label}: {v:.2f} mm^3 (verwacht {expected}), rel {rel:.1e}, rebuild_ok={sh['rebuild_ok']}")
        if rel >= 1e-4:
            failures.append(label)
        if args.keep_open and i == 0:
            out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "out", "shell.png")
            session.screenshot(out)
            print(f"  screenshot -> {out}")
        else:
            session.close_part()

    if not failures:
        print("\nM4 shell PASS: open en gesloten wand-volumes kloppen.")
        return 0
    print(f"\nM4 shell FAIL: {failures}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
