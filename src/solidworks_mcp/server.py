"""MCP server exposing SolidWorks part-modelling tools over stdio.

All tools delegate to a single SolidWorksSession that runs on one dedicated COM
thread (ComWorker). Failures are returned as {"ok": false, "error": "..."} so
the agent can read and react to them in the build -> measure -> correct loop,
rather than getting an opaque stack trace.
"""

import pythoncom
from mcp.server.fastmcp import FastMCP

from .com_worker import ComWorker
from .errors import SolidWorksError
from .session import SolidWorksSession

mcp = FastMCP("solidworks-mcp")
_worker = ComWorker()
_session = SolidWorksSession()


async def _call(fn, *args, **kwargs) -> dict:
    """Run a session method on the COM thread and normalise errors to a result dict."""
    try:
        return await _worker.call(lambda: fn(*args, **kwargs))
    except SolidWorksError as exc:
        return {"ok": False, "error": str(exc)}
    except pythoncom.com_error as exc:
        # Extract the human-readable description if SolidWorks supplied one;
        # raw HRESULT tuples are useless as a correction-loop signal.
        info = getattr(exc, "excepinfo", None)
        desc = info[2] if info and len(info) > 2 and info[2] else getattr(exc, "strerror", None)
        return {"ok": False, "error": f"SolidWorks COM-fout: {desc or exc}"}
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the agent
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def get_status() -> dict:
    """Report whether SolidWorks is reachable, its revision, and the active/current part."""
    return await _call(_session.get_status)


@mcp.tool()
async def new_part() -> dict:
    """Create a new empty part document; it becomes the current part."""
    return await _call(_session.new_part)


@mcp.tool()
async def add_box(width_mm: float, height_mm: float, depth_mm: float,
                  name: str = "BlockExtrude") -> dict:
    """Add a rectangular block: sketch width x height on the first plane, extrude by depth.

    Dimensions are in millimetres. Returns the created feature name, the
    addressable depth dimension ('D1@<name>'), and the resulting mass properties.
    """
    return await _call(_session.add_box, width_mm, height_mm, depth_mm, name)


@mcp.tool()
async def add_cylinder(diameter_mm: float, height_mm: float, name: str = "Revolve") -> dict:
    """Create a cylinder by revolving a profile 360° about an axis.

    The first revolve-based primitive. Returns the resulting mass properties
    (volume = π · r² · h). Use new_part first.
    """
    return await _call(_session.add_cylinder, diameter_mm, height_mm, name)


@mcp.tool()
async def add_hole(diameter_mm: float, x_mm: float, y_mm: float, name: str = "Hole") -> dict:
    """Cut a circular through-hole at (x_mm, y_mm), through the part's depth axis.

    The hole runs straight through the thickness (the add_box extrude direction),
    perpendicular to the width x height profile face. Coordinates share add_box's
    system (the centre of a 40x20 profile is x=20, y=10). Returns mass properties.
    """
    return await _call(_session.add_hole, diameter_mm, x_mm, y_mm, name)


@mcp.tool()
async def add_fillet(radius_mm: float, edges: str = "all", name: str = "Fillet") -> dict:
    """Round edges of the current part with one constant radius (mm).

    edges: "all" (default), or "x"/"y"/"z" to round only the edges parallel to
    that world axis ("z" = the depth edges of an add_box block). Returns the
    number of edges filleted and the resulting mass properties.
    """
    return await _call(_session.add_fillet, radius_mm, edges, name)


@mcp.tool()
async def add_chamfer(distance_mm: float, edges: str = "all", name: str = "Chamfer") -> dict:
    """Chamfer edges of the current part at 45° with the given distance (mm).

    edges: "all" (default), or "x"/"y"/"z" to chamfer only the edges parallel to
    that world axis. Returns the number of edges chamfered and the resulting mass
    properties.
    """
    return await _call(_session.add_chamfer, distance_mm, edges, name)


@mcp.tool()
async def set_dimension(dimension_name: str, value_mm: float) -> dict:
    """Set a named driving dimension (e.g. 'D1@BlockExtrude') in mm, rebuild, and remeasure.

    This is the parametric edit at the heart of the correction loop.
    """
    return await _call(_session.set_dimension, dimension_name, value_mm)


@mcp.tool()
async def rebuild(top_only: bool = False) -> dict:
    """Force a rebuild of the current part and report whether it rebuilt without errors."""
    return await _call(_session.rebuild, top_only)


@mcp.tool()
async def get_mass_properties() -> dict:
    """Get volume (mm^3), mass (kg), surface area (mm^2), centre of mass, and bounding box."""
    return await _call(_session.get_mass_properties)


@mcp.tool()
async def get_bounding_box() -> dict:
    """Get the tight bounding box of the current part (min/max/size in mm)."""
    return await _call(_session.get_bounding_box)


@mcp.tool()
async def export(path: str, file_format: str | None = None) -> dict:
    """Export the current part to STEP/STL/IGES/Parasolid/3MF (format inferred from extension).

    Silent (no prompts). Verifies the file appears on disk and reports its size.
    """
    return await _call(_session.export, path, file_format)


@mcp.tool()
async def screenshot(path: str) -> dict:
    """Save an isometric, zoom-to-fit screenshot of the current part (PNG/BMP/JPG)."""
    return await _call(_session.screenshot, path)


@mcp.tool()
async def close_part(save: bool = False) -> dict:
    """Close the current part without saving (export first if you need the geometry)."""
    return await _call(_session.close_part, save)


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    try:
        mcp.run()
    finally:
        _worker.shutdown()


if __name__ == "__main__":
    main()
