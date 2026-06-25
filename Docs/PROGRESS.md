# SolidWorks MCP — Progress log

## Environment (verified 2026-06-25)

- SolidWorks: **2026 (3DEXPERIENCE R2026x)**, file version 34.2.0.0127, COM ProgID
  `SldWorks.Application`, CLSID `{666aaee2-7a21-40fc-b768-2078840a88c3}`.
- Install dir: `C:\Program Files\Dassault Systemes\SOLIDWORKS 3DEXPERIENCE R2026x\SOLIDWORKS\`.
- Typelibs: `sldworks.tlb` (GUID `{83A33D31-27C5-11CE-BFD4-00400513BB57}`, v34.0),
  `swconst.tlb` (constants).
- Python 3.13.7, venv at `.venv`, `pywin32 312`, `mcp 1.28.0`.

## Milestones

### M0 — Connection ✅
`scripts/probe_connection.py`. Attaches via `GetActiveObject`, reads
`RevisionNumber` (34.2.0), reports active doc.

### M1 — Build + measure ✅
`scripts/m1_block.py`. new part → rectangle on first plane → extrude →
`CreateMassProperty`. Block 40×20×10 mm → volume 8000 mm³, area 2800 mm²,
centre of mass (20,10,5) mm, mass 0.008 kg — all match hand calc exactly.

### M2 — Parametric ✅
`scripts/m2_parametric.py`. Change `D1@BlockExtrude` 10→25 mm → rebuild →
volume 8000→20000 mm³ (ratio 2.5). Proves the parametric loop.

### M3 — Full agent loop via MCP server ✅
`scripts/test_mcp_server.py`. Drives the server over stdio (10 tools):
get_status → new_part → add_box → measure → set_dimension → measure →
bounding_box → export STEP (15.8 KB, valid AP203) + STL → screenshot (valid
isometric render) → close_part. All checks pass.

### Hardening (post-review, 2026-06-25)
Adversarial code-review pass (4 dimensions, findings verified). Applied:
- **Call timeout** in `ComWorker.call` — a blocked COM call (modal dialog) now
  surfaces a readable timeout error instead of wedging the server forever.
- **Mass properties fail loud** if SI units can't be forced (`UseSystemUnits`),
  instead of silently returning wrong-scaled numbers.
- **`set_dimension` reads the value back** after rebuild and reports the applied
  value + an `applied` flag, so a driven/equation dimension that ignores the
  write is visible to the agent.
- **Readable COM errors** (extract `excepinfo` description) and a sketch-result
  check in `add_box` (fail at the root cause, not later at the extrude).
- Removed dead code (`DOC_TYPE_NAMES`, unused doc-type constants,
  `deg_to_rad`/`rad_to_deg`). Re-run: M1/M2/MCP all still green.

## Key API findings (this build)

These were read from the installed typelib (`scripts/introspect_api.py`), not
guessed — and several differ from common web docs:

- `swDefaultTemplatePart = 8` (not 9, as widely stated online).
- `GetActiveObject` returns a dispatch whose `GetTypeInfo()` raises
  `TYPE_E_ELEMENTNOTFOUND` → `EnsureDispatch`/`CastTo` fail → **early binding via
  manual wrapping in generated classes is mandatory** (see `binding.py`).
- `IModelDoc2` has **no** `GetBox`. Part bounding box comes from
  `IPartDoc.GetPartBox(NoConversion=True)` (returns metres).
- `RevisionNumber` resolves as a property under late binding, a method under
  early binding — handle both (`callable` check).
- `FeatureManager.FeatureExtrusion3` takes 23 args; types verified
  (bool/int/double) against the typelib.
- Plane selection by name (`"Front Plane"`) is language-dependent; we walk the
  feature tree for `GetTypeName2() == "RefPlane"` instead.

## Next (M4)

- Generic sketch primitives; revolve; fillet on selected edges.
- **Hole / cut-extrude on a face** (needed for the kickoff M3 spec
  "block 40×20×10, Ø8 hole centred") — requires robust face selection.
- Equations (`IEquationMgr`).
- Richer rebuild-error reporting (feature-level error/warning enumeration).

## Notes

- Standalone scripts run single-threaded (no COM worker needed); the server
  pins COM to one worker thread.
- Repeated `--keep-open` runs leave untitled parts (Part1, Part2, …) open in
  SolidWorks; harmless, close them manually.
