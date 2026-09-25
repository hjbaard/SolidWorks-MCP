"""End-to-end test of the MCP server, driven over stdio like a real agent.

Spawns `python -m solidworks_mcp.server` as a subprocess, speaks the MCP
protocol to it, and runs the M3 verification loop against the live SolidWorks:

    get_status -> new_part -> add_box(40x20x10) -> measure (expect 8000 mm^3)
    -> set_dimension depth=25 -> measure (expect 20000 mm^3) -> bounding box
    -> export STEP + STL -> screenshot -> close_part

Run with the venv python (the server module must be importable there):
    .venv\\Scripts\\python.exe scripts\\test_mcp_server.py
"""

import asyncio
import json
import math
import os
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
PYTHON = sys.executable


def payload(result) -> dict:
    """Extract the tool's dict return from an MCP CallToolResult, shape-tolerant."""
    sc = getattr(result, "structuredContent", None)
    if sc:
        return sc["result"] if set(sc.keys()) == {"result"} else sc
    for item in result.content:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)
    return {}


def approx(actual: float, expected: float, rel: float = 1e-4) -> bool:
    return abs(actual - expected) / expected < rel


async def run() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    params = StdioServerParameters(command=PYTHON, args=["-m", "solidworks_mcp.server"])
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {name}{(' - ' + detail) if detail else ''}")
        if not cond:
            failures.append(name)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"Tools offered ({len(names)}): {', '.join(names)}")

            print("\n[1] get_status")
            r = payload(await session.call_tool("get_status", {}))
            check("verbonden", r.get("connected") is True, f"revision={r.get('revision')}")

            print("[2] new_part")
            r = payload(await session.call_tool("new_part", {}))
            check("part created", r.get("ok") is True, f"title={r.get('title')}")

            print("[3] add_box 40x20x10")
            r = payload(await session.call_tool(
                "add_box", {"width_mm": 40, "height_mm": 20, "depth_mm": 10}))
            vol1 = r.get("mass_properties", {}).get("volume_mm3", 0)
            dim_name = r.get("depth_dimension")
            check("volume == 8000 mm^3", approx(vol1, 8000.0), f"{vol1:.1f} mm^3")
            check("depth dimension reported", dim_name == "D1@BlockExtrude", str(dim_name))

            print("[4] set_dimension depth -> 25 mm")
            r = payload(await session.call_tool(
                "set_dimension", {"dimension_name": dim_name, "value_mm": 25}))
            vol2 = r.get("mass_properties", {}).get("volume_mm3", 0)
            check("rebuild ok", r.get("rebuild_ok") is True)
            check("volume == 20000 mm^3", approx(vol2, 20000.0), f"{vol2:.1f} mm^3")

            print("[4b] add_hole Ø8 centred")
            r = payload(await session.call_tool(
                "add_hole", {"diameter_mm": 8, "x_mm": 20, "y_mm": 10}))
            vol_hole = r.get("mass_properties", {}).get("volume_mm3", 0)
            expected_hole = 40 * 20 * 25 - math.pi * 4 ** 2 * 25  # block 40x20x25 minus Ø8 through
            check("volume after hole", approx(vol_hole, expected_hole),
                  f"{vol_hole:.1f} mm^3 (expected {expected_hole:.1f})")

            print("[4c] add_fillet r2 on all edges")
            r = payload(await session.call_tool("add_fillet", {"radius_mm": 2}))
            vol_fil = r.get("mass_properties", {}).get("volume_mm3", 0)
            check("fillet built", r.get("ok") is True and r.get("edges_filleted", 0) > 0,
                  f"{r.get('edges_filleted')} edges, {vol_fil:.1f} mm^3")
            check("material removed by fillet", 0 < vol_fil < vol_hole,
                  f"{vol_fil:.1f} < {vol_hole:.1f}")

            print("[5] get_bounding_box")
            r = payload(await session.call_tool("get_bounding_box", {}))
            size = r.get("bounding_box_mm", {}).get("size_mm")
            check("bbox == [40,20,25]", size == [40.0, 20.0, 25.0], str(size))

            print("[6] export STEP + STL")
            for ext in ("step", "stl"):
                path = os.path.join(OUT_DIR, f"block.{ext}")
                if os.path.isfile(path):
                    os.remove(path)
                r = payload(await session.call_tool("export", {"path": path}))
                ok = r.get("ok") is True and os.path.isfile(path) and r.get("bytes", 0) > 0
                check(f"export {ext}", ok, f"{r.get('bytes')} bytes")

            print("[7] screenshot")
            png = os.path.join(OUT_DIR, "block.png")
            if os.path.isfile(png):
                os.remove(png)
            r = payload(await session.call_tool("screenshot", {"path": png}))
            check("screenshot png", r.get("ok") is True and os.path.isfile(png),
                  f"{r.get('bytes')} bytes")

            print("[8] close_part")
            r = payload(await session.call_tool("close_part", {}))
            check("part closed", r.get("ok") is True, f"closed={r.get('closed')}")

    print()
    if failures:
        print(f"MCP SERVER TEST FAIL: {len(failures)} checks failed: {failures}")
        return 1
    print("MCP SERVER TEST PASS: the full agent loop works end to end.")
    return 0


def main() -> int:
    return asyncio.run(asyncio.wait_for(run(), timeout=180))


if __name__ == "__main__":
    sys.exit(main())
