"""M1 - prove the geometry + measurement pipeline (standalone, no MCP server).

Builds a block and verifies the measured volume against the hand calculation.
Drives solidworks_mcp.session.SolidWorksSession directly: a single-threaded
script needs no COM worker thread, and this doubles as an integration test of
the packaged operations.

    .venv\\Scripts\\python.exe scripts\\m1_block.py
    .venv\\Scripts\\python.exe scripts\\m1_block.py --keep-open
"""

import argparse
import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    parser = argparse.ArgumentParser(description="M1: build & verify a block.")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--width", type=float, default=40.0)
    parser.add_argument("--height", type=float, default=20.0)
    parser.add_argument("--depth", type=float, default=10.0)
    args = parser.parse_args()

    session = SolidWorksSession()
    status = session.connect()
    print(f"OK: connected to SolidWorks {status['revision']}")

    part = session.new_part()
    print(f"OK: nieuw part '{part['title']}'")

    result = session.add_box(args.width, args.height, args.depth)
    props = result["mass_properties"]
    actual = props["volume_mm3"]
    expected = args.width * args.height * args.depth
    rel_err = abs(actual - expected) / expected

    print(f"OK: block {args.width}x{args.height}x{args.depth} mm, feature '{result['feature']}'")
    print("--- mass properties ---")
    print(f"  volume    : {actual:.1f} mm^3  (expected {expected:.1f})")
    print(f"  mass      : {props['mass_kg']:.6f} kg")
    print(f"  area      : {props['surface_area_mm2']:.1f} mm^2")
    print(f"  com       : {props['center_of_mass_mm']} mm")
    print(f"  bbox size : {props['bounding_box_mm']['size_mm']} mm")

    if not args.keep_open:
        session.close_part()
        print("OK: document closed (not saved)")

    if rel_err < 1e-6:
        print("\nM1 PASS: the measured volume matches the hand calculation.")
        return 0
    print(f"\nM1 FAIL: volume rel. error {rel_err:.2e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
