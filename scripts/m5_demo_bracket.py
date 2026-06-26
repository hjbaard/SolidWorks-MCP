r"""M5 end-to-end demo: a functional 3D-print mounting bracket.

Drives the MCP session to build a real part through the full
build -> measure -> verify loop, asserting the volume against a hand calc after
EACH step, then exports a fine STL + screenshot for printing. This is the
whole-toolset validation: if every checkpoint matches, the tools compose.

The bracket (plate spans x:0..100, y:0..80, z:0..8, add_box coordinates):
- 100 x 80 x 8 base plate
- central motor bore (Ø16, through)
- 4-bolt circle around the bore (Ø3.4, r=15.5)
- 4 corner counterbored screw holes (Ø5.5 clearance, Ø10 x 5.5 cbore) -> flush caps
- a cable-routing slot (30 x 6, through)
- rounded vertical corners (R5)
- fine-tessellation STL export

Run with SolidWorks open:
    .\.venv\Scripts\python.exe scripts\m5_demo_bracket.py
"""
import math
import os
import tempfile

from solidworks_mcp.session import SolidWorksSession

TOL = 0.5  # mm^3; analytic steps match to well within this


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def check(label, result, expected):
    got = vol(result)
    ok = abs(got - expected) < TOL
    print(f"  [{'OK ' if ok else 'XX '}] {label:<34} vol={got:10.3f}  expected={expected:10.3f}")
    if not ok:
        raise AssertionError(f"{label}: vol {got:.3f} != expected {expected:.3f}")
    return got


def main():
    s = SolidWorksSession()
    s.connect()
    print("Building mounting bracket (each step verified vs hand calc)...")

    s.new_part()

    # 1. base plate 100 x 80 x 8
    v = 100 * 80 * 8
    check("base plate 100x80x8", s.add_box(100, 80, 8), v)

    # 2. central motor bore Ø16 through 8
    bore = math.pi * 8 ** 2 * 8
    v -= bore
    check("motor bore Ø16", s.add_hole(16, 50, 40, name="MotorBore"), v)

    # 3. seed bolt hole Ø3.4 on the r=15.5 bolt circle
    bolt = math.pi * 1.7 ** 2 * 8
    v -= bolt
    check("seed bolt hole Ø3.4", s.add_hole(3.4, 65.5, 40, name="Bolt"), v)

    # 4. circular pattern -> 4 bolts total (adds 3)
    v -= 3 * bolt
    check("4x bolt circle (pattern)", s.add_circular_pattern(4, 50, 40, feature_name="Bolt"), v)

    # 5. four corner counterbores: Ø5.5 clearance through + Ø10 x 5.5 cbore
    cbore_each = math.pi * 2.75 ** 2 * 8 + math.pi * (5 ** 2 - 2.75 ** 2) * 5.5
    for (cx, cy) in [(12, 12), (88, 12), (12, 68), (88, 68)]:
        v -= cbore_each
        check(f"counterbore ({cx},{cy})", s.add_counterbore_hole(5.5, 10, 5.5, cx, cy), v)

    # 6. round the four vertical corners R5 -- BEFORE the slot, so edges="z" sees
    # only the 4 box corners (a through slot adds tangent z-edges of its own).
    fillet_each = (5 ** 2 - math.pi * 5 ** 2 / 4) * 8
    v -= 4 * fillet_each
    r = s.add_fillet(5, edges="z", name="CornerRound")
    assert r["edges_filleted"] == 4, f"expected 4 corner edges, got {r['edges_filleted']}"
    check("corner fillets R5 (x4)", r, v)

    # 7. cable-routing slot 30 x 6 through, near the top edge
    slot_area = 30 * 6 + math.pi * 3 ** 2
    v -= slot_area * 8
    check("cable slot 30x6 (through)", s.cut_slot(30, 6, 50, 72, 0, None), v)

    # measurements
    bbox = s.get_bounding_box()["bounding_box_mm"]
    print(f"\n  bounding box size = {bbox['size_mm']} mm (expect ~[100, 80, 8])")

    # 8. export fine STL + screenshot for printing
    out = tempfile.mkdtemp(prefix="bracket_")
    stl = s.export(os.path.join(out, "bracket.stl"), quality="fine")
    shot = s.screenshot(os.path.join(out, "bracket.png"))
    print(f"\n  STL  ({stl['resolution']}): {stl['path']}  ({stl['bytes']} bytes)")
    print(f"  PNG : {shot['path']}")

    print(f"\nDONE. Final volume {v:.3f} mm^3, all checkpoints matched. Part left open in SolidWorks.")


if __name__ == "__main__":
    main()
