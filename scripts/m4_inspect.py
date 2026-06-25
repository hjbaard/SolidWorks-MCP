"""M4 - inspection (list_faces / list_edges) + index-based edge selection.

A 40x20x10 box has 6 planar faces (total area 2800 mm^2) and 12 line edges.
Then fillet two edges chosen by index to prove targeted selection.

    .venv\\Scripts\\python.exe scripts\\m4_inspect.py
"""

import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    session = SolidWorksSession()
    session.connect()
    session.new_part()
    session.add_box(40, 20, 10)

    faces = session.list_faces()
    edges = session.list_edges()
    planar = sum(1 for f in faces["faces"] if f["type"] == "planar")
    lines = sum(1 for e in edges["edges"] if e["type"] == "line")
    total_area = round(sum(f["area_mm2"] for f in faces["faces"]), 1)
    print(f"faces: {faces['count']} ({planar} planar, total area {total_area} mm^2)")
    print(f"edges: {edges['count']} ({lines} lines)")
    print(f"  sample face: {faces['faces'][0]}")
    print(f"  sample edge: {edges['edges'][0]}")

    fil = session.add_fillet(2, edges="0,1")
    print(f"fillet edges='0,1' -> {fil['edges_filleted']} randen, rebuild_ok={fil['rebuild_ok']}")

    session.close_part()

    ok = (faces["count"] == 6 and planar == 6 and edges["count"] == 12 and lines == 12
          and abs(total_area - 2800.0) < 1.0 and fil["edges_filleted"] == 2)
    if ok:
        print("\nM4 inspect PASS: list_faces/list_edges + index-selectie kloppen.")
        return 0
    print("\nM4 inspect FAIL.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
