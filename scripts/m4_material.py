"""M4 - material assignment, standalone.

Assigns a material and checks density/mass reflect it (not the 1000 kg/m^3
default), and that mass == volume * density.

    .venv\\Scripts\\python.exe scripts\\m4_material.py
    .venv\\Scripts\\python.exe scripts\\m4_material.py --material "AISI 1020"
"""

import argparse
import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    parser = argparse.ArgumentParser(description="M4: material assignment.")
    parser.add_argument("--material", default="6061 Alloy")
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    session.new_part()
    session.add_box(40, 20, 10)

    res = session.set_material(args.material)
    mp = res["mass_properties"]
    density = mp["density_kg_m3"]
    mass = mp["mass_kg"]
    vol = mp["volume_mm3"]
    expected_mass = vol * 1e-9 * density
    print(f"OK: material '{args.material}' applied")
    print(f"  density   : {density:.1f} kg/m^3  (the default would be 1000)")
    print(f"  mass      : {mass:.5f} kg  (volume {vol:.0f} mm^3 x density -> {expected_mass:.5f})")

    session.close_part()

    ok = abs(density - 1000.0) > 1.0 and abs(mass - expected_mass) < 1e-6
    if ok:
        print("\nM4 material PASS: density and mass reflect the material.")
        return 0
    print("\nM4 material FAIL.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
