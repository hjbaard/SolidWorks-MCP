"""M2 - prove the parametric loop (standalone, no MCP server).

Builds the M1 block, then changes the named depth dimension and rebuilds, and
verifies the volume changes exactly as predicted -- the core promise of
parametric CAD and the signal the agentic loop runs on. Drives the packaged
SolidWorksSession directly (integration test of set_dimension).

    .venv\\Scripts\\python.exe scripts\\m2_parametric.py
    .venv\\Scripts\\python.exe scripts\\m2_parametric.py --keep-open
"""

import argparse
import sys

from solidworks_mcp.session import SolidWorksSession


def main() -> int:
    parser = argparse.ArgumentParser(description="M2: parametric dimension change.")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--width", type=float, default=40.0)
    parser.add_argument("--height", type=float, default=20.0)
    parser.add_argument("--depth", type=float, default=10.0)
    parser.add_argument("--new-depth", type=float, default=25.0)
    args = parser.parse_args()

    session = SolidWorksSession()
    session.connect()
    session.new_part()

    box = session.add_box(args.width, args.height, args.depth)
    dim_name = box["depth_dimension"]
    v1 = box["mass_properties"]["volume_mm3"]
    expected_v1 = args.width * args.height * args.depth
    print(f"OK: block built, depth {args.depth} mm; dimension '{dim_name}'")
    print(f"  volume v1  : {v1:.1f} mm^3  (expected {expected_v1:.1f})")

    edit = session.set_dimension(dim_name, args.new_depth)
    v2 = edit["mass_properties"]["volume_mm3"]
    expected_v2 = args.width * args.height * args.new_depth
    print(f"OK: '{dim_name}' {edit['old_value_mm']:.1f} -> {edit['new_value_mm']} mm; "
          f"rebuild_ok={edit['rebuild_ok']}")
    print(f"  volume v2  : {v2:.1f} mm^3  (expected {expected_v2:.1f})")
    print(f"  ratio v2/v1: {v2 / v1:.4f}  (expected {args.new_depth / args.depth:.4f})")

    if not args.keep_open:
        session.close_part()
        print("OK: document closed (not saved)")

    err1 = abs(v1 - expected_v1) / expected_v1
    err2 = abs(v2 - expected_v2) / expected_v2
    if err1 < 1e-6 and err2 < 1e-6:
        print("\nM2 PASS: a parametric dimension change propagates predictably to the volume.")
        return 0
    print(f"\nM2 FAIL: deviation v1={err1:.2e}, v2={err2:.2e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
