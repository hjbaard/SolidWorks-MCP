"""M4 - directional edge selection for fillet/chamfer, standalone.

Fillets only the edges parallel to one axis and checks the right count is hit:
a 40x20x10 add_box block has exactly 4 edges parallel to each of x, y, z.

    .venv\\Scripts\\python.exe scripts\\m4_select_edges.py
    .venv\\Scripts\\python.exe scripts\\m4_select_edges.py --axis x --keep-open
"""

import argparse
import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: directional edge selection.")
    parser.add_argument("--axis", choices=["x", "y", "z"], default="z")
    parser.add_argument("--radius", type=float, default=2.0)
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    session.new_part()

    box = session.add_box(40, 20, 10)
    v_box = box["mass_properties"]["volume_mm3"]

    fil = session.add_fillet(args.radius, edges=args.axis)
    edges = fil["edges_filleted"]
    v = fil["mass_properties"]["volume_mm3"]
    print(f"OK: fillet r{args.radius} op edges='{args.axis}' -> {edges} randen geselecteerd")
    print(f"  volume: {v:.2f} mm^3  (verwijderd: {v_box - v:.2f} mm^3)")

    if args.keep_open:
        import os
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "out", f"edges_{args.axis}.png")
        session.screenshot(out)
        print(f"OK: screenshot -> {out}")
    else:
        session.close_part()
        print("OK: document gesloten")

    # A box has exactly 4 edges parallel to each axis; material must be removed
    # (the amount scales with edge length, so only check it is positive).
    ok = edges == 4 and (v_box - v) > 0
    if ok:
        print(f"\nM4 edge-select PASS: precies de 4 '{args.axis}'-randen afgerond.")
        return 0
    print(f"\nM4 edge-select FAIL: {edges} randen (verwacht 4).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
