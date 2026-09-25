"""M4 - save/open round-trip, standalone.

Build a block, save it as .sldprt, close it, reopen it, and confirm the geometry
survived (same volume). Proves persistence.

    .venv\\Scripts\\python.exe scripts\\m4_saveopen.py
"""

import os
import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
    path = os.path.join(out, "roundtrip.sldprt")
    if os.path.isfile(path):
        os.remove(path)

    session = SolidWorksSession()
    session.connect()
    session.new_part()
    session.add_box(40, 20, 10)
    v_before = session.get_mass_properties()["mass_properties"]["volume_mm3"]

    saved = session.save_part(path)
    print(f"OK: saved -> {saved['path']} ({saved['bytes']} bytes)")
    session.close_part()

    opened = session.open_part(path)
    v_after = session.get_mass_properties()["mass_properties"]["volume_mm3"]
    print(f"OK: heropend '{opened['title']}'; volume {v_after:.1f} mm^3 (was {v_before:.1f})")
    session.close_part()

    ok = os.path.isfile(path) and abs(v_after - v_before) < 1e-6 and abs(v_before - 8000) < 1e-6
    if ok:
        print("\nM4 save/open PASS: the part survives the round trip.")
        return 0
    print("\nM4 save/open FAIL.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
