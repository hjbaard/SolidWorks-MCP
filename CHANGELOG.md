# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

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
