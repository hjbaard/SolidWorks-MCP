"""Check this machine's SolidWorks against what the tools need.

`solidworks-mcp --selftest`, with SolidWorks open, builds small parts through the
session the MCP tools use, compares each with a hand calculation, closes them
without saving, and prints a report to paste into an issue. It covers what
differs between installations (release, language, templates, Hole Wizard
tables, thread profiles, materials); the full test suite lives in the repository.
"""

import importlib.metadata
import math
import os
import platform
import tempfile
import uuid

from . import __version__

BLOCK_MM3 = 40 * 20 * 10


class CheckFailed(Exception):
    """A check ran, but SolidWorks built something other than the hand calculation."""


def _volume(result, expected, tolerance=0.01) -> str:
    got = result["mass_properties"]["volume_mm3"]
    if abs(got - expected) > tolerance:
        raise CheckFailed(f"volume {got:.3f} mm3, expected {expected:.3f}")
    return f"{got:.3f} mm3"


def _iso_groove_mm3_per_mm(d, p, inner_width, outer_width):
    """Thread groove volume per mm: the ISO basic trapezoid (depth 5*sqrt(3)/16 * P)
    swept along the helix fills area * 2*pi*r_centroid per pitch."""
    h = 5 * math.sqrt(3) / 16 * p
    area = (inner_width + outer_width) / 2 * h
    r_centroid = (d / 2 - h) + h * (inner_width + 2 * outer_width) / (3 * (inner_width + outer_width))
    return area * 2 * math.pi * r_centroid / p


def _block(sw):
    sw.new_part()
    return sw.add_box(40, 20, 10, name="Block")


def _check_block(sw):
    return _volume(_block(sw), BLOCK_MM3)


def _check_dimension(sw):
    block = _block(sw)
    return _volume(sw.set_dimension(block["dimensions"]["depth"], 25), 40 * 20 * 25)


def _check_hole(sw):
    _block(sw)
    return _volume(sw.add_hole(6, 10, 10), BLOCK_MM3 - math.pi * 3 ** 2 * 10)


def _check_side_hole(sw):
    _block(sw)
    return _volume(sw.add_hole_on_face(4, "+x", 40, 10, 5, depth_mm=5), BLOCK_MM3 - math.pi * 2 ** 2 * 5)


def _check_fillet(sw):
    _block(sw)
    return _volume(sw.add_fillet(2, edges="z"), BLOCK_MM3 - 4 * (1 - math.pi / 4) * 2 ** 2 * 10)


def _check_cylinder(sw):
    sw.new_part()
    return _volume(sw.add_cylinder(20, 20), math.pi * 10 ** 2 * 20, tolerance=0.1)


def _check_hole_wizard(sw):
    _block(sw)
    hole = sw.add_hole_wizard("clearance", "M3", "+z", 20, 10, 10)
    diameter = hole["hole"]["diameter_mm"]
    if abs(diameter - 3.4) > 1e-6:
        raise CheckFailed(f"an ISO 273 normal M3 clearance hole is 3.4 mm; the Hole Wizard drilled {diameter} mm")
    return f"{_volume(hole, BLOCK_MM3 - math.pi * 1.7 ** 2 * 10)}, diameter {diameter} mm"


def _check_thread(sw):
    # 1 %, looser than the test suite: another release's thread profile may
    # differ slightly, while a wrong or missing thread is far off
    sw.new_part()
    rod = sw.add_disc(10, 40)["mass_properties"]["volume_mm3"]
    removed = rod - sw.add_thread("M10x1.5", 0, 0, 40, 20)["mass_properties"]["volume_mm3"]
    expected = 20 * _iso_groove_mm3_per_mm(10, 1.5, 1.5 / 4, 7 * 1.5 / 8)
    if abs(removed - expected) > 0.01 * expected:
        raise CheckFailed(f"the thread removed {removed:.3f} mm3; an ISO M10x1.5 groove over 20 mm is {expected:.3f}")
    return f"removed {removed:.3f} mm3 (ISO groove {expected:.3f})"


def _check_material(sw):
    _block(sw)
    density = sw.set_material("6061 Alloy")["mass_properties"]["density_kg_m3"]
    if abs(density - 2700) > 50:
        raise CheckFailed(f"6061 Alloy is about 2700 kg/m3, got {density:.0f}")
    return f"{density:.0f} kg/m3"


def _check_mirror(sw):
    # a Ø40 disc centred on the origin; the hole at x = 10 mirrors to x = -10
    sw.new_part()
    sw.add_disc(40, 10)
    sw.add_hole(6, 10, 0, name="Hole")
    return _volume(sw.add_mirror("right", features=["Hole"]), math.pi * (20 ** 2 - 2 * 3 ** 2) * 10)


def _check_text(sw):
    # letters have no hand calculation: the tool itself refuses text that cuts nothing
    _block(sw)
    return f"letters {sw.add_text_on_face('V1', '+z', 10, 5, 10, 5, 0.5)['text_area_mm2']:.3f} mm2"


