"""M4 - prove cut-extrude (a through-hole), standalone.

1) Kickoff M3 spec: 40x20x10 block with a centred Ø8 through-hole; verify the
   volume against the hand calc (block - cylinder).
2) Off-center hole: confirm (x_mm, y_mm) really map to add_box's coordinate
   system (not a mirrored face-sketch frame) by checking the centre of mass
   shifts away from the hole to the predicted point. The centred test alone is
   symmetric and would not catch a mirrored frame.

    .venv\\Scripts\\python.exe scripts\\m4_hole.py
    .venv\\Scripts\\python.exe scripts\\m4_hole.py --keep-open
"""

import argparse
import math
import sys

from solidworks_mcp.session import SolidWorksSession

W, H, D, DIA = 40.0, 20.0, 10.0, 8.0
CYL = math.pi * (DIA / 2) ** 2 * D  # full through-cylinder volume


def expected_com(hx, hy):
    """COM of a W x H x D block minus a Ø DIA cylinder at (hx, hy), through D."""
    v = W * H * D - CYL
    cx = (W * H * D * (W / 2) - CYL * hx) / v
    cy = (W * H * D * (H / 2) - CYL * hy) / v
    return cx, cy


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: block with through-holes.")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    failures = []

    # 1) centred hole -> volume check
    session.new_part()
    session.add_box(W, H, D)
    hole = session.add_hole(DIA, W / 2, H / 2)
    v = hole["mass_properties"]["volume_mm3"]
    expected_v = W * H * D - CYL
    rel = abs(v - expected_v) / expected_v
    print(f"[1] centre hole: volume {v:.3f} mm^3 (expected {expected_v:.3f}), rel {rel:.1e}")
    if rel >= 1e-5:
        failures.append("centered volume")
    if not args.keep_open:
        session.close_part()

    # 2) off-center hole at (10, 6) -> centre-of-mass check (frame is not mirrored)
    session.new_part()
    session.add_box(W, H, D)
    hole2 = session.add_hole(DIA, 10.0, 6.0)
    com = hole2["mass_properties"]["center_of_mass_mm"]
    ecx, ecy = expected_com(10.0, 6.0)
    okx = abs(com[0] - ecx) < 0.05
    oky = abs(com[1] - ecy) < 0.05
    print(f"[2] off-centre hole (10,6): COM {com[:2]} (expected ({ecx:.3f}, {ecy:.3f}))")
    if not (okx and oky):
        failures.append("off-center COM (frame mirrored?)")
    if not args.keep_open:
        session.close_part()

    if not failures:
        print("\nM4 hole PASS: volume and coordinate frame are correct.")
        return 0
    print(f"\nM4 hole FAIL: {failures}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
