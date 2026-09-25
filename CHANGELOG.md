# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `add_rib`: a straight rib / gusset in a plane parallel to the Front plane,
  grown toward a given point until it meets the part. Verified against a hand
  calculation (an L-bracket gusset adds exactly a*b/2 * thickness).

### Changed
- All error messages and script output are English now (they were Dutch).
- The server now starts and lists its tools on any OS, so MCP directories
  (Glama) can introspect it in a Linux container. Off Windows every tool call
  fails loud: SolidWorks still needs Windows. `pywin32` is a Windows-only
  dependency now.

### Fixed
- `add_lofted_solid` left its helper offset planes visible, cluttering every
  screenshot; they are hidden after the loft now.

## [0.2.1] — 2026-09-25

### Fixed
- **Only SOLIDWORKS 2026 could connect**: the typelib version was hard-coded to
  34 (2026), so on any other release the first call failed with "Library not
  registered". The version is now read from the running SolidWorks.
- **Fresh installs crashed on start-up**: `mcp>=1.0` now resolves to mcp 2.x,
  which renamed `FastMCP`. Pinned to `mcp>=1.26,<2` (1.26–1.30 verified).

### Added
- `server.json` for the MCP Registry (`io.github.hjbaard/solidworks-mcp`) and
  PyPI-ready package metadata.

### Changed
- README rewritten: what it does and why, a gallery built by the tools
  themselves, a one-line install via `uvx`, and an honest compatibility note.

### Removed
- `scripts/m6_demo_kamer.py`: it needed private sample parts, so nobody else
  could run it. Assemblies stay covered by `tests/test_assembly.py`.

## [0.2.0] — 2026-09-05

### Added
- **Assemblies (M6)**: `new_assembly`, `open_assembly`, `save_assembly`,
  `insert_component`, `list_components`, `set_component_transform`, `add_mate`
  (coincident / distance / parallel / perpendicular between planar faces),
  `check_interference` and `get_assembly_bounding_box`. `export`, `screenshot`,
  `rebuild`, `get_mass_properties` and `close_part` now serve assemblies too.
- Every placement is verified against the geometry: a written component
  transform is read back and compared, and a mate is measured back after the
  rebuild (perpendicular distance, or the angle between the faces) and rejected
  if it did not deliver what was asked.
- Face selectors take an optional `:inner` suffix (`+y:inner`) to address the
  cavity side of a hollow part.
- `scripts/m6_demo_kamer.py`: assembles a furnished bedroom from three parts and
  asserts the bounding box, the floor contact, a 50 mm distance mate and a
  clash-free interference check.

### Fixed
- **Face selection on hollow parts**: `_planar_face_by_normal` returned whichever
  face the API listed first when several shared the same normal, so on a shelled
  box it could pick the inner face of the opposite wall instead of the outer
  skin. It now picks the extreme along the direction — outermost by default,
  innermost with `:inner`.
- A sketch left open by a rejected on-face point (`add_hole_on_face`,
  `cut_profile_on_face`) broke the *next* operation; the sketch is now always
  closed, and a missing active sketch fails at its own root cause.
- `get_bounding_box` on an assembly returned `null` instead of failing, and
  `IAssemblyDoc.GetBox` reports stale extents until the assembly is rebuilt —
  both now give a live box or a readable error.

## [0.1.0] — 2026-06-26

**First public release — an early, experimental first draft.** It works
end-to-end against SOLIDWORKS 2026 (3DEXPERIENCE R2026x) on the author's machine,
but the API surface and conventions may still change. Treat it as a starting
point, not a stable product.

### Added
- MCP server (stdio) that drives a locally running SolidWorks instance over the
  COM API, with all calls serialised on one dedicated STA worker thread.
- **Build**: `add_box`, `add_cylinder`, `add_cone`, `add_disc`,
  `add_extruded_profile`, `add_extruded_spline` (free-form), `add_revolved_profile`
  (general revolve), `add_swept_pipe` (round profile along a path),
  `add_swept_profile` (any cross-section along a path), `add_lofted_solid`.
- **Cut**: `add_hole`, `add_hole_on_face` (any planar face), `add_counterbore_hole`,
  `cut_profile`, `cut_profile_on_face`, `cut_slot`.
- **Modify**: `add_fillet`, `add_chamfer`, `add_shell`, `add_linear_pattern`,
  `add_circular_pattern`, `set_dimension`, `set_equation`, `set_material`, `rebuild`.
- **Inspect/measure**: `get_status`, `get_mass_properties`, `get_bounding_box`,
  `list_faces`, `list_edges`.
- **I/O**: `export` (STEP/STL/IGES/Parasolid/3MF, with STL/3MF tessellation
  resolution control), `screenshot`, `save_part`, `open_part`, `new_part`,
  `close_part`.
- Two-layer test suite (pytest): pure unit tests + SolidWorks integration tests
  that verify each feature's volume against a hand calculation.
- `scripts/m5_demo_bracket.py`: an end-to-end demo that builds a functional
  3D-print mounting bracket through the full build → measure → verify loop.

### Known limitations
- **Mirror** is not supported (both SolidWorks API routes fail on this build).
- Swept profiles require the path to start at the origin heading +X.
- 3D (non-planar) sweep paths, countersink holes, and embossed text are not yet
  implemented.
- Verified only against SOLIDWORKS 2026 (3DEXPERIENCE R2026x).
