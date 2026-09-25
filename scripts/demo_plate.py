"""Demo: autonomously design a mounting plate from a spec, verify, and export.

Shows the closed agentic loop on the real tool set:
  spec -> build -> measure -> verify against spec -> finish (fillet) -> export.

Spec: a 120 x 120 x 12 mm plate, central Ø40 bore, 6 x Ø10 bolt holes on a Ø90
bolt circle, then R8 rounded vertical corners. The volume after drilling is
checked against the hand calculation before the (volume-changing) fillet.

    .venv\\Scripts\\python.exe scripts\\demo_plate.py
"""

import math
import os
import sys

from solidworks_mcp.session import SolidWorksSession

# --- the spec the "agent" is asked to satisfy ---
W = H = 120.0
THICK = 12.0
BORE_D = 40.0
BOLT_D = 10.0
BOLT_CIRCLE_D = 90.0
N_BOLTS = 6
CORNER_R = 8.0

CX, CY = W / 2, H / 2
BOLT_RADIUS = BOLT_CIRCLE_D / 2


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    return ok


def main() -> int:
    s = SolidWorksSession()
    s.connect()
    s.new_part()
    ok = True

    print("1) BUILD plate + bore + bolt circle")
    s.add_box(W, H, THICK)
    s.add_hole(BORE_D, CX, CY, name="Bore")
    s.add_hole(BOLT_D, CX, CY + BOLT_RADIUS, name="Bolt")   # one bolt at top
    pat = s.add_circular_pattern(N_BOLTS, CX, CY)            # around the bore axis

    print("2) MEASURE + VERIFY against the spec (pre-fillet)")
    mp = pat["mass_properties"]
    vol = mp["volume_mm3"]
    expected = (W * H * THICK
                - math.pi * (BORE_D / 2) ** 2 * THICK
                - N_BOLTS * math.pi * (BOLT_D / 2) ** 2 * THICK)
    ok &= check("volume vs hand calc", abs(vol - expected) / expected < 1e-4,
                f"{vol:.1f} mm^3 (expected {expected:.1f})")
    size = mp["bounding_box_mm"]["size_mm"]
    ok &= check("bounding box", size == [W, H, THICK], str(size))

    print("3) FINISH: round the 4 vertical corners R8")
    fil = s.add_fillet(CORNER_R, edges="z")
    ok &= check("4 corner edges rounded", fil["edges_filleted"] == 4,
                f"{fil['edges_filleted']} edges")
    vol_final = fil["mass_properties"]["volume_mm3"]
    ok &= check("fillet removed material", 0 < (vol - vol_final) < vol * 0.05,
                f"{vol_final:.1f} mm^3")

    print("4) EXPORT + SCREENSHOT")
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
    os.makedirs(out_dir, exist_ok=True)
    step = s.export(os.path.join(out_dir, "flange.step"))
    ok &= check("STEP export", step["ok"] and step["bytes"] > 0, f"{step['bytes']} bytes")
    shot = s.screenshot(os.path.join(out_dir, "flange.png"))
    ok &= check("screenshot", shot["ok"], shot.get("path", ""))

    print(f"\n  final mass: {fil['mass_properties']['mass_kg']:.3f} kg, "
          f"volume {vol_final:.1f} mm^3")
    print("\nDEMO PASS: plate designed, verified and exported."
          if ok else "\nDEMO FAIL.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