def _check_delete(sw):
    _block(sw)
    sw.add_hole(6, 10, 10, name="Hole")
    return _volume(sw.delete_feature("Hole"), BLOCK_MM3)


def _check_export(sw):
    _block(sw)
    path = os.path.join(tempfile.gettempdir(), f"solidworks_mcp_selftest_{uuid.uuid4().hex}.stl")
    try:
        return f"{sw.export(path)['bytes']} bytes"
    finally:
        if os.path.exists(path):
            os.remove(path)


def _check_import(sw):
    _block(sw)
    path = os.path.join(tempfile.gettempdir(), f"solidworks_mcp_selftest_{uuid.uuid4().hex}.step")
    try:
        sw.export(path)
        sw.close_part()
        imported = sw.open_part(path)
        sw.close_part()  # let go of the file before it is removed
        return _volume(imported, BLOCK_MM3)
    finally:
        if os.path.exists(path):
            os.remove(path)


def _check_assembly(sw):
    return sw.new_assembly()["title"]


CHECKS = [
    ("block 40x20x10, fully defined sketch", _check_block),
    ("change a dimension (depth 10 -> 25)", _check_dimension),
    ("through hole", _check_hole),
    ("blind hole in a side face", _check_side_hole),
    ("fillet the vertical edges", _check_fillet),
    ("cylinder (revolve)", _check_cylinder),
    ("Hole Wizard: ISO M3 clearance hole", _check_hole_wizard),
    ("thread M10x1.5 (Thread feature)", _check_thread),
    ("material 6061 Alloy", _check_material),
    ("mirror a hole about the Right plane", _check_mirror),
    ("engrave text (sketch text and fonts)", _check_text),
    ("delete a feature", _check_delete),
    ("STL export", _check_export),
    ("STEP export and import", _check_import),
    ("new assembly", _check_assembly),
]


def _run_one(sw, check) -> str:
    """One check, run as the server runs a tool call: SOLIDWORKS 2026 SP4.0
    crashed while sketching otherwise."""
    return sw.run_guarded(_check_and_close, sw, check)


def _check_and_close(sw, check) -> str:
    try:
        detail = check(sw)
        loose = sw._under_defined_sketches() if sw._model is not None else []  # a check may close its own
        if loose:
            raise CheckFailed(f"sketches left under-defined: {', '.join(loose)}")
        return detail
    finally:
        if sw._model is not None:
            sw.close_part()


def run_checks(sw, checks):
    """Run each check on a document of its own; yield (label, passed, detail).

    A failure does not stop the others, so one report shows all that works and
    all that does not. Every check's document is closed unsaved.
    """
    for label, check in checks:
        try:
            detail = _run_one(sw, check)
        except Exception as exc:  # the report lists every failure, then the next check runs
            yield label, False, f"{type(exc).__name__}: {exc}"
        else:
            yield label, True, detail


def summary(results) -> str:
    passed = sum(1 for _, ok, _ in results if ok)
    return f"{passed} of {len(results)} checks passed."


def release_year(revision) -> int:
    """SOLIDWORKS release year from its revision number: 34.x is 2026, 28.x is 2020."""
    return 1992 + int(str(revision).split(".")[0])


def _version_of(distribution) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def _report_hint() -> None:
    entries = importlib.metadata.metadata("solidworks-mcp").get_all("Project-URL") or []
    urls = dict(entry.split(", ", 1) for entry in entries)
    print("Please report the result, whether it worked or not:")
    print(f"{urls['Issues']}/new?template=compatibility.yml" if "Issues" in urls
          else "on the project's issue tracker (this install's metadata has no link: reinstall solidworks-mcp)")


def main(sw) -> int:
    """Print the report for `sw`, the server's session; return the exit code."""
    print(f"solidworks-mcp {__version__} selftest")
    print(f"Python {platform.python_version()} on {platform.platform()}, "
          f"pywin32 {_version_of('pywin32')}, mcp {_version_of('mcp')}")
    try:
        sw.connect()
        info = sw.describe_installation()
    except Exception as exc:  # e.g. SolidWorks is not running: the report says so
        print(f"FAIL  connect to SolidWorks: {exc}")
        _report_hint()
        return 1
    print(f"SOLIDWORKS {release_year(info['revision'])} (revision {info['revision']}), language {info['language']}")
    for kind in ("part", "assembly"):
        template = info[f"{kind}_template"]
        where = "a file" if template["found"] else "not a file, so SolidWorks' own default is used"
        print(f"{kind} template: {template['path'] or '(none set)'} ({where})")
    print(f"part template units: {info['units']}")
    print()
    results = []
    for label, passed, detail in run_checks(sw, CHECKS):
        print(f"{'ok' if passed else 'FAIL':<4}  {label}: {detail}", flush=True)
        results.append((label, passed, detail))
    print()
    print(summary(results))
    _report_hint()
    return 0 if all(passed for _, passed, _ in results) else 1
