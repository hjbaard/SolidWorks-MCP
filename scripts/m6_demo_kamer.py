r"""M6 end-to-end demo: assemble a furnished bedroom and prove nothing clashes.

Composes three existing parts into one assembly through the full
build -> measure -> verify loop, asserting every placement against the geometry
after each step. This is the assembly counterpart of scripts/m5_demo_bracket.py:
if every checkpoint matches, the assembly tools compose.

The room (Slaapkamer-ruimte.SLDPRT, fixed at the origin, Y is up):
- outside  x 0..3900, y -100..2600, z 0..2480   -> bounding box 3900 x 2700 x 2480
- inside   x 100..3800, y 0..2600, z 100..2380  -> 3700 x 2600 x 2280, floor at y = 0
- a door in the x = 0..100 wall and a window in the x = 3800..3900 wall

The furniture:
- Bed (960 x 350 x 2000) against the right-hand wall, held there by MATES:
  its underside coincident with the floor and its +X side 50 mm from the wall.
  It is inserted deliberately 90 mm off so the mates have to move it.
- Bureau (1100 x 750 x 600) placed by TRANSFORM only, clear of door and bed.

Run with SolidWorks open:
    .\.venv\Scripts\python.exe scripts\m6_demo_kamer.py
"""
import os
import tempfile

from solidworks_mcp.session import SolidWorksSession

PARTS_DIR = r"D:\Ontwikkeling\Kamer Yara"
ROOM = os.path.join(PARTS_DIR, "Slaapkamer-ruimte.SLDPRT")
BED = os.path.join(PARTS_DIR, "Bed.SLDPRT")
DESK = os.path.join(PARTS_DIR, "Bureau.SLDPRT")

TOL = 1e-3  # mm; SolidWorks solves mates far tighter than this

# Room geometry the placements are derived from (hand-read from the part).
WALL = 100.0
INSIDE_MAX_X = 3900.0 - WALL      # 3800: inner face of the right-hand wall
FLOOR_Y = 0.0                     # top of the floor slab
BED_SIZE = (960.0, 350.0, 2000.0)
DESK_SIZE = (1100.0, 750.0, 600.0)

BED_GAP = 50.0                    # the distance mate under test
BED_X = INSIDE_MAX_X - BED_GAP - BED_SIZE[0]   # 2790: where the mates must land it
BED_Z = 300.0
DESK_X, DESK_Z = 200.0, 1700.0

ASSEMBLY_SIZE = [3900.0, 2700.0, 2480.0]


def check(label, got, expected, tol=TOL):
    """Assert one measured number/vector against the hand-derived expectation."""
    values = got if isinstance(got, (list, tuple)) else [got]
    wants = expected if isinstance(expected, (list, tuple)) else [expected]
    ok = len(values) == len(wants) and all(abs(g - w) < tol for g, w in zip(values, wants))
    shown = [round(v, 3) for v in values]
    print(f"  [{'OK ' if ok else 'XX '}] {label:<44} {shown} vs {list(wants)}")
    if not ok:
        raise AssertionError(f"{label}: {shown} != {list(wants)}")


def component(session, name):
    """One entry from list_components, by name."""
    for entry in session.list_components()["components"]:
        if entry["name"].lower().startswith(name.lower()):
            return entry
    raise AssertionError(f"component '{name}' niet gevonden")


def main():
    session = SolidWorksSession()
    session.connect()
    print("Assembling bedroom (every placement verified against the geometry)...")

    session.new_assembly()

    # 1. the room, fixed at the origin -- the ground everything else is placed against
    room = session.insert_component(ROOM, 0, 0, 0)["component"]
    assert room["fixed"], "de eerste component hoort fixed te zijn"
    check("room origin at (0,0,0)", room["position_mm"], [0, 0, 0])
    check("room bounding box", room["bounding_box_mm"]["size_mm"], ASSEMBLY_SIZE)

    # 2. the bed, inserted 90 mm too far from the wall and 25 mm off the floor,
    #    so the mates below have to do real work
    bed = session.insert_component(BED, BED_X - 90.0, 25.0, BED_Z)["component"]
    assert not bed["fixed"], "een tweede component hoort vrij te zijn voor mates"
    check("bed inserted (deliberately wrong)", bed["position_mm"], [BED_X - 90.0, 25.0, BED_Z])

    # 3. mate 1: the bed's underside onto the floor. The floor is the room's INNER
    #    +Y face -- its outer +Y face is the rim around the open top.
    floor_mate = session.add_mate("Bed", "-y", "Slaapkamer-ruimte", "+y:inner",
                                  mate_type="coincident")
    check("coincident bed underside / floor", floor_mate["distance_mm"], 0.0)
    check("bed dropped onto the floor", component(session, "Bed")["position_mm"][1], FLOOR_Y)

    # 4. mate 2: 50 mm between the bed's +X side and the inner face of the
    #    right-hand wall -- the acceptance measurement of this milestone
    wall_mate = session.add_mate("Bed", "+x", "Slaapkamer-ruimte", "-x:inner",
                                 mate_type="distance", distance_mm=BED_GAP)
    check("distance mate reports 50 mm", wall_mate["distance_mm"], BED_GAP)
    bed = component(session, "Bed")
    check("bed moved to x from its transform", bed["position_mm"][0], BED_X)
    check("bed side really 50 mm from wall",
          INSIDE_MAX_X - bed["bounding_box_mm"]["max_mm"][0], BED_GAP)

    # 5. the desk, placed by transform only (no mates) -- the other route
    session.insert_component(DESK, DESK_X, FLOOR_Y, DESK_Z)
    desk = session.set_component_transform("Bureau", DESK_X, FLOOR_Y, DESK_Z)
    check("desk placed by transform", desk["position_mm"], [DESK_X, FLOOR_Y, DESK_Z])
    check("desk bounding box",
          desk["bounding_box_mm"]["max_mm"],
          [DESK_X + DESK_SIZE[0], FLOOR_Y + DESK_SIZE[1], DESK_Z + DESK_SIZE[2]])

    # 6. the acceptance criteria, measured on the finished assembly
    print("\nAcceptance:")
    session.rebuild()
    check("assembly bounding box",
          session.get_assembly_bounding_box()["bounding_box_mm"]["size_mm"], ASSEMBLY_SIZE)
    for name in ("Bed", "Bureau"):
        check(f"{name} stands on the floor (y = 0)",
              component(session, name)["bounding_box_mm"]["min_mm"][1], FLOOR_Y)
    check("bed 50 mm from the wall (from the transform)",
          component(session, "Bed")["position_mm"][0], BED_X)

    clashes = session.check_interference()
    for clash in clashes["interferences"]:
        print(f"       clash {clash['components']} {clash['volume_mm3']:.1f} mm3")
    check("interfering pairs", clashes["count"], 0)

    # 7. reuse the part-level I/O on the assembly
    out = tempfile.mkdtemp(prefix="kamer_")
    saved = session.save_assembly(os.path.join(out, "Kamer-Yara.sldasm"))
    shot = session.screenshot(os.path.join(out, "Kamer-Yara.png"))
    step = session.export(os.path.join(out, "Kamer-Yara.step"))
    print(f"\n  SLDASM: {saved['path']} ({saved['bytes']} bytes)")
    print(f"  PNG   : {shot['path']} ({shot['bytes']} bytes)")
    print(f"  STEP  : {step['path']} ({step['bytes']} bytes)")

    print("\nDONE. Every checkpoint matched; assembly left open in SolidWorks.")


if __name__ == "__main__":
    main()
