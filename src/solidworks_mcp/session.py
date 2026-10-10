"""SolidWorks operations.

A thin, stateful wrapper over the COM API. Every method here must run on the COM
worker thread (see com_worker). Methods return plain JSON-serialisable dicts so
the MCP tools can hand them straight back to the agent.

The call sequences (enum values, FeatureExtrusion3 argument order, the
language-independent plane walk, forced-SI mass properties) are the ones proven
green by scripts/m1_block.py and scripts/m2_parametric.py.
"""

import contextlib
import itertools
import math
import os
import re
import shutil
import tempfile
import uuid
from xml.sax.saxutils import quoteattr

import pythoncom
import win32com.client
import win32gui

from . import __version__, binding, free_sketch
from .constants import (
    FEATURE_ERRORS,
    DRAWING_FORMATS,
    EXPORT_FORMATS,
    IMPORT_FORMATS,
    LENGTH_UNITS,
    MATE_TYPES,
    MCP_MATERIAL_DATABASE,
    RAY_HIT_WIDTH,
    SW_ANGULAR_DIMENSION,
    SW_DIMENSION_DRIVING,
    SW_DIMENSION_PARAM_ANGULAR,
    SW_ADD_COMPONENT_CURRENT_CONFIG,
    SW_ADD_MATE_NO_ERROR,
    SW_BODY_SOLID,
    SCREW_FITS,
    SKETCH_STATUSES,
    SW_CHAMFER_ANGLE_DISTANCE,
    SW_COMPONENT_LIGHTWEIGHT_STATES,
    SW_COMPONENT_SUPPRESSED,
    SW_CONSTRAINED_CORNER_KEEP,
    SW_CONSTRAINT_RADIUS,
    SW_COSMETIC_THREAD_WITH_CALLOUT,
    SW_DELETE_ABSORBED,
    SW_DELETE_CHILDREN,
    SW_DETAILING_NO_OPTION,
    SW_DOC_ASSEMBLY,
    SW_DOC_DRAWING,
    SW_DOC_PART,
    SW_DONT_REBUILD_ACTIVE_DOC,
    SW_DWG_PAPER_A4,
    SW_END_COND_BLIND,
    SW_END_COND_MID_PLANE,
    SW_END_COND_THROUGH_ALL,
    SW_END_COND_UP_TO_NEXT,
    SW_END_COND_UP_TO_SURFACE,
    SW_FEATURE_CHAMFER_FLIP,
    SW_FEATURE_SCOPE_ALL_BODIES,
    SW_FILE_LOCATIONS_MATERIALS,
    SW_FILLET_OPT_PROPAGATE,
    SW_FILLET_OPT_STRAIGHT_TRANSITION,
    SW_FILLET_OPT_UNIFORM_RADIUS,
    SW_FILLET_TYPE_FULL_ROUND,
    SW_FILLET_TYPE_SIMPLE,
    SW_FILLET_TYPE_VARIABLE,
    SW_MARK_FULL_ROUND_CENTRE,
    SW_MARK_FULL_ROUND_SIDE_1,
    SW_MARK_FULL_ROUND_SIDE_2,
    SW_MARK_MIRROR_BODY,
    SW_MARK_MIRROR_FEATURE,
    SW_MARK_MIRROR_PLANE,
    SW_MATE_ALIGN_CLOSEST,
    SW_MOVE_BEFORE,
    SW_OPEN_DOC_SILENT,
    SW_PREF_DEFAULT_TEMPLATE_ASSEMBLY,
    SW_PREF_DEFAULT_TEMPLATE_PART,
    SW_FM_SWEEP_THREAD,
    SW_FULLY_CONSTRAINED,
    SW_HOR_LINEAR_DIMENSION,
    SW_IMPORT_ENTIRE_MODEL,
    SW_INSERT_DIMENSIONS,
    SW_ISO_SCREW_CLEARANCES,
    SW_ISO_SOCKET_COUNTERSUNK,
    SW_ISO_SOCKET_HEAD_CAP,
    SW_ISO_TAPPED_HOLE,
    SW_PREF_DEFAULT_TEMPLATE_DRAWING,
    SW_RAY_HIT_EXIT,
    SW_RAY_NORMALS_ENTRY_EXIT,
    SW_REF_PLANE_ANGLE,
    SW_REF_PLANE_COINCIDENT,
    SW_REF_PLANE_DISTANCE,
    SW_REF_PLANE_FLIP,
    SW_RELATIONS_ALL,
    SW_SKETCH_ARC,
    SW_SKETCH_LINE,
    SW_SKETCH_SPLINE,
    SW_SKETCH_TEXT,
    SW_SAVE_AS_CURRENT_VERSION,
    SW_SAVE_AS_OPTIONS_SILENT,
    SW_SEL_EXT_SKETCH_POINTS,
    SW_SEL_FACES,
    SW_SEL_SKETCH_POINTS,
    SW_SLOT_CREATION_LINE,
    SW_SLOT_LENGTH_CENTER,
    SW_START_SKETCH_PLANE,
    SW_STL_ANGLE_TOLERANCE,
    SW_STL_DEVIATION,
    SW_STL_QUALITY,
    SW_STL_QUALITY_COARSE,
    SW_STL_QUALITY_CUSTOM,
    SW_STL_QUALITY_FINE,
    SW_SUPPRESS_FEATURE,
    SW_TEXT_JUSTIFY_LEFT,
    SW_THIS_CONFIGURATION,
    SW_THREAD_END_BLIND,
    SW_THREAD_METHOD_CUT,
    SW_TOGGLE_3D_INTERCONNECT,
    SW_TOGGLE_INPUT_DIM_VAL_ON_CREATE,
    SW_TOGGLE_STL_DONT_TRANSLATE,
    SW_TOGGLE_DISPLAY_AXES,
    SW_TOGGLE_DISPLAY_PLANES,
    SW_TOGGLE_STL_ONE_FILE,
    SW_UNITS_LINEAR,
    SW_UNSUPPRESS_DEPENDENT,
    SW_VERT_LINEAR_DIMENSION,
    SW_WZD_COUNTERBORE,
    SW_WZD_COUNTERSINK,
    SW_WZD_HOLE,
    SW_WZD_STANDARD_ISO,
    SW_WZD_TAP,
    THREAD_PROFILE_EXTERNAL,
    THREAD_PROFILE_INTERNAL,
    VIEWS,
)
from .errors import SolidWorksError
from .mesh_tools import area, compare_sections, extents, load_mesh, section, simplify
from .region_tools import swept_outline
from .sketch_constraints import MAX_DIMENSIONED_VERTICES, SketchDefiner
from .units import deg_to_rad, m_to_mm, mm_to_m


class SolidWorksSession:
    """Holds the SolidWorks connection and the current part document."""

    def __init__(self) -> None:
        self._sw = None       # early-bound ISldWorks
        self._mod = None      # generated wrapper module
        self._model = None    # current IModelDoc2
        self._quiet_depth = 0  # inside _quiet_ui
        self._to_wake = []     # (document, [(object, property)]) to switch back on after it

    # --- connection -----------------------------------------------------------

    def _ensure(self):
        if self._sw is None:
            self.connect()
        return self._sw

    def connect(self) -> dict:
        """Attach to the running SolidWorks instance and configure it for automation."""
        self._sw = binding.connect()
        self._mod = binding.module()
        self._configure_for_automation()
        return self.get_status()

    def _configure_for_automation(self) -> None:
        # Suppress the modal "enter dimension value" popup so an unattended run
        # cannot deadlock waiting for a click. Best-effort.
        try:
            self._sw.SetUserPreferenceToggle(SW_TOGGLE_INPUT_DIM_VAL_ON_CREATE, False)
        except pythoncom.com_error:
            pass

    def _revision(self):
        # On the early-bound wrapper RevisionNumber may come back as a property
        # (string) or as a method, depending on how makepy generated it; handle
        # both. See Docs/PROGRESS.md.
        rev = self._sw.RevisionNumber
        return rev() if callable(rev) else rev

    def _require_model(self):
        """The current document, made SolidWorks' active one first.

        Sketching in a document that is open but not active fails at the first
        relation ('refused a horizontal relation'); that happened after
        open_part returned a part that was already open behind another document,
        and would after any switch of window in SolidWorks itself.
        """
        if self._model is None:
            raise SolidWorksError("No active document. Call 'new_part' or 'new_assembly' first.")
        title = self._model.GetTitle()
        active = binding.wrap(self._sw.ActiveDoc, self._mod.IModelDoc2)
        if active is None or active.GetTitle() != title:
            self._sw.ActivateDoc3(title, False, SW_DONT_REBUILD_ACTIVE_DOC, 0)
        if self._quiet_depth:
            self._quiet(self._model)
        if int(self._model.GetType()) == SW_DOC_ASSEMBLY and not self._model.IsEditingSelf():
            # left editing one of its parts (a double click in SolidWorks): the other
            # components show see-through and AddComponent5 returns None
            binding.wrap(self._model, self._mod.IAssemblyDoc).EditAssembly()
            if not self._model.IsEditingSelf():
                raise SolidWorksError(f"'{title}' is editing one of its parts and would not go back to editing "
                                      "the assembly; choose Edit Assembly in SolidWorks.")
        return self._model

    def _require_part(self):
        """The current document as an IPartDoc; raises if it is an assembly.

        Part tools would otherwise sketch into an assembly and fail much later
        with an opaque message, so the doc type is checked at the choke points
        every part builder passes through (_first_ref_plane / _solid_body).
        """
        model = self._require_model()
        if int(model.GetType()) != SW_DOC_PART:
            raise SolidWorksError(
                f"The current document '{model.GetTitle()}' is an assembly, not a part. "
                "Call 'new_part' or 'open_part', or use the assembly tools."
            )
        return binding.wrap(model, self._mod.IPartDoc)

    def _require_assembly(self):
        """The current document as an IAssemblyDoc; raises if it is a part."""
        model = self._require_model()
        if int(model.GetType()) != SW_DOC_ASSEMBLY:
            raise SolidWorksError(
                f"The current document '{model.GetTitle()}' is not an assembly. "
                "Call 'new_assembly' or 'open_assembly' first."
            )
        return binding.wrap(model, self._mod.IAssemblyDoc)

    # --- status ---------------------------------------------------------------

    def get_status(self) -> dict:
        sw = self._ensure()
        active = binding.wrap(sw.ActiveDoc, self._mod.IModelDoc2)
        active_title = active.GetTitle() if active is not None else None
        return {
            "ok": True,
            "connected": True,
            "server_version": __version__,
            "revision": self._revision(),
            "active_document": active_title,
            "current_part": self._model.GetTitle() if self._model is not None else None,
        }

    def describe_installation(self) -> dict:
        """What differs between SolidWorks installations: release, language,
        default templates and the part template's length unit.

        For the selftest. Opens a new part to read the unit and closes it again.
        """
        sw = self._ensure()
        templates = {}
        for kind, preference in (("part", SW_PREF_DEFAULT_TEMPLATE_PART),
                                 ("assembly", SW_PREF_DEFAULT_TEMPLATE_ASSEMBLY)):
            path = sw.GetUserPreferenceStringValue(preference)
            templates[f"{kind}_template"] = {"path": path, "found": bool(path) and os.path.isfile(path)}
        self.new_part()
        try:
            extension = binding.wrap(self._model.Extension, self._mod.IModelDocExtension)
            unit = extension.GetUserPreferenceInteger(SW_UNITS_LINEAR, SW_DETAILING_NO_OPTION)
        finally:
            self.close_part()
        return {"revision": self._revision(), "language": sw.GetCurrentLanguage(),
                "units": LENGTH_UNITS.get(unit, f"unit {unit}"), **templates}

    # --- document lifecycle ---------------------------------------------------

    def new_part(self) -> dict:
        """Create a new empty part; it becomes the current document."""
        sw = self._ensure()
        template = sw.GetUserPreferenceStringValue(SW_PREF_DEFAULT_TEMPLATE_PART)
        model = None
        if template and os.path.isfile(template):
            model = binding.wrap(sw.NewDocument(template, 0, 0, 0), self._mod.IModelDoc2)
        if model is None:
            # Fallback avoids a "template not found" modal dialog.
            model = binding.wrap(sw.NewPart(), self._mod.IModelDoc2)
        if model is None:
            raise SolidWorksError("Could not create a new part document (template and NewPart both failed).")
        self._model = model
        return {"ok": True, "title": model.GetTitle()}

    def close_part(self, save: bool = False) -> dict:
        """Close the current document (part or assembly). CloseDoc never prompts.

        Without a current document it closes SolidWorks' active one, but only
        when that has no unsaved changes: closing never asks, so they would be
        lost.
        """
        if save:
            raise SolidWorksError("Saving on close is not supported yet; use 'save_part', 'save_assembly' or 'export' first.")
        if self._model is None:
            return self._close_active_document()
        title = self._require_model().GetTitle()
        self._sw.CloseDoc(title)
        self._model = None
        return {"ok": True, "closed": title}

    _DOC_TYPES = {SW_DOC_PART: "part", SW_DOC_ASSEMBLY: "assembly", SW_DOC_DRAWING: "drawing"}

    def _open_documents(self) -> list:
        return [binding.wrap(d, self._mod.IModelDoc2) for d in (self._ensure().GetDocuments() or ())]

    def _document_entry(self, document) -> dict:
        return {"title": document.GetTitle(), "path": document.GetPathName(),
                "type": self._DOC_TYPES.get(int(document.GetType()), "other"),
                "modified": bool(document.GetSaveFlag()), "visible": bool(document.Visible)}

    def list_documents(self) -> dict:
        """The documents open in SolidWorks: title, path (empty until saved),
        type, unsaved changes, and which one is current here and active there.
        Parts an open assembly loaded come along, not visible."""
        current = self._model.GetTitle() if self._model is not None else None
        active = binding.wrap(self._ensure().ActiveDoc, self._mod.IModelDoc2)
        active = active.GetTitle() if active is not None else None
        out = [{**self._document_entry(d), "current": d.GetTitle() == current, "active": d.GetTitle() == active}
               for d in self._open_documents()]
        return {"ok": True, "count": len(out), "documents": out}

    def activate_document(self, title: str) -> dict:
        """Make an open document current again by its title (list_documents),
        also a new one that was never saved."""
        documents = self._open_documents()
        key = re.sub(r"\.(sldprt|sldasm|slddrw)$", "", str(title).strip(), flags=re.IGNORECASE).lower()
        matches = [d for d in documents if d.GetTitle() == title] or \
            [d for d in documents if re.sub(r"\.(sldprt|sldasm|slddrw)$", "", d.GetTitle(), flags=re.IGNORECASE).lower() == key]
        if len(matches) != 1:
            listed = ", ".join(d.GetTitle() for d in documents) or "none"
            raise SolidWorksError(f"No open document '{title}' (open: {listed}).")
        self._model = matches[0]
        self._require_model()  # SolidWorks' active document too
        return {"ok": True, "current": self._model.GetTitle(), **self._document_entry(self._model)}

    def _close_active_document(self) -> dict:
        active = binding.wrap(self._ensure().ActiveDoc, self._mod.IModelDoc2)
        if active is None:
            raise SolidWorksError("No document is open.")
        title = active.GetTitle()
        if active.GetSaveFlag():
            raise SolidWorksError(
                f"There is no current document, and SolidWorks' active document '{title}' has unsaved "
                "changes, so it stays open. Save it in SolidWorks, or open it with open_part or "
                "open_assembly to work on it."
            )
        self._sw.CloseDoc(title)
        return {"ok": True, "closed": title}

    def _write_via_saveas3(self, abs_path: str, document=None) -> None:
        """SaveAs3 `document` (the current one by default) to abs_path, silent,
        and verify the file was actually (re)written.

        Checks the modification time advanced, so a silent SaveAs3 failure over a
        pre-existing file (locked/read-only target) is not reported as success.
        """
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        before = os.path.getmtime(abs_path) if os.path.isfile(abs_path) else None
        result = (document or self._model).SaveAs3(abs_path, SW_SAVE_AS_CURRENT_VERSION, SW_SAVE_AS_OPTIONS_SILENT)
        if not os.path.isfile(abs_path) or (before is not None and os.path.getmtime(abs_path) == before):
            raise SolidWorksError(
                f"Write failed: '{abs_path}' was not (re)written; SaveAs3 returned {result}. "
                "Is the file open or locked?"
            )

    def save_part(self, path: str) -> dict:
        """Save the current part to a native .sldprt file (silent)."""
        self._require_model()
        abs_path = os.path.abspath(path)
        if not abs_path.lower().endswith(".sldprt"):
            abs_path += ".sldprt"
        self._write_via_saveas3(abs_path)
        return {"ok": True, "path": abs_path, "bytes": os.path.getsize(abs_path)}

    def open_part(self, path: str) -> dict:
        """Open an existing .sldprt, or import a STEP, IGES or Parasolid file as a
        new part; it becomes the current part.

        An imported body has no history, but the tools work on it: holes and
        pockets on its faces, bosses, fillets. An import also returns its solid
        body count and mass properties, to see what came in and where it lies.
        """
        abs_path, imported = self._native_or_neutral(path, "sldprt", "a part")
        sw = self._ensure()
        if imported:
            model = self._import_document(sw, abs_path, SW_DOC_PART)
            bodies = binding.wrap(model, self._mod.IPartDoc).GetBodies2(SW_BODY_SOLID, True) or ()
            if not bodies:
                sw.CloseDoc(model.GetTitle())
                raise SolidWorksError(f"{os.path.basename(abs_path)} brought in no solid body (only surfaces?), "
                                      "so there is nothing to build on.")
            self._model = model
            return {"ok": True, "title": model.GetTitle(), "path": abs_path, "imported": True,
                    "solid_bodies": len(bodies), "mass_properties": self.get_mass_properties()["mass_properties"]}
        result = sw.OpenDoc6(abs_path, SW_DOC_PART, 0, "", 0, 0)
        doc = result[0] if isinstance(result, tuple) else result
        model = binding.wrap(doc, self._mod.IModelDoc2)
        if model is None:
            raise SolidWorksError(f"Could not open the part: {abs_path}")
        self._model = model
        return {"ok": True, "title": model.GetTitle(), "path": abs_path}

    @staticmethod
    def _native_or_neutral(path: str, native: str, kind: str) -> tuple:
        """(absolute path, whether it needs an import); other formats are refused
        before SolidWorks is touched."""
        abs_path = os.path.abspath(path)
        extension = os.path.splitext(abs_path)[1].lower().lstrip(".")
        if extension != native and extension not in IMPORT_FORMATS:
            raise SolidWorksError(
                f"Cannot open a .{extension} file as {kind}: use .{native}, or import "
                f"{', '.join('.' + f for f in sorted(IMPORT_FORMATS))}."
            )
        if not os.path.isfile(abs_path):
            raise SolidWorksError(f"File not found: {abs_path}")
        return abs_path, extension != native

    def _import_document(self, sw, abs_path: str, doc_type: int):
        """Import a STEP/IGES/Parasolid file; a document of the other type is
        closed again (components and all) with the tool that does take it.

        An assembly is imported with 3D Interconnect switched off for the call:
        with it on, the parts arrive wrapped in one sub-assembly, out of reach
        of list_components and add_mate. The user's setting is restored after.
        """
        interconnect = sw.GetUserPreferenceToggle(SW_TOGGLE_3D_INTERCONNECT)
        if doc_type == SW_DOC_ASSEMBLY:
            sw.SetUserPreferenceToggle(SW_TOGGLE_3D_INTERCONNECT, False)
        before = {d.GetTitle() for d in self._open_documents()}
        try:
            doc, errors = self._load_file(sw, abs_path)
        finally:
            sw.SetUserPreferenceToggle(SW_TOGGLE_3D_INTERCONNECT, interconnect)
        model = binding.wrap(doc, self._mod.IModelDoc2)
        name = os.path.basename(abs_path)
        if model is None:
            # error 1 came with the parts it had made left open, hidden and unsaved
            raise SolidWorksError(f"SolidWorks could not import {abs_path} (error {errors}"
                                  f"{', no reason given' if errors == 1 else ''}).{self._close_new_documents(before)}")
        if int(model.GetType()) != doc_type:
            raise SolidWorksError((f"{name} holds an assembly, not a part: open it with open_assembly."
                                   if doc_type == SW_DOC_PART else
                                   f"{name} holds a single part, not an assembly: open it with open_part.")
                                  + self._close_new_documents(before))
        return model

    def _close_new_documents(self, before: set) -> str:
        """Close every document not open before, assemblies first (a part an
        open assembly holds stays); '' when all went, else what stayed."""
        for _ in range(3):
            # titles first: closing an assembly closes its parts, whose objects then answer nothing
            new = sorted((int(d.GetType()) != SW_DOC_ASSEMBLY, d.GetTitle()) for d in self._open_documents()
                         if d.GetTitle() not in before)
            if not new:
                return ""
            for _, title in new:
                self._sw.CloseDoc(title)
        stayed = sorted(d.GetTitle() for d in self._open_documents() if d.GetTitle() not in before)
        return f" It left open: {', '.join(stayed)}; close each with activate_document and close_part." if stayed else ""

    @staticmethod
    def _load_file(sw, abs_path: str) -> tuple:
        """LoadFile4 with the format's own import settings: (document or None, errors)."""
        return sw.LoadFile4(abs_path, "r", sw.GetImportFileData(abs_path), 0)

    # --- geometry -------------------------------------------------------------

    def _first_ref_plane(self):
        """First reference plane via tree walk (language-independent: 'RefPlane').

        Avoids SelectByID2('Front Plane', ...), which breaks on non-English
        installs. In a fresh part the first RefPlane is the Front plane.
        """
        self._require_part()
        feat = binding.wrap(self._model.FirstFeature(), self._mod.IFeature)
        while feat is not None:
            try:
                if feat.GetTypeName2() == "RefPlane":
                    return feat
            except pythoncom.com_error:
                pass
            feat = binding.wrap(feat.GetNextFeature(), self._mod.IFeature)
        return None

    def _solid_body(self):
        """The first solid body of the current part (early-bound IBody2)."""
        part = self._require_part()
        bodies = part.GetBodies2(SW_BODY_SOLID, True)
        if not bodies:
            raise SolidWorksError("No solid body; build geometry first (e.g. add_box).")
        if not isinstance(bodies, (list, tuple)):
            bodies = [bodies]
        return binding.wrap(bodies[0], self._mod.IBody2)

    # A face's normal alone does not identify it: a shelled/walled part has SEVERAL
    # planar faces with the same outward normal (e.g. for +Z the outer top face and
    # the inner floor of the opposite wall). They are told apart by WHERE they sit
    # along that normal, so every face lookup picks an extreme: 'outer' = furthest
    # along the direction (the part's outside skin), 'inner' = least far (the
    # cavity side). Picking whichever face the API happened to list first -- what
    # this used to do -- silently returned the wrong one on any hollow part.
    _FACE_SIDES = ("outer", "inner")

    def _planar_faces_facing(self, faces, target) -> list:
        """(IFace2, position_mm along `target`) of every PLANAR face facing `target`.

        Non-planar faces (a cylinder left by a hole, a fillet surface) are skipped
        so every result is a valid sketch base.
        """
        tx, ty, tz = target
        found = []
        for face_dispatch in faces:
            face = binding.wrap(face_dispatch, self._mod.IFace2)
            surface = binding.wrap(face.GetSurface(), self._mod.ISurface)
            if surface is None or not surface.IsPlane():
                continue  # only sketch on flat faces
            nx, ny, nz = face.Normal
            if nx * tx + ny * ty + nz * tz < 0.999:  # ~2.6 degrees
                continue
            box = face.GetBox()  # planar face -> its box centre lies in the plane
            found.append((face, m_to_mm(sum((box[i] + box[i + 3]) / 2.0 * target[i] for i in range(3)))))
        return found

    def _pick_planar_face(self, faces, target, side: str):
        """Extreme PLANAR face along `target`: 'outer' = max, 'inner' = min position.

        Returns (IFace2, position_mm along `target`) or (None, None) if no planar
        face faces that way.
        """
        if side not in self._FACE_SIDES:
            raise SolidWorksError(f"Unknown face side '{side}'. Use 'outer' or 'inner'.")
        facing = self._planar_faces_facing(faces, target)
        if not facing:
            return None, None
        return (max if side == "outer" else min)(facing, key=lambda fp: fp[1])

    def _body_faces(self, body) -> list:
        faces = body.GetFaces()
        if not faces:
            return []
        return list(faces) if isinstance(faces, (list, tuple)) else [faces]

    def _planar_face_by_normal(self, body, target, side: str = "outer"):
        """The body's outermost (default) or innermost planar face facing `target`."""
        faces = self._body_faces(body)
        return self._pick_planar_face(faces, target, side)[0] if faces else None

    def _select_face_through(self, body, normal, side, point_mm, label: str):
        """Select the planar face facing `normal` that the 3D point lies on.

        A face selector with an explicit ':outer'/':inner' side keeps picking that
        extreme face; without one the point decides, so a pocket floor or a step
        between the outermost and innermost face can be sketched on too.
        """
        if side is not None:
            return self._select_planar_face(body, normal, label, side)
        position = sum(p * n for p, n in zip(point_mm, normal))
        facing = self._planar_faces_facing(self._body_faces(body), normal)
        at_level = [f for f, pos in facing if abs(pos - position) <= self._ON_FACE_TOLERANCE_MM]
        # faces at one height (a top split by a slot): the one the point is on
        face = min(at_level, key=lambda f: self._off_face_mm(f, point_mm), default=None)
        if face is None:
            levels = ", ".join(f"{pos:g}" for pos in sorted({round(pos, 6) for _, pos in facing})) or "none"
            raise SolidWorksError(
                f"No planar {label} face through ({', '.join(f'{c:g}' for c in point_mm)}) mm: the faces "
                f"facing that way lie at {levels} mm along it. Give a point on one of them."
            )
        self._model.ClearSelection2(True)
        if not binding.wrap(face, self._mod.IEntity).Select4(False, None):
            raise SolidWorksError(f"Could not select the {label} face.")
        return face

    def _off_face_mm(self, face, point_mm) -> float:
        """How far the 3D point lies from the face: 0 on it, more beyond its edge
        or over an opening in it, such as a hole."""
        nearest = face.GetClosestPointOn(*(mm_to_m(c) for c in point_mm))
        if not nearest or len(nearest) < 3:
            raise SolidWorksError("SolidWorks found no nearest point on the face.")
        return math.dist(point_mm, [m_to_mm(c) for c in nearest[:3]])

    _DIRECTIONS = {
        "+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0),
        "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
        "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
    }

    def _parse_direction(self, token: str):
        """'+z'/'-x'/... -> a unit vector tuple. Raises on an unknown token."""
        key = (token or "").lower().strip()
        if key not in self._DIRECTIONS:
            raise SolidWorksError(f"Unknown direction '{token}'. Use +x/-x/+y/-y/+z/-z.")
        return self._DIRECTIONS[key]

    @staticmethod
    def _face_index(selector) -> int | None:
        """'#5' or '5' -> 5, a face by its list_faces index; a direction -> None."""
        text = str(selector).strip()
        digits = text[1:] if text.startswith("#") else text
        return int(digits) if digits.isdigit() else None

    def _parse_face_selector(self, token: str, default_side: str | None = "outer"):
        """'+z' or '+z:inner' -> ((0,0,1), 'outer'|'inner'); pure, unit-tested.

        The optional ':inner' suffix asks for the cavity-side face instead of the
        outside skin, where several faces share the same normal (a shelled box, a
        room). Without a suffix the side is `default_side`; tools that get a point
        on the face pass None and let the point pick the face.
        """
        text = (token or "").lower().strip()
        direction, _, side = text.partition(":")
        side = side.strip() or default_side
        if side is None:
            return self._parse_direction(direction), None
        if side not in self._FACE_SIDES:
            raise SolidWorksError(
                f"Unknown face side ':{side}' in '{token}'. Use ':outer' (default) or ':inner'."
            )
        return self._parse_direction(direction), side

    def _edge_parallel_to(self, edge_dispatch, target) -> bool:
        """True if a STRAIGHT edge runs parallel to unit vector `target`.

        Requires the edge's underlying curve to be a line, so arcs left by a
        fillet/chamfer (open arcs that DO have two vertices) and a hole's circle
        are never matched, then compares the line direction against `target`.
        """
        edge = binding.wrap(edge_dispatch, self._mod.IEdge)
        curve = binding.wrap(edge.GetCurve(), self._mod.ICurve)
        if curve is None or not curve.IsLine():
            return False
        start = edge.GetStartVertex()
        end = edge.GetEndVertex()
        if start is None or end is None:
            return False
        p1 = binding.wrap(start, self._mod.IVertex).GetPoint()
        p2 = binding.wrap(end, self._mod.IVertex).GetPoint()
        dx, dy, dz = p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2]
        length = (dx * dx + dy * dy + dz * dz) ** 0.5
        if length < 1e-9:
            return False
        dot = abs(dx * target[0] + dy * target[1] + dz * target[2]) / length
        return dot > 0.999  # ~2.6 degrees

    _EDGE_AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}

    @staticmethod
    def _parse_edge_indices(selector):
        """Return a list of int indices if `selector` denotes indices, else None.

        Accepts a list/tuple of ints, or a string like '2,5' / '2 5'.
        """
        if isinstance(selector, (list, tuple)):
            return [int(i) for i in selector]
        text = str(selector).strip()
        if text and any(c.isdigit() for c in text) and all(c.isdigit() or c in ", " for c in text):
            return [int(p) for p in text.replace(",", " ").split()]
        return None

    def _select_edges(self, body, selector="all") -> int:
        """Append-select body edges matching `selector`; return how many.

        selector: 'all' = every edge; 'x'|'y'|'z' = straight edges parallel to
        that world axis (for an add_box block, 'z' is the depth edges); a face
        outline like '+z:outline' (see _select_outline); or explicit indices as
        [2, 5] or '2,5' (into list_edges order).
        """
        edges = body.GetEdges()
        if not edges:
            return 0
        if not isinstance(edges, (list, tuple)):
            edges = [edges]
        self._model.ClearSelection2(True)
        count = 0

        indices = self._parse_edge_indices(selector)
        if indices is not None:
            for idx in indices:
                if idx < 0 or idx >= len(edges):
                    raise SolidWorksError(f"Edge index {idx} is out of range (0..{len(edges) - 1}).")
                if binding.wrap(edges[idx], self._mod.IEntity).Select4(True, None):
                    count += 1
            return count

        feature = re.fullmatch(r"feature:(.+)", str(selector).strip(), re.IGNORECASE)
        if feature:
            return self._select_feature_edges(feature.group(1).strip())
        sel = str(selector).lower().replace(" ", "")
        outline = re.fullmatch(r"([+-][xyz]):outline", sel)
        if outline:
            return self._select_outline(body, outline.group(1))
        if sel != "all" and sel not in self._EDGE_AXES:
            raise SolidWorksError(
                f"Unknown edge selector '{selector}'. Use 'all', 'x'/'y'/'z', a face outline like "
                "'+z:outline', a feature's edges like 'feature:Boss', or indices like '2,5'."
            )
        target = self._EDGE_AXES.get(sel)
        for edge_dispatch in edges:
            if target is not None and not self._edge_parallel_to(edge_dispatch, target):
                continue
            if binding.wrap(edge_dispatch, self._mod.IEntity).Select4(True, None):
                count += 1
        return count

    def _select_feature_edges(self, name: str) -> int:
        """Append-select every edge of the faces feature `name` made: rounding them
        all at once gives a boss or a rib the soft look of a moulded part."""
        feature = self._history_feature(name)
        extension = binding.wrap(self._model.Extension, self._mod.IModelDocExtension)
        seen, count = set(), 0
        for face in feature.GetFaces() or ():
            for edge in binding.wrap(face, self._mod.IFace2).GetEdges() or ():
                key = bytes(extension.GetPersistReference3(edge))  # one edge, two faces: select it once
                if key in seen:
                    continue
                seen.add(key)
                if binding.wrap(edge, self._mod.IEntity).Select4(True, None):
                    count += 1
        if not count:
            raise SolidWorksError(f"Feature '{name}' has no edges to round.")
        return count

    def _select_outline(self, body, direction: str) -> int:
        """Select the outer loop of the outermost planar face facing `direction`:
        its outline, without the edges of holes and pockets inside it."""
        face = self._planar_face_by_normal(body, self._parse_direction(direction), "outer")
        if face is None:
            raise SolidWorksError(f"No planar {direction} face to take the outline of.")
        loops = [binding.wrap(loop, self._mod.ILoop2) for loop in binding.wrap(face, self._mod.IFace2).GetLoops() or ()]
        outer = next((loop for loop in loops if loop.IsOuter()), None)
        if outer is None:
            raise SolidWorksError(f"The {direction} face has no outer loop.")
        count = 0
        for edge in outer.GetEdges() or ():
            if binding.wrap(edge, self._mod.IEntity).Select4(True, None):
                count += 1
        return count

    def _finish_feature(self, feature, name: str, **extra) -> dict:
        """Name a freshly created feature, rebuild, and build the result dict.

        Shared tail for the feature builders. `rebuild_ok` mirrors set_dimension
        so the agent can tell when a feature was created but the rebuild failed.
        """
        try:
            feature.Name = name
        except pythoncom.com_error:
            pass  # naming is best-effort; we read the real name back below
        rebuilt_ok = bool(self._model.ForceRebuild3(False))
        return {
            "ok": True,
            "feature": feature.Name,
            "rebuild_ok": rebuilt_ok,
            **extra,
            "mass_properties": self.get_mass_properties()["mass_properties"],
        }

    @staticmethod
    def _with_depth(result: dict, depth_mm) -> dict:
        """Name a blind cut's depth among its dimensions; a through-all cut has none."""
        if depth_mm is not None:
            result["dimensions"]["depth"] = f"D1@{result['feature']}"
        return result

    def _extrude_sketch(self, depth_mm: float | None, name: str, sketch: dict, hint: str = "",
                        role: str = "depth", reverse: bool = False, draft_deg: float = 0.0,
                        merge: bool = True, end_face=None) -> dict:
        """Extrude the sketch just closed (it stays selected) depth_mm along its
        normal (reverse: the other way), merged with the body; finish the
        feature and name its depth. depth_mm None runs up to the next face of
        the part instead, or up to end_face when given, ending on its shape,
        with no depth to name.

        Shared by every boss. `sketch` is the _define_sketch result, `role` the
        depth's name among the dimensions, `hint` what to check when it fails.
        draft_deg tapers the walls inwards as they rise (negative: outwards) and
        is named 'draft'; merge=False keeps the result a body of its own, named
        in the result's 'body'.
        """
        bodies_before = {body.Name for body in self._part_bodies()}
        feat_mgr = binding.wrap(self._model.FeatureManager, self._mod.IFeatureManager)
        up_to_next = depth_mm is None
        if end_face is not None:
            data = binding.wrap(binding.wrap(self._model.SelectionManager, self._mod.ISelectionMgr).CreateSelectData(),
                                self._mod.ISelectData)
            data.Mark = 1
            if not binding.wrap(end_face, self._mod.IEntity).Select4(True, data):
                raise SolidWorksError("Could not select the face to extrude up to.")
        end = SW_END_COND_BLIND if not up_to_next else SW_END_COND_UP_TO_SURFACE if end_face else SW_END_COND_UP_TO_NEXT
        extrude = feat_mgr.FeatureExtrusion3(
            True, False, reverse,      # Sd (single dir), Flip, Dir
            end, 0,                    # T1, T2 (end conditions)
            0.0 if up_to_next else mm_to_m(depth_mm), 0.0,  # D1 (depth), D2
            bool(draft_deg), False,    # Dchk1 (draft), Dchk2
            draft_deg < 0, False,      # Ddir1 (outward), Ddir2
            math.radians(abs(draft_deg)), 0.0,  # Dang1, Dang2 (draft, radians)
            False, False,              # OffsetReverse1, OffsetReverse2
            False, False,              # TranslateSurface1, TranslateSurface2
            bool(merge),               # Merge
            True,                      # UseFeatScope
            True,                      # UseAutoSelect
            SW_START_SKETCH_PLANE,     # T0 (start condition)
            0.0,                       # StartOffset
            False,                     # FlipStartOffset
        )
        if extrude is None:
            raise SolidWorksError(f"FeatureExtrusion3 failed (None). {hint}".strip())
        result = self._finish_feature(extrude, name, **sketch)
        if not up_to_next:
            result["dimensions"][role] = f"D1@{result['feature']}"
        if draft_deg:
            feature = binding.wrap(extrude, self._mod.IFeature)
            result["dimensions"]["draft"] = next(d["name"] for d in self._dimension_entries(feature)
                                                 if d["unit"] == "deg")
        if not merge:
            [result["body"]] = [body.Name for body in self._part_bodies() if body.Name not in bodies_before]
        return result

    def add_box(self, width_mm: float, height_mm: float, depth_mm: float,
                name: str = "BlockExtrude") -> dict:
        """Sketch a rectangle on the first plane and extrude it; returns mass props.

        The extrude feature gets the stable name `name` so its depth dimension is
        addressable as 'D1@<name>' (used by set_dimension) regardless of language.
        The sketch is fully defined: `dimensions` names the width, height and
        depth, each changeable with set_dimension.
        """
        model = self._require_model()
        for value, label in ((width_mm, "width"), (height_mm, "height"), (depth_mm, "depth")):
            if value <= 0:
                raise SolidWorksError(f"{label} must be > 0 (got {value}).")

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("No reference plane found in the feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Could not select the reference plane.")

        w, h = mm_to_m(width_mm), mm_to_m(height_mm)
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            lines = self._draw_polyline(sk, [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)])
            sketch = self._define_sketch(sk, lines, names={("x", 1): "width", ("y", 2): "height"})
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)  # close the sketch (it stays selected for the extrude)

        result = self._extrude_sketch(depth_mm, name, sketch, "Is the sketch valid?")
        result["depth_dimension"] = result["dimensions"]["depth"]
        return result

    @staticmethod
    def _clean_polygon(points_mm) -> list:
        """Distinct polygon vertices as tuples, [x, y] or [x, y, z]; pure (no COM), unit-testable.

        Drops coincident consecutive points and a trailing point equal to the
        first (so open and explicitly-closed rings both work). Raises if fewer
        than 3 distinct vertices remain.
        """
        def same(a, b):
            return all(abs(p - q) < 1e-9 for p, q in zip(a, b))

        cleaned = []
        for point in points_mm:
            p = tuple(float(c) for c in point)
            if not cleaned or not same(p, cleaned[-1]):
                cleaned.append(p)
        if len(cleaned) >= 2 and same(cleaned[0], cleaned[-1]):
            cleaned.pop()  # drop an explicit closing point
        if len(cleaned) < 3:
            raise SolidWorksError(
                f"A profile needs at least 3 distinct points (got {len(cleaned)})."
            )
        return cleaned

    def _loft_section(self, section):
        """A loft profile: a cleaned polygon, or a checked round section
        {"center_mm": [x, y], "diameter_mm": d}; pure, unit-tested."""
        if not isinstance(section, dict):
            return self._clean_polygon(section)
        try:
            (cx, cy), diameter = section["center_mm"], float(section["diameter_mm"])
            centre = (float(cx), float(cy))
        except (KeyError, TypeError, ValueError):
            raise SolidWorksError(f'A round section is {{"center_mm": [x, y], "diameter_mm": d}} (got {section}).') from None
        if diameter <= 0:
            raise SolidWorksError(f"A round section needs a diameter > 0 (got {diameter}).")
        return {"center_mm": centre, "diameter_mm": diameter}

    @staticmethod
    def _loft_start(section) -> tuple:
        """Where a loft profile starts: +x on a round section, else the first
        vertex; pure, unit-tested."""
        if isinstance(section, dict):
            (cx, cy), diameter = section["center_mm"], section["diameter_mm"]
            return cx + diameter / 2, cy
        return tuple(section[0])

    @staticmethod
    def _check_draft(draft_deg) -> None:
        if not -90.0 < draft_deg < 90.0:
            raise SolidWorksError(f"draft_deg must be between -90 and 90 (got {draft_deg}).")

    @staticmethod
    def _turned_points(points_mm, rotate_deg, about_mm) -> list:
        """[x, y] points turned rotate_deg counterclockwise about about_mm (the
        origin when None); pure, unit-tested."""
        if not rotate_deg:
            return points_mm
        try:
            cx, cy = (0.0, 0.0) if about_mm is None else (float(about_mm[0]), float(about_mm[1]))
            pts = [(float(p[0]), float(p[1])) for p in points_mm]
        except (TypeError, ValueError, IndexError):
            raise SolidWorksError(f"rotate_deg turns [x, y] points about [x, y] (got about_mm={about_mm}).") from None
        c, s = math.cos(math.radians(rotate_deg)), math.sin(math.radians(rotate_deg))
        return [[cx + c * (x - cx) - s * (y - cy), cy + s * (x - cx) + c * (y - cy)] for x, y in pts]

    @staticmethod
    def _corner_groups(corner_radii_mm, count: int) -> list:
        """[(radius_mm, [corner indices]), ...] in corner order; pure (no COM), unit-tested.

        corner_radii_mm is None, one radius for every corner, or one per vertex
        (0 = sharp). Corners with the same radius share one dimension, the way
        vertices at the same x share one, so they form one group.
        """
        if corner_radii_mm is None:
            return []
        radii = ([float(corner_radii_mm)] * count if isinstance(corner_radii_mm, (int, float))
                 else [float(r) for r in corner_radii_mm])
        if len(radii) != count:
            raise SolidWorksError(
                f"corner_radii_mm has {len(radii)} values for {count} corners: give one radius for all "
                "corners, or one per vertex (0 = sharp)."
            )
        if any(r < 0 for r in radii):
            raise SolidWorksError(f"Corner radii must be >= 0 (got {radii}).")
        groups = {}
        for i, radius in enumerate(radii):
            if radius > 0:
                groups.setdefault(radius, []).append(i)
        if groups and count > MAX_DIMENSIONED_VERTICES:
            raise SolidWorksError(
                f"Corner radii need a profile of at most {MAX_DIMENSIONED_VERTICES} points (this one has "
                f"{count}): larger profiles are fixed, not dimensioned."
            )
        return sorted(groups.items(), key=lambda group: group[1][0])

    @staticmethod
    def _check_corner_radii(points, groups) -> None:
        """Fail before SolidWorks does; pure (no COM), unit-tested.

        A rounded corner sets its arc back r / tan(angle / 2) along both edges,
        so each needs a real corner and each edge room for the arcs at both its
        ends. Lengths and angles hold in 2D sketch and 3D model points alike.
        """
        n = len(points)
        setback = {}
        for radius, corners in groups:
            for i in corners:
                here = points[i]
                u = [p - h for p, h in zip(points[i - 1], here)]
                w = [p - h for p, h in zip(points[(i + 1) % n], here)]
                cos = sum(a * b for a, b in zip(u, w)) / (math.hypot(*u) * math.hypot(*w))
                angle = math.acos(max(-1.0, min(1.0, cos)))
                if angle < 1e-6 or angle > math.pi - 1e-6:
                    raise SolidWorksError(f"Corner {i} cannot be rounded: its edges are in line.")
                setback[i] = radius / math.tan(angle / 2)
        for i in range(n):
            j = (i + 1) % n
            need = setback.get(i, 0.0) + setback.get(j, 0.0)
            length = math.dist(points[i], points[j])
            if need and need > length - 1e-6:
                raise SolidWorksError(
                    f"Edge {i}-{j} is {length:g} mm long, but the corner radii at its ends need "
                    f"{need:g} mm of it. Use smaller radii."
                    + (" Arcs that would meet merge into one, which the corners cannot keep: for a "
                       "circle use add_disc, for round ends add_extruded_slot, for any outline add_sketch with "
                       "tangent arcs." if need < length + 1e-6 else "")
                )

    @staticmethod
    def _round_polyline(points_mm, radius_mm) -> list:
        """Open polyline with filleted interior corners; pure (no COM), unit-testable.

        Returns drawable segments in mm: ("line", (x1,y1), (x2,y2)) or
        ("arc", (cx,cy), (sx,sy), (ex,ey), direction) where direction is +1 (CCW)
        or -1 (CW). Each interior corner is replaced by a tangent arc of radius
        radius_mm. A straight 2-point path needs no radius; a path with corners
        requires radius_mm > 0. Raises if the radius does not fit a segment.
        """
        clean = []
        for p in points_mm:
            q = (float(p[0]), float(p[1]))
            if not clean or abs(q[0] - clean[-1][0]) > 1e-9 or abs(q[1] - clean[-1][1]) > 1e-9:
                clean.append(q)
        if len(clean) < 2:
            raise SolidWorksError(f"A path needs at least 2 distinct points (got {len(clean)}).")
        if len(clean) == 2:
            return [("line", clean[0], clean[1])]
        if radius_mm <= 0:
            raise SolidWorksError("bend_radius must be > 0 for a path with corners.")

        # Pass 1: fillet geometry per interior corner that genuinely turns.
        corners = []
        for i in range(1, len(clean) - 1):
            a, v, b = clean[i - 1], clean[i], clean[i + 1]
            ax, ay = a[0] - v[0], a[1] - v[1]
            bx, by = b[0] - v[0], b[1] - v[1]
            la, lb = math.hypot(ax, ay), math.hypot(bx, by)
            ax, ay, bx, by = ax / la, ay / la, bx / lb, by / lb
            theta = math.acos(max(-1.0, min(1.0, ax * bx + ay * by)))
            if abs(theta - math.pi) < 1e-6:
                continue  # collinear straight-through: no corner to round
            if theta < 1e-6:
                raise SolidWorksError(
                    f"The path doubles back on itself at point {i}; a sweep path cannot fold back 180 degrees."
                )
            setback = radius_mm / math.tan(theta / 2.0)
            if setback > la - 1e-9 or setback > lb - 1e-9:
                raise SolidWorksError(f"bend_radius {radius_mm} is too large for the segment at point {i}.")
            t_in = (v[0] + ax * setback, v[1] + ay * setback)
            t_out = (v[0] + bx * setback, v[1] + by * setback)
            bisx, bisy = ax + bx, ay + by
            lbis = math.hypot(bisx, bisy)
            dist_c = radius_mm / math.sin(theta / 2.0)
            c = (v[0] + bisx / lbis * dist_c, v[1] + bisy / lbis * dist_c)
            cross = (t_in[0] - c[0]) * (t_out[1] - c[1]) - (t_in[1] - c[1]) * (t_out[0] - c[0])
            corners.append({"i": i, "setback": setback, "t_in": t_in, "t_out": t_out,
                            "c": c, "dir": 1 if cross > 0 else -1})

        # Pass 2: two corners sharing a segment must not both eat past its length.
        # Use the distance between corner VERTICES (collinear points between them
        # were skipped and consume no setback).
        for prev, nxt in zip(corners, corners[1:]):
            shared = math.dist(clean[prev["i"]], clean[nxt["i"]])
            if prev["setback"] + nxt["setback"] > shared - 1e-9:
                raise SolidWorksError(
                    f"bend_radius {radius_mm} is too large: the bends at points {prev['i']} and {nxt['i']} "
                    "overlap on the segment between them."
                )

        # Pass 3: emit segments, skipping any zero-length connecting line.
        segs = []
        cur = clean[0]
        for corner in corners:
            if math.dist(cur, corner["t_in"]) > 1e-9:
                segs.append(("line", cur, corner["t_in"]))
            segs.append(("arc", corner["c"], corner["t_in"], corner["t_out"], corner["dir"]))
            cur = corner["t_out"]
        segs.append(("line", cur, clean[-1]))
        return segs

    @staticmethod
    def _draw_polyline(sk, pts_m, closed: bool = True) -> list:
        """Draw CreateLine segments through 2D points in METRES; return the lines.

        The sketch must already be open; segment i runs from point i to i+1,
        and the last back to the first when closed. Shared by the +Z polygon
        path and the any-face path (which supplies transformed sketch
        coordinates). Drawn with AddToDB: otherwise SolidWorks' automatic
        relations snap a nearly horizontal/vertical segment, which silently
        moves a point and makes a closing segment fail outright. That leaves
        every relation to _define_sketch.
        """
        n = len(pts_m)
        lines = []
        sk.AddToDB = True
        try:
            for i in range(n if closed else n - 1):
                x1, y1 = pts_m[i]
                x2, y2 = pts_m[(i + 1) % n]
                line = sk.CreateLine(x1, y1, 0.0, x2, y2, 0.0)
                if not line:
                    raise SolidWorksError(f"Could not create line segment {i}.")
                lines.append(line)
        finally:
            sk.AddToDB = False
        return lines

    @staticmethod
    def _draw_circle(sk, u_m, v_m, radius_m):
        """A circle in the open sketch, drawn with AddToDB: else SolidWorks'
        inference snaps a centre near an axis or another sketch onto it (a hole
        0.75 mm below the x axis landed on it, verified)."""
        sk.AddToDB = True
        try:
            circle = sk.CreateCircleByRadius(u_m, v_m, 0.0, radius_m)
        finally:
            sk.AddToDB = False
        if not circle:
            raise SolidWorksError("Circle sketch failed: CreateCircleByRadius returned nothing.")
        return circle

    @staticmethod
    def _draw_centerline(sk, x1_m, y1_m, x2_m, y2_m):
        """A revolve axis, drawn like _draw_polyline so no automatic relation lands on it."""
        sk.AddToDB = True
        try:
            axis = sk.CreateCenterLine(x1_m, y1_m, 0.0, x2_m, y2_m, 0.0)
        finally:
            sk.AddToDB = False
        if not axis:
            raise SolidWorksError("Could not create the revolve axis (centerline).")
        return axis

    def _origin_point(self):
        """The model origin as a sketch point, found via the tree (language-independent)."""
        for feat in self._iter_features():
            if feat.GetTypeName2() == "OriginProfileFeature":
                points = binding.wrap(feat.GetSpecificFeature2(), self._mod.ISketch).GetSketchPoints2()
                if points:
                    return binding.wrap(points[0], self._mod.ISketchPoint)
        raise SolidWorksError("The part has no origin point to dimension the sketch from.")

    def _open_sketch_definer(self, sk):
        sketch = binding.wrap(sk.ActiveSketch, self._mod.ISketch)
        if sketch is None:
            raise SolidWorksError("No open sketch to define.")
        return sketch, SketchDefiner(self._model, self._mod, sk, sketch, self._origin_point())

    def _define_sketch(self, sk, lines=(), circles=(), names=None) -> dict:
        """Fully define the open sketch (see sketch_constraints).

        Returns {'dimensions': {role: 'name@Sketch3'}, 'fully_defined': bool},
        ready to merge into a tool result.
        """
        sketch, definer = self._open_sketch_definer(sk)
        u0, v0, _ = self._sketch_coords(sketch, 0.0, 0.0, 0.0)  # the origin, projected
        dims = definer.define(lines, circles, names, (m_to_mm(u0), m_to_mm(v0)))
        return {"dimensions": dims, "fully_defined": definer.fully_defined()}

    def _fix_sketch(self, sk, segments) -> dict:
        """Freeze the open sketch's segments: for paths and splines, which have no
        dimensions worth editing. Same result shape as _define_sketch."""
        _, definer = self._open_sketch_definer(sk)
        definer.fix(segments)
        return {"dimensions": {}, "fully_defined": definer.fully_defined()}

    def _sketch_closed_polygon(self, sk, points_mm, corner_radii_mm=None) -> dict:
        """Open a sketch, draw a closed polygon from [x, y] points (mm) and define it.

        Tolerant of open and explicitly-closed rings (see _clean_polygon);
        corner_radii_mm rounds its corners (see _round_corners). Returns the
        _define_sketch result.
        """
        points = self._clean_polygon(points_mm)
        corners = self._profile_corners(points, corner_radii_mm)
        sk.InsertSketch(True)
        try:
            return self._draw_defined_polygon(sk, [(mm_to_m(x), mm_to_m(y)) for x, y in points], corners)
        finally:
            self._model.ClearSelection2(True)
            sk.InsertSketch(True)  # close the sketch, also when drawing failed

    def _profile_corners(self, points_mm, corner_radii_mm) -> tuple:
        """Check a profile's corner radii before any sketch opens, so a refused
        radius leaves nothing behind. Returns (vertex count, corner groups)."""
        points = self._clean_polygon(points_mm)
        groups = self._corner_groups(corner_radii_mm, len(points))
        self._check_corner_radii(points, groups)
        return len(points), groups

    def _draw_defined_polygon(self, sk, polygon_m, corners) -> dict:
        """Draw the cleaned polygon (sketch coordinates, metres) in the open
        sketch, fully define it and round its corners."""
        count, groups = corners
        if groups and len(polygon_m) != count:
            raise SolidWorksError(
                "Two profile points lie within a micrometre of each other: remove one, so each corner "
                "radius lands on its own corner."
            )
        lines = self._draw_polyline(sk, polygon_m)
        return self._round_corners(sk, lines, groups, self._define_sketch(sk, lines))

    def _round_corners(self, sk, lines, groups, defined: dict) -> dict:
        """Round corners of the open, fully defined polygon with sketch fillets.

        Each corner keeps its dimensions as a virtual sharp. One fillet call per
        group of equal radii: SolidWorks gives it one radius dimension and ties
        the group with equal relations. Returns `defined` plus that dimension,
        named 'radius' when every rounded corner shares it, else 'r<i>' after
        the group's first corner.
        """
        if not groups:
            return defined
        sketch = binding.wrap(sk.ActiveSketch, self._mod.ISketch)
        corners = [binding.wrap(binding.wrap(line, self._mod.ISketchLine).GetStartPoint2(), self._mod.ISketchPoint)
                   for line in lines]
        dimensions = dict(defined["dimensions"])
        for radius, indices in groups:
            radii_before = {d.GetNameForSelection() for d in self._radius_dimensions(sketch)}
            arcs_before = self._arc_count(sketch)
            self._model.ClearSelection2(True)
            if not all(corners[i].Select4(k > 0, None) for k, i in enumerate(indices)):
                raise SolidWorksError(f"Could not select corner(s) {indices} to round.")
            if sk.CreateFillet(mm_to_m(radius), SW_CONSTRAINED_CORNER_KEEP) is None:
                raise SolidWorksError(f"SolidWorks could not round corner(s) {indices} with R{radius:g}.")
            rounded = self._arc_count(sketch) - arcs_before
            new = [d for d in self._radius_dimensions(sketch) if d.GetNameForSelection() not in radii_before]
            if rounded != len(indices) or len(new) != 1:
                raise SolidWorksError(
                    f"Rounding corner(s) {indices} with R{radius:g} gave {rounded} arc(s) and {len(new)} "
                    "radius dimension(s); expected one arc per corner and one dimension."
                )
            role = "radius" if len(groups) == 1 else f"r{indices[0]}"
            new[0].Name = role
            dimensions[role] = new[0].GetNameForSelection()
        self._model.ClearSelection2(True)
        return {"dimensions": dimensions, "fully_defined": sketch.GetConstrainedStatus() == SW_FULLY_CONSTRAINED}

    def _radius_dimensions(self, sketch) -> list:
        """The sketch's radius dimensions (IDimension), found through their relations."""
        relations = binding.wrap(sketch.RelationManager, self._mod.ISketchRelationManager)
        found = []
        for relation_dispatch in relations.GetRelations(SW_RELATIONS_ALL) or ():
            relation = binding.wrap(relation_dispatch, self._mod.ISketchRelation)
            if relation.GetRelationType() == SW_CONSTRAINT_RADIUS:
                display = binding.wrap(relation.GetDisplayDimension(), self._mod.IDisplayDimension)
                found.append(binding.wrap(display.GetDimension2(0), self._mod.IDimension))
        return found

    def _arc_count(self, sketch) -> int:
        return sum(1 for segment in sketch.GetSketchSegments() or ()
                   if binding.wrap(segment, self._mod.ISketchSegment).GetType() == SW_SKETCH_ARC)

    def _open_face_sketch(self, sk, face: str):
        """Open a sketch on the already-selected face and return it (never None).

        Callers must close it in a `finally`: a sketch left open after a rejected
        point makes the NEXT InsertSketch close it instead of opening a new one,
        so one loud failure would break the following operation too.
        """
        sk.InsertSketch(True)
        sketch = binding.wrap(sk.ActiveSketch, self._mod.ISketch)
        if sketch is None:
            raise SolidWorksError(
                f"Could not open a sketch on the {face} face (InsertSketch gave no active sketch). "
                "Was a sketch left open by an earlier failed operation?"
            )
        return sketch

    def _select_planar_face(self, body, normal, label: str, side: str = "outer"):
        """Select the outermost (default) or innermost planar face facing `normal`."""
        face = self._planar_face_by_normal(body, normal, side)
        if face is None:
            raise SolidWorksError(f"No planar {label} face found.")
        self._model.ClearSelection2(True)
        if not binding.wrap(face, self._mod.IEntity).Select4(False, None):
            raise SolidWorksError(f"Could not select the {label} face.")
        return face

    def add_extruded_profile(self, points_mm: list, depth_mm: float,
                             name: str = "Extrude", corner_radii_mm=None, rotate_deg: float = 0.0,
                             about_mm: list | None = None, draft_deg: float = 0.0, merge: bool = True) -> dict:
        """Extrude a closed polygon profile into a solid on the first plane.

        points_mm is a list of [x, y] vertices (mm) in the first-plane coordinate
        system (same as add_box); the polygon is auto-closed and extruded by
        depth_mm along the plane normal. Unlocks arbitrary prismatic shapes
        (L-brackets, T-sections, polygons, ...). corner_radii_mm rounds the
        corners with real sketch fillets: one radius for all, or one per vertex
        (0 = sharp). rotate_deg turns the profile counterclockwise about about_mm
        ([x, y], the origin by default) before it is drawn, for parts at an angle
        such as a crank at 150 degrees. draft_deg tapers the walls inwards as
        they rise (negative: outwards), a dimension 'draft'; merge=False keeps the
        result a separate body (see list_bodies, combine_bodies). Returns mass
        properties (volume = polygon area * depth; a right-angled corner of
        radius r loses r^2 (1 - pi/4), a concave one gains it).
        """
        model = self._require_model()
        if depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        if not points_mm:
            raise SolidWorksError("No profile points given.")
        self._check_draft(draft_deg)
        points_mm = self._turned_points(points_mm, rotate_deg, about_mm)

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("No reference plane found in the feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Could not select the reference plane.")

        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sketch = self._sketch_closed_polygon(sk, points_mm, corner_radii_mm)
        return self._extrude_sketch(depth_mm, name, sketch, "Is the profile closed and not self-intersecting?",
                                    draft_deg=draft_deg, merge=merge)

    def add_extruded_spline(self, points_mm: list, depth_mm: float,
                            name: str = "Spline") -> dict:
        """Extrude a smooth CLOSED spline through the given points (organic shapes).

        points_mm = [[x, y], ...] in mm: interpolation points the spline passes
        through, on the first plane. The curve is closed (last -> first) and
        extruded depth_mm along the plane normal -- like add_extruded_profile but
        smooth/curved (cams, fillided outlines, free-form bosses). NOTE: a spline's
        enclosed area is not analytic, so the returned volume is the measured truth,
        not a hand-calc. Returns mass properties. Use new_part first.
        """
        model = self._require_model()
        if depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        pts = self._clean_polygon(points_mm)  # >= 3 distinct points

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("No reference plane found in the feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Could not select the reference plane.")

        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            coords = []
            for x, y in pts + [pts[0]]:  # repeat the first point to close the spline
                coords += [mm_to_m(x), mm_to_m(y), 0.0]
            point_data = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, coords)
            spline = sk.CreateSpline2(point_data, False)  # SimulateNaturalEnds=False
            if not spline:
                raise SolidWorksError("Spline sketch failed: CreateSpline2 returned nothing.")
            sketch = self._fix_sketch(sk, [spline])  # its points are the design; nothing to dimension
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)  # close the sketch
        return self._extrude_sketch(depth_mm, name, sketch, "Is the spline closed and not self-intersecting?")

    def add_disc(self, diameter_mm: float, thickness_mm: float, name: str = "Disc",
                 x_mm: float = 0.0, y_mm: float = 0.0) -> dict:
        """Create a disc / puck / flange: a circle extruded along +Z, centred at (x, y).

        Unlike add_cylinder (revolve, axis Y), the disc's flat faces are +Z/-Z, so
        add_hole and add_circular_pattern compose with it directly -- this is how
        round-flange bolt circles are built. The centre defaults to the origin;
        off it, its x and y are dimensions. Returns mass properties (volume =
        pi * r^2 * thickness).
        """
        model = self._require_model()
        if diameter_mm <= 0 or thickness_mm <= 0:
            raise SolidWorksError("diameter and thickness must be > 0.")

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("No reference plane found in the feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Could not select the reference plane.")

        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            circle = self._draw_circle(sk, mm_to_m(x_mm), mm_to_m(y_mm), mm_to_m(diameter_mm / 2.0))
            sketch = self._define_sketch(sk, circles=[circle], names={("x", 0): "x", ("y", 0): "y"})
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)
        return self._extrude_sketch(thickness_mm, name, sketch, role="thickness")

    def add_cylinder(self, diameter_mm: float, height_mm: float, name: str = "Revolve") -> dict:
        """Create a cylinder by revolving a rectangular profile 360 deg about an axis.

        Sketches a radius x height rectangle on the first plane with one edge on
        the revolve axis (a centerline at x=0) and revolves it fully. A single
        centerline is auto-detected as the axis. This proves the revolve path; the
        same plumbing extends to cones / general profiles next. Returns mass
        properties (volume should equal pi * r^2 * h).
        """
        model = self._require_model()
        for value, label in ((diameter_mm, "diameter"), (height_mm, "height")):
            if value <= 0:
                raise SolidWorksError(f"{label} must be > 0 (got {value}).")

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("No reference plane found in the feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Could not select the reference plane.")

        radius = mm_to_m(diameter_mm / 2.0)
        height = mm_to_m(height_mm)
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            lines = self._draw_polyline(sk, [(0.0, 0.0), (radius, 0.0), (radius, height), (0.0, height)])
            axis = self._draw_centerline(sk, 0.0, 0.0, 0.0, height)  # axis at x=0
            sketch = self._define_sketch(sk, lines + [axis], names={("x", 1): "radius", ("y", 2): "height"})
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)  # exit sketch

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        revolve = feat_mgr.FeatureRevolve2(
            True, True, False, False,    # SingleDir, IsSolid, IsThin, IsCut
            False, False,                # ReverseDir, BothDirectionUpToSameEntity
            SW_END_COND_BLIND, 0,        # Dir1Type, Dir2Type
            deg_to_rad(360.0), 0.0,      # Dir1Angle (full revolve), Dir2Angle
            False, False, 0.0, 0.0,      # OffsetReverse1/2, OffsetDistance1/2
            0, 0.0, 0.0,                 # ThinType, ThinThickness1/2
            True, True, True,            # Merge, UseFeatScope, UseAutoSelect
        )
        if revolve is None:
            raise SolidWorksError("FeatureRevolve2 failed (None). Is the profile valid?")
        return self._finish_feature(revolve, name, **sketch)

    def add_cone(self, bottom_diameter_mm: float, top_diameter_mm: float,
                 height_mm: float, name: str = "Revolve") -> dict:
        """Create a cone/frustum by revolving a trapezoidal profile 360 deg.

        top_diameter_mm = 0 gives a full cone. Reuses the add_cylinder revolve
        plumbing (a closed profile + a centerline axis). Returns mass properties
        (volume = pi*h/3 * (rb^2 + rb*rt + rt^2)).
        """
        model = self._require_model()
        if bottom_diameter_mm <= 0 or height_mm <= 0:
            raise SolidWorksError("bottom_diameter and height must be > 0.")
        if top_diameter_mm < 0:
            raise SolidWorksError("top_diameter cannot be negative.")
        if top_diameter_mm >= bottom_diameter_mm:
            raise SolidWorksError("top_diameter must be smaller than bottom_diameter (otherwise use add_cylinder).")

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("No reference plane found in the feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Could not select the reference plane.")

        rb = mm_to_m(bottom_diameter_mm / 2.0)
        rt = mm_to_m(top_diameter_mm / 2.0)
        h = mm_to_m(height_mm)
        # bottom edge, slant edge, top edge (omitted for a full cone), axis edge
        profile = [(0.0, 0.0), (rb, 0.0), (rt, h), (0.0, h)] if rt > 1e-9 else [(0.0, 0.0), (rb, 0.0), (0.0, h)]
        names = {("x", 1): "bottom_radius", ("y", 2): "height"}
        if rt > 1e-9:
            names[("x", 2)] = "top_radius"
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            lines = self._draw_polyline(sk, profile)
            axis = self._draw_centerline(sk, 0.0, 0.0, 0.0, h)  # revolve axis at x=0
            sketch = self._define_sketch(sk, lines + [axis], names=names)
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        revolve = feat_mgr.FeatureRevolve2(
            True, True, False, False, False, False,
            SW_END_COND_BLIND, 0, deg_to_rad(360.0), 0.0,
            False, False, 0.0, 0.0, 0, 0.0, 0.0, True, True, True,
        )
        if revolve is None:
            raise SolidWorksError("FeatureRevolve2 failed (None). Is the profile closed?")
        return self._finish_feature(revolve, name, **sketch)

    def add_revolved_profile(self, profile_mm: list, angle_deg: float = 360.0,
                             name: str = "Revolve", corner_radii_mm=None, axis_mm=None) -> dict:
        """Revolve a closed profile on the Front plane about an axis in it.

        Without axis_mm: profile_mm = [[r, z], ...] in mm, r the distance from
        the revolve axis (the Y axis), z the position along it. With axis_mm =
        [[x1, y1], [x2, y2]]: profile_mm gives [x, y] points on the Front plane
        and the axis is the line through the two points, at any place and angle
        (a hub turned about its own centre); its ends are dimensions too. The
        polygon is auto-closed and spun `angle_deg` (default 360). Points on the
        axis give a solid like add_cone; a profile away from it gives a ring.
        The profile may not cross the axis. corner_radii_mm rounds corners as for
        add_extruded_profile; corners on the axis cannot be rounded. Returns mass
        properties.
        """
        pts = self._clean_polygon(profile_mm)
        if axis_mm is None:
            if any(r < -1e-9 for r, _ in pts):
                raise SolidWorksError("radius (first coordinate) cannot be negative -- the profile may not cross "
                                      "the axis.")
            z_vals = [z for _, z in pts]
            if max(z_vals) - min(z_vals) < 1e-9:
                raise SolidWorksError("The profile is flat along the axis (one z for every point): give it height.")
            start, end = (0.0, min(z_vals)), (0.0, max(z_vals))
        else:
            start, end = self._revolve_axis(axis_mm)
        offsets = [self._offset_from_line(start, end, p) for p in pts]
        if min(offsets) < -1e-9 and max(offsets) > 1e-9:
            raise SolidWorksError("The profile crosses the axis: put it on one side of the axis line.")
        if all(abs(d) < 1e-9 for d in offsets):
            raise SolidWorksError("The profile lies entirely on the axis.")
        on_axis_points = [abs(d) < 1e-9 for d in offsets]
        lone = [i for i, on in enumerate(on_axis_points)
                if on and not (on_axis_points[i - 1] or on_axis_points[(i + 1) % len(pts)])]
        if lone:
            # the solid would pinch to a point there, which SolidWorks refuses (verified)
            raise SolidWorksError(f"Point(s) {lone} touch the axis on their own: a profile may not meet its axis "
                                  "in a single point. Move it off the axis, or lay a whole edge on it.")
        if not 0.0 < angle_deg <= 360.0:
            raise SolidWorksError(f"angle must be in (0, 360] (got {angle_deg}).")
        corners = self._profile_corners(pts, corner_radii_mm)
        on_axis = sorted(i for _, group in corners[1] for i in group if abs(offsets[i]) < 1e-9)
        if on_axis:
            raise SolidWorksError(
                f"Corner(s) {on_axis} lie on the axis and cannot be rounded; round the outer edges instead."
            )
        model = self._require_model()

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("No reference plane found in the feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Could not select the reference plane.")

        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            lines = self._draw_polyline(sk, [(mm_to_m(x), mm_to_m(y)) for x, y in pts])
            axis = self._draw_centerline(sk, *(mm_to_m(c) for c in start), *(mm_to_m(c) for c in end))
            sketch = self._round_corners(sk, lines, corners[1], self._define_sketch(sk, lines + [axis]))
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        revolve = feat_mgr.FeatureRevolve2(
            True, True, False, False, False, False,
            SW_END_COND_BLIND, 0, deg_to_rad(angle_deg), 0.0,
            False, False, 0.0, 0.0, 0, 0.0, 0.0, True, True, True,
        )
        if revolve is None:
            raise SolidWorksError("FeatureRevolve2 failed (None). Is the profile closed and valid?")
        return self._finish_feature(revolve, name, **sketch)

    @staticmethod
    def _revolve_axis(axis_mm) -> tuple:
        """[[x1, y1], [x2, y2]] -> two distinct (x, y) points (mm); pure, unit-tested."""
        try:
            (x1, y1), (x2, y2) = axis_mm
            start, end = (float(x1), float(y1)), (float(x2), float(y2))
        except (TypeError, ValueError):
            raise SolidWorksError(f"axis_mm needs two points [[x1, y1], [x2, y2]] (got {axis_mm}).") from None
        if math.dist(start, end) < 1e-6:
            raise SolidWorksError("The two axis points coincide: give two points on the axis.")
        return start, end

    @staticmethod
    def _offset_from_line(start, end, point) -> float:
        """Signed distance (mm) of `point` from the line start-end: which side, and how far."""
        dx, dy = end[0] - start[0], end[1] - start[1]
        return (dx * (point[1] - start[1]) - dy * (point[0] - start[0])) / math.hypot(dx, dy)

    def _draw_path_on_front(self, model, path_mm, bend_radius_mm) -> tuple:
        """Draw a rounded polyline path on the Front plane.

        Returns (the new sketch's name, its _fix_sketch result). Shared by the
        sweep builders. Pins the sketch to the Front plane (every builder selects
        its plane explicitly), rounds interior corners with bend_radius_mm (see
        _round_polyline), and identifies the just-drawn sketch via a
        ProfileFeature before/after diff. The path is fixed rather than
        dimensioned: it is given as points, and its bends follow from them.
        Drawn with AddToDB, so no automatic relation makes the Fix redundant.
        """
        segs = self._round_polyline(path_mm, bend_radius_mm)
        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("No reference plane found in the feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Could not select the reference plane.")

        before = self._profile_feature_names()
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            drawn = []
            sk.AddToDB = True
            try:
                for s in segs:
                    if s[0] == "line":
                        (x1, y1), (x2, y2) = s[1], s[2]
                        seg = sk.CreateLine(mm_to_m(x1), mm_to_m(y1), 0.0, mm_to_m(x2), mm_to_m(y2), 0.0)
                        if not seg:
                            raise SolidWorksError("Could not create a path line.")
                    else:
                        _, c, p1, p2, direction = s
                        seg = sk.CreateArc(mm_to_m(c[0]), mm_to_m(c[1]), 0.0,
                                           mm_to_m(p1[0]), mm_to_m(p1[1]), 0.0,
                                           mm_to_m(p2[0]), mm_to_m(p2[1]), 0.0, direction)
                        if seg is None:
                            raise SolidWorksError("Could not create a path arc.")
                    drawn.append(seg)
            finally:
                sk.AddToDB = False
            fixed = self._fix_sketch(sk, drawn)
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)  # close the path sketch

        new_names = self._profile_feature_names() - before
        if len(new_names) != 1:
            raise SolidWorksError(
                f"Could not identify the path just drawn (expected 1 new sketch, found {len(new_names)})."
            )
        return new_names.pop(), fixed

    def add_swept_pipe(self, path_mm: list, diameter_mm: float,
                       bend_radius_mm: float = 0.0, name: str = "Pipe", smooth: bool = False) -> dict:
        """Sweep a circular profile (pipe/tube/rod) along a 2D path on the Front plane.

        path_mm = [[x, y], ...] in mm: the pipe centreline. Interior corners are
        rounded with bend_radius_mm (required when the path has corners; a 2-point
        straight path needs none); smooth=True runs a spline through the points
        instead, a flowing curve without straight stretches. diameter_mm is the
        outer diameter; the round profile is generated perpendicular to the path
        automatically. Returns mass properties (volume = pi*(d/2)^2 * path_length)
        and path_length_mm. Use new_part first.
        """
        model = self._require_model()
        if diameter_mm <= 0:
            raise SolidWorksError(f"diameter must be > 0 (got {diameter_mm}).")

        if smooth:
            path_name, path = self._draw_spline_path_on_front(model, path_mm)
        else:
            path_name, path = self._draw_path_on_front(model, path_mm, bend_radius_mm)
        model.ClearSelection2(True)
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        if not ext.SelectByID2(path_name, "SKETCH", 0.0, 0.0, 0.0, False, 4, None, 0):  # mark 4 = sweep path
            raise SolidWorksError(f"Could not select the path '{path_name}'.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        pipe = feat_mgr.InsertProtrusionSwept4(
            False,     # Propagate
            False,     # Alignment
            0,         # TwistCtrlOption
            False,     # KeepTangency
            False,     # BAdvancedSmoothing
            0, 0,      # Start/EndMatchingType
            False,     # IsThinBody
            0.0, 0.0, 0,  # Thickness1, Thickness2, ThinType
            0,         # PathAlign
            True,      # Merge
            True,      # UseFeatScope
            True,      # UseAutoSelect
            0.0,       # TwistAngle
            True,      # BMergeSmoothFaces
            True,      # CircularProfile (auto round profile, perpendicular to path)
            mm_to_m(diameter_mm),  # CircularProfileDiameter
            0,         # Direction
        )
        if pipe is None:
            raise SolidWorksError(
                "InsertProtrusionSwept4 failed (None). Is the path valid "
                "(no overlapping bends, radius fits)?"
            )
        return self._finish_feature(pipe, name, **path)

    def _draw_spline_path_on_front(self, model, path_mm) -> tuple:
        """Draw an open spline through path_mm on the Front plane, fixed (its
        points are the design). Returns (the sketch's name, its fix result with
        the path's length)."""
        pts = [(float(p[0]), float(p[1])) for p in path_mm]
        if len(pts) < 2 or len({p for p in pts}) != len(pts):
            raise SolidWorksError(f"A smooth path needs 2 or more distinct points (got {path_mm}).")
        plane = self._first_ref_plane()
        if plane is None or not plane.Select2(False, 0):
            raise SolidWorksError("Could not select the Front plane for the path.")
        before = self._profile_feature_names()
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            coords = [mm_to_m(c) for x, y in pts for c in (x, y, 0.0)]
            sk.AddToDB = True
            try:
                spline = sk.CreateSpline2(win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, coords), False)
            finally:
                sk.AddToDB = False
            if not spline:
                raise SolidWorksError("Spline path failed: CreateSpline2 returned nothing.")
            length = m_to_mm(binding.wrap(spline, self._mod.ISketchSegment).GetLength())
            fixed = self._fix_sketch(sk, [spline])
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)
        new_names = self._profile_feature_names() - before
        if len(new_names) != 1:
            raise SolidWorksError(f"Could not identify the path just drawn (found {len(new_names)} new sketches).")
        return new_names.pop(), {**fixed, "path_length_mm": round(length, 4)}

    @staticmethod
    def _require_path_starts_along_x(path_mm) -> None:
        """Fail-fast: a swept-profile path must start at the origin heading +X.

        The cross-section sits on the Right plane (normal +X), so the path's start
        tangent must be +X for the profile to be perpendicular to it. Pure helper.
        """
        raw = [(float(x), float(y)) for x, y in path_mm]
        if len(raw) < 2:
            raise SolidWorksError("path needs at least 2 points.")
        p0 = raw[0]
        p1 = next((p for p in raw[1:] if abs(p[0] - p0[0]) > 1e-9 or abs(p[1] - p0[1]) > 1e-9), None)
        if p1 is None:
            raise SolidWorksError("path needs at least 2 distinct points.")
        if math.hypot(p0[0], p0[1]) > 1e-6:
            raise SolidWorksError("path must start at the origin (0,0).")
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        if dx / math.hypot(dx, dy) < 1.0 - 1e-6:  # start tangent not +X
            raise SolidWorksError("path must start along +X (first segment pointing +X).")

    def add_swept_profile(self, profile_mm: list, path_mm: list,
                          bend_radius_mm: float = 0.0, name: str = "Sweep",
                          corner_radii_mm=None) -> dict:
        """Sweep an arbitrary closed PROFILE (cross-section) along a 2D PATH.

        profile_mm = [[u, v], ...] in mm: the closed cross-section, drawn on the
        Right plane (u along world +Y, v along world +Z), centred near the origin.
        path_mm = [[x, y], ...] in mm on the Front plane; it MUST start at the
        origin heading +X (so the profile is perpendicular to the path there).
        Interior path corners are rounded with bend_radius_mm; corner_radii_mm
        rounds the profile's corners as for add_extruded_profile. Volume =
        profile_area * path_length (Pappus). For non-round extrusions along a path
        (rails, gaskets, trim, channels). Returns mass properties. Use new_part first.
        """
        model = self._require_model()
        prof = self._clean_polygon(profile_mm)  # >= 3 distinct points
        corners = self._profile_corners(prof, corner_radii_mm)
        self._require_path_starts_along_x(path_mm)

        planes = self._ref_planes()
        if len(planes) < 3:
            raise SolidWorksError("No Right plane found (expected Front/Top/Right).")
        right = planes[2]  # tree order: Front, Top, Right

        # profile (cross-section) on the Right plane, perpendicular to the +X start
        if not right.Select2(False, 0):
            raise SolidWorksError("Could not select the Right plane.")
        before = self._profile_feature_names()
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            profile = self._draw_defined_polygon(sk, [(mm_to_m(u), mm_to_m(v)) for u, v in prof], corners)
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)
        profile_names = self._profile_feature_names() - before
        if len(profile_names) != 1:
            raise SolidWorksError("Could not identify the profile after drawing it.")
        profile_name = profile_names.pop()

        # path on the Front plane (reuses the rounded-polyline path builder)
        path_name, path = self._draw_path_on_front(model, path_mm, bend_radius_mm)

        model.ClearSelection2(True)
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        if not ext.SelectByID2(profile_name, "SKETCH", 0.0, 0.0, 0.0, False, 1, None, 0):  # mark 1 = profile
            raise SolidWorksError(f"Could not select the profile '{profile_name}'.")
        if not ext.SelectByID2(path_name, "SKETCH", 0.0, 0.0, 0.0, True, 4, None, 0):  # mark 4 = path
            raise SolidWorksError(f"Could not select the path '{path_name}'.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        sweep = feat_mgr.InsertProtrusionSwept4(
            False,     # Propagate
            False,     # Alignment
            0,         # TwistCtrlOption
            False,     # KeepTangency
            False,     # BAdvancedSmoothing
            0, 0,      # Start/EndMatchingType
            False,     # IsThinBody
            0.0, 0.0, 0,  # Thickness1, Thickness2, ThinType
            0,         # PathAlign
            True,      # Merge
            True,      # UseFeatScope
            True,      # UseAutoSelect
            0.0,       # TwistAngle
            True,      # BMergeSmoothFaces
            False,     # CircularProfile (use the selected profile sketch)
            0.0,       # CircularProfileDiameter
            0,         # Direction
        )
        if sweep is None:
            raise SolidWorksError(
                "InsertProtrusionSwept4 failed (None). Is the profile on the Right plane "
                "and does the path start at the origin along +X?"
            )
        return self._finish_feature(sweep, name, dimensions=profile["dimensions"],
                                    fully_defined=profile["fully_defined"] and path["fully_defined"])

    def add_lofted_solid(self, profiles_mm: list, heights_mm: list, name: str = "Loft") -> dict:
        """Loft (blend) 2+ closed profiles on parallel planes stacked along +Z.

        profiles_mm: a list of profiles, each a list of [x, y] vertices (mm) in the
        Front-plane coordinate system (same convention as add_extruded_profile),
        or a round section {"center_mm": [x, y], "diameter_mm": d}: thick at the
        knee, thin towards the foot, the centres free to wander. heights_mm: the
        +Z offset (mm) of each profile's plane; same length as profiles_mm,
        strictly increasing, starting at 0. A 2-profile loft is a ruled
        transition; 3+ profiles blend smoothly through the intermediate ones.
        Each profile starts at its first vertex (a round one on +x), so give
        polygons in a consistent vertex order/orientation to avoid a twisted
        blend. Returns mass properties. Use new_part first.
        """
        model = self._require_model()
        if len(profiles_mm) != len(heights_mm):
            raise SolidWorksError("profiles_mm and heights_mm must have the same length.")
        if len(profiles_mm) < 2:
            raise SolidWorksError("A loft needs at least 2 profiles.")
        if heights_mm[0] != 0:
            raise SolidWorksError("heights_mm[0] must be 0 (the first profile sits on the Front plane).")
        for lo, hi in zip(heights_mm, heights_mm[1:]):
            if hi <= lo:
                raise SolidWorksError("heights_mm must be strictly increasing.")
        cleaned = [self._loft_section(p) for p in profiles_mm]  # a circle, or >= 3 distinct points

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)

        sketch_names = []
        helper_planes = []
        dimensions, fully_defined = {}, True
        for k, (poly, height) in enumerate(zip(cleaned, heights_mm)):
            plane, created = self._plane_at("front", height)
            if created:
                helper_planes.append(plane)
                dimensions[f"profile{k}_height"] = self._first_dimension_name(plane)
            model.ClearSelection2(True)
            if not plane.Select2(False, 0):
                raise SolidWorksError(f"Could not select the plane at z={height}.")
            before = self._profile_feature_names()
            sk.InsertSketch(True)
            try:
                if isinstance(poly, dict):
                    (cx, cy), diameter = poly["center_mm"], poly["diameter_mm"]
                    circle = self._draw_circle(sk, mm_to_m(cx), mm_to_m(cy), mm_to_m(diameter / 2))
                    defined = self._define_sketch(sk, circles=[circle], names={
                        ("x", 0): "x", ("y", 0): "y", ("diameter", 0): "diameter"})
                else:
                    defined = self._define_sketch(sk, self._draw_polyline(sk, [(mm_to_m(x), mm_to_m(y)) for x, y in poly]))
            finally:
                model.ClearSelection2(True)
                sk.InsertSketch(True)
            dimensions.update({f"profile{k}_{role}": dim for role, dim in defined["dimensions"].items()})
            fully_defined = fully_defined and defined["fully_defined"]
            new_names = self._profile_feature_names() - before
            if len(new_names) != 1:
                raise SolidWorksError(f"Could not identify the profile at z={height}.")
            sketch_names.append(new_names.pop())

        model.ClearSelection2(True)
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        for sketch_name, poly, height in zip(sketch_names, cleaned, heights_mm):
            # The pick point is the profile's start point: each on +x of its circle
            # (or each first vertex) keeps the loft from twisting (verified)
            x, y = self._loft_start(poly)
            if not ext.SelectByID2(sketch_name, "SKETCH", mm_to_m(x), mm_to_m(y), mm_to_m(height),
                                   True, 1, None, 0):  # mark 1, append
                raise SolidWorksError(f"Could not select profile '{sketch_name}'.")

        loft = feat_mgr.InsertProtrusionBlend(
            False,        # Closed
            False,        # KeepTangency
            False,        # ForceNonRational
            1.0,          # TessToleranceFactor
            0, 0,         # Start/EndMatchingType
            0.0, 0.0,     # Start/EndTangentLength
            False, False, # Start/EndTangentDir
            False,        # IsThinBody
            0.0, 0.0, 0,  # Thickness1, Thickness2, ThinType
            True,         # Merge
            True,         # UseFeatScope
            True,         # UseAutoSelect
        )
        if loft is None:
            raise SolidWorksError(
                "InsertProtrusionBlend failed (None). Are the profiles stacked validly?"
            )
        # The offset planes are construction geometry; hide them so they don't
        # clutter screenshots.
        for plane in helper_planes:
            if plane.Select2(False, 0):
                model.BlankRefGeom()
        model.ClearSelection2(True)
        return self._finish_feature(loft, name, dimensions=dimensions, fully_defined=fully_defined)

    @staticmethod
    def _rib_material_reversed(start, end, toward) -> bool:
        """Whether InsertRib must reverse its material side to grow toward `toward`.

        SolidWorks grows a rib to the RIGHT of start->end (viewed from +Z), so a
        toward point on the left needs ReverseMaterialDir. Pure, unit-tested.
        """
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            raise SolidWorksError("The rib line has zero length: start and end coincide.")
        side = dx * (toward[1] - start[1]) - dy * (toward[0] - start[0])  # > 0: left
        if abs(side) / length < 1e-6:
            raise SolidWorksError(
                "toward_mm lies on the rib line; pick a point on the side to fill."
            )
        return side > 0

    def add_rib(self, start_mm: list, end_mm: list, toward_mm: list, thickness_mm: float,
                z_mm: float, name: str = "Rib") -> dict:
        """Add a straight rib (gusset) in a plane parallel to the Front plane at z_mm.

        The rib's free edge runs start_mm -> end_mm ([x, y], the same Front-plane
        coordinates as add_extruded_profile). SolidWorks grows it toward toward_mm
        (any point on the side to fill, e.g. an L-bracket's inner corner) until it
        meets the part, thickness_mm thick and centred on the plane. Returns mass
        properties: a triangular gusset with legs a and b adds a*b/2 * thickness.
        """
        model = self._require_model()
        if thickness_mm <= 0:
            raise SolidWorksError(f"thickness_mm must be > 0 (got {thickness_mm}).")
        if z_mm < 0:
            raise SolidWorksError(f"z_mm must be >= 0 (got {z_mm}); planes are offset from the Front plane toward +Z.")
        reverse = self._rib_material_reversed(start_mm, end_mm, toward_mm)

        plane, created = self._plane_at("front", z_mm)
        helper_plane = plane if created else None
        model.ClearSelection2(True)
        if not plane.Select2(False, 0):
            raise SolidWorksError(f"Could not select the plane at z={z_mm}.")
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        try:
            sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
            sk.InsertSketch(True)
            try:
                line = self._draw_polyline(sk, [(mm_to_m(start_mm[0]), mm_to_m(start_mm[1])),
                                                (mm_to_m(end_mm[0]), mm_to_m(end_mm[1]))], closed=False)
                sketch = self._define_sketch(sk, line)
            finally:
                model.ClearSelection2(True)
                sk.InsertSketch(True)  # close the sketch; it stays selected for the rib
            before = {f.Name for f in self._iter_features()}
            # InsertRib returns nothing, so the new feature is taken from the tree.
            feat_mgr.InsertRib(True, False, mm_to_m(thickness_mm), 0, reverse,
                               False, False, 0.0, False, False)
            rib = next((f for f in self._iter_features()
                        if f.Name not in before and f.GetTypeName2() == "Rib"), None)
        finally:
            if helper_plane is not None and helper_plane.Select2(False, 0):
                model.BlankRefGeom()  # construction geometry; keep screenshots clean
            model.ClearSelection2(True)
        if rib is None:
            raise SolidWorksError(
                "Rib not created: on the toward_mm side the rib meets no material. "
                "Pick toward_mm on the side where the part is (e.g. the inner corner)."
            )
        return self._finish_feature(rib, name, **sketch)

    def _cut_circle_on_z(self, model, diameter_mm: float, x_mm: float, y_mm: float,
                         through: bool, depth_mm: float = 0.0):
        """Cut one circle on the +Z face -- through-all or blind to depth_mm.

        Returns (the sketch's _define_sketch result, the raw FeatureCut4 feature
        or None on failure) so callers attach their own error message. Shared
        by add_hole and add_counterbore_hole; sketching on the selected +Z face is
        what makes the cut direction unambiguous. (x_mm, y_mm) are in add_box
        coordinates.
        """
        body = self._solid_body()
        self._select_planar_face(body, (0.0, 0.0, 1.0), "+Z")
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)  # the sketch is created on the selected face
        try:
            circle = self._draw_circle(sk, mm_to_m(x_mm), mm_to_m(y_mm), mm_to_m(diameter_mm / 2.0))
            sketch = self._define_sketch(sk, circles=[circle], names={("x", 0): "x", ("y", 0): "y"})
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)  # close the sketch

        t1 = SW_END_COND_THROUGH_ALL if through else SW_END_COND_BLIND
        d1 = 0.0 if through else mm_to_m(depth_mm)
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        return sketch, feat_mgr.FeatureCut4(
            True, False, False,                # Sd, Flip, Dir
            t1, 0,                             # T1 (end condition), T2
            d1, 0.0,                           # D1 (depth, 0 for through-all), D2
            False, False,                      # Dchk1, Dchk2
            False, False,                      # Ddir1, Ddir2
            0.0, 0.0,                          # Dang1, Dang2
            False, False,                      # OffsetReverse1, OffsetReverse2
            False, False,                      # TranslateSurface1, TranslateSurface2
            False,                             # NormalCut
            True,                              # UseFeatScope
            True,                              # UseAutoSelect
            False,                             # AssemblyFeatureScope
            False,                             # AutoSelectComponents
            False,                             # PropagateFeatureToParts
            SW_START_SKETCH_PLANE,             # T0 (start condition)
            0.0,                               # StartOffset
            False,                             # FlipStartOffset
            False,                             # OptimizeGeometry
        )

    def add_hole(self, diameter_mm: float, x_mm: float, y_mm: float,
                 name: str = "Hole") -> dict:
        """Cut a circular through-hole at (x, y), straight through the depth axis.

        Selects the +Z face (the face parallel to add_box's width x height
        profile) and cuts through all material to the opposite face -- i.e. a hole
        through a plate's thickness, along the extrude direction. (x_mm, y_mm) are
        in add_box's coordinate system, so the centre of a 40x20 profile is x=20,
        y=10. Returns the resulting mass properties.
        """
        model = self._require_model()
        if diameter_mm <= 0:
            raise SolidWorksError(f"diameter must be > 0 (got {diameter_mm}).")
        sketch, cut = self._cut_circle_on_z(model, diameter_mm, x_mm, y_mm, through=True)
        if cut is None:
            raise SolidWorksError(
                "FeatureCut4 failed (None). Is (x, y) inside the part's material?"
            )
        return self._finish_feature(cut, name, **sketch)

    def add_counterbore_hole(self, clearance_diameter_mm: float, cbore_diameter_mm: float,
                             cbore_depth_mm: float, x_mm: float, y_mm: float,
                             name: str = "Counterbore") -> dict:
        """Cut a counterbored screw hole on the +Z face at (x, y).

        A clearance shank cut THROUGH_ALL, plus a larger coaxial flat-bottom pocket
        cut BLIND to cbore_depth_mm from +Z -- so a cap-head screw (or heat-set
        insert) sits flush/recessed. Volume removed =
        pi*r_clear^2*thickness + pi*(R_cbore^2 - r_clear^2)*cbore_depth.
        Returns mass properties. Use after building a plate (e.g. add_box).
        """
        model = self._require_model()
        if clearance_diameter_mm <= 0 or cbore_diameter_mm <= 0:
            raise SolidWorksError("diameters must be > 0.")
        if cbore_diameter_mm <= clearance_diameter_mm:
            raise SolidWorksError("cbore_diameter must be larger than clearance_diameter.")
        if cbore_depth_mm <= 0:
            raise SolidWorksError(f"cbore_depth must be > 0 (got {cbore_depth_mm}).")

        # Through clearance shank first (clean +Z face), then the blind pocket: the
        # pocket removes the annular ring around the already-cut shank.
        shank_sketch, shank = self._cut_circle_on_z(model, clearance_diameter_mm, x_mm, y_mm, through=True)
        if shank is None:
            raise SolidWorksError(
                "Clearance hole (FeatureCut4) failed (None). Is (x, y) inside the material?"
            )
        cbore_sketch, cbore = self._cut_circle_on_z(model, cbore_diameter_mm, x_mm, y_mm,
                                                    through=False, depth_mm=cbore_depth_mm)
        if cbore is None:
            raise SolidWorksError("Counterbore pocket (FeatureCut4) failed (None).")
        # two sketches, one per cut: the pocket's position is its own, set it along
        # (a centre on an origin axis has a relation there, not a dimension)
        dimensions = {("clearance_diameter" if role == "diameter" else role): dim
                      for role, dim in shank_sketch["dimensions"].items()}
        dimensions.update({f"cbore_{role}": dim for role, dim in cbore_sketch["dimensions"].items()})
        result = self._finish_feature(cbore, name, dimensions=dimensions,
                                      fully_defined=shank_sketch["fully_defined"] and cbore_sketch["fully_defined"])
        dimensions["cbore_depth"] = f"D1@{result['feature']}"
        return result

    # A point given to add_hole_on_face / cut_profile_on_face must LIE on the
    # chosen face. ModelToSketchTransform's out-of-plane component (local[2]) is
    # exactly 0 for an on-face point (verified) and equals the off-face distance
    # otherwise; without this guard the 2D projection silently relocates the
    # feature onto the face. 1 um catches any real mistake by orders of magnitude
    # while absorbing transform round-off.
    _ON_FACE_TOLERANCE_MM = 1e-3

    # where a ray that picks a face at a point starts, above it along the normal
    _PICK_ABOVE_MM = 0.01

    _EDGE_POINT_TOLERANCE_MM = 0.01
    _THREAD_DIAMETER_TOLERANCE_MM = 0.01

    @staticmethod
    def _parse_thread_size(size) -> tuple:
        """(diameter, pitch) in mm from an ISO metric size like 'M10x1.5'; pure."""
        match = re.fullmatch(r"M(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", str(size))
        if not match:
            raise SolidWorksError(
                f"Unknown thread size '{size}'. Use ISO metric notation like 'M10x1.5' or 'M3x0.5'."
            )
        return float(match.group(1)), float(match.group(2))

    @staticmethod
    def _thread_minor_diameter(diameter_mm, pitch_mm) -> float:
        """ISO basic minor diameter D1 = D - 2 * 5*sqrt(3)/16 * P (ISO 724); pure."""
        return diameter_mm - 5 * math.sqrt(3) / 8 * pitch_mm

    def _circular_edges_at(self, x_mm, y_mm, z_mm) -> list:
        """(raw edge, radius in mm) of every circular edge centred on (x, y, z) mm."""
        edges = self._solid_body().GetEdges() or ()
        if not isinstance(edges, (list, tuple)):
            edges = [edges]
        found = []
        for edge_dispatch in edges:
            curve = binding.wrap(binding.wrap(edge_dispatch, self._mod.IEdge).GetCurve(), self._mod.ICurve)
            if curve is None or not curve.IsCircle():
                continue
            params = curve.CircleParams  # centre xyz, axis xyz, radius (m)
            centre = [m_to_mm(v) for v in params[0:3]]
            if math.dist(centre, (x_mm, y_mm, z_mm)) < self._EDGE_POINT_TOLERANCE_MM:
                found.append((edge_dispatch, m_to_mm(params[6])))
        return found

    def add_thread(self, size: str, x_mm: float, y_mm: float, z_mm: float,
                   length_mm: float, internal: bool = False, name: str = "Thread") -> dict:
        """Cut a real (printable) ISO metric thread with SolidWorks' Thread feature.

        size: ISO metric as SolidWorks names it, e.g. 'M10x1.5', 'M3x0.5', 'M10x1.0'.
        (x_mm, y_mm, z_mm): centre of the circular edge where the thread starts --
        the end face of a rod (external) or the mouth of a hole (internal); the
        thread runs length_mm into the material, right-handed.
        External: the rod must have the nominal diameter (M10 -> Ø10).
        Internal: the hole must have the ISO basic minor diameter D - 1.0825*P
        (M10x1.5 -> Ø8.376). SolidWorks shifts a tapped thread with the hole, so
        any other hole would give a wrong thread and is refused.
        """
        model = self._require_model()
        if length_mm <= 0:
            raise SolidWorksError(f"length_mm must be > 0 (got {length_mm}).")
        diameter, pitch = self._parse_thread_size(size)
        needed = self._thread_minor_diameter(diameter, pitch) if internal else diameter
        what = "hole" if internal else "rod"

        edges = self._circular_edges_at(x_mm, y_mm, z_mm)
        if not edges:
            raise SolidWorksError(
                f"No circular edge centred on ({x_mm:g}, {y_mm:g}, {z_mm:g}) mm. Give the centre "
                f"of the {what}'s edge on the face where the thread starts."
            )
        edge, radius = min(edges, key=lambda e: abs(2 * e[1] - needed))
        if abs(2 * radius - needed) > self._THREAD_DIAMETER_TOLERANCE_MM:
            hint = " Drill that ISO basic minor diameter first." if internal else ""
            raise SolidWorksError(
                f"An {'internal' if internal else 'external'} {size} thread needs a Ø{needed:.3f} "
                f"{what}, but the edge at ({x_mm:g}, {y_mm:g}, {z_mm:g}) is Ø{2 * radius:.3f}.{hint}"
            )

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        data = binding.wrap(feat_mgr.CreateDefinition(SW_FM_SWEEP_THREAD), self._mod.IThreadFeatureData)
        if data is None:
            raise SolidWorksError("CreateDefinition gave no thread definition (needs SOLIDWORKS 2016 or later).")
        data.InitializeThreadData()
        profile = THREAD_PROFILE_INTERNAL if internal else THREAD_PROFILE_EXTERNAL
        data.Type = profile
        library = data.Type  # SolidWorks resolves the name to its .sldlfp file, or '' if missing
        if not library:
            raise SolidWorksError(
                f"Thread profile library '{profile}' not found "
                "(Tools > Options > File Locations > Thread Profiles)."
            )
        # SolidWorks accepts ANY size string and silently cuts garbage for an unknown
        # one, so check it against the library's configurations (one per size).
        sizes = list(self._sw.GetConfigurationNames(library) or ())
        if size not in sizes:
            same = [s for s in sizes if s.startswith(f"M{diameter:g}x")]
            raise SolidWorksError(
                f"'{size}' is not a {profile} size. "
                + (f"Valid M{diameter:g} sizes: {same}." if same else f"Valid sizes: {sizes}.")
            )
        data.Edge = edge
        data.ThreadMethod = SW_THREAD_METHOD_CUT
        data.Size = size
        data.EndCondition = SW_THREAD_END_BLIND
        data.BlindDepth = mm_to_m(length_mm)
        data.RightHanded = True
        thread = binding.wrap(feat_mgr.CreateFeature(data), self._mod.IFeature)
        if thread is None:
            raise SolidWorksError(
                f"The thread feature failed (CreateFeature returned None). Is there "
                f"{length_mm:g} mm of material behind the edge?"
            )
        return self._finish_feature(thread, name, thread={
            "size": size,
            "pitch_mm": pitch,
            "major_diameter_mm": diameter,
            "minor_diameter_mm": round(self._thread_minor_diameter(diameter, pitch), 4),
            "length_mm": length_mm,
            "internal": internal,
            "right_handed": True,
        })

    # --- Hole Wizard: SolidWorks' own ISO tables -----------------------------------

    # kind -> (swWzdGeneralHoleTypes_e, fastener type): ISO 273 clearances,
    # counterbores for ISO 4762 socket head cap screws, countersinks for ISO 10642
    # socket countersunk screws, ISO tapped holes.
    _WIZARD_KINDS = {
        "clearance": (SW_WZD_HOLE, SW_ISO_SCREW_CLEARANCES),
        "counterbore": (SW_WZD_COUNTERBORE, SW_ISO_SOCKET_HEAD_CAP),
        "countersink": (SW_WZD_COUNTERSINK, SW_ISO_SOCKET_COUNTERSUNK),
        "tapped": (SW_WZD_TAP, SW_ISO_TAPPED_HOLE),
    }
    _WIZARD_THREADS = ("cosmetic", "modeled")

    @staticmethod
    def _wizard_values(kind: str, fit: int, through: bool) -> list:
        """HoleWizard5's Value1..Value12 for `kind`; -1 means 'from the standard'.

        Found by trial on SOLIDWORKS 2026, each vector verified by a volume test:
        a plain hole cut through all fails unless its unused values are 0, while a
        tapped hole given zeros cuts garbage. A tapped hole keeps its cosmetic
        thread: without one SolidWorks mills the threaded length at the major
        diameter.
        """
        if kind == "tapped":
            thread_end = 1.0 if through else 0.0  # swWzdHoleThreadEndCondition_e
            return [-1.0] * 6 + [float(SW_COSMETIC_THREAD_WITH_CALLOUT), thread_end] + [-1.0] * 4
        if kind == "clearance":
            return [float(fit)] + [0.0 if through else -1.0] * 11
        return [-1.0, -1.0, -1.0, float(fit)] + [-1.0] * 8  # counterbore/countersink: fit is Value4

    def _coarse_thread_size(self, size: str) -> str:
        """'M3' -> 'M3x0.5': the coarse (largest) pitch the Metric Tap library has."""
        if not re.fullmatch(r"M\d+(?:\.\d+)?", str(size)):
            raise SolidWorksError(f"A modeled thread needs an ISO metric size like 'M3' (got '{size}').")
        feat_mgr = binding.wrap(self._model.FeatureManager, self._mod.IFeatureManager)
        data = binding.wrap(feat_mgr.CreateDefinition(SW_FM_SWEEP_THREAD), self._mod.IThreadFeatureData)
        if data is None:
            raise SolidWorksError("CreateDefinition gave no thread definition (needs SOLIDWORKS 2016 or later).")
        data.InitializeThreadData()
        data.Type = THREAD_PROFILE_INTERNAL
        sizes = [s for s in (self._sw.GetConfigurationNames(data.Type) or ()) if s.startswith(f"{size}x")]
        if not sizes:
            raise SolidWorksError(f"The '{THREAD_PROFILE_INTERNAL}' thread library has no {size} size.")
        return max(sizes, key=lambda s: self._parse_thread_size(s)[1])

    def _through_length(self, point_mm, normal, radius_mm: float) -> float:
        """How far a hole of radius_mm entering at point_mm runs along -normal, to its far edge."""
        edges = self._solid_body().GetEdges() or ()
        if not isinstance(edges, (list, tuple)):
            edges = [edges]
        length = 0.0
        for edge_dispatch in edges:
            curve = binding.wrap(binding.wrap(edge_dispatch, self._mod.IEdge).GetCurve(), self._mod.ICurve)
            if curve is None or not curve.IsCircle():
                continue
            params = curve.CircleParams  # centre xyz, axis xyz, radius (m)
            if abs(m_to_mm(params[6]) - radius_mm) > self._THREAD_DIAMETER_TOLERANCE_MM:
                continue
            offset = [m_to_mm(c) - p for c, p in zip(params[0:3], point_mm)]
            depth = -sum(o * n for o, n in zip(offset, normal))
            off_axis = math.dist(offset, [-depth * n for n in normal])
            if off_axis < self._EDGE_POINT_TOLERANCE_MM:
                length = max(length, depth)
        if length <= 0:
            raise SolidWorksError("Could not find where the through hole leaves the part.")
        return length

    def _pin_wizard_position(self, feat, point_mm) -> tuple:
        """Fully define a wizard hole's position sketch, AT point_mm.

        The Hole Wizard puts its hole where the face was picked, which lands some
        0.04 mm off, in an under-defined sketch. Its point gets dimensions from the
        origin that carry the exact coordinates. Returns (dimensions, fully_defined).
        """
        model = self._model
        position = next((s for s in self._sub_features(feat) if s.GetTypeName2() == "ProfileFeature"
                         and len(binding.wrap(s.GetSpecificFeature2(), self._mod.ISketch).GetSketchPoints2() or ()) == 1
                         and not binding.wrap(s.GetSpecificFeature2(), self._mod.ISketch).GetSketchSegments()), None)
        if position is None:
            raise SolidWorksError(f"The Hole Wizard hole '{feat.Name}' has no position sketch to define.")
        model.ClearSelection2(True)
        if not position.Select2(False, 0):
            raise SolidWorksError(f"Could not select the position sketch of '{feat.Name}'.")
        model.EditSketch()
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        try:
            sketch, definer = self._open_sketch_definer(sk)
            point = binding.wrap(sketch.GetSketchPoints2()[0], self._mod.ISketchPoint)
            u, v, _ = self._sketch_coords(sketch, *(mm_to_m(c) for c in point_mm))
            u0, v0, _ = self._sketch_coords(sketch, 0.0, 0.0, 0.0)
            # x/y are the face sketch's own horizontal/vertical directions
            dims = definer.place_point(point, (m_to_mm(u - u0), m_to_mm(v - v0)))
            return dims, definer.fully_defined()
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)  # leave the position sketch

    def _wizard_hole_info(self, feat, kind: str, size: str, fit: str) -> dict:
        """The sizes SolidWorks took from its standard table for this hole (mm, degrees)."""
        data = binding.wrap(feat.GetDefinition(), self._mod.IWizardHoleFeatureData2)
        mm = lambda v: round(m_to_mm(v), 4)
        info = {"standard": "ISO", "kind": kind, "size": size}
        if kind == "tapped":
            info.update(tap_drill_diameter_mm=mm(data.TapDrillDiameter or data.ThruTapDrillDiameter),
                        thread_diameter_mm=mm(data.ThreadDiameter),
                        thread_depth_mm=mm(data.ThreadDepth), cosmetic_thread=data.CosmeticThreadType > 0)
        else:
            info.update(fit=fit, diameter_mm=mm(data.ThruHoleDiameter or data.HoleDiameter))
        if kind == "counterbore":
            info.update(cbore_diameter_mm=mm(data.CounterBoreDiameter), cbore_depth_mm=mm(data.CounterBoreDepth))
        if kind == "countersink":
            info.update(csink_diameter_mm=mm(data.CounterSinkDiameter),
                        csink_angle_deg=round(math.degrees(data.CounterSinkAngle), 3))
        return info

    def add_hole_wizard(self, kind: str, size: str, face: str, x_mm: float, y_mm: float, z_mm: float,
                        depth_mm: float | None = None, fit: str = "normal", thread: str = "cosmetic",
                        name: str | None = None) -> dict:
        """An ISO hole from SolidWorks' Hole Wizard, sized by its standard tables.

        kind: 'clearance' (ISO 273), 'counterbore' (socket head cap screw,
        ISO 4762), 'countersink' (socket countersunk screw, ISO 10642) or
        'tapped'. size: ISO metric, e.g. 'M3'. Centred at 3D point (x, y, z) on
        the face through it (as add_hole_on_face); through all, or depth_mm deep
        (a blind hole ends in a 118 degree drill point). fit: 'close', 'normal'
        or 'loose' (not for tapped). thread (tapped only): 'cosmetic' drills the
        ISO tap drill and adds SolidWorks' cosmetic thread; 'modeled' drills the
        ISO basic minor diameter and cuts a real, printable thread over the
        standard thread depth (all the way through a through hole). Returns the
        standard's sizes in `hole`, and the position as dimensions x/y.
        """
        model = self._require_model()
        if kind not in self._WIZARD_KINDS:
            raise SolidWorksError(f"Unknown hole kind '{kind}'. Use one of {sorted(self._WIZARD_KINDS)}.")
        if fit not in SCREW_FITS:
            raise SolidWorksError(f"Unknown fit '{fit}'. Use one of {sorted(SCREW_FITS)}.")
        if thread not in self._WIZARD_THREADS:
            raise SolidWorksError(f"Unknown thread '{thread}'. Use 'cosmetic' or 'modeled'.")
        if thread == "modeled" and kind != "tapped":
            raise SolidWorksError("A modeled thread needs kind='tapped'.")
        if depth_mm is not None and depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        point = (x_mm, y_mm, z_mm)
        coarse = self._coarse_thread_size(size) if thread == "modeled" else None
        diameter_m = -1.0  # from the standard
        if coarse:
            major, pitch = self._parse_thread_size(coarse)
            diameter_m = mm_to_m(self._thread_minor_diameter(major, pitch))

        normal, side = self._parse_face_selector(face, default_side=None)
        target = self._select_face_through(self._solid_body(), normal, side, point, face)
        off = self._off_face_mm(target, point)
        if off > self._ON_FACE_TOLERANCE_MM:  # the pick would land on whatever lies there, e.g. a countersink
            raise SolidWorksError(
                f"({x_mm:g}, {y_mm:g}, {z_mm:g}) mm is not on the {face} face but {off:.3g} mm off it: "
                f"beyond its edge, or over an opening such as an earlier hole. Give a point on the face."
            )
        model.ClearSelection2(True)
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        # The wizard drills where the face is picked. SelectByID2 picks what the
        # view shows first at that spot (in a side view: another face, or none),
        # so a ray from just above the point straight into the face picks it.
        start = [mm_to_m(c + n * self._PICK_ABOVE_MM) for c, n in zip(point, normal)]
        if not ext.SelectByRay(*start, *(-n for n in normal), mm_to_m(self._ON_FACE_TOLERANCE_MM),
                               SW_SEL_FACES, False, 0, 0):
            raise SolidWorksError(f"Could not pick the {face} face at ({x_mm:g}, {y_mm:g}, {z_mm:g}) mm.")
        hole_type, fastener = self._WIZARD_KINDS[kind]
        through = depth_mm is None
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        feat = feat_mgr.HoleWizard5(
            hole_type, SW_WZD_STANDARD_ISO, fastener, size,
            SW_END_COND_THROUGH_ALL if through else SW_END_COND_BLIND,
            diameter_m, -1.0 if through else mm_to_m(depth_mm), -1.0,  # Diameter, Depth, Length (slots)
            *self._wizard_values(kind, SCREW_FITS[fit], through),
            "", False, False, True, False, False, False,  # ThreadClass (inch only), RevDir, scope flags
        )
        if feat is None:
            raise SolidWorksError(
                f"The Hole Wizard made no {kind} hole of size '{size}'. Is it an ISO metric size "
                f"in SolidWorks' table (e.g. 'M3', 'M4', 'M10'), with material at the point?"
            )
        feat = binding.wrap(feat, self._mod.IFeature)
        dims, fully_defined = self._pin_wizard_position(feat, point)
        info = self._wizard_hole_info(feat, kind, size, fit)
        result = self._finish_feature(feat, name or feat.Name, dimensions=dims,
                                      fully_defined=fully_defined, hole=info)
        if coarse:
            radius = m_to_mm(diameter_m) / 2
            length = self._through_length(point, normal, radius) if through else info["thread_depth_mm"]
            threaded = self.add_thread(coarse, x_mm, y_mm, z_mm, length, internal=True,
                                       name=f"{result['feature']} Thread")
            result.update(thread=threaded["thread"], mass_properties=threaded["mass_properties"])
        return result

    def _sketch_coords(self, sketch, x_m, y_m, z_m):
        """A 3D model point (m) in the sketch's own coordinates (u, v, w), in m.

        Via ISketch.ModelToSketchTransform; w is the distance off the sketch
        plane. The point must be a proper SAFEARRAY VARIANT -- a plain Python
        list is mis-marshalled by CreatePoint.
        """
        xform = binding.wrap(sketch.ModelToSketchTransform, self._mod.IMathTransform)
        mathutil = binding.wrap(self._sw.GetMathUtility(), self._mod.IMathUtility)
        coords = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x_m, y_m, z_m])
        p = binding.wrap(mathutil.CreatePoint(coords), self._mod.IMathPoint)
        return binding.wrap(p.MultiplyTransform(xform), self._mod.IMathPoint).ArrayData

    def _model_to_sketch_uv(self, sketch, x_m, y_m, z_m, face):
        """Map a 3D model point (m) to the active sketch's local 2D (u, v) (m).

        The point must LIE on the sketch's face: local[2] is its perpendicular
        distance to the face plane, which we reject past _ON_FACE_TOLERANCE_MM so
        an off-face point fails fast instead of being silently projected onto the
        face (which would place the feature at the wrong spot). `face` names the
        face in the error.
        """
        local = self._sketch_coords(sketch, x_m, y_m, z_m)
        off_mm = m_to_mm(local[2])
        if abs(off_mm) > self._ON_FACE_TOLERANCE_MM:
            raise SolidWorksError(
                f"Point ({m_to_mm(x_m):g}, {m_to_mm(y_m):g}, {m_to_mm(z_m):g}) mm is not "
                f"on the {face} face: it is {off_mm:.3f} mm off the face. Give a "
                f"point on the face (the perpendicular distance must be ~0)."
            )
        return local[0], local[1]

    def _open_sketch_on_face(self, face: str, point_mm):
        """Select the face -- the one through point_mm unless the selector names a
        side -- and open a sketch on it. Returns (sketch manager, open sketch)."""
        body = self._solid_body()
        normal, side = self._parse_face_selector(face, default_side=None)
        self._select_face_through(body, normal, side, point_mm, face)
        sk = binding.wrap(self._model.SketchManager, self._mod.ISketchManager)
        return sk, self._open_face_sketch(sk, face)

    def _sketch_circle_on_face(self, face: str, diameter_mm: float, x_mm: float, y_mm: float, z_mm: float) -> dict:
        """A fully defined circle on the face through (x, y, z); the sketch is closed again."""
        sk, sketch = self._open_sketch_on_face(face, (x_mm, y_mm, z_mm))
        try:
            u, v = self._model_to_sketch_uv(sketch, mm_to_m(x_mm), mm_to_m(y_mm),
                                            mm_to_m(z_mm), face)
            circle = self._draw_circle(sk, u, v, mm_to_m(diameter_mm / 2.0))
            # x/y are the face sketch's own horizontal/vertical directions
            return self._define_sketch(sk, circles=[circle], names={("x", 0): "x", ("y", 0): "y"})
        finally:
            self._model.ClearSelection2(True)
            sk.InsertSketch(True)  # close the sketch, also when the point is rejected

    def _sketch_polygon_on_face(self, face: str, points_mm, corner_radii_mm=None) -> dict:
        """A fully defined polygon through 3D points on the face, its corners
        rounded by corner_radii_mm; the sketch is closed again."""
        if not points_mm:
            raise SolidWorksError("No profile points given.")
        corners = self._profile_corners(points_mm, corner_radii_mm)
        sk, sketch = self._open_sketch_on_face(face, points_mm[0])
        try:
            uv_m = [self._model_to_sketch_uv(sketch, mm_to_m(p[0]), mm_to_m(p[1]),
                                             mm_to_m(p[2]), face)
                    for p in points_mm]
            # x/y dimensions run along the face sketch's own axes
            return self._draw_defined_polygon(sk, self._clean_polygon(uv_m), corners)
        finally:
            self._model.ClearSelection2(True)
            sk.InsertSketch(True)  # close the sketch, also when a point is rejected

    def add_hole_on_face(self, diameter_mm: float, face: str, x_mm: float, y_mm: float, z_mm: float,
                         depth_mm: float | None = None, name: str = "Hole") -> dict:
        """Drill a round hole on any planar face, centred at 3D point (x, y, z).

        face is a direction '+x'/'-x'/'+y'/'-y'/'+z'/'-z'; the planar face facing
        that way THROUGH the point is used, so a pocket floor or a step works too
        (':outer'/':inner' force the outermost/innermost face). The hole runs
        through all material along the face normal, or depth_mm deep: a blind
        hole for a heat-set insert or a screw pilot. (add_hole is the +Z 2D
        convenience version of this.)
        """
        model = self._require_model()
        if diameter_mm <= 0:
            raise SolidWorksError(f"diameter must be > 0 (got {diameter_mm}).")
        if depth_mm is not None and depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        defined = self._sketch_circle_on_face(face, diameter_mm, x_mm, y_mm, z_mm)

        t1, d1 = (SW_END_COND_THROUGH_ALL, 0.0) if depth_mm is None else (SW_END_COND_BLIND, mm_to_m(depth_mm))
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        cut = feat_mgr.FeatureCut4(
            True, False, False, t1, 0, d1, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, False, True, True, False, False, False,
            SW_START_SKETCH_PLANE, 0.0, False, False,
        )
        if cut is None:
            raise SolidWorksError(
                f"FeatureCut4 failed (None). Is ({x_mm}, {y_mm}, {z_mm}) on the {face} face?"
            )
        return self._with_depth(self._finish_feature(cut, name, **defined), depth_mm)

    def add_boss_on_face(self, diameter_mm: float, face: str, x_mm: float, y_mm: float, z_mm: float,
                         height_mm: float, name: str = "Boss") -> dict:
        """Grow a round boss (standoff, peg) height_mm out of any planar face.

        Centred at 3D point (x, y, z) on the face; the face is found as for
        add_hole_on_face. Add a blind hole in its top for a heat-set insert.
        Volume added = pi * (d/2)^2 * height.
        """
        self._require_model()
        if diameter_mm <= 0 or height_mm <= 0:
            raise SolidWorksError(f"diameter and height must be > 0 (got {diameter_mm}, {height_mm}).")
        defined = self._sketch_circle_on_face(face, diameter_mm, x_mm, y_mm, z_mm)
        return self._extrude_sketch(height_mm, name, defined, "Is the circle on the face?", role="height")

    def add_extruded_profile_on_face(self, points_mm: list, face: str, depth_mm: float,
                                     name: str = "Boss", corner_radii_mm=None) -> dict:
        """Grow a polygon boss depth_mm out of any planar face.

        points_mm are 3D [x, y, z] vertices on the face (auto-closed); the face is
        found as for add_hole_on_face. For pads, ledges and mounting blocks on an
        existing part. corner_radii_mm rounds the corners as for
        add_extruded_profile. Volume added = polygon area * depth.
        """
        self._require_model()
        if depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        defined = self._sketch_polygon_on_face(face, points_mm, corner_radii_mm)
        return self._extrude_sketch(depth_mm, name, defined, "Is the profile closed and on the face?")

    @staticmethod
    def _require_installed_font(font: str) -> None:
        """Windows draws an unknown font in another one, silently, and SolidWorks
        still reports the name it was given: so check the installed families."""
        families = set()

        def collect(logfont, *_):
            families.add(logfont.lfFaceName.lower())
            return 1

        screen = win32gui.GetDC(0)
        try:
            win32gui.EnumFontFamilies(screen, None, collect, None)
        finally:
            win32gui.ReleaseDC(0, screen)
        if font.lower() not in families:
            raise SolidWorksError(f"Font '{font}' is not installed; leave font out for SolidWorks' own.")

    def add_text_on_face(self, text: str, face: str, x_mm: float, y_mm: float, z_mm: float,
                         height_mm: float, depth_mm: float, emboss: bool = False,
                         font: str | None = None, name: str = "Text") -> dict:
        """Engrave text into any planar face, or emboss it (emboss=True).

        (x, y, z) is the lower-left corner of the text, on the face (found as
        for add_hole_on_face); the text runs along the face sketch's horizontal
        axis: upright along +x on the +z and -y faces, turned on the others
        (see Docs/PROGRESS.md). height_mm is the character height, depth_mm how
        deep the letters go or how high they stand. The position is two
        dimensions from the origin ('x', 'y') and the depth a third; font is an
        installed font (default SolidWorks' own). Letters have no hand
        calculation, so the result gives text_area_mm2 = volume change / depth.
        """
        if not str(text).strip():
            raise SolidWorksError("No text given.")
        if height_mm <= 0 or depth_mm <= 0:
            raise SolidWorksError(f"height and depth must be > 0 (got {height_mm}, {depth_mm}).")
        model = self._require_model()
        if font is not None:
            self._require_installed_font(font)
        before_volume = self.get_mass_properties()["mass_properties"]["volume_mm3"]
        sk, sketch = self._open_sketch_on_face(face, (x_mm, y_mm, z_mm))
        try:
            u, v = self._model_to_sketch_uv(sketch, mm_to_m(x_mm), mm_to_m(y_mm), mm_to_m(z_mm), face)
            sketch_text = binding.wrap(model.InsertSketchText(u, v, 0.0, str(text), SW_TEXT_JUSTIFY_LEFT, 0, 0, 100, 100),
                                       self._mod.ISketchText)
            if sketch_text is None:
                raise SolidWorksError("InsertSketchText failed (None).")
            text_format = binding.wrap(sketch_text.GetTextFormat(), self._mod.ITextFormat)
            text_format.CharHeight = mm_to_m(height_mm)
            if font is not None:
                text_format.TypeFaceName = font
            if not sketch_text.SetTextFormat(False, text_format):
                raise SolidWorksError("Could not set the text's height and font.")
            points = sketch.GetSketchPoints2() or ()
            if len(points) != 1:
                raise SolidWorksError(f"Expected one insertion point in the text sketch, found {len(points)}.")
            _, definer = self._open_sketch_definer(sk)
            u0, v0, _ = self._sketch_coords(sketch, 0.0, 0.0, 0.0)  # the origin, projected
            dims = definer.place_point(binding.wrap(points[0], self._mod.ISketchPoint),
                                       (m_to_mm(u - u0), m_to_mm(v - v0)), {("x", 0): "x", ("y", 0): "y"})
            defined = {"dimensions": dims, "fully_defined": definer.fully_defined()}
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)  # close the sketch, also when the text was refused

        if emboss:
            result = self._extrude_sketch(depth_mm, name, defined, f"Is ({x_mm}, {y_mm}, {z_mm}) on the {face} face?")
        else:
            feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
            cut = feat_mgr.FeatureCut4(
                True, False, False, SW_END_COND_BLIND, 0, mm_to_m(depth_mm), 0.0,
                False, False, False, False, 0.0, 0.0,
                False, False, False, False, False, True, True, False, False, False,
                SW_START_SKETCH_PLANE, 0.0, False, False,
            )
            if cut is None:
                raise SolidWorksError(f"FeatureCut4 failed (None). Is ({x_mm}, {y_mm}, {z_mm}) on the {face} face?")
            result = self._with_depth(self._finish_feature(cut, name, **defined), depth_mm)
        change = abs(result["mass_properties"]["volume_mm3"] - before_volume)
        if change < 1e-6:
            raise SolidWorksError("The text changed nothing; is its point inside the face?")  # run_guarded cleans up
        result["text_area_mm2"] = round(change / depth_mm, 4)
        return result

    def cut_profile(self, points_mm: list, depth_mm: float | None = None,
                    name: str = "Cut", corner_radii_mm=None, rotate_deg: float = 0.0,
                    about_mm: list | None = None) -> dict:
        """Cut a polygonal pocket/slot from the +Z face, blind or through.

        points_mm is a list of [x, y] vertices (mm) in add_box coordinates. The
        polygon is auto-closed and cut into the part: blind by depth_mm, or all
        the way through when depth_mm is None. corner_radii_mm rounds the
        corners as for add_extruded_profile; rotate_deg turns the profile about
        about_mm first, as there. Returns mass properties.
        """
        model = self._require_model()
        if not points_mm:
            raise SolidWorksError("No profile points given.")
        points_mm = self._turned_points(points_mm, rotate_deg, about_mm)
        if depth_mm is not None and depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")

        body = self._solid_body()
        self._select_planar_face(body, (0.0, 0.0, 1.0), "+Z")
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sketch = self._sketch_closed_polygon(sk, points_mm, corner_radii_mm)

        if depth_mm is None:
            t1, d1 = SW_END_COND_THROUGH_ALL, 0.0
        else:
            t1, d1 = SW_END_COND_BLIND, mm_to_m(depth_mm)

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        cut = feat_mgr.FeatureCut4(
            True, False, False, t1, 0, d1, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, False,
            True, True, False, False, False,
            SW_START_SKETCH_PLANE, 0.0, False, False,
        )
        if cut is None:
            raise SolidWorksError("FeatureCut4 failed (None). Is the profile on the +Z face?")
        return self._with_depth(self._finish_feature(cut, name, **sketch), depth_mm)

    def cut_profile_on_face(self, points_mm: list, face: str,
                            depth_mm: float | None = None, name: str = "Cut",
                            corner_radii_mm=None) -> dict:
        """Cut a polygon pocket/slot on ANY planar face, blind or through.

        points_mm is a list of 3D [x, y, z] vertices (mm) that lie on one face
        facing `face` ('+x'/'-x'/...): the face through them is used, as for
        add_hole_on_face. Each is mapped into the face-sketch via the
        model->sketch transform. The polygon is auto-closed; cut blind by depth_mm
        or through when depth_mm is None. corner_radii_mm rounds the corners as
        for add_extruded_profile. Returns mass properties.
        """
        model = self._require_model()
        if depth_mm is not None and depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        defined = self._sketch_polygon_on_face(face, points_mm, corner_radii_mm)

        if depth_mm is None:
            t1, d1 = SW_END_COND_THROUGH_ALL, 0.0
        else:
            t1, d1 = SW_END_COND_BLIND, mm_to_m(depth_mm)

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        cut = feat_mgr.FeatureCut4(
            True, False, False, t1, 0, d1, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, False,
            True, True, False, False, False,
            SW_START_SKETCH_PLANE, 0.0, False, False,
        )
        if cut is None:
            raise SolidWorksError(f"FeatureCut4 failed (None). Are the points on the {face} face?")
        return self._with_depth(self._finish_feature(cut, name, **defined), depth_mm)

    def cut_offset_pocket(self, face: str, x_mm: float, y_mm: float, z_mm: float, rim_mm: float,
                          depth_mm: float | None = None, name: str = "Pocket") -> dict:
        """Pocket a planar face, leaving a rim rim_mm wide along its outline.

        The recessed web of an I-beam that follows a curved link, a tray, or a
        frame (depth_mm None: through all). The face is the one facing `face`
        through (x, y, z), as for the *_on_face tools. The rim follows the
        outline, arcs and splines included; holes in the face stay holes. The
        pocket's edge is the outline offset inwards, tied to it, so rim and
        depth are dimensions and the pocket follows the outline when it changes.
        """
        if rim_mm <= 0:
            raise SolidWorksError(f"rim must be > 0 (got {rim_mm}).")
        if depth_mm is not None and depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        model = self._require_model()
        normal, side = self._parse_face_selector(face, default_side=None)
        target = self._select_face_through(self._solid_body(), normal, side, (x_mm, y_mm, z_mm), face)
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sketch = self._open_face_sketch(sk, face)
        try:
            fully_defined = self._offset_outline(sketch, target, rim_mm, face)
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)
        dimensions = {"rim": self._rename_first_dimension(self._history()[-1], "rim")}
        model.ClearSelection2(True)
        if not self._history()[-1].Select2(False, 0):
            raise SolidWorksError("Could not select the pocket's sketch.")
        t1, d1 = (SW_END_COND_THROUGH_ALL, 0.0) if depth_mm is None else (SW_END_COND_BLIND, mm_to_m(depth_mm))
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        cut = feat_mgr.FeatureCut4(
            True, False, False, t1, 0, d1, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, False,
            True, True, False, False, False,
            SW_START_SKETCH_PLANE, 0.0, False, False,
        )
        if cut is None:
            raise SolidWorksError(f"FeatureCut4 failed (None) for the pocket in the {face} face.")
        return self._with_depth(self._finish_feature(cut, name, dimensions=dimensions, fully_defined=fully_defined),
                                depth_mm)

    def _offset_outline(self, sketch, face, offset_mm: float, label: str) -> bool:
        """Draw the face's outer loop offset offset_mm inwards in the open sketch
        (Offset Entities: tied to the edges by one dimension); whether the
        sketch is then fully defined."""
        loops = [binding.wrap(loop, self._mod.ILoop2) for loop in binding.wrap(face, self._mod.IFace2).GetLoops() or ()]
        outer = next((loop for loop in loops if loop.IsOuter()), None)
        if outer is None:
            raise SolidWorksError(f"The {label} face has no outer loop.")
        self._model.ClearSelection2(True)
        for edge in outer.GetEdges() or ():
            if not binding.wrap(edge, self._mod.IEntity).Select4(True, None):
                raise SolidWorksError(f"Could not select the outline of the {label} face.")
        # a negative offset runs inwards from a face's outer loop (verified)
        if not self._model.SketchOffsetEntities2(-mm_to_m(offset_mm), False, True):
            raise SolidWorksError(f"SolidWorks could not offset the {label} face's outline by {offset_mm:g} mm: "
                                  "is the rim wider than half the face?")
        return sketch.GetConstrainedStatus() == SW_FULLY_CONSTRAINED

    def _rename_first_dimension(self, feature, role: str) -> str:
        """Give the feature's first dimension the name `role`; returns the name
        set_dimension takes, e.g. 'rim@Sketch2'."""
        display = binding.wrap(feature.GetFirstDisplayDimension(), self._mod.IDisplayDimension)
        if display is None:
            raise SolidWorksError(f"'{feature.Name}' has no dimension to name '{role}'.")
        dim = binding.wrap(display.GetDimension2(0), self._mod.IDimension)
        dim.Name = role
        return dim.GetNameForSelection()

    _REF_PLANE_INDEX = {"front": 0, "top": 1, "right": 2}  # tree order in a new part

    def cut_profile_through_plane(self, points_mm: list, plane: str,
                                  depth_mm: float | None = None, name: str = "Cut",
                                  corner_radii_mm=None, keep_inside: bool = False) -> dict:
        """Cut a polygon sketched on a reference plane, symmetric about it.

        plane: 'front' (z = 0), 'top' (y = 0), 'right' (x = 0) or the name of
        another plane in the part (e.g. 'Plane1'). points_mm are 3D [x, y, z]
        vertices ON that plane (e.g. x = 0 for 'right'). The cut runs
        through all in both directions (depth_mm None), or depth_mm in total,
        centred on the plane. For shapes seen from the side: wedges, windows and
        recesses symmetric about the plane. corner_radii_mm rounds the corners
        as for add_extruded_profile. keep_inside=True cuts away everything
        OUTSIDE the profile instead (through all): the part becomes what it has
        in common with the profile seen from that side, so a front view
        extruded and a side view cut this way give a 3D shape. Returns mass
        properties.
        """
        if depth_mm is not None and depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        if keep_inside and depth_mm is not None:
            raise SolidWorksError("keep_inside cuts through all: leave depth_mm out.")
        model = self._require_model()
        if not points_mm:
            raise SolidWorksError("No profile points given.")
        corners = self._profile_corners(points_mm, corner_radii_mm)

        ref, label = self._named_plane(plane)
        model.ClearSelection2(True)
        if not ref.Select2(False, 0):
            raise SolidWorksError(f"Could not select the {label}.")
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sketch = self._open_face_sketch(sk, label)
        try:
            uv_m = [self._model_to_sketch_uv(sketch, mm_to_m(p[0]), mm_to_m(p[1]),
                                             mm_to_m(p[2]), label)
                    for p in points_mm]
            defined = self._draw_defined_polygon(sk, self._clean_polygon(uv_m), corners)
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)  # close the sketch, also when a point is rejected

        if depth_mm is None:
            single, t1, t2, d1 = False, SW_END_COND_THROUGH_ALL, SW_END_COND_THROUGH_ALL, 0.0
        else:
            single, t1, t2, d1 = True, SW_END_COND_MID_PLANE, 0, mm_to_m(depth_mm)
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        cut = feat_mgr.FeatureCut4(
            single, bool(keep_inside), False, t1, t2, d1, 0.0,  # Flip: cut away the outside
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, False,
            True, True, False, False, False,
            SW_START_SKETCH_PLANE, 0.0, False, False,
        )
        if cut is None:
            raise SolidWorksError(
                f"FeatureCut4 failed (None). Does the profile on the {label} cross the part?"
            )
        return self._with_depth(self._finish_feature(cut, name, **defined), depth_mm)

    def add_extruded_profile_on_plane(self, points_mm: list, plane: str, depth_mm: float,
                                      reverse: bool = False, name: str = "Extrude",
                                      corner_radii_mm=None, draft_deg: float = 0.0, merge: bool = True) -> dict:
        """Extrude a polygon sketched on a reference plane, merged with the body.

        plane: 'front', 'top', 'right' or a plane by name, such as one add_plane
        made at an angle. points_mm are 3D [x, y, z] vertices ON that plane
        (add_plane gives its origin and axes: origin + u x_axis + v y_axis).
        depth_mm along the plane's normal, or the other way with reverse=True.
        corner_radii_mm, draft_deg and merge work as for add_extruded_profile.
        Returns mass properties.
        """
        if depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        if not points_mm:
            raise SolidWorksError("No profile points given.")
        self._check_draft(draft_deg)
        corners = self._profile_corners(points_mm, corner_radii_mm)
        model = self._require_model()
        ref, label = self._named_plane(plane)
        model.ClearSelection2(True)
        if not ref.Select2(False, 0):
            raise SolidWorksError(f"Could not select the {label}.")
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sketch = self._open_face_sketch(sk, label)
        try:
            uv_m = [self._model_to_sketch_uv(sketch, *(mm_to_m(c) for c in p), label) for p in points_mm]
            defined = self._draw_defined_polygon(sk, self._clean_polygon(uv_m), corners)
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)  # close the sketch, also when a point is rejected
        return self._extrude_sketch(depth_mm, name, defined, f"Is the profile on the {label} closed?", reverse=reverse,
                                    draft_deg=draft_deg, merge=merge)

    def _define_slot(self, sk) -> dict:
        """Define an open straight-slot sketch by its end-arc centres and width.

        Only the construction centreline and one end arc get constraints: the
        slot's own relations tie its straight edges and other arc to them.
        """
        sketch = binding.wrap(sk.ActiveSketch, self._mod.ISketch)
        segments = [binding.wrap(s, self._mod.ISketchSegment) for s in (sketch.GetSketchSegments() or ())]
        centreline = next((s for s in segments if s.GetType() == SW_SKETCH_LINE and s.ConstructionGeometry), None)
        arc = next((s for s in segments if s.GetType() == SW_SKETCH_ARC), None)
        if centreline is None or arc is None:
            raise SolidWorksError("The slot sketch has no centreline or end arc to dimension.")
        names = {("x", 0): "end1_x", ("y", 0): "end1_y", ("x", 1): "end2_x", ("y", 1): "end2_y",
                 ("diameter", 0): "width"}
        return self._define_sketch(sk, [centreline], [arc], names=names)

    def _sketch_slot(self, sk, c1, c2, width_mm: float) -> dict:
        """Draw a straight slot in the open sketch between end-arc centres c1 and
        c2 ([x, y] mm) and define it; returns the _define_slot result."""
        dx, dy = c2[0] - c1[0], c2[1] - c1[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            raise SolidWorksError("The slot's two ends coincide; give two different points.")
        px, py = -dy / length, dx / length  # perpendicular: CreateSketchSlot takes a point on a side
        edge = ((c1[0] + c2[0]) / 2 + width_mm / 2 * px, (c1[1] + c2[1]) / 2 + width_mm / 2 * py)
        seg = sk.CreateSketchSlot(
            SW_SLOT_CREATION_LINE, SW_SLOT_LENGTH_CENTER, mm_to_m(width_mm),
            mm_to_m(c1[0]), mm_to_m(c1[1]), 0.0,
            mm_to_m(c2[0]), mm_to_m(c2[1]), 0.0,
            mm_to_m(edge[0]), mm_to_m(edge[1]), 0.0,
            1, False,
        )
        if not seg:
            raise SolidWorksError("Slot sketch failed: CreateSketchSlot returned nothing.")
        return self._define_slot(sk)

    def add_extruded_slot(self, start_mm: list, end_mm: list, width_mm: float, depth_mm: float,
                          name: str = "Slot") -> dict:
        """Extrude a stadium on the first plane: half circles of diameter width_mm
        centred on start_mm and end_mm ([x, y] mm), joined by straight sides.

        A rounded tab, lug or link between two points, with the points themselves
        as the arc centres (end1_x ... end2_y and width are its dimensions).
        Volume = (width * length + pi * (width / 2)^2) * depth.
        """
        model = self._require_model()
        if width_mm <= 0 or depth_mm <= 0:
            raise SolidWorksError(f"width and depth must be > 0 (got {width_mm}, {depth_mm}).")
        plane = self._first_ref_plane()
        if plane is None or not plane.Select2(False, 0):
            raise SolidWorksError("Could not select the Front plane.")
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            sketch = self._sketch_slot(sk, (float(start_mm[0]), float(start_mm[1])),
                                       (float(end_mm[0]), float(end_mm[1])), width_mm)
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)
        return self._extrude_sketch(depth_mm, name, sketch, "Is the slot valid?")

    def cut_slot(self, length_mm: float, width_mm: float, x_mm: float, y_mm: float,
                 angle_deg: float = 0.0, depth_mm: float | None = None, name: str = "Slot") -> dict:
        """Cut a straight slotted hole (obround) on the +Z face, blind or through.

        Centred at (x_mm, y_mm); length_mm is centre-to-centre of the end arcs,
        width_mm the slot width, angle_deg the orientation in the +Z plane. Cut
        blind by depth_mm or through (None). Returns mass properties.
        """
        model = self._require_model()
        if length_mm <= 0 or width_mm <= 0:
            raise SolidWorksError("length and width must be > 0.")

        rad = deg_to_rad(angle_deg)
        ax, ay = math.cos(rad), math.sin(rad)      # slot axis direction
        half = length_mm / 2.0
        c1 = (x_mm - half * ax, y_mm - half * ay)
        c2 = (x_mm + half * ax, y_mm + half * ay)

        body = self._solid_body()
        self._select_planar_face(body, (0.0, 0.0, 1.0), "+Z")
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            sketch = self._sketch_slot(sk, c1, c2, width_mm)
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)

        if depth_mm is None:
            t1, d1 = SW_END_COND_THROUGH_ALL, 0.0
        else:
            if depth_mm <= 0:
                raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
            t1, d1 = SW_END_COND_BLIND, mm_to_m(depth_mm)

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        cut = feat_mgr.FeatureCut4(
            True, False, False, t1, 0, d1, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, False,
            True, True, False, False, False,
            SW_START_SKETCH_PLANE, 0.0, False, False,
        )
        if cut is None:
            raise SolidWorksError("FeatureCut4 failed (None). Does the slot fit on the +Z face?")
        return self._with_depth(self._finish_feature(cut, name, **sketch), depth_mm)

    def add_fillet(self, radius_mm: float, edges: str = "all", name: str = "Fillet",
                   radii_at_mm: list | None = None, skip_shorter_mm: float | None = None,
                   tangent_propagation: bool = False) -> dict:
        """Round edges of the part's solid body with one constant radius.

        edges: 'all' (default); a world axis 'x'|'y'|'z' (straight edges parallel
        to it, e.g. 'z' = the depth edges of an add_box block); a face outline
        like '+z:outline' (the outer edges of the top face, not those of holes in
        it); every edge of one feature, 'feature:Boss'; or explicit indices like
        '2,5' from list_edges. radii_at_mm = [[x, y, z, r], ...] makes the radius
        vary: r at the edge end at each point (list_edges gives the ends),
        radius_mm at the other ends, straight in between; 'vertex_radii' names
        each end's radius dimension. skip_shorter_mm leaves out the edges shorter
        than that (slivers a radius cannot follow). tangent_propagation=True
        carries the round on along the edges that run on smoothly from the given
        ones, as SolidWorks' own default does: an edge that ends where it runs
        into another tangentially, such as round a fillet, rounds only so. When
        SolidWorks refuses, each edge is tried alone, also with propagation, and
        the error names the ones that do not fit. Returns how many edges were
        filleted and the resulting mass properties (volume drops as convex edges
        are rounded off).
        """
        model = self._require_model()
        if radius_mm <= 0:
            raise SolidWorksError(f"radius must be > 0 (got {radius_mm}).")

        body = self._solid_body()
        edge_count = self._select_edges(body, edges)
        if edge_count == 0:
            raise SolidWorksError(f"No edges found for selector '{edges}'.")
        skipped = 0
        if skip_shorter_mm is not None:
            skipped = self._deselect_short_edges(skip_shorter_mm)
            edge_count -= skipped
            if edge_count == 0:
                raise SolidWorksError(f"Every edge of '{edges}' is shorter than {skip_shorter_mm:g} mm.")
        if radii_at_mm:
            return self._variable_fillet(radius_mm, radii_at_mm, edge_count, name)

        selected = self._selected_objects()
        fillet = self._uniform_fillet(radius_mm, tangent_propagation)
        if fillet is None:
            raise SolidWorksError(self._fillet_refusal(radius_mm, selected, tangent_propagation))
        result = self._finish_feature(fillet, name, edges_filleted=edge_count)
        if skip_shorter_mm is not None:
            result["edges_skipped"] = skipped
        return result

    def _uniform_fillet(self, radius_mm: float, propagate: bool = False):
        """FeatureFillet3 on the selected edges with one radius; None when refused."""
        feat_mgr = binding.wrap(self._model.FeatureManager, self._mod.IFeatureManager)
        return feat_mgr.FeatureFillet3(
            SW_FILLET_OPT_UNIFORM_RADIUS | (SW_FILLET_OPT_PROPAGATE if propagate else 0),  # Options
            mm_to_m(radius_mm),                # R1 (uniform radius)
            0.0, 0.0,                          # R2, Rho
            SW_FILLET_TYPE_SIMPLE,             # Ftyp
            0, 0,                              # OverflowType, ConicRhoType
            None, None, None, None,            # Radii, Dist2Arr, RhoArr, SetBackDistances
            None, None, None,                  # PointRadius/Dist2/Rho arrays
        )

    def _selected_objects(self) -> list:
        selmgr = binding.wrap(self._model.SelectionManager, self._mod.ISelectionMgr)
        return [selmgr.GetSelectedObject6(i, -1) for i in range(1, selmgr.GetSelectedObjectCount2(-1) + 1)]

    def _deselect_short_edges(self, shorter_mm: float) -> int:
        """Keep only the selected edges at least shorter_mm long; return how many went."""
        selected = self._selected_objects()
        long_enough = [e for e in selected if self._edge_entry(0, e)["length_mm"] >= shorter_mm]
        self._model.ClearSelection2(True)
        for edge in long_enough:
            binding.wrap(edge, self._mod.IEntity).Select4(True, None)
        return len(selected) - len(long_enough)

    _MAX_FILLET_TRIALS = 200

    def _fillet_refusal(self, radius_mm: float, edges: list, propagate: bool = False) -> str:
        """Why a fillet was refused. The edges are added one at a time to a
        trial fillet (removed again each time): an edge that breaks the group
        is left out, and tried alone to tell which fail on their own; one that
        does is tried with tangent propagation too, when that was off."""
        if len(edges) > self._MAX_FILLET_TRIALS:
            return f"SolidWorks could not round {len(edges)} edge(s) with R{radius_mm:g}: is the radius too large?"
        model = self._model
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        index_of = {ref: i for i, ref in enumerate(self._persist_refs(self._solid_body().GetEdges() or ()))}

        def rounds(refs, chain=propagate, radius=radius_mm) -> bool:
            model.ClearSelection2(True)
            for ref in refs:
                binding.wrap(extension.GetObjectByPersistReference3(ref)[0], self._mod.IEntity).Select4(True, None)
            trial = self._uniform_fillet(radius, chain)
            model.ClearSelection2(True)
            if trial is None:
                return False
            binding.wrap(trial, self._mod.IFeature).Select2(False, 0)
            extension.DeleteSelection2(SW_DELETE_ABSORBED)
            model.ClearSelection2(True)
            return True

        fits, alone, together = [], [], []
        for ref in self._persist_refs(edges):
            if rounds(fits + [ref]):
                fits.append(ref)
            else:
                (together if rounds([ref]) else alone).append(ref)
        chained = [ref for ref in alone if not propagate and rounds([ref], True)]
        alone = [ref for ref in alone if ref not in chained]

        def named(refs) -> str:
            return ", ".join(f"{index_of.get(ref, -1)} ({self._edge_entry(0, extension.GetObjectByPersistReference3(ref)[0])['length_mm']:g} mm)"
                             for ref in refs)

        parts = []
        if chained:
            parts.append(f"edge(s) {named(chained)} fail alone but round with tangent_propagation=True: they end where "
                         "they run on smoothly into further edges, such as round a fillet, which the round must follow")
        if alone:
            parts.append(f"edge(s) {named(alone)} fail even alone{'' if propagate else ', also with tangent propagation'}"
                         + self._largest_rounds(alone, index_of, lambda ref, r: rounds([ref], radius=r), radius_mm))
        if together:
            parts.append(f"edge(s) {named(together)} round alone but not with the others")
        message = f"R{radius_mm:g} does not round all {len(edges)} edge(s): {'; '.join(parts)}."
        if fits:
            keep = ",".join(str(index_of.get(ref, -1)) for ref in fits)
            message += f" The other {len(fits)} round together: edges=\"{keep}\" (list_edges indices)."
        return message + " Else a smaller radius, or skip_shorter_mm for slivers."

    # bisection steps for the largest round an edge takes: radius / 2^8, 0.01 mm at R2.5
    _RADIUS_STEPS = 8
    _MAX_RADIUS_SEARCHES = 6

    def _largest_rounds(self, refs, index_of, rounds, radius_mm: float) -> str:
        """', the largest round each takes: 13 R1.99, ...', found by halving
        the step between a round that fits and one that does not; '' for more
        edges than are worth the trials."""
        if len(refs) > self._MAX_RADIUS_SEARCHES:
            return ""
        found = []
        for ref in refs:
            low, high = 0.0, radius_mm
            for _ in range(self._RADIUS_STEPS):
                middle = (low + high) / 2
                low, high = (middle, high) if rounds(ref, middle) else (low, middle)
            largest = math.floor(low * 100) / 100
            found.append(("" if len(refs) == 1 else f"{index_of.get(ref, -1)} ")
                         + (f"R{largest:g}" if largest else "none"))
        label = "the largest round it takes" if len(refs) == 1 else "the largest round each takes"
        return f"; {label}: {', '.join(found)}"

    _VERTEX_TOLERANCE_MM = 0.01

    def _variable_fillet(self, radius_mm: float, radii_at_mm, edge_count: int, name: str) -> dict:
        """Fillet the selected edges with a radius that runs straight from end to
        end: radii_at_mm at the ends there, radius_mm at the others."""
        ends = self._selected_edge_ends()
        radii = self._vertex_radii(ends, radii_at_mm, radius_mm)
        feat_mgr = binding.wrap(self._model.FeatureManager, self._mod.IFeatureManager)
        fillet = feat_mgr.FeatureFillet3(
            SW_FILLET_OPT_STRAIGHT_TRANSITION, mm_to_m(radius_mm), 0.0, 0.0, SW_FILLET_TYPE_VARIABLE, 0, 0,
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [mm_to_m(r) for r in radii]),  # one per end
            None, None, None, None, None, None,
        )
        if fillet is None:
            raise SolidWorksError("FeatureFillet3 failed (None). Is a radius too large for the geometry?")
        result = self._finish_feature(fillet, name, edges_filleted=edge_count)
        feature = binding.wrap(fillet, self._mod.IFeature)
        self._check_vertex_radii(feature, dict(zip(ends, radii)))
        values = {d["name"]: d["value"] for d in self._dimension_entries(feature)}
        result["vertex_radii"] = []
        for k, (end, radius) in enumerate(zip(ends, radii)):
            dimension = f"{self._vertex_radius_dimension(k)}@{result['feature']}"
            if values.get(dimension) is None or abs(values[dimension] - radius) > 1e-6:
                raise SolidWorksError(f"Could not find the dimension of the radius at {list(end)} ({dimension}).")
            result["vertex_radii"].append({"at_mm": list(end), "radius_mm": radius, "dimension": dimension})
        return result

    def _vertex_mm(self, vertex) -> tuple:
        return tuple(round(m_to_mm(c), 4) for c in binding.wrap(vertex, self._mod.IVertex).GetPoint())

    def _selected_edge_ends(self) -> list:
        """The ends of the selected edges in the order SolidWorks numbers them for
        a variable fillet: edge by edge as selected, start before end, each once
        (verified)."""
        selmgr = binding.wrap(self._model.SelectionManager, self._mod.ISelectionMgr)
        ends = []
        for i in range(1, selmgr.GetSelectedObjectCount2(-1) + 1):
            edge = binding.wrap(selmgr.GetSelectedObject6(i, -1), self._mod.IEdge)
            vertices = [edge.GetStartVertex(), edge.GetEndVertex()]
            if None in vertices:
                raise SolidWorksError("A variable fillet needs edges with two ends; a full circle has none.")
            for vertex in vertices:
                position = self._vertex_mm(vertex)
                if position not in ends:
                    ends.append(position)
        return ends

    @classmethod
    def _vertex_radii(cls, ends_mm, radii_at_mm, default_mm) -> list:
        """The radius at each edge end: r where a point [x, y, z, r] lies on that
        end, default_mm at the others; pure, unit-tested."""
        radii = [default_mm] * len(ends_mm)
        placed = set()
        for entry in radii_at_mm:
            try:
                x, y, z, radius = (float(v) for v in entry)
            except (TypeError, ValueError):
                raise SolidWorksError(f"radii_at_mm takes [x, y, z, radius] per edge end (got {entry}).") from None
            if radius <= 0:
                raise SolidWorksError(f"A variable fillet needs a radius > 0 at every end (got {radius:g}).")
            at = next((k for k, end in enumerate(ends_mm) if math.dist(end, (x, y, z)) <= cls._VERTEX_TOLERANCE_MM), None)
            if at is None:
                listed = ", ".join(f"({', '.join(f'{c:g}' for c in end)})" for end in ends_mm)
                raise SolidWorksError(f"No end of the edges at ({x:g}, {y:g}, {z:g}); they end at {listed}.")
            if at in placed:
                raise SolidWorksError(f"radii_at_mm gives the end at ({x:g}, {y:g}, {z:g}) a radius twice.")
            placed.add(at)
            radii[at] = radius
        return radii

    @staticmethod
    def _vertex_radius_dimension(k: int) -> str:
        """SolidWorks names the radii of a variable fillet D0, D01, D02, ... in the
        order it got them (verified up to D011)."""
        return "D0" if k == 0 else f"D0{k}"

    def _check_vertex_radii(self, feature, radius_at: dict) -> None:
        """Read back the radius SolidWorks gave each end: the radii go in as a bare
        list, so a different numbering would put them on the wrong ends unseen."""
        data = binding.wrap(feature.GetDefinition(), self._mod.IVariableFilletFeatureData2)
        if not data.AccessSelections(self._model, None):
            raise SolidWorksError("Could not read the variable fillet back.")
        try:
            for i in range(data.FilletEdgeCount):
                edge = binding.wrap(data.GetFilletEdgeAtIndex(i), self._mod.IEdge)
                for vertex in (edge.GetStartVertex(), edge.GetEndVertex()):
                    end, got = self._vertex_mm(vertex), m_to_mm(data.GetRadius(vertex))
                    if end not in radius_at or abs(got - radius_at[end]) > 1e-6:
                        raise SolidWorksError(f"SolidWorks put R{got:g} at {list(end)}, not the radius asked for there.")
        finally:
            data.ReleaseSelectionAccess()

    def add_full_round(self, face: str, x_mm: float, y_mm: float, z_mm: float, name: str = "FullRound") -> dict:
        """Round a rib's top off completely: a fillet tangent to the planar face
        facing `face` through the point and to the two opposite flat side faces
        along it that lie closest together. Its radius is half their distance,
        'width_mm', so it follows the rib; there is no radius dimension. Hand
        calculation: a rib w wide and L long loses L w^2 (1/2 - pi/8)."""
        model = self._require_model()
        normal, side = self._parse_face_selector(face, default_side=None)
        centre = self._select_face_through(self._solid_body(), normal, side, (x_mm, y_mm, z_mm), face)
        sides = self._side_faces(centre, normal)
        first, second, width = self._full_round_sides([(f.Normal, self._plane_point_mm(f)) for f in sides])
        selmgr = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        model.ClearSelection2(True)
        for entity, mark in ((sides[first], SW_MARK_FULL_ROUND_SIDE_1), (centre, SW_MARK_FULL_ROUND_CENTRE),
                             (sides[second], SW_MARK_FULL_ROUND_SIDE_2)):
            data = binding.wrap(selmgr.CreateSelectData(), self._mod.ISelectData)
            data.Mark = mark
            if not binding.wrap(entity, self._mod.IEntity).Select4(True, data):
                raise SolidWorksError("Could not select the faces for the full round.")
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        fillet = feat_mgr.FeatureFillet3(0, 0.0, 0.0, 0.0, SW_FILLET_TYPE_FULL_ROUND, 0, 0,
                                         None, None, None, None, None, None, None)
        if fillet is None:
            raise SolidWorksError(f"SolidWorks could not round the {face} face off between its sides.")
        return self._finish_feature(fillet, name, width_mm=round(width, 4))

    def _side_faces(self, centre, normal) -> list:
        """The flat faces along the edges of `centre` that stand square to it, each once."""
        extension = binding.wrap(self._model.Extension, self._mod.IModelDocExtension)
        seen, sides = set(), []
        for edge in binding.wrap(centre, self._mod.IFace2).GetEdges() or ():
            for neighbour in binding.wrap(edge, self._mod.IEdge).GetTwoAdjacentFaces2() or ():
                neighbour = binding.wrap(neighbour, self._mod.IFace2)
                if not binding.wrap(neighbour.GetSurface(), self._mod.ISurface).IsPlane():
                    continue
                if abs(sum(a * b for a, b in zip(neighbour.Normal, normal))) > 1e-6:
                    continue  # the centre face itself, or a slanted neighbour
                key = bytes(extension.GetPersistReference3(neighbour))
                if key not in seen:
                    seen.add(key)
                    sides.append(neighbour)
        return sides

    def _plane_point_mm(self, face) -> tuple:
        """A point on a planar face's plane (PlaneParams: normal, then root point)."""
        params = binding.wrap(binding.wrap(face, self._mod.IFace2).GetSurface(), self._mod.ISurface).PlaneParams
        return tuple(m_to_mm(c) for c in params[3:6])

    @staticmethod
    def _full_round_sides(sides) -> tuple:
        """Of the side faces [(normal, point on it)], the opposite pair that lies
        closest together: (i, j, distance); pure, unit-tested."""
        pairs = []
        for i, (normal, point) in enumerate(sides):
            for j in range(i + 1, len(sides)):
                other_normal, other_point = sides[j]
                if sum(a * b for a, b in zip(normal, other_normal)) < -0.999999:
                    pairs.append((abs(sum((b - a) * n for a, b, n in zip(point, other_point, normal))), i, j))
        if not pairs:
            raise SolidWorksError("A full round needs two opposite flat side faces along the face, such as a rib's sides.")
        pairs.sort()
        if len(pairs) > 1 and pairs[1][0] - pairs[0][0] < 1e-6:
            raise SolidWorksError(f"Two pairs of side faces are equally far apart ({pairs[0][0]:g} mm): the full "
                                  "round cannot tell which way to run.")
        distance, i, j = pairs[0]
        return i, j, distance

    def add_chamfer(self, distance_mm: float, edges: str = "all", name: str = "Chamfer",
                    angle_deg: float = 45.0, from_face: str | None = None) -> dict:
        """Chamfer edges of the part's solid body: distance_mm back along one
        face, at angle_deg to it (45, the default, is symmetric).

        edges: 'all' (default); a world axis 'x'|'y'|'z'; a face outline like
        '+z:outline'; every edge of one feature, 'feature:Boss'; or explicit
        indices like '2,5' from list_edges. Another angle than 45 needs
        from_face, which way the face the distance runs along faces ('-z': the
        underside, the chamfer leaving it at angle_deg, 60 for a printable
        edge). Round an arc the chamfer is a cone. Returns how many edges were
        chamfered and the resulting mass properties.
        """
        self._require_model()
        if distance_mm <= 0:
            raise SolidWorksError(f"distance must be > 0 (got {distance_mm}).")
        if not 0 < angle_deg < 90:
            raise SolidWorksError(f"angle_deg must be between 0 and 90 (got {angle_deg}).")
        if angle_deg != 45 and from_face is None:
            raise SolidWorksError(f"A chamfer at {angle_deg:g} degrees needs from_face: which way the face faces "
                                  "that the distance runs along and the angle is measured from, e.g. '-z'.")
        reference = self._parse_direction(from_face) if from_face is not None else None
        body = self._solid_body()
        chamfer, edge_count = self._insert_chamfer(body, edges, distance_mm, angle_deg, flip=False)
        if reference is not None:
            tilts = [self._tilt_deg(face, reference) for face in chamfer.GetFaces() or ()]
            if tilts and all(abs(t - (90 - angle_deg)) < self._CHAMFER_ANGLE_TOLERANCE_DEG for t in tilts):
                # SolidWorks measured from the other face of every edge
                self._model.ClearSelection2(True)
                chamfer.Select2(False, 0)
                binding.wrap(self._model.Extension, self._mod.IModelDocExtension).DeleteSelection2(SW_DELETE_ABSORBED)
                chamfer, edge_count = self._insert_chamfer(self._solid_body(), edges, distance_mm, angle_deg,
                                                           flip=True)
                tilts = [self._tilt_deg(face, reference) for face in chamfer.GetFaces() or ()]
            off = [round(t, 2) for t in tilts if abs(t - angle_deg) >= self._CHAMFER_ANGLE_TOLERANCE_DEG]
            if off or not tilts:
                raise SolidWorksError(f"The chamfer does not leave the {from_face} face at {angle_deg:g} degrees "
                                      f"everywhere (its faces stand at {off or 'none'} to it): chamfer only edges "
                                      f"of the {from_face} face, and edges that run the other way in a call of "
                                      "their own.")
        return self._finish_feature(chamfer, name, edges_chamfered=edge_count)

    _CHAMFER_ANGLE_TOLERANCE_DEG = 0.5

    def _insert_chamfer(self, body, edges: str, distance_mm: float, angle_deg: float, flip: bool):
        """Select the edges and chamfer them; (feature, edge count)."""
        edge_count = self._select_edges(body, edges)
        if edge_count == 0:
            raise SolidWorksError(f"No edges found for selector '{edges}'.")
        feat_mgr = binding.wrap(self._model.FeatureManager, self._mod.IFeatureManager)
        chamfer = feat_mgr.InsertFeatureChamfer(
            SW_FEATURE_CHAMFER_FLIP if flip else 0,  # Options (no tangent propagation)
            SW_CHAMFER_ANGLE_DISTANCE,           # ChamferType (distance + angle)
            mm_to_m(distance_mm),                # Width (the setback distance)
            deg_to_rad(angle_deg),               # Angle (45 deg -> symmetric chamfer)
            0.0,                                 # OtherDist
            0.0, 0.0, 0.0,                       # Vertex chamfer distances
        )
        if chamfer is None:
            raise SolidWorksError(
                "InsertFeatureChamfer failed (None). Is the distance too large for the geometry?"
            )
        return binding.wrap(chamfer, self._mod.IFeature), edge_count

    def _tilt_deg(self, face, direction) -> float:
        """The angle between a face and the plane facing `direction`, in degrees:
        between their normals, averaged over the face's facets (a cone's is the
        same all round)."""
        triangles = self._face_triangles(binding.wrap(face, self._mod.IFace2))
        if not triangles:
            return float("nan")
        cosines = [sum(n * d for n, d in zip(normal, direction)) for *_, normal in triangles]
        return math.degrees(math.acos(max(-1.0, min(1.0, sum(cosines) / len(cosines)))))

    def add_shell(self, thickness_mm: float, open_face: str = "+z") -> dict:
        """Hollow the part to a wall of `thickness_mm`, optionally opening one face.

        open_face: a direction '+z'/'-z'/'+x'/... selects the planar face to remove
        (an open shell); 'none' makes a fully closed hollow. Returns mass
        properties (volume drops to just the walls). InsertFeatureShell returns no
        feature object, so there is no feature name.
        """
        model = self._require_model()
        if thickness_mm <= 0:
            raise SolidWorksError(f"thickness must be > 0 (got {thickness_mm}).")

        body = self._solid_body()
        opened = (open_face or "none").lower()
        if opened == "none":
            model.ClearSelection2(True)
        else:
            normal, side = self._parse_face_selector(opened)
            self._select_planar_face(body, normal, opened, side)

        # Outward=False: the wall grows inward, so the outer size is unchanged.
        model.InsertFeatureShell(mm_to_m(thickness_mm), False)
        rebuilt_ok = bool(model.ForceRebuild3(False))
        props = self.get_mass_properties()["mass_properties"]
        if abs(props["volume_mm3"]) < 1e-6:
            raise SolidWorksError("The shell removed all material; is the wall too thick?")
        return {"ok": True, "open_face": opened, "rebuild_ok": rebuilt_ok, "mass_properties": props}

    def _first_edge_along(self, body, direction):
        """First straight edge parallel to `direction`; returns (p1, p2, dispatch).

        Returns the edge's raw dispatch too so the caller can select the exact
        edge it analysed (keeping a computed flip in lockstep with the selection),
        rather than re-resolving by coordinate. (None, None, None) if no match.
        """
        edges = body.GetEdges()
        if not edges:
            return None, None, None
        if not isinstance(edges, (list, tuple)):
            edges = [edges]
        for edge_dispatch in edges:
            edge = binding.wrap(edge_dispatch, self._mod.IEdge)
            curve = binding.wrap(edge.GetCurve(), self._mod.ICurve)
            if not (curve is not None and curve.IsLine()):
                continue
            start, end = edge.GetStartVertex(), edge.GetEndVertex()
            if start is None or end is None:
                continue
            p1 = binding.wrap(start, self._mod.IVertex).GetPoint()
            p2 = binding.wrap(end, self._mod.IVertex).GetPoint()
            dx, dy, dz = p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2]
            length = (dx * dx + dy * dy + dz * dz) ** 0.5
            if length > 1e-9 and abs(dx * direction[0] + dy * direction[1] + dz * direction[2]) / length > 0.999:
                return p1, p2, edge_dispatch
        return None, None, None

    # Feature types that are NOT valid pattern seeds (folders, sketches, reference
    # geometry, and finishing/repeat features). Patterning these is a no-op or
    # nonsensical, so the default-seed walk skips them.
    _NON_SEED_TYPES = {
        "DetailCabinet", "Fillet", "Chamfer", "Shell",
        "LPattern", "CircPattern", "LocalLPattern", "LocalCirPattern",
        "MirrorSolid", "MirrorPattern", "RefPlane", "RefAxis",
        "ProfileFeature", "OriginProfileFeature",
    }

    def _iter_features(self):
        """Yield each feature in the tree as a wrapped IFeature, in tree order."""
        feat = binding.wrap(self._model.FirstFeature(), self._mod.IFeature)
        while feat is not None:
            yield feat
            feat = binding.wrap(feat.GetNextFeature(), self._mod.IFeature)

    def _profile_feature_names(self) -> set:
        """Names of all sketches (ProfileFeature) in the tree.

        A before/after diff around drawing a sketch identifies exactly the one
        just created -- robust to pre-existing sketches and tree ordering, unlike
        a 'last ProfileFeature' assumption.
        """
        names = set()
        for feat in self._iter_features():
            try:
                if feat.GetTypeName2() == "ProfileFeature":
                    names.add(feat.Name)
            except pythoncom.com_error:
                pass
        return names

    def _sub_features(self, feat) -> list:
        subs, sub = [], binding.wrap(feat.GetFirstSubFeature(), self._mod.IFeature)
        while sub is not None:
            subs.append(sub)
            sub = binding.wrap(sub.GetNextSubFeature(), self._mod.IFeature)
        return subs

    def _under_defined_sketches(self) -> list:
        """'Sketch3 (under defined)' for every sketch in the part that is not fully defined.

        Includes sketches that exist only as sub-features, such as the position
        sketch of a Hole Wizard hole, which the feature walk does not visit.
        """
        found, seen = [], set()
        for feat in self._iter_features():
            for candidate in [feat, *self._sub_features(feat)]:
                if candidate.GetTypeName2() != "ProfileFeature" or candidate.Name in seen:
                    continue
                seen.add(candidate.Name)
                status = binding.wrap(candidate.GetSpecificFeature2(), self._mod.ISketch).GetConstrainedStatus()
                if status != SW_FULLY_CONSTRAINED:
                    found.append(f"{candidate.Name} ({SKETCH_STATUSES.get(status, f'status {status}')})")
        return found

    def _ref_planes(self) -> list:
        """All reference planes in tree order (fresh part: [Front, Top, Right, ...])."""
        self._require_part()
        planes = []
        for feat in self._iter_features():
            try:
                if feat.GetTypeName2() == "RefPlane":
                    planes.append(feat)
            except pythoncom.com_error:
                pass
        return planes

    def _last_ref_plane(self):
        """The most recently created reference plane (e.g. a fresh loft offset plane)."""
        planes = self._ref_planes()
        return planes[-1] if planes else None

    def _first_dimension_name(self, feature) -> str:
        """The feature's first dimension as set_dimension takes it, e.g. 'D1@Plane1'."""
        display = feature.GetFirstDisplayDimension()
        if display is None:
            raise SolidWorksError(f"Feature '{feature.Name}' has no dimension.")
        dim = binding.wrap(binding.wrap(display, self._mod.IDisplayDimension).GetDimension2(0), self._mod.IDimension)
        return dim.GetNameForSelection()

    def _last_feature_name(self) -> str:
        """Name of the most recent body-modifying feature (the default pattern seed).

        Skips folders, sketches, reference geometry and finishing/repeat features
        (fillet/chamfer/shell/patterns) so the default seed is a real boss/cut/
        hole/revolve. Pass feature_name explicitly to override.
        """
        feat = binding.wrap(self._model.FirstFeature(), self._mod.IFeature)
        seed = None
        while feat is not None:
            try:
                tname = feat.GetTypeName2() or ""
            except pythoncom.com_error:
                tname = ""
            if tname and not tname.endswith("Folder") and tname not in self._NON_SEED_TYPES:
                seed = feat
            feat = binding.wrap(feat.GetNextFeature(), self._mod.IFeature)
        if seed is None:
            raise SolidWorksError(
                "No patternable feature found; pass feature_name explicitly."
            )
        return seed.Name

    def add_linear_pattern(self, count: int, spacing_mm: float, direction: str = "+x",
                           feature_name: str | None = None) -> dict:
        """Repeat a feature `count` times, `spacing_mm` apart, along a direction.

        direction: '+x'/'-x'/'+y'/... (a body edge parallel to that axis sets the
        direction; flip is chosen so the pattern runs the requested way).
        feature_name: the feature to repeat (e.g. 'Hole'); defaults to the most
        recently added feature. Selection marks: direction edge = 1, seed = 4.
        """
        model = self._require_model()
        if count < 2:
            raise SolidWorksError(f"count must be >= 2 (got {count}).")
        if spacing_mm <= 0:
            raise SolidWorksError(f"spacing must be > 0 (got {spacing_mm}).")

        dvec = self._parse_direction(direction)
        body = self._solid_body()
        p1, p2, edge_dispatch = self._first_edge_along(body, dvec)
        if p1 is None:
            raise SolidWorksError(f"No straight edge parallel to {direction} found.")
        along = (p2[0] - p1[0]) * dvec[0] + (p2[1] - p1[1]) * dvec[1] + (p2[2] - p1[2]) * dvec[2]
        flip = along < 0  # pattern follows the edge's p1->p2 dir; flip to match `direction`

        seed = feature_name or self._last_feature_name()
        selmgr = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        model.ClearSelection2(True)
        select_data = binding.wrap(selmgr.CreateSelectData(), self._mod.ISelectData)
        select_data.Mark = 1
        if not binding.wrap(edge_dispatch, self._mod.IEntity).Select4(False, select_data):
            raise SolidWorksError("Could not select the direction edge.")
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        if not ext.SelectByID2(seed, "BODYFEATURE", 0.0, 0.0, 0.0, True, 4, None, 0):
            raise SolidWorksError(f"Could not select the seed feature '{seed}'.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        pattern = feat_mgr.FeatureLinearPattern(count, mm_to_m(spacing_mm), 1, 0.0,
                                                flip, False, "", "")
        if pattern is None:
            raise SolidWorksError("FeatureLinearPattern failed (None). Do all instances fit on the part?")
        return self._finish_feature(pattern, "LinearPattern", instances=count,
                                    seed=seed, direction=direction)

    # A cylinder axis is accepted as a pattern axis only if its centre is within
    # this many mm of the requested (cx, cy) -- avoids silently grabbing a far or
    # unrelated curved face.
    _CYL_AXIS_TOLERANCE_MM = 1.0

    def _cylindrical_face_near(self, body, cx_mm, cy_mm):
        """Raw dispatch of the CYLINDRICAL face whose bbox-centre (x,y) is nearest
        (cx,cy) and within tolerance, else None.

        Verifies the surface is actually a cylinder (not a cone/fillet/sphere) and
        applies a distance floor, mirroring the rigour of _planar_face_by_normal.
        """
        faces = body.GetFaces()
        if not faces:
            return None
        if not isinstance(faces, (list, tuple)):
            faces = [faces]
        best, best_d = None, None
        for face_dispatch in faces:
            face = binding.wrap(face_dispatch, self._mod.IFace2)
            surface = binding.wrap(face.GetSurface(), self._mod.ISurface)
            if surface is None or not surface.IsCylinder():
                continue
            box = face.GetBox()
            if not box or len(box) < 6:
                continue
            ccx = m_to_mm((box[0] + box[3]) / 2)
            ccy = m_to_mm((box[1] + box[4]) / 2)
            d = (ccx - cx_mm) ** 2 + (ccy - cy_mm) ** 2
            if best_d is None or d < best_d:
                best_d, best = d, face_dispatch
        if best is None or best_d > self._CYL_AXIS_TOLERANCE_MM ** 2:
            return None
        return best

    def add_circular_pattern(self, count: int, center_x_mm: float, center_y_mm: float,
                             feature_name: str | None = None) -> dict:
        """Repeat a feature `count` times evenly around 360 deg about an axis.

        The axis is the cylindrical face nearest (center_x_mm, center_y_mm) -- e.g.
        a centre hole drilled there. feature_name defaults to the last feature.
        A bolt circle: drill a centre hole + one bolt hole, then pattern the bolt
        hole. Selection marks: axis face = 1, seed feature = 4; the per-instance
        angle is 360/count degrees.
        """
        model = self._require_model()
        if count < 2:
            raise SolidWorksError(f"count must be >= 2 (got {count}).")

        body = self._solid_body()
        face = self._cylindrical_face_near(body, center_x_mm, center_y_mm)
        if face is None:
            raise SolidWorksError(
                f"No cylindrical face found at ({center_x_mm}, {center_y_mm}) to use as the axis. "
                "Drill a centre hole there first."
            )
        seed = feature_name or self._last_feature_name()
        selmgr = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        model.ClearSelection2(True)
        select_data = binding.wrap(selmgr.CreateSelectData(), self._mod.ISelectData)
        select_data.Mark = 1
        if not binding.wrap(face, self._mod.IEntity).Select4(True, select_data):
            raise SolidWorksError("Could not select the axis face.")
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        if not ext.SelectByID2(seed, "BODYFEATURE", 0.0, 0.0, 0.0, True, 4, None, 0):
            raise SolidWorksError(f"Could not select the seed feature '{seed}'.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        pattern = feat_mgr.FeatureCircularPattern(count, deg_to_rad(360.0) / count, False, "")
        if pattern is None:
            raise SolidWorksError("FeatureCircularPattern failed (None).")
        return self._finish_feature(pattern, "CircularPattern", instances=count,
                                    seed=seed, center_mm=[center_x_mm, center_y_mm])

    def _named_plane(self, plane: str):
        """A reference plane: 'front' / 'top' / 'right' (the default planes, in
        any template language) or the name of a plane in the tree, such as one a
        person made. Returns (plane, label)."""
        key = str(plane).lower()
        planes = self._ref_planes()
        if key in self._REF_PLANE_INDEX:
            return planes[self._REF_PLANE_INDEX[key]], f"{key} plane"
        named = [p for p in planes if p.Name == plane] or [p for p in planes if p.Name.lower() == key]
        if len(named) != 1:
            raise SolidWorksError(f"No plane '{plane}'. Use 'front', 'top', 'right' or a plane of the part: "
                                  f"{', '.join(p.Name for p in planes)}.")
        return named[0], f"plane '{named[0].Name}'"

    def _plane_at(self, plane: str, offset_mm: float):
        """The plane `plane` (see _named_plane), or a new plane parallel to it
        offset_mm along its normal (negative: the other way). Returns (plane, created)."""
        model = self._require_model()
        base, label = self._named_plane(plane)
        if offset_mm == 0:
            return base, False
        model.ClearSelection2(True)
        if not base.Select2(False, 0):
            raise SolidWorksError(f"Could not select the {label}.")
        constraint = SW_REF_PLANE_DISTANCE | (SW_REF_PLANE_FLIP if offset_mm < 0 else 0)
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        if feat_mgr.InsertRefPlane(constraint, mm_to_m(abs(offset_mm)), 0, 0.0, 0, 0.0) is None:
            raise SolidWorksError(f"Could not create a plane {offset_mm:g} mm from the {label}.")
        # InsertRefPlane's return is a generic dispatch without Select2; take the
        # new plane from the tree instead.
        return self._last_ref_plane(), True

    # The default planes that hold each model axis, and each default plane's normal.
    _AXIS_PLANES = {"x": ("front", "top"), "y": ("front", "right"), "z": ("top", "right")}
    _PLANE_NORMALS = {"front": (0.0, 0.0, 1.0), "top": (0.0, 1.0, 0.0), "right": (1.0, 0.0, 0.0)}

    def add_plane(self, base: str = "front", offset_mm: float = 0.0, angle_deg: float = 0.0,
                  about: str | None = None, name: str | None = None) -> dict:
        """A reference plane to build on, its position a dimension.

        Either `base` (front/top/right or a plane by name) moved offset_mm along
        its normal, or a default plane turned angle_deg about the model axis
        `about` that it holds (front holds x and y, top x and z, right y and z),
        by the right-hand rule. For both, turn first and offset that plane next.
        Returns the plane's name and how a sketch on it lies in the model:
        origin_mm, normal, x_axis and y_axis, so a point (u, v) on it is
        origin + u x_axis + v y_axis.
        """
        if bool(offset_mm) == bool(angle_deg):
            raise SolidWorksError("Give offset_mm or angle_deg (for both: turn first, then offset that plane).")
        key = axis = None
        if angle_deg:
            key, axis = str(base).lower(), str(about or "").lower()
            if axis not in self._AXIS_PLANES:
                raise SolidWorksError(f"about must be 'x', 'y' or 'z' (got {about!r}).")
            if key not in self._AXIS_PLANES[axis]:
                raise SolidWorksError(f"The {axis} axis lies in the {' and '.join(self._AXIS_PLANES[axis])} planes: "
                                      f"turn one of those about it (got '{base}').")
            if not 0.0 < abs(angle_deg) < 180.0:
                raise SolidWorksError(f"angle_deg must be between -180 and 180, not 0 (got {angle_deg}).")
        self._require_model()
        if angle_deg:
            plane = self._turned_plane(key, axis, angle_deg)
        else:
            plane, _ = self._plane_at(base, offset_mm)
        if name:
            plane.Name = name
            if plane.Name != name:
                raise SolidWorksError(f"The plane could not be named '{name}' (it is '{plane.Name}'): is the name taken?")
        dimension = self._first_dimension_name(plane)
        return {"ok": True, "plane": plane.Name, "dimensions": {"angle" if angle_deg else "offset": dimension},
                **self._plane_frame(plane)}

    def _turned_plane(self, base: str, axis: str, angle_deg: float):
        """The default plane `base` turned angle_deg about the model axis `axis`
        (right-hand rule). SolidWorks picks its own sense, so the normal is
        checked and the plane built the other way round when it is wrong."""
        model = self._model
        base_plane, _ = self._named_plane(base)
        expected = self._turned_normal(self._PLANE_NORMALS[base], axis, angle_deg)
        reference_axis = self._model_axis(axis)
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        for flip in (False, True):
            before = {f.Name for f in self._history()}
            model.ClearSelection2(True)
            if not (base_plane.Select2(False, 0) and reference_axis.Select2(True, 1)):
                raise SolidWorksError(f"Could not select the {base} plane and the {axis} axis.")
            constraint = SW_REF_PLANE_ANGLE | (SW_REF_PLANE_FLIP if flip else 0)
            if feat_mgr.InsertRefPlane(constraint, math.radians(abs(angle_deg)), SW_REF_PLANE_COINCIDENT,
                                       0.0, 0, 0.0) is None:
                raise SolidWorksError(f"Could not turn the {base} plane {angle_deg:g} degrees about {axis}.")
            plane = self._last_ref_plane()
            normal = self._plane_frame(plane)["normal"]
            if abs(sum(n * e for n, e in zip(normal, expected))) > 1 - 1e-6:
                return plane
            self._remove_features_since(before)  # turned the wrong way: build it the other way round
        raise SolidWorksError(f"SolidWorks did not turn the {base} plane {angle_deg:g} degrees about {axis}.")

    @staticmethod
    def _turned_normal(normal, axis: str, angle_deg: float) -> list:
        """`normal` turned angle_deg about the model axis `axis` by the right-hand
        rule; pure, unit-tested."""
        c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
        x, y, z = normal
        if axis == "x":
            return [x, c * y - s * z, s * y + c * z]
        if axis == "y":
            return [c * x + s * z, y, -s * x + c * z]
        return [c * x - s * y, s * x + c * y, z]

    def _model_axis(self, axis: str):
        """A reference axis along the model axis `axis`, where its two default
        planes meet; made once, named 'Axis X' etc., and hidden."""
        label = f"Axis {axis.upper()}"
        found = [f for f in self._history() if f.Name == label and f.GetTypeName2() == "RefAxis"]
        if found:
            return found[0]
        model = self._model
        first, second = (self._named_plane(key)[0] for key in self._AXIS_PLANES[axis])
        model.ClearSelection2(True)
        if not (first.Select2(False, 0) and second.Select2(True, 0)) or not model.InsertAxis2(True):
            raise SolidWorksError(f"Could not make a reference axis along {axis}.")
        made = [f for f in self._history() if f.GetTypeName2() == "RefAxis"][-1]
        made.Name = label
        if made.Select2(False, 0):
            model.BlankRefGeom()  # construction geometry; keep screenshots clean
        model.ClearSelection2(True)
        return made

    def add_sketch(self, plane: str, start_mm: list, segments: list, name: str | None = None) -> dict:
        """Draw a chain of lines, arcs and splines on a plane, fully defined (see
        free_sketch): smooth joints get tangent relations, the rest dimensions
        from the origin. Returns the sketch's name for extrude_sketch / cut_sketch,
        its dimensions, the points given (x<i>/y<i> number them) and the plane's
        frame: [u, v] lies at origin_mm + u x_axis + v y_axis."""
        model = self._require_model()
        self._require_part()
        chain = free_sketch.build_chain(start_mm, segments)
        plan = free_sketch.plan_chain(chain)
        if not plan.fully_defined:
            raise SolidWorksError("Could not work out the relations and dimensions for this sketch.")
        ref, label = self._named_plane(plane)
        frame = self._plane_frame(ref)
        model.ClearSelection2(True)
        if not ref.Select2(False, 0):
            raise SolidWorksError(f"Could not select the {label}.")
        before = self._profile_feature_names()
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        try:
            drawn = self._draw_chain(sk, chain)
            sketch, definer = self._open_sketch_definer(sk)
            dimensions = definer.define_chain(plan, drawn, self._chain_sketch_points(drawn, chain))
            fully_defined = definer.fully_defined()
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)
        new_names = self._profile_feature_names() - before
        if len(new_names) != 1:
            raise SolidWorksError(f"Could not identify the sketch just drawn (found {len(new_names)} new sketches).")
        sketch_name = new_names.pop()
        if name:
            feature = self._history_feature(sketch_name)
            feature.Name = name
            sketch_name = feature.Name
            dimensions = {role: f"{dim.split('@')[0]}@{sketch_name}" for role, dim in dimensions.items()}
        if not fully_defined:
            raise SolidWorksError(f"{sketch_name} came out under- or over-defined; this is a bug, please report it.")
        return {"ok": True, "sketch": sketch_name, "closed": chain.closed,
                "points_mm": [[round(c, 6) for c in chain.points[i]] for i in chain.user_points],
                "dimensions": dimensions, "fully_defined": True,
                "origin_mm": frame["origin_mm"], "x_axis": frame["x_axis"], "y_axis": frame["y_axis"]}

    @staticmethod
    def _draw_chain(sk, chain) -> list:
        """Draw the chain's segments in the open sketch, with AddToDB so that no
        automatic relation moves them; consecutive segments share their points."""
        drawn = []
        sk.AddToDB = True
        try:
            for k, entity in enumerate(chain.entities):
                if entity[0] == "line":
                    (u1, v1), (u2, v2) = chain.points[entity[1]], chain.points[entity[2]]
                    segment = sk.CreateLine(mm_to_m(u1), mm_to_m(v1), 0.0, mm_to_m(u2), mm_to_m(v2), 0.0)
                elif entity[0] == "arc":
                    (su, sv), (eu, ev), (cu, cv) = (chain.points[i] for i in entity[1:4])
                    segment = sk.CreateArc(mm_to_m(cu), mm_to_m(cv), 0.0, mm_to_m(su), mm_to_m(sv), 0.0,
                                           mm_to_m(eu), mm_to_m(ev), 0.0, entity[4])
                else:
                    coords = [mm_to_m(c) for i in entity[1] for c in (*chain.points[i], 0.0)]
                    segment = sk.CreateSpline2(win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, coords),
                                               False)
                if not segment:
                    raise SolidWorksError(f"SolidWorks could not draw segment {k} ({entity[0]}).")
                drawn.append(segment)
        finally:
            sk.AddToDB = False
        return drawn

    def _chain_sketch_points(self, drawn, chain) -> dict:
        """chain point -> the sketch points SolidWorks made there, matched by position."""
        found = {}
        for entity, segment in zip(chain.entities, drawn):
            if entity[0] == "line":
                line = binding.wrap(segment, self._mod.ISketchLine)
                candidates = [line.GetStartPoint2(), line.GetEndPoint2()]
            elif entity[0] == "arc":
                arc = binding.wrap(segment, self._mod.ISketchArc)
                candidates = [arc.GetStartPoint2(), arc.GetEndPoint2(), arc.GetCenterPoint2()]
            else:
                candidates = list(binding.wrap(segment, self._mod.ISketchSpline).GetPoints2() or ())
            for candidate in candidates:
                point = binding.wrap(candidate, self._mod.ISketchPoint)
                at = (m_to_mm(point.X), m_to_mm(point.Y))
                index = next((i for i, p in enumerate(chain.points) if math.dist(p, at) <= 1e-6), None)
                if index is None:
                    raise SolidWorksError(f"SolidWorks drew a point at ({at[0]:g}, {at[1]:g}) that the sketch does not have.")
                twins = found.setdefault(index, [])
                if all(tuple(t.GetID()) != tuple(point.GetID()) for t in twins):
                    twins.append(point)
        missing = [chain.points[i] for i in range(len(chain.points)) if i not in found]
        if missing:
            raise SolidWorksError(f"SolidWorks did not draw the points {missing}.")
        return found

    def _plane_frame(self, plane) -> dict:
        """How a sketch on `plane` lies in the model: origin (mm), normal and the
        sketch's x and y axes as unit vectors."""
        model = self._model
        model.ClearSelection2(True)
        if not plane.Select2(False, 0):
            raise SolidWorksError(f"Could not select '{plane.Name}'.")
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sketch = self._open_face_sketch(sk, plane.Name)
        try:
            to_model = binding.wrap(binding.wrap(sketch.ModelToSketchTransform, self._mod.IMathTransform).Inverse(),
                                    self._mod.IMathTransform)
            origin = self._to_model_mm(to_model, 0.0, 0.0, 0.0)
            # a metre along each sketch axis keeps the rounded millimetres precise
            axes = [[round((c - o) / 1000.0, 6) + 0.0 for c, o in zip(self._to_model_mm(to_model, *unit), origin)]
                    for unit in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]
        finally:
            model.ClearSelection2(True)
            sk.InsertSketch(True)  # an empty sketch is dropped again
        return {"origin_mm": origin, "x_axis": axes[0], "y_axis": axes[1], "normal": axes[2]}

    def run_guarded(self, fn, *args, **kwargs):
        """Run a tool; when it fails, delete what it added to the current part.

        Every MCP tool call goes through here, so a refused or failed call leaves
        the part as it was: no stray sketch, which would also count in the
        bounding box. A tool that switched documents is not rolled back.
        """
        self._reattach_if_gone()
        snapshot = self._history_snapshot()
        busy = self._command_in_progress(True)
        try:
            with self._quiet_ui():
                return fn(*args, **kwargs)
        except SolidWorksError as exc:
            left = self._roll_back(snapshot)
            if left:
                raise SolidWorksError(f"{exc} Could not remove what the call added: {', '.join(left)}.") from exc
            raise
        except Exception:
            self._roll_back(snapshot)
            raise
        finally:
            self._command_in_progress(busy)

    # A link into a SolidWorks that is gone (restarted or closed): RPC server
    # unavailable, RPC call failed, object disconnected from its clients
    _GONE_HRESULTS = {-2147023174, -2147023170, -2147417848}

    def _reattach_if_gone(self) -> None:
        """Attach to the running SolidWorks when the one this server held was
        restarted: the old link answers every call with an RPC error. The
        current document went with the old SolidWorks."""
        if self._sw is None:
            return
        try:
            self._sw.CommandInProgress
        except pythoncom.com_error as exc:
            if exc.hresult not in self._GONE_HRESULTS:
                raise
            self._sw = self._model = None
            self.connect()

    @contextlib.contextmanager
    def _quiet_ui(self):
        """Keep SolidWorks' window still while tools work on its documents.

        SOLIDWORKS 2026 SP4.0 crashed at random while the API sketched: an
        access violation in slduiu.dll, in its window message loop, also with
        CommandInProgress set. With the view, the feature tree and new sketch
        entities left undrawn it did not (6 crashes in 7 runs before, none in
        15 calls after). Everything is switched on and redrawn afterwards.
        """
        self._quiet_depth += 1
        try:
            yield
        finally:
            self._quiet_depth -= 1
            if not self._quiet_depth:
                self._wake_all()

    def _quiet(self, model) -> None:
        """Stop `model`'s view, feature tree and sketcher from redrawing, noting
        what was on so _wake_all switches just that back on."""
        view = binding.wrap(model.ActiveView, self._mod.IModelView)
        features = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        sketches = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        switched = []
        for target, prop in ((view, "EnableGraphicsUpdate"), (features, "EnableFeatureTree"),
                             (features, "EnableFeatureTreeWindow"), (sketches, "DisplayWhenAdded")):
            if target is not None and getattr(target, prop):
                setattr(target, prop, False)
                switched.append((target, prop))
        if switched:
            self._to_wake.append((model, switched))

    def _wake_all(self) -> None:
        to_wake, self._to_wake = self._to_wake, []
        for model, switched in to_wake:
            try:
                for target, prop in switched:
                    setattr(target, prop, True)
                model.GraphicsRedraw2()
            except pythoncom.com_error:
                pass  # the document was closed during the call: nothing left to show

    def _command_in_progress(self, flag):
        """Set ISldWorks.CommandInProgress; return what it was when this changed
        it, else None, which leaves it alone when passed back. Set, SolidWorks
        stops redrawing between the calls of one command: COM calls ran 100
        times faster (144 edges read in 0.01 s, not 1.5 s; verified). It counts,
        every True needing its own False (verified), so it is only set when it
        is not already, and only that change is undone."""
        if self._sw is None or flag is None:
            return None
        before = bool(self._sw.CommandInProgress)
        if before == bool(flag):
            return None
        self._sw.CommandInProgress = bool(flag)
        return before

    def _history_snapshot(self):
        """(title, history names) of the current part, or None without one."""
        if self._model is None or int(self._model.GetType()) != SW_DOC_PART:
            return None
        return self._model.GetTitle(), {f.Name for f in self._history()}

    def _roll_back(self, snapshot) -> list:
        if snapshot is None or self._model is None or self._model.GetTitle() != snapshot[0]:
            return []
        return self._remove_features_since(snapshot[1])

    def _remove_features_since(self, before: set) -> list:
        """Delete what the history gained since `before`, newest first; returns
        the names that could not be removed."""
        model = self._require_model()
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        for feat in reversed([f for f in self._history() if f.Name not in before]):
            model.ClearSelection2(True)
            if feat.Select2(False, 0):
                extension.DeleteSelection2(SW_DELETE_ABSORBED)
        model.ForceRebuild3(False)
        return [f.Name for f in self._history() if f.Name not in before]

    _MIRROR_AXES = {"front": "z", "top": "y", "right": "x"}

    def add_mirror(self, plane: str, offset_mm: float = 0.0, features: list | None = None,
                   name: str = "Mirror") -> dict:
        """Mirror features, or the whole body, about a plane moved offset_mm.

        plane 'front' / 'top' / 'right' mirrors about z / y / x = offset_mm; the
        name of another plane in the part (e.g. 'Plane1') mirrors about that
        plane moved offset_mm along its normal.
        features are list_features names; their copies follow them, so changing
        a seed's dimension changes its copy too. Without features the body is
        mirrored and merged: model half of a symmetric part and mirror it about
        the face where the halves meet. The plane's position comes back as the
        dimension `plane_offset`. SolidWorks quietly builds a mirror whose copy
        lands outside the part or on its seed; that fails here instead, and the
        part stays as it was.
        """
        if features is not None and not features:
            raise SolidWorksError("features is empty: name the features to mirror, or leave it out to mirror the body.")
        model = self._require_model()
        _, label = self._named_plane(plane)
        key = str(plane).lower()
        where = (f"{self._MIRROR_AXES[key]} = {offset_mm:g}" if key in self._MIRROR_AXES
                 else f"{label} + {offset_mm:g} mm")
        seeds = [self._history_feature(n) for n in features or ()]
        mirror_plane, created = self._plane_at(plane, offset_mm)
        model.ClearSelection2(True)
        if seeds:
            for seed in seeds:
                if not seed.Select2(True, SW_MARK_MIRROR_FEATURE):
                    raise SolidWorksError(f"Could not select '{seed.Name}'.")
        else:
            select_data = binding.wrap(binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
                                       .CreateSelectData(), self._mod.ISelectData)
            select_data.Mark = SW_MARK_MIRROR_BODY
            if not self._solid_body().Select2(False, select_data):
                raise SolidWorksError("Could not select the body.")
        if not mirror_plane.Select2(True, SW_MARK_MIRROR_PLANE):
            raise SolidWorksError(f"Could not select the mirror plane at {where}.")
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        mirror = binding.wrap(feat_mgr.InsertMirrorFeature2(not seeds, False, not seeds, False,
                                                            SW_FEATURE_SCOPE_ALL_BODIES), self._mod.IFeature)
        model.ForceRebuild3(False)
        problem = self._mirror_problem(mirror, bool(seeds), where)
        if problem:
            raise SolidWorksError(problem)  # run_guarded removes the mirror and its plane
        if created and mirror_plane.Select2(False, 0):
            model.BlankRefGeom()  # construction geometry; keep screenshots clean
        model.ClearSelection2(True)
        dimensions = {"plane_offset": self._first_dimension_name(mirror_plane)} if created else {}
        return self._finish_feature(mirror, name, dimensions=dimensions, fully_defined=True)

    @staticmethod
    def _mirror_problem(mirror, of_features: bool, where: str) -> str | None:
        """Why the mirror just built is no good, or None."""
        if mirror is None:
            if of_features:
                return f"SolidWorks built no mirror about {where}: mirror features, not sketches."
            return (f"SolidWorks built no mirror about {where}: the mirrored body must touch the original. "
                    "Put the plane on the face where the halves meet.")
        code, warning = mirror.GetErrorCode2()
        if code:
            return (f"SolidWorks flags the mirror about {where} ({'warning' if warning else 'error'} {code}): "
                    "a copy lands outside the part, or on top of its seed (a seed on the plane). "
                    "Put the plane where every copy lands in material.")
        return None

    # --- parametric edit ------------------------------------------------------

    def set_dimension(self, dimension_name: str, value_mm: float) -> dict:
        """Set a named driving dimension (e.g. 'D1@BlockExtrude'), rebuild, remeasure.

        value_mm is in degrees for an angle (a revolve, an angle mate); the
        result's keys then end in _deg. A point's coordinate (a sketch's 'x3'
        or 'y2', from the origin) takes a sign, -5 being the other side of
        the origin; any other dimension is a size and cannot be negative.
        A value SolidWorks does not take is refused, the part left as it was.
        """
        self._require_model()
        dim = self._dimension(dimension_name)
        unit, to_system, from_system = self._dimension_unit(dim)
        coordinate = self._coordinate_of(dimension_name)
        if coordinate is None and unit == "mm" and value_mm < 0:
            raise SolidWorksError(f"'{dimension_name}' is a size and cannot be negative (got {value_mm:g}); only a "
                                  "point's coordinate from the origin takes a sign. The part is as it was.")

        def current() -> float:
            return from_system(dim.SystemValue) if coordinate is None else self._coordinate_of(dimension_name)()

        old, original = current(), dim.SystemValue
        target = self._joint_angle(dim, unit, value_mm) if coordinate is None else value_mm
        # A coordinate is a distance to SolidWorks: a negative value puts the
        # point on the other side, a positive one keeps the side it is on.
        crosses = coordinate is not None and (value_mm < 0) != (old < 0)
        rebuilt_ok = self._write_dimension(dim, to_system(-abs(value_mm) if crosses else abs(value_mm) if coordinate
                                                          else target))
        # A driven or equation-controlled dimension, or a value SolidWorks keeps
        # out (a width of 0), ignores the write without a word, yet a width of 0
        # left its sketch 'invalid solution': write the old value back.
        applied = current()
        if abs(applied - target) >= 1e-6:
            self._write_dimension(dim, original)
            if abs(current() - old) >= 1e-6:
                raise SolidWorksError(f"'{dimension_name}' did not take {value_mm:g} {unit}, and putting back "
                                      f"{old:g} failed: it reads {current():g}. Check the part.")
            raise SolidWorksError(f"'{dimension_name}' stays at {old:g} {unit}: SolidWorks did not take "
                                  f"{value_mm:g}, as {self._why_not_taken(dimension_name, dim)}. The part is as it was.")
        return {
            "ok": True,
            "dimension": dimension_name,
            f"old_value_{unit}": round(old, 6),
            f"requested_value_{unit}": value_mm,
            f"new_value_{unit}": round(applied + value_mm - target, 6),  # -30, not the 330 SolidWorks holds
            "applied": True,
            "rebuild_ok": rebuilt_ok,
            "mass_properties": self.get_mass_properties()["mass_properties"],
        }

    def _write_dimension(self, dim, system_value: float) -> bool:
        """Write a dimension's SystemValue and rebuild; returns rebuild_ok.

        SolidWorks now and then ignores a write at random (3 in 80 on a fresh
        box, measured): the old value reads back, and the same write once more
        takes. A value it refuses stays refused, for the caller to report. A
        coordinate written negative reads back positive, so sizes are compared.
        """
        for _ in range(2):
            dim.SystemValue = system_value
            rebuilt_ok = bool(self._model.ForceRebuild3(False))
            if abs(abs(dim.SystemValue) - abs(system_value)) < 1e-9:
                break
        return rebuilt_ok

    def _joint_angle(self, dim, unit: str, value: float) -> float:
        """The value to write for `value`: an angle mate's dimension turns its
        joint the whole way round, and SolidWorks takes 0..360 for it, so -30
        is written as 330 (the same turn); any other dimension as it is."""
        if unit != "deg":
            return value
        owner = binding.wrap(dim.GetFeatureOwner(), self._mod.IFeature)
        return value % 360 if owner is not None and owner.GetTypeName2().startswith("Mate") else value

    def _why_not_taken(self, dimension_name: str, dim) -> str:
        """Why SolidWorks kept a dimension at its value."""
        equation = next((e["equation"] for e in self._equation_entries()
                         if (e["name"] or "").lower() == dimension_name.lower()), None)
        if equation:
            return f"it follows the equation {equation}: change that with set_equation, or delete_equation"
        if dim.DrivenState != SW_DIMENSION_DRIVING:
            return "it is a reference (driven) dimension, which follows the geometry"
        return "it refuses such a value (a size of 0, a negative angle)"

    def _coordinate_of(self, dimension_name: str):
        """For a sketch point's horizontal or vertical distance from the origin
        (an external sketch point), a function giving the point's coordinate
        along it in mm, with its sign; None for any other dimension."""
        dim_name, _, rest = dimension_name.partition("@")
        if not rest or int(self._model.GetType()) != SW_DOC_PART:
            return None
        sketch_name = rest.split("@")[0]
        feature = next((candidate for feat in self._iter_features() for candidate in [feat, *self._sub_features(feat)]
                        if candidate.Name == sketch_name and candidate.GetTypeName2() == "ProfileFeature"), None)
        if feature is None:
            return None
        display = binding.wrap(feature.GetFirstDisplayDimension(), self._mod.IDisplayDimension)
        while display is not None and binding.wrap(display.GetDimension2(0), self._mod.IDimension).Name != dim_name:
            display = binding.wrap(feature.GetNextDisplayDimension(display), self._mod.IDisplayDimension)
        if display is None or display.Type2 not in (SW_HOR_LINEAR_DIMENSION, SW_VERT_LINEAR_DIMENSION):
            return None
        annotation = binding.wrap(display.GetAnnotation(), self._mod.IAnnotation)
        attached = list(zip(annotation.GetAttachedEntities3() or (), annotation.GetAttachedEntityTypes() or ()))
        points = [binding.wrap(e, self._mod.ISketchPoint) for e, t in attached if t == SW_SEL_SKETCH_POINTS]
        origins = [binding.wrap(e, self._mod.ISketchPoint) for e, t in attached if t == SW_SEL_EXT_SKETCH_POINTS]
        if len(points) != 1 or len(origins) != 1:
            return None
        axis = "X" if display.Type2 == SW_HOR_LINEAR_DIMENSION else "Y"
        return lambda: m_to_mm(getattr(points[0], axis) - getattr(origins[0], axis))

    def _dimension(self, dimension_name: str):
        dim = binding.wrap(self._require_model().Parameter(dimension_name), self._mod.IDimension)
        if dim is None:
            raise SolidWorksError(
                f"Dimension '{dimension_name}' not found. "
                "Use the 'D1@<feature>' notation."
            )
        return dim

    @staticmethod
    def _dimension_unit(dim):
        """('deg', to radians, from radians) for an angle, else ('mm', to m, from m).
        An angle written as millimetres would land as a thousandth of a radian."""
        if dim.GetType() == SW_DIMENSION_PARAM_ANGULAR:
            return "deg", math.radians, math.degrees
        return "mm", mm_to_m, m_to_mm

    def set_equation(self, equation: str) -> dict:
        """Add an equation, or replace the one that sets the same name, then
        rebuild and remeasure (see set_equations).

        equation is a SolidWorks equation string, e.g.
        '"D1@BlockExtrude" = 25' or '"D1@BlockExtrude" = 2 * "D1@Sketch1"'.
        Unlike set_dimension (a one-off value), this persists a relation in the
        model. Returns the resulting mass properties.
        """
        result = self.set_equations([equation])
        [done] = result.pop("equations")
        return {"ok": True, **done, **result}

    def set_equations(self, equations: list) -> dict:
        """Add or replace equations, then rebuild once. One that sets a name
        already set (a global variable in any case, or a dimension) replaces it
        in place. A refused equation undoes the whole list. SolidWorks solves
        them in the order they need, not as listed: a variable added below the
        equations that use it would otherwise take two rebuilds and warn.
        Returns per equation its index and whether it replaced one, the features
        that fail to rebuild, and the mass properties."""
        model = self._require_model()
        if isinstance(equations, str) or not equations:
            raise SolidWorksError('set_equations takes a list of equations, such as [\'"L" = 85\'].')
        names = [self._equation_lhs(text) for text in equations]  # every one checked before any change
        eqmgr = self._equation_mgr()
        eqmgr.AutomaticSolveOrder = True
        journal, done = [], []
        try:
            for text, name in zip(equations, names):
                index = self._equation_index(eqmgr, name)
                if index is None:
                    index = eqmgr.Add2(eqmgr.GetCount(), text, False)  # solved by the rebuild below
                    if index < 0:
                        raise SolidWorksError(f"SolidWorks refused {text}: check the expression and the names in it.")
                    journal.append((index, None))
                else:
                    before = eqmgr.Equation(index)
                    eqmgr.SetEquation(index, text)
                    if not self._same_equation(eqmgr.Equation(index), text):  # a refusal keeps the old text, silently
                        raise SolidWorksError(f"SolidWorks refused {text}: check the expression and the names in it.")
                    journal.append((index, before))
                done.append({"equation": text, "index": index, "replaced": journal[-1][1] is not None})
        except SolidWorksError:
            for index, before in reversed(journal):
                if before is None:
                    eqmgr.Delete(index)
                else:
                    eqmgr.SetEquation(index, before)
            model.ForceRebuild3(False)
            raise
        rebuilt_ok = bool(model.ForceRebuild3(False))
        return {"ok": True, "equations": done, "rebuild_ok": rebuilt_ok,
                "failing_features": self._failing_features(),
                "mass_properties": self.get_mass_properties()["mass_properties"]}

    def list_equations(self) -> dict:
        """The equations and global variables in order: index, equation, the name
        it sets, its value and whether it is a global variable. `broken` marks
        one that names a dimension or variable that is gone, as one left behind
        by delete_feature. automatic_solve_order False: SolidWorks solves them
        as listed, so one above the line that sets its variable warns and lags
        a rebuild; any set_equation turns it on."""
        out = self._equation_entries()
        return {"ok": True, "count": len(out), "equations": out,
                "automatic_solve_order": bool(self._equation_mgr().AutomaticSolveOrder)}

    def _equation_entries(self) -> list:
        model = self._require_model()
        eqmgr = self._equation_mgr()
        texts = [eqmgr.Equation(i) for i in range(eqmgr.GetCount())]
        variables = {self._equation_names(t)[0].lower() for i, t in enumerate(texts)
                     if eqmgr.GlobalVariable(i) and self._equation_names(t)}
        out = []
        for i, text in enumerate(texts):
            names = self._equation_names(text)
            out.append({"index": i, "equation": text, "name": names[0] if names else None,
                        "value": round(eqmgr.Value(i), 6), "global": bool(eqmgr.GlobalVariable(i)),
                        "broken": any(n.lower() not in variables and model.Parameter(n) is None for n in names)})
        return out

    def delete_equation(self, equation) -> dict:
        """Delete an equation by the name it sets ('L_thigh', 'D1@Boss') or by its
        list_equations index, then rebuild and remeasure."""
        model = self._require_model()
        eqmgr = self._equation_mgr()
        count = eqmgr.GetCount()
        key = str(equation).strip()
        if key.isdigit():
            index = int(key)
            if index >= count:
                raise SolidWorksError(f"No equation {index}: there are {count} (0..{count - 1}).")
        else:
            index = self._equation_index(eqmgr, key.strip('"'))
            if index is None:
                listed = ", ".join(eqmgr.Equation(i) for i in range(count)) or "none"
                raise SolidWorksError(f"No equation sets '{key}' (there are: {listed}).")
        text = eqmgr.Equation(index)
        if eqmgr.Delete(index) < 0:
            raise SolidWorksError(f"SolidWorks refused to delete {text}.")
        rebuilt_ok = bool(model.ForceRebuild3(False))
        return {"ok": True, "deleted": text, "count": eqmgr.GetCount(), "rebuild_ok": rebuilt_ok,
                "failing_features": self._failing_features(),
                "mass_properties": self.get_mass_properties()["mass_properties"]}

    def _equation_mgr(self):
        eqmgr = binding.wrap(self._require_model().GetEquationMgr(), self._mod.IEquationMgr)
        if eqmgr is None:
            raise SolidWorksError("No EquationManager available.")
        return eqmgr

    def _equation_index(self, eqmgr, name: str):
        """The index of the equation that sets `name` (any case), or None."""
        for i in range(eqmgr.GetCount()):
            names = self._equation_names(eqmgr.Equation(i))
            if names and names[0].lower() == name.lower():
                return i
        return None

    @staticmethod
    def _equation_lhs(equation) -> str:
        """The name an equation sets: '"L" = 85' -> 'L'; pure, unit-tested."""
        match = re.match(r'\s*"([^"]+)"\s*=', str(equation))
        if not match:
            raise SolidWorksError(f'An equation is "name" = expression, the name in double quotes (got {equation!r}).')
        return match.group(1)

    @staticmethod
    def _equation_names(equation: str) -> list:
        """Every quoted name in an equation, the one it sets first; pure, unit-tested."""
        return re.findall(r'"([^"]+)"', str(equation))

    @staticmethod
    def _same_equation(a: str, b: str) -> bool:
        return re.sub(r"\s+", "", a).lower() == re.sub(r"\s+", "", b).lower()

    @staticmethod
    def _error_entry(code, warning) -> dict:
        """A feature's error code with what it means, where known; pure."""
        return {"code": code, "warning": bool(warning), "cause": FEATURE_ERRORS.get(code, f"SolidWorks error {code}")}

    def _failing_features(self) -> list:
        """The features that fail to rebuild (errors, not warnings), in tree order."""
        failing = []
        for feat in self._iter_features():
            code, warning = feat.GetErrorCode2()
            if code and not warning:
                failing.append({"name": feat.Name, "type": feat.GetTypeName2(), **self._error_entry(code, warning)})
        return failing

    def list_dimensions(self) -> dict:
        """Every dimension in the part, feature by feature, as set_dimension takes it.

        For a part opened from disk, or after the tool results that named them
        are gone. Values are mm, angles degrees; `driving` is False for a
        reference (driven) dimension, which set_dimension cannot change.
        """
        self._require_part()
        dims, seen = [], set()
        for feat in self._iter_features():
            for entry in self._dimension_entries(feat):
                if entry["name"] not in seen:
                    seen.add(entry["name"])
                    dims.append(entry)
        return {"ok": True, "count": len(dims), "dimensions": dims}

    def _dimension_entries(self, feat) -> list:
        """The feature's dimensions: name (as set_dimension takes it), value in
        mm or degrees, and whether it is driving."""
        entries = []
        display = feat.GetFirstDisplayDimension()
        while display is not None:
            shown = binding.wrap(display, self._mod.IDisplayDimension)
            dim = binding.wrap(shown.GetDimension2(0), self._mod.IDimension)
            angular = shown.GetType() == SW_ANGULAR_DIMENSION
            value = dim.SystemValue
            entries.append({
                "name": dim.GetNameForSelection(),
                "feature": feat.Name,
                "value": round(math.degrees(value) if angular else m_to_mm(value), 6),
                "unit": "deg" if angular else "mm",
                "driving": dim.DrivenState == SW_DIMENSION_DRIVING,
            })
            display = feat.GetNextDisplayDimension(display)
        return entries

    # --- a person's sketches: read, extrude, cut ---------------------------------

    def _sketch_by_name(self, name: str):
        """A sketch of the current part by name, also one absorbed by a feature."""
        self._require_part()
        sketches = [candidate for feat in self._iter_features() for candidate in [feat, *self._sub_features(feat)]
                    if candidate.GetTypeName2() == "ProfileFeature"]
        for sketch in sketches:
            if sketch.Name == name:
                return sketch
        names = sorted({s.Name for s in sketches})
        raise SolidWorksError(f"No sketch '{name}' in the part (there are: {', '.join(names) or 'none'}).")

    def read_sketch(self, name: str) -> dict:
        """A sketch of the current part read back in MODEL coordinates (mm): its
        lines, arcs, circles and splines (construction ones marked), its
        dimensions, and whether it is fully defined. For checking a design
        against fixed points before building on it.
        """
        feature = self._sketch_by_name(name)
        sketch = binding.wrap(feature.GetSpecificFeature2(), self._mod.ISketch)
        to_model = binding.wrap(binding.wrap(sketch.ModelToSketchTransform, self._mod.IMathTransform).Inverse(),
                                self._mod.IMathTransform)
        segments = [self._segment_entry(s, to_model) for s in sketch.GetSketchSegments() or ()]
        status = sketch.GetConstrainedStatus()
        return {"ok": True, "name": feature.Name, "fully_defined": status == SW_FULLY_CONSTRAINED,
                "status": SKETCH_STATUSES.get(status, f"status {status}"), "segments": segments,
                "dimensions": self._dimension_entries(feature)}

    def _segment_entry(self, segment_dispatch, to_model) -> dict:
        segment = binding.wrap(segment_dispatch, self._mod.ISketchSegment)
        kind = segment.GetType()
        entry = {"construction": bool(segment.ConstructionGeometry)}

        def point(dispatch):
            p = binding.wrap(dispatch, self._mod.ISketchPoint)
            return self._to_model_mm(to_model, p.X, p.Y, p.Z)

        if kind == SW_SKETCH_LINE:
            line = binding.wrap(segment_dispatch, self._mod.ISketchLine)
            entry.update(type="line", start_mm=point(line.GetStartPoint2()), end_mm=point(line.GetEndPoint2()))
        elif kind == SW_SKETCH_ARC:
            arc = binding.wrap(segment_dispatch, self._mod.ISketchArc)
            circle = bool(arc.IsCircle())
            entry.update(type="circle" if circle else "arc", centre_mm=point(arc.GetCenterPoint2()),
                         radius_mm=round(m_to_mm(arc.GetRadius()), 4))
            if not circle:
                entry.update(start_mm=point(arc.GetStartPoint2()), end_mm=point(arc.GetEndPoint2()))
        else:
            names = {SW_SKETCH_SPLINE: "spline", SW_SKETCH_TEXT: "text"}
            entry.update(type=names.get(kind, f"segment type {kind}"), length_mm=round(m_to_mm(segment.GetLength()), 4))
        return entry

    def _to_model_mm(self, transform, x_m, y_m, z_m) -> list:
        mathutil = binding.wrap(self._sw.GetMathUtility(), self._mod.IMathUtility)
        coords = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x_m, y_m, z_m])
        moved = binding.wrap(binding.wrap(mathutil.CreatePoint(coords), self._mod.IMathPoint).MultiplyTransform(transform),
                             self._mod.IMathPoint)
        return [round(m_to_mm(c), 4) + 0.0 for c in moved.ArrayData]

    def extrude_sketch(self, sketch: str, depth_mm: float | None = None, reverse: bool = False,
                       name: str = "Extrude", up_to: str | None = None) -> dict:
        """Extrude an existing sketch of the current part, by name (one a person
        drew), depth_mm along its normal (reverse=True: the other way), merged
        with the body; or with up_to='next' up to the next face of the part,
        ending on its shape, such as a post into a curved wall, and following
        it when the wall changes; or with up_to='@x,y,z' up to the face through
        that point. The sketch's own dimensions come back."""
        if depth_mm is not None and up_to is not None:
            raise SolidWorksError("Give depth_mm or up_to, not both.")
        if depth_mm is None and up_to is None:
            raise SolidWorksError("extrude_sketch needs depth_mm or up_to='next'.")
        if up_to is not None and up_to != "next" and not str(up_to).strip().startswith("@"):
            raise SolidWorksError(f"Unknown up_to '{up_to}'. Use 'next': up to the next face of the part, or "
                                  "'@x,y,z': up to the face through that point.")
        if depth_mm is not None and depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        end_face = self._part_face_at(self._point_selector(up_to)) if up_to not in (None, "next") else None
        defined = self._select_person_sketch(sketch)
        hint = f"Is '{sketch}' a closed profile?" if up_to is None else (
            f"Is '{sketch}' a closed profile with {'that face' if end_face else 'a face of the part'} ahead of it"
            f"{'' if reverse else ' (or behind it: reverse=True)'}?")
        try:
            return self._extrude_sketch(depth_mm, name, defined, hint, reverse=reverse, end_face=end_face)
        except SolidWorksError as exc:
            if up_to != "next":
                raise
            ahead = self._face_ahead(sketch, reverse)
            if ahead is None:
                raise
            # a side of the profile on a face of the part (a strip against a boss) fails 'next' alone
            raise SolidWorksError(f"{exc} Up to next also fails when a side of the profile lies on a face of the "
                                  f"part. The face ahead of the profile's middle: up_to='@{ahead}' ends on it.") from exc

    def _face_ahead(self, sketch_name: str, reverse: bool) -> str | None:
        """Where a ray from the middle of the sketch, along its extrusion,
        first meets the part: 'x,y,z' in mm for up_to, or None."""
        sketch = binding.wrap(self._sketch_by_name(sketch_name).GetSpecificFeature2(), self._mod.ISketch)
        to_model = binding.wrap(binding.wrap(sketch.ModelToSketchTransform, self._mod.IMathTransform).Inverse(),
                                self._mod.IMathTransform)
        origin = self._to_model_mm(to_model, 0.0, 0.0, 0.0)
        normal = [(c - o) * (-1 if reverse else 1) for c, o in zip(self._to_model_mm(to_model, 0.0, 0.0, 0.001), origin)]
        corners = [s["start_mm"] for s in self.read_sketch(sketch_name)["segments"]
                   if "start_mm" in s and not s["construction"]]
        if not corners:
            return None
        middle = [sum(c[i] for c in corners) / len(corners) for i in range(3)]
        model = self._model
        model.ClearSelection2(True)
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        try:
            if not extension.SelectByRay(*(mm_to_m(c) for c in middle), *normal, mm_to_m(self._ON_FACE_TOLERANCE_MM),
                                         SW_SEL_FACES, False, 0, 0):
                return None
            hit = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr).GetSelectionPoint2(1, -1)
            return ",".join(f"{m_to_mm(c):.3f}" for c in hit[:3])  # well within the 0.01 mm a face is found by
        finally:
            model.ClearSelection2(True)

    def _part_face_at(self, point_mm: list):
        """The current part's face through point_mm."""
        def gap(face) -> float:
            nearest = face.GetClosestPointOn(*(mm_to_m(c) for c in point_mm))
            return math.dist(point_mm, [m_to_mm(c) for c in nearest[:3]])

        faces = [binding.wrap(f, self._mod.IFace2) for body in self._part_bodies() for f in body.GetFaces() or ()]
        face = min(faces, key=gap)
        if gap(face) > self._ON_COMPONENT_FACE_MM:
            raise SolidWorksError(f"No face of the part goes through ({', '.join(f'{c:g}' for c in point_mm)}): "
                                  f"the nearest lies {gap(face):.3g} mm away.")
        return face

    def cut_sketch(self, sketch: str, depth_mm: float | None = None, reverse: bool = False,
                   name: str = "Cut") -> dict:
        """Cut an existing sketch of the current part, by name, into the body:
        depth_mm deep, or through all when depth_mm is None, against the
        sketch's normal (FeatureCut4's own direction, verified): into the part
        from a face. reverse=True cuts along the normal, as needed from a plane
        with the part in front of it."""
        if depth_mm is not None and depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        model = self._require_model()
        defined = self._select_person_sketch(sketch)
        t1, d1 = (SW_END_COND_THROUGH_ALL, 0.0) if depth_mm is None else (SW_END_COND_BLIND, mm_to_m(depth_mm))
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        cut = feat_mgr.FeatureCut4(
            True, False, reverse, t1, 0, d1, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, False, True, True, False, False, False,
            SW_START_SKETCH_PLANE, 0.0, False, False,
        )
        if cut is None:
            raise SolidWorksError(f"FeatureCut4 failed (None). Is '{sketch}' a closed profile that meets the part"
                                  f"{'' if reverse else ' (or does it need reverse=True)'}?")
        return self._with_depth(self._finish_feature(cut, name, **defined), depth_mm)

    def _select_person_sketch(self, name: str) -> dict:
        """Select a sketch by name for a feature; its dimensions and definition state."""
        feature = self._sketch_by_name(name)
        self._model.ClearSelection2(True)
        if not feature.Select2(False, 0):
            raise SolidWorksError(f"Could not select the sketch '{name}'.")
        dims = {entry["name"].split("@")[0]: entry["name"] for entry in self._dimension_entries(feature)}
        status = binding.wrap(feature.GetSpecificFeature2(), self._mod.ISketch).GetConstrainedStatus()
        return {"dimensions": dims, "fully_defined": status == SW_FULLY_CONSTRAINED}

    # --- history: list, delete and suppress features ---------------------------

    def _history(self) -> list:
        """The part's features after the origin, in tree order: what was modelled.

        Every part starts with SolidWorks' folders, the three default planes and
        the origin; whatever follows the origin was built by someone.
        """
        self._require_part()
        history, after_origin = [], False
        for feat in self._iter_features():
            if after_origin:
                history.append(feat)
            elif feat.GetTypeName2() == "OriginProfileFeature":
                after_origin = True
        return history

    def _history_feature(self, name: str):
        history = self._history()
        for feat in history:
            if feat.Name == name:
                return feat
        listed = ", ".join(f.Name for f in history) or "none"
        raise SolidWorksError(f"No feature '{name}' in the part's history (there are: {listed}).")

    @staticmethod
    def _is_suppressed(feat) -> bool:
        return bool(feat.IsSuppressed2(SW_THIS_CONFIGURATION, None)[0])

    def list_features(self) -> dict:
        """The part's modelling history in tree order: name, type, suppressed.

        Sketches are listed in their own right; types are SolidWorks' own names
        (ProfileFeature = sketch, Extrusion, ICE = cut-extrude, Fillet, ...). A
        feature SolidWorks flags carries `error` {code, warning}: warning False
        means it fails to rebuild. `under_defined_sketches` names every sketch
        that can still move, which a part drawn by hand may have.
        """
        features = []
        for feat in self._history():
            entry = {"name": feat.Name, "type": feat.GetTypeName2(), "suppressed": self._is_suppressed(feat)}
            code, warning = feat.GetErrorCode2()
            if code:
                entry["error"] = self._error_entry(code, warning)
            features.append(entry)
        return {"ok": True, "count": len(features), "features": features,
                "under_defined_sketches": self._under_defined_sketches()}

    def delete_feature(self, name: str, with_children: bool = False, with_equations: bool = False,
                       dry_run: bool = False) -> dict:
        """Delete a history feature with the sketches it absorbed; rebuild, remeasure.

        What is built on it (a fillet on its edges, a sketch on its face) would
        be left broken, so it refuses and names those, and what is built on
        them in turn, unless with_children=True deletes them too. `deleted`
        lists what went, in tree order; dry_run=True lists it as
        `would_delete` and leaves the part as it is. Equations that named its
        dimensions break: `broken_equations` lists them, unless
        with_equations=True deletes them (`deleted_equations`); global
        variables stay.
        """
        model = self._require_model()
        feature = self._history_feature(name)
        going = self._goes_along(feature, with_children=True)
        dependents = [n for n in going if n not in self._goes_along(feature, with_children=False)]
        if dependents and not with_children:
            raise SolidWorksError(
                f"'{name}' has dependents: {', '.join(dependents)}. Delete those first, suppress "
                f"'{name}' instead, or pass with_children=True to delete them too."
            )
        if dry_run:
            return {"ok": True, "would_delete": going, "mass_properties": self.get_mass_properties()["mass_properties"]}
        before = [f.Name for f in self._history()]
        broken_before = {e["equation"] for e in self._equation_entries() if e["broken"]}
        model.ClearSelection2(True)
        if not feature.Select2(False, 0):
            raise SolidWorksError(f"Could not select '{name}'.")
        options = SW_DELETE_ABSORBED | (SW_DELETE_CHILDREN if with_children else 0)
        if not binding.wrap(model.Extension, self._mod.IModelDocExtension).DeleteSelection2(options):
            raise SolidWorksError(f"SolidWorks refused to delete '{name}'.")
        rebuilt_ok = bool(model.ForceRebuild3(False))
        after = {f.Name for f in self._history()}
        result = {"ok": True, "deleted": [n for n in before if n not in after]}
        broken = [e for e in self._equation_entries() if e["broken"] and e["equation"] not in broken_before]
        if with_equations and broken:
            eqmgr = self._equation_mgr()
            for entry in sorted(broken, key=lambda e: e["index"], reverse=True):  # later indexes first
                if eqmgr.Delete(entry["index"]) < 0:
                    raise SolidWorksError(f"SolidWorks refused to delete {entry['equation']}.")
            rebuilt_ok = bool(model.ForceRebuild3(False))
            result["deleted_equations"] = [e["equation"] for e in broken]
            # one that used a variable set by a deleted equation breaks in turn
            broken = [e for e in self._equation_entries() if e["broken"] and e["equation"] not in broken_before]
        result.update(broken_equations=[e["equation"] for e in broken], rebuild_ok=rebuilt_ok,
                      mass_properties=self.get_mass_properties()["mass_properties"])
        return result

    def _goes_along(self, feature, with_children: bool) -> list:
        """The history features a delete of `feature` takes, in tree order: it
        and the sketches it absorbed; with_children also what is built on it,
        on what is built on that, and so on (GetChildren gives one level)."""
        names, todo = set(), [feature]
        while todo:
            feat = todo.pop()
            if feat.Name in names:
                continue
            names.add(feat.Name)
            todo += self._sub_features(feat)
            if with_children:
                todo += [binding.wrap(c, self._mod.IFeature) for c in (feat.GetChildren() or ())]
        return [f.Name for f in self._history() if f.Name in names]

    def suppress_feature(self, name: str, suppress: bool = True) -> dict:
        """Suppress a history feature or bring it back; rebuild, remeasure.

        Unlike delete_feature it keeps the feature and its dimensions, so it
        suits trying a variant. What depends on it follows both ways: SolidWorks
        suppresses the dependents along, and suppress=False brings them back
        too. `changed` lists every feature whose state changed.
        """
        model = self._require_model()
        feature = self._history_feature(name)
        before = {f.Name: self._is_suppressed(f) for f in self._history()}
        action = SW_SUPPRESS_FEATURE if suppress else SW_UNSUPPRESS_DEPENDENT
        if not feature.SetSuppression2(action, SW_THIS_CONFIGURATION, None):
            raise SolidWorksError(f"SolidWorks refused to {'suppress' if suppress else 'unsuppress'} '{name}'.")
        rebuilt_ok = bool(model.ForceRebuild3(False))
        return {
            "ok": True,
            "changed": [f.Name for f in self._history() if self._is_suppressed(f) != before[f.Name]],
            "rebuild_ok": rebuilt_ok,
            "mass_properties": self.get_mass_properties()["mass_properties"],
        }

    def reorder_feature(self, name: str, before: str) -> dict:
        """Move a history feature, with its sketch, to just before another one
        and its sketch; rebuild, remeasure.

        A boss added last fills the holes cut before it; moved before them,
        they cut through it again. Refused, the part as it was, when the
        feature is built on something at or after that place.
        """
        model = self._require_model()
        feature, target = self._history_feature(name), self._history_feature(before)
        if feature.Name == target.Name:
            raise SolidWorksError(f"'{name}' cannot move before itself.")
        names = [f.Name for f in self._history()]
        # before the target's own sketch, so the two stay together in the tree
        place = min((f.Name for f in [target, *self._sub_features(target)] if f.Name in names), key=names.index)
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        if not extension.ReorderFeature(feature.Name, place, SW_MOVE_BEFORE):
            moved = self._goes_along(feature, with_children=False)
            parents = {binding.wrap(p, self._mod.IFeature).Name for f in self._history() if f.Name in moved
                       for p in (f.GetParents() or ())}
            later = [n for n in names[names.index(place):] if n in parents and n not in moved]
            raise SolidWorksError(f"SolidWorks did not move '{name}' before '{before}'"
                                  + (f": it is built on {', '.join(later)}, which would come after it."
                                     if later else ".") + " The part is as it was.")
        rebuilt_ok = bool(model.ForceRebuild3(False))
        return {"ok": True, "features": [f.Name for f in self._history()], "rebuild_ok": rebuilt_ok,
                "failing_features": self._failing_features(),
                "mass_properties": self.get_mass_properties()["mass_properties"]}

    # --- meshes: slice a reference, compare the part with it ---------------------

    @staticmethod
    def _loop_entry(loop, with_points: bool) -> dict:
        points = simplify(loop)
        u0, u1, v0, v1 = extents(points)
        entry = {"area_mm2": round(area(points), 4), "min_mm": [round(u0, 4), round(v0, 4)],
                 "max_mm": [round(u1, 4), round(v1, 4)], "point_count": len(points)}
        if with_points:
            entry["points_mm"] = [[round(u, 4), round(v, 4)] for u, v in points]
        return entry

    def slice_mesh(self, path: str, axis: str, heights_mm: list, frame: str = "object") -> dict:
        """Cross-sections of an STL or 3MF mesh at the given heights along axis.

        Each section lists its closed loops, largest first (the outline, then
        holes and pockets), as polygon points (mm): (y, z) across x, (x, z)
        across y, (x, y) across z. Only exactly collinear points are dropped, so
        the loops are ready to use as profiles. frame (3MF): 'object' = the
        mesh's own modelling frame, 'build' = as placed on the slicer's plate.
        No SolidWorks needed.
        """
        triangles = load_mesh(path, frame)
        sections = [{"height_mm": h, "loops": [self._loop_entry(l, True) for l in section(triangles, axis, h)]}
                    for h in heights_mm]
        return {"ok": True, "axis": axis, "frame": frame, "sections": sections}

    def _part_triangles(self) -> list:
        """The current part as triangles in its own model frame (mm), via a fine STL export."""
        path = os.path.join(tempfile.gettempdir(), f"solidworks_mcp_{uuid.uuid4().hex}.stl")
        try:
            self.export(path, quality="fine")
            return load_mesh(path)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def compare_with_mesh(self, path: str, axis: str, heights_mm: list, frame: str = "object",
                          offset_mm: list | None = None) -> dict:
        """Slice the current part and a reference mesh at the same heights; compare.

        The mesh is moved by offset_mm ([dx, dy, dz]) into the part's frame
        first. Each reference loop is paired with the nearest loop of the part:
        a different area means a misread feature, equal areas with different
        extents a shifted frame. The part is measured through a fine STL, so
        curved walls differ by up to the tessellation's chord error.
        """
        self._require_part()
        offset = [0.0, 0.0, 0.0] if offset_mm is None else [float(v) for v in offset_mm]
        if len(offset) != 3:
            raise SolidWorksError(f"offset_mm needs [dx, dy, dz] (got {offset_mm}).")
        reference = [tuple(tuple(c + o for c, o in zip(v, offset)) for v in tri) for tri in load_mesh(path, frame)]
        part = self._part_triangles()
        sections, worst_extent, worst_area = [], 0.0, 0.0
        for h in heights_mm:
            compared = compare_sections(section(reference, axis, h), section(part, axis, h))
            for pair in compared["pairs"]:
                worst_extent = max(worst_extent, pair["max_extent_diff_mm"])
                worst_area = max(worst_area, abs(pair["area_diff_mm2"]))
            sections.append({"height_mm": h, **compared})
        return {"ok": True, "axis": axis, "sections": sections,
                "worst_extent_diff_mm": round(worst_extent, 4), "worst_area_diff_mm2": round(worst_area, 4)}

    def set_appearance(self, rgb: list | None = None, transparency: float | None = None,
                       component: str | None = None) -> dict:
        """Colour (rgb, 0..255 each) and transparency (0 solid .. 1 clear) of the
        current part, or in an assembly of one component ('Leg-1/Thigh-1' inside
        a sub-assembly); what is left out stays as it was. A component's colour
        is the assembly's, the part keeps its own."""
        if rgb is None and transparency is None:
            raise SolidWorksError("Give rgb, transparency or both.")
        model = self._require_model()
        if int(model.GetType()) == SW_DOC_ASSEMBLY:
            if component is None:
                raise SolidWorksError("In an assembly, name the component to colour (list_components).")
            comp = self._component_by_name(binding.wrap(model, self._mod.IAssemblyDoc), component)
            current = comp.GetMaterialPropertyValues2(SW_THIS_CONFIGURATION, None)
            if current[0] < 0:  # no colour of its own in the assembly yet: start from the part's
                part = binding.wrap(comp.GetModelDoc2(), self._mod.IModelDoc2)
                current = part.MaterialPropertyValues if part is not None else self._DEFAULT_APPEARANCE
            values = self._appearance_values(current, rgb, transparency)
            comp.SetMaterialPropertyValues2(self._doubles(values), SW_THIS_CONFIGURATION, None)
            stored = comp.GetMaterialPropertyValues2(SW_THIS_CONFIGURATION, None)
        else:
            if component is not None:
                raise SolidWorksError("component is for an assembly; a part is coloured as a whole.")
            values = self._appearance_values(model.MaterialPropertyValues, rgb, transparency)
            model.MaterialPropertyValues = self._doubles(values)
            stored = model.MaterialPropertyValues
        model.GraphicsRedraw2()
        # SolidWorks keeps colours in steps of 1/255
        if any(abs(a - b) > 1 / 255 for a, b in zip(stored, values)):
            raise SolidWorksError(f"SolidWorks did not take the appearance: it reads {list(stored)}.")
        return {"ok": True, "rgb": [round(c * 255) for c in stored[:3]], "transparency": round(stored[7], 3),
                **({"component": comp.Name2} if component is not None else {})}

    # red, green, blue, ambient, diffuse, specular, shininess, transparency, emission (0..1)
    _DEFAULT_APPEARANCE = (0.8, 0.8, 0.8, 1.0, 1.0, 0.5, 0.4, 0.0, 0.0)

    @staticmethod
    def _appearance_values(current, rgb, transparency) -> list:
        """The nine material property values with the colour and/or transparency
        replaced; pure, unit-tested."""
        values = [float(v) for v in current]
        if rgb is not None:
            if len(rgb) != 3 or any(not 0 <= c <= 255 for c in rgb):
                raise SolidWorksError(f"rgb is three numbers 0..255 (got {rgb}).")
            values[:3] = [c / 255 for c in rgb]
        if transparency is not None:
            if not 0 <= transparency <= 1:
                raise SolidWorksError(f"transparency runs from 0 (solid) to 1 (clear) (got {transparency}).")
            values[7] = float(transparency)
        return values

    @staticmethod
    def _doubles(values) -> object:
        """A VARIANT array of doubles: a plain list is passed as variants and the
        material values are ignored without a word."""
        return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])

    def set_material(self, name: str, database: str = "", density_kg_m3: float | None = None) -> dict:
        """Assign a material by name so mass/density reflect a real material.

        name: a material in the SolidWorks database, e.g. '6061 Alloy',
        'AISI 1020', 'ABS', 'Plain Carbon Steel'. database: path to a .sldmat, or
        '' for the default databases. density_kg_m3 makes a material of its own
        under that name instead (say TPU at 1210) that carries only its density;
        the part keeps it, also where the material is unknown. Returns mass
        properties (with density).
        """
        if density_kg_m3 is not None and database:
            raise SolidWorksError("Give a database or a density_kg_m3, not both.")
        if density_kg_m3 is not None and density_kg_m3 <= 0:
            raise SolidWorksError(f"density_kg_m3 must be > 0 (got {density_kg_m3}).")
        model = self._require_model()
        part = self._require_part()
        if density_kg_m3 is None:
            part.SetMaterialPropertyName2("", database, name)
        else:
            self._assign_own_material(part, name, density_kg_m3)
        rebuilt_ok = bool(model.ForceRebuild3(False))
        # Verify by reading the applied name back (robust to re-assignment and to
        # materials near 1000 kg/m^3, where a density heuristic would lie).
        applied = part.GetMaterialPropertyName2("")
        applied_name = applied[0] if isinstance(applied, (list, tuple)) else applied
        if (applied_name or "").strip().lower() != name.strip().lower():
            raise SolidWorksError(
                f"Material '{name}' was not applied (active: '{applied_name}'). "
                "Check the exact name, e.g. '6061 Alloy', 'AISI 1020', 'ABS'."
            )
        return {
            "ok": True,
            "material": applied_name,
            "rebuild_ok": rebuilt_ok,
            "mass_properties": self.get_mass_properties()["mass_properties"],
        }

    def _assign_own_material(self, part, name: str, density_kg_m3: float) -> None:
        """SolidWorks reads a .sldmat only from its material folders, so one is
        written to a temporary folder that joins them while the material is
        assigned. The part keeps the material's properties once it is gone."""
        folder = tempfile.mkdtemp(prefix="solidworks_mcp_")
        path = os.path.join(folder, f"{MCP_MATERIAL_DATABASE}.sldmat")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._material_xml(name, density_kg_m3))
        folders = self._sw.GetUserPreferenceStringValue(SW_FILE_LOCATIONS_MATERIALS)
        try:
            self._sw.SetUserPreferenceStringValue(SW_FILE_LOCATIONS_MATERIALS, f"{folders};{folder}" if folders else folder)
            part.SetMaterialPropertyName2("", path, name)
        finally:
            self._sw.SetUserPreferenceStringValue(SW_FILE_LOCATIONS_MATERIALS, folders)
            shutil.rmtree(folder, ignore_errors=True)

    @staticmethod
    def _material_xml(name: str, density_kg_m3: float) -> str:
        """A .sldmat with one material that carries only a density; pure, unit-tested."""
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<mstns:materials xmlns:mstns="http://www.solidworks.com/sldmaterials" version="2008.03">\n'
                f'  <classification name="{MCP_MATERIAL_DATABASE}">\n'
                f'    <material name={quoteattr(name)}>\n'
                '      <physicalproperties>\n'
                f'        <DENS displayname="Density" value="{float(density_kg_m3)!r}"/>\n'
                '      </physicalproperties>\n'
                '    </material>\n'
                '  </classification>\n'
                '</mstns:materials>\n')

    def rebuild(self, top_only: bool = False) -> dict:
        model = self._require_model()
        rebuilt_ok = bool(model.ForceRebuild3(top_only))
        result = {"ok": True, "rebuild_ok": rebuilt_ok}
        if int(model.GetType()) == SW_DOC_ASSEMBLY:  # a mate that lost its face holds nothing, yet rebuilds fine
            result["mate_errors"] = [{"name": mate.Name, "error": trouble} for mate in self._mates()
                                     if (trouble := self._mate_trouble(mate))]
        return result

    # --- measurement ----------------------------------------------------------

    def get_mass_properties(self) -> dict:
        """Volume/mass/area/centre-of-mass plus bounding box, all in SI->mm, forced SI."""
        model = self._require_model()
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        mp = binding.wrap(ext.CreateMassProperty(), self._mod.IMassProperty)
        if mp is None:
            raise SolidWorksError("CreateMassProperty returned None.")
        # Force SI (m, kg) regardless of document units. This is coupled to the
        # fixed 1e9/1e6/m_to_mm factors below, so do NOT swallow a failure here:
        # silently wrong units would be worse than a loud error.
        mp.UseSystemUnits = True
        if not mp.UseSystemUnits:
            raise SolidWorksError("Could not force mass properties to SI units (UseSystemUnits=False).")
        com = mp.CenterOfMass
        props = {
            "volume_mm3": mp.Volume * 1e9,
            "mass_kg": mp.Mass,
            "density_kg_m3": mp.Density,
            "surface_area_mm2": mp.SurfaceArea * 1e6,
            "center_of_mass_mm": [round(m_to_mm(c), 6) for c in com],
        }
        props["bounding_box_mm"] = self._bounding_box()
        return {"ok": True, "mass_properties": props}

    def _bounding_box(self):
        # IModelDoc2 has no GetBox, so the call depends on the document type. A
        # part joins its solid bodies' tight boxes, from their extreme points:
        # IPartDoc.GetPartBox runs loose round curved faces (10.004 for a loft
        # ending at 10, verified; millimetres round spline cuts). An empty part
        # has no box. An assembly joins the tight boxes of its components:
        # IAssemblyDoc.GetBox joins their turned outer boxes, too big for a
        # rotated part (verified: a 20 mm disc turned 45 degrees came out 28.3 wide).
        model = self._require_model()
        if int(model.GetType()) == SW_DOC_ASSEMBLY:
            assembly = binding.wrap(model, self._mod.IAssemblyDoc)
            assembly.EditRebuild()  # mates move components on a rebuild: measure where they end up
            return self._joined_box([self._component_box(c) for c in self._components(assembly)])
        return self._joined_box([self._body_box(body) for body in self._part_bodies()])

    def get_bounding_box(self) -> dict:
        self._require_model()
        return {"ok": True, "bounding_box_mm": self._bounding_box()}

    def _axis_of(self, dx, dy, dz, length):
        """Return 'x'|'y'|'z' if the vector is parallel to that axis, else None."""
        if length < 1e-9:
            return None
        for axis, (tx, ty, tz) in self._EDGE_AXES.items():
            if abs(dx * tx + dy * ty + dz * tz) / length > 0.999:
                return axis
        return None

    def list_faces(self, component: str | None = None) -> dict:
        """Inspect faces: index, planar?, normal, area, centre; a cylindrical face
        also gives its axis, radius and a point on the axis (a hole's centre).

        Lets an agent see the geometry before choosing one. Indices are positional
        in the body's face list and shift as features are added. component names
        a component of the current assembly; its faces are given in its own frame,
        e.g. to read a hole circle off an imported part.
        """
        if component is None:
            self._require_model()
            out = [self._face_entry(i, face) for i, face in enumerate(self._body_faces(self._solid_body()))]
        else:
            comp = self._component_by_name(self._require_assembly(), component)
            out = []
            for i, (face, part) in enumerate(self._component_faces(comp)):
                entry = self._face_entry(i, face, self._part_frame_in(comp, part))
                if part.Name2 != comp.Name2:
                    entry["part"] = part.Name2  # a part inside a sub-assembly
                out.append(entry)
        return {"ok": True, "count": len(out), "faces": out}

    def _face_entry(self, index: int, face_dispatch, frame=None) -> dict:
        """A face's index, type, area and centre; a planar face's normal, a
        cylinder's axis. `frame` (rotation rows, shift mm) moves it from its
        part's coordinates into a sub-assembly's."""
        face = binding.wrap(face_dispatch, self._mod.IFace2)
        surface = binding.wrap(face.GetSurface(), self._mod.ISurface)
        planar = bool(surface is not None and surface.IsPlane())
        rotation, shift = frame or ([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], [0.0, 0.0, 0.0])

        def turn(v):
            return [sum(rotation[row][i] * v[i] for i in range(3)) for row in range(3)]

        def place(p):
            return [c + s for c, s in zip(turn(p), shift)]

        box = face.GetBox()
        center = None
        if box and len(box) >= 6:
            center = [m_to_mm((box[j] + box[j + 3]) / 2) for j in range(3)]
        entry = {
            "index": index,
            "type": "planar" if planar else "curved",
            "area_mm2": round(face.GetArea() * 1e6, 3),
            "center_mm": None if center is None else [round(c, 3) + 0.0 for c in place(center)],
        }
        if planar:
            entry["normal"] = [round(n, 4) + 0.0 for n in turn(face.Normal)]
        elif surface is not None and surface.IsCylinder() and center is not None:
            cylinder = self._cylinder_entry(surface, center)
            entry["cylinder"] = {"axis": [round(a, 6) + 0.0 for a in turn(cylinder["axis"])],
                                 "point_mm": [round(p, 4) + 0.0 for p in place(cylinder["point_mm"])],
                                 "radius_mm": cylinder["radius_mm"]}
        return entry

    @staticmethod
    def _cylinder_entry(surface, center_mm) -> dict:
        """Axis (either sense), radius, and the point on the axis nearest the
        face's centre: for a hole, its centre halfway down."""
        params = surface.CylinderParams  # axis origin xyz, axis direction xyz, radius (metres)
        origin = [m_to_mm(c) for c in params[0:3]]
        length = math.hypot(*params[3:6])
        axis = [c / length for c in params[3:6]]
        along = sum((c - o) * a for c, o, a in zip(center_mm, origin, axis))
        point = [o + along * a for o, a in zip(origin, axis)]
        return {"axis": axis, "point_mm": point, "radius_mm": round(m_to_mm(params[6]), 4)}

    def list_edges(self, face=None, feature: str | None = None, within_mm=None,
                   min_length_mm: float | None = None) -> dict:
        """Inspect the solid body's edges: index, type, length and ends (a full
        circle has none); lines also give axis and midpoint. Narrow the list
        with face (a list_faces index, '#5'), feature (the edges of the faces it
        made), within_mm ([[x1, y1, z1], [x2, y2, z2]]: both ends inside) and
        min_length_mm. Indices stay those of the whole list, for add_fillet."""
        self._require_model()
        body = self._solid_body()
        edges = list(body.GetEdges() or ())
        chosen = range(len(edges))
        if face is not None or feature is not None:
            index_of = {ref: i for i, ref in enumerate(self._persist_refs(edges))}
            for picked in (self._face_edges(body, face) if face is not None else None,
                           self._feature_edges(feature) if feature is not None else None):
                if picked is not None:
                    keep = {index_of[ref] for ref in self._persist_refs(picked) if ref in index_of}
                    chosen = [i for i in chosen if i in keep]
        box = self._box_corners(within_mm) if within_mm is not None else None
        out = []
        for i in chosen:
            entry = self._edge_entry(i, edges[i])
            if min_length_mm is not None and entry["length_mm"] < min_length_mm:
                continue
            if box is not None and not all(all(lo <= c <= hi for c, lo, hi in zip(p, *box))
                                           for p in entry.get("ends_mm", [entry["start_mm"]])):
                continue
            entry.pop("start_mm")
            out.append(entry)
        return {"ok": True, "count": len(out), "edges": out}

    _CURVE_TYPES = {3001: "line", 3002: "circle"}  # ICurve.Identity

    def _edge_entry(self, index: int, edge_dispatch) -> dict:
        """One edge in four COM calls: its curve, the curve's type, its ends and
        parameter range in one array (GetCurveParams2), and its length."""
        edge = binding.wrap(edge_dispatch, self._mod.IEdge)
        curve = binding.wrap(edge.GetCurve(), self._mod.ICurve)
        params = edge.GetCurveParams2()  # start xyz, end xyz (m), start and end parameter
        start = [round(m_to_mm(v), 4) + 0.0 for v in params[0:3]]
        end = [round(m_to_mm(v), 4) + 0.0 for v in params[3:6]]
        kind = self._CURVE_TYPES.get(curve.Identity(), "curve")
        length = m_to_mm(curve.GetLength3(params[6], params[7]))
        entry = {"index": index, "type": kind, "length_mm": round(length, 3), "start_mm": start}
        if math.dist(start, end) > 1e-6:  # a full circle starts where it ends
            entry["ends_mm"] = [start, end]
        if kind == "line":
            d = [b - a for a, b in zip(start, end)]
            entry["midpoint_mm"] = [round((a + b) / 2, 3) for a, b in zip(start, end)]
            entry["axis"] = self._axis_of(*d, math.dist(start, end))
        return entry

    def _persist_refs(self, entities) -> list:
        extension = binding.wrap(self._model.Extension, self._mod.IModelDocExtension)
        return [bytes(extension.GetPersistReference3(e)) for e in entities]

    def _face_edges(self, body, face) -> list:
        index = self._face_index(face)
        faces = self._body_faces(body)
        if index is None or not 0 <= index < len(faces):
            raise SolidWorksError(f"face takes a list_faces index, '#0' to '#{len(faces) - 1}' (got {face!r}).")
        return list(binding.wrap(faces[index], self._mod.IFace2).GetEdges() or ())

    def _feature_edges(self, name: str) -> list:
        return [edge for face in (self._history_feature(name).GetFaces() or ())
                for edge in binding.wrap(face, self._mod.IFace2).GetEdges() or ()]

    @staticmethod
    def _box_corners(within_mm) -> tuple:
        """[[x1, y1, z1], [x2, y2, z2]] -> (low, high), corners in any order; pure."""
        try:
            (a, b) = within_mm
            a, b = [float(c) for c in a], [float(c) for c in b]
            assert len(a) == len(b) == 3
        except (TypeError, ValueError, AssertionError):
            raise SolidWorksError(f"within_mm takes two corners [[x1, y1, z1], [x2, y2, z2]] (got {within_mm}).") from None
        return [min(p, q) for p, q in zip(a, b)], [max(p, q) for p, q in zip(a, b)]

    # --- bodies: separate, combined, split -------------------------------------------

    _BODY_OPERATIONS = {"add": 15903, "subtract": 15902, "common": 15901}  # swBodyOperationType_e

    def _part_bodies(self) -> list:
        """The current part's solid bodies (IBody2), in SolidWorks' order."""
        bodies = binding.wrap(self._model, self._mod.IPartDoc).GetBodies2(SW_BODY_SOLID, False) or ()
        return [binding.wrap(body, self._mod.IBody2) for body in bodies]

    def list_bodies(self) -> dict:
        """The part's solid bodies: name, volume and bounding box. A part holds
        several after merge=False or split_body; combine_bodies joins them."""
        self._require_part()
        out = []
        for body in self._part_bodies():
            out.append({"name": body.Name, "volume_mm3": round(body.GetMassProperties(1.0)[3] * 1e9, 4),
                        "bounding_box_mm": self._body_box(body)})
        return {"ok": True, "count": len(out), "bodies": out}

    def _body_box(self, body) -> dict:
        """A body's tight box in part coordinates (mm), from its extreme points."""
        low = [m_to_mm(self._extreme_point(body, [-1.0 if i == axis else 0.0 for i in range(3)])[axis])
               for axis in range(3)]
        high = [m_to_mm(self._extreme_point(body, [1.0 if i == axis else 0.0 for i in range(3)])[axis])
                for axis in range(3)]
        return self._joined_box([{"min_mm": [round(v, 4) for v in low], "max_mm": [round(v, 4) for v in high]}])

    def combine_bodies(self, operation: str, main: str, tools: list | None = None, name: str = "Combine") -> dict:
        """Combine solid bodies of the part into `main` (a list_bodies name):
        'add' joins the tools to it, 'subtract' cuts them out of it, 'common'
        keeps only what it shares with them. tools: body names, every other body
        by default."""
        key = str(operation).lower()
        if key not in self._BODY_OPERATIONS:
            raise SolidWorksError(f"operation must be one of {sorted(self._BODY_OPERATIONS)} (got '{operation}').")
        model = self._require_model()
        self._require_part()
        bodies = {body.Name: body for body in self._part_bodies()}
        if main not in bodies:
            raise SolidWorksError(f"No body '{main}' (there are: {', '.join(bodies) or 'none'}).")
        names = list(tools) if tools else [n for n in bodies if n != main]
        unknown = [n for n in names if n not in bodies or n == main]
        if unknown or not names:
            raise SolidWorksError(f"Give other bodies of the part to combine with '{main}' "
                                  f"(there are: {', '.join(bodies)}; got {names}).")
        model.ClearSelection2(True)
        # the list holds the main body too: add and common fail without it (verified)
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        combined = feat_mgr.InsertCombineFeature(
            self._BODY_OPERATIONS[key], bodies[main],
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
                                    [bodies[n]._oleobj_ for n in [main, *names]]))
        if combined is None:
            raise SolidWorksError(f"SolidWorks could not {key} {', '.join(names)} and '{main}': do they overlap?")
        result = self._finish_feature(combined, name, combined=[main, *names])
        result["bodies"] = [body.Name for body in self._part_bodies()]
        return result

    def split_body(self, plane: str, name: str = "Split") -> dict:
        """Split the part's body in two along a plane (front/top/right or a plane
        by name, such as one add_plane made): two bodies, named in the result,
        to combine differently or check apart."""
        model = self._require_model()
        self._require_part()
        ref, label = self._named_plane(plane)
        model.ClearSelection2(True)
        if not ref.Select2(False, 0):
            raise SolidWorksError(f"Could not select the {label}.")
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        pieces = feat_mgr.PreSplitBody() or ()
        if len(pieces) < 2:
            model.ClearSelection2(True)
            raise SolidWorksError(f"The {label} does not cut through the part.")
        # one empty origin and save path per piece: keep them all in this part (verified)
        split = feat_mgr.PostSplitBody(
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [getattr(p, "_oleobj_", p) for p in pieces]),
            False,
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [None] * len(pieces)),
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_BSTR, [""] * len(pieces)))
        if split is None:
            raise SolidWorksError(f"SolidWorks could not split the part along the {label}.")
        result = self._finish_feature(split, name)
        result["bodies"] = [body.Name for body in self._part_bodies()]
        return result

    # --- 3D printing --------------------------------------------------------------

    _MAX_WALL_SAMPLES = 100_000  # rays per check; beyond that the samples spread out

    def check_printability(self, up: str = "+z", overhang_deg: float = 45.0,
                           min_wall_mm: float | None = None) -> dict:
        """Check the current part for 3D printing, built along `up`.

        Overhangs: downward faces leaning more than overhang_deg from vertical
        (90 = a flat ceiling) need support; faces resting on the bed do not
        count. With min_wall_mm, also the walls thinner than that, measured
        straight through the material from points spread over every face; a
        spot right beside a sharp edge reads as thin as the wedge there is.
        Faces are list_faces indexes, so a finding can be selected and fixed.
        """
        direction = self._parse_direction(up)
        if not 0.0 < overhang_deg < 90.0:
            raise SolidWorksError(f"overhang_deg must be between 0 and 90 (got {overhang_deg}).")
        if min_wall_mm is not None and min_wall_mm <= 0:
            raise SolidWorksError(f"min_wall_mm must be > 0 (got {min_wall_mm}).")
        self._require_model()
        body = self._solid_body()
        triangles = [(index, *tri) for index, face in enumerate(self._body_faces(body))
                     for tri in self._face_triangles(binding.wrap(face, self._mod.IFace2))]
        result = {"ok": True, "up": up, **self._overhangs(triangles, direction, overhang_deg)}
        if min_wall_mm is not None:
            result["thin_walls"] = self._thin_walls(body, triangles, min_wall_mm)
        return result

    @staticmethod
    def _face_triangles(face) -> list:
        """The face's display tessellation as (a, b, c, unit outward normal) in mm."""
        corners, normals = face.GetTessTriangles(True) or (), face.GetTessNorms() or ()
        triangles = []
        for t in range(len(corners) // 9):
            a, b, c = ([m_to_mm(v) for v in corners[9 * t + 3 * k:9 * t + 3 * k + 3]] for k in range(3))
            normal = [sum(normals[9 * t + 3 * k + i] for k in range(3)) for i in range(3)]
            length = math.hypot(*normal)
            if length:
                triangles.append((a, b, c, [n / length for n in normal]))
        return triangles

    @staticmethod
    def _triangle_area(a, b, c) -> float:
        ab, ac = [q - p for p, q in zip(a, b)], [q - p for p, q in zip(a, c)]
        return math.hypot(ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2],
                          ab[0] * ac[1] - ab[1] * ac[0]) / 2

    def _overhangs(self, triangles, up, limit_deg) -> dict:
        """Overhanging area per face, and the area resting on the bed."""
        def height(p):
            return sum(c * u for c, u in zip(p, up))

        bed = min(height(p) for _, *corners, _ in triangles for p in corners)
        faces, bed_area = {}, 0.0
        for index, a, b, c, normal in triangles:
            area = self._triangle_area(a, b, c)
            if all(abs(height(p) - bed) < 1e-4 for p in (a, b, c)):
                bed_area += area
                continue
            lean = math.degrees(math.asin(max(0.0, min(1.0, -sum(n * u for n, u in zip(normal, up))))))
            if lean > limit_deg + 1e-6:
                entry = faces.setdefault(index, {"index": index, "area_mm2": 0.0, "worst_deg": 0.0, "sum": [0.0] * 3})
                entry["area_mm2"] += area
                entry["worst_deg"] = max(entry["worst_deg"], lean)
                entry["sum"] = [s + area * (p + q + r) / 3 for s, p, q, r in zip(entry["sum"], a, b, c)]
        found = []
        for entry in sorted(faces.values(), key=lambda e: -e["area_mm2"]):
            centre = [s / entry["area_mm2"] for s in entry.pop("sum")]
            found.append({**entry, "area_mm2": round(entry["area_mm2"], 3), "worst_deg": round(entry["worst_deg"], 2),
                          "center_mm": [round(c, 3) for c in centre]})
        heights = [height(p) for _, *corners, _ in triangles for p in corners]
        return {"height_mm": round(max(heights) - bed, 4), "bed_contact_mm2": round(bed_area, 3),
                "overhang": {"limit_deg": limit_deg, "area_mm2": round(sum(f["area_mm2"] for f in found), 3),
                             "faces": found}}

    @staticmethod
    def _triangle_samples(a, b, c, spacing) -> list:
        """Points inside triangle abc such that every spot of it lies within
        `spacing` of one: columns across its longest edge, each filled up to the
        triangle's height there. As many as its size needs, so a long sliver of
        a curved face gets one row, not a grid of its length squared (7225
        points for a 67.6 x 0.2 mm sliver). Pure, unit-tested."""
        a, b, c = max(((a, b, c), (b, c, a), (c, a, b)), key=lambda t: math.dist(t[0], t[1]))  # a-b longest
        length = math.dist(a, b)
        along = [(q - p) / length for p, q in zip(a, b)] if length else None
        foot = sum((r - p) * e for p, r, e in zip(a, c, along)) if along else 0.0  # within a-b: it is the longest
        up = [r - p - foot * e for p, r, e in zip(a, c, along)] if along else [0.0] * 3
        height = math.hypot(*up)
        if not height:
            return [[(p + q + r) / 3 for p, q, r in zip(a, b, c)]]
        up = [u / height for u in up]
        columns = math.ceil(length / spacing)
        points = []
        for j in range(columns):
            x = (j + 0.5) * length / columns
            top = height * (x / foot if x <= foot else (length - x) / (length - foot))
            rows = max(1, math.ceil(top / spacing))
            for k in range(rows):
                y = (k + 0.5) * top / rows
                points.append([p + x * e + y * u for p, e, u in zip(a, along, up)])
        return points

    @classmethod
    def _wall_samples(cls, triangles, min_wall_mm) -> tuple:
        """(index, point, normal) spread over every face about min_wall_mm apart,
        at most _MAX_WALL_SAMPLES of them, and the spacing they keep: one batch
        of over a million rays hung SolidWorks. Pure, unit-tested."""
        area = sum(cls._triangle_area(a, b, c) for _, a, b, c, _ in triangles)
        spacing = max(min_wall_mm, math.sqrt(2 * area / cls._MAX_WALL_SAMPLES))
        samples = [(index, point, normal) for index, a, b, c, normal in triangles
                   for point in cls._triangle_samples(a, b, c, spacing)]
        stride = math.ceil(len(samples) / cls._MAX_WALL_SAMPLES)  # every stride-th: still spread over all faces
        return samples[::stride], spacing * math.sqrt(stride)

    def _thin_walls(self, body, triangles, min_wall_mm) -> dict:
        """Wall thickness straight through the material from sample points on
        every face (one batch of rays), and the faces thinner than min_wall_mm."""
        samples, spacing = self._wall_samples(triangles, min_wall_mm)
        starts, directions, owners = [], [], []
        for index, point, normal in samples:
            starts += [mm_to_m(p - 1e-4 * n) for p, n in zip(point, normal)]  # just inside the material
            directions += [-n for n in normal]
            owners.append((index, point))
        model = self._model
        hits = model.RayIntersections(
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [body._oleobj_]),
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, starts),
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, directions),
            SW_RAY_NORMALS_ENTRY_EXIT, 0.0, 0.0)
        points = model.GetRayIntersectionsPoints() if hits else ()
        through = {}
        for h in range(hits):
            row = points[h * RAY_HIT_WIDTH:(h + 1) * RAY_HIT_WIDTH]
            ray = int(row[1])
            if not int(row[2]) & SW_RAY_HIT_EXIT:
                continue  # a sample on a hollow face's facet starts in the air and enters first
            depth = m_to_mm(math.dist(row[3:6], starts[3 * ray:3 * ray + 3]))
            if depth > 1e-3 and depth < through.get(ray, math.inf):
                through[ray] = depth
        if not through:
            raise SolidWorksError("No ray came out of the part: the wall thickness could not be measured.")
        thinnest = {}
        for ray, depth in through.items():
            index, point = owners[ray]
            if depth < thinnest.get(index, (math.inf,))[0]:
                thinnest[index] = (depth, point)
        thin = [{"index": index, "thinnest_mm": round(depth, 3), "at_mm": [round(c, 3) for c in point]}
                for index, (depth, point) in sorted(thinnest.items()) if depth < min_wall_mm - 1e-6]
        return {"min_wall_mm": min_wall_mm, "thinnest_mm": round(min(d for d, _ in thinnest.values()), 3),
                "sample_spacing_mm": round(spacing, 3), "faces": thin}

    # --- output ---------------------------------------------------------------

    _MESH_EXPORT_FORMATS = {"stl", "3mf"}

    def _apply_stl_resolution(self, quality: str, deviation_mm, angle_deg) -> dict:
        """Set the global STL/3MF tessellation prefs; return the prior values.

        quality 'coarse'|'fine'; or pass deviation_mm (+ optional angle_deg) for a
        reproducible Custom resolution (overrides quality). Caller MUST restore the
        returned values afterwards -- these are application-wide preferences.
        """
        if deviation_mm is not None and deviation_mm <= 0:
            raise SolidWorksError(f"deviation_mm must be > 0 (got {deviation_mm}).")
        if angle_deg is not None and angle_deg <= 0:
            raise SolidWorksError(f"angle_deg must be > 0 (got {angle_deg}).")
        levels = {"coarse": SW_STL_QUALITY_COARSE, "fine": SW_STL_QUALITY_FINE}
        if deviation_mm is None and quality not in levels:
            raise SolidWorksError(f"quality must be 'coarse' or 'fine' (got '{quality}').")

        sw = self._sw
        old = {
            "quality": sw.GetUserPreferenceIntegerValue(SW_STL_QUALITY),
            "deviation": sw.GetUserPreferenceDoubleValue(SW_STL_DEVIATION),
            "angle": sw.GetUserPreferenceDoubleValue(SW_STL_ANGLE_TOLERANCE),
        }
        if deviation_mm is not None:
            sw.SetUserPreferenceIntegerValue(SW_STL_QUALITY, SW_STL_QUALITY_CUSTOM)
            sw.SetUserPreferenceDoubleValue(SW_STL_DEVIATION, mm_to_m(deviation_mm))
            if angle_deg is not None:
                sw.SetUserPreferenceDoubleValue(SW_STL_ANGLE_TOLERANCE, deg_to_rad(angle_deg))
        else:
            sw.SetUserPreferenceIntegerValue(SW_STL_QUALITY, levels[quality])
        return old

    def _restore_stl_resolution(self, old: dict) -> None:
        """Restore STL prefs saved by _apply_stl_resolution (no lasting side effect)."""
        sw = self._sw
        sw.SetUserPreferenceIntegerValue(SW_STL_QUALITY, old["quality"])
        sw.SetUserPreferenceDoubleValue(SW_STL_DEVIATION, old["deviation"])
        sw.SetUserPreferenceDoubleValue(SW_STL_ANGLE_TOLERANCE, old["angle"])

    def export(self, path: str, file_format: str | None = None, quality: str = "fine",
               deviation_mm: float | None = None, angle_deg: float | None = None,
               per_component: bool = False) -> dict:
        """Export the current part (STEP/STL/IGES/Parasolid/3MF/image) via SaveAs3.

        Silent (no overwrite prompt). Success is verified by checking the file
        actually appears on disk, because SaveAs3's return code is unreliable.

        For STL/3MF, tessellation resolution is applied first (and restored after):
        quality 'coarse'|'fine' (default 'fine' for print quality), or pass
        deviation_mm (+ optional angle_deg) for a reproducible Custom resolution
        (overrides quality). Ignored for STEP/IGES/Parasolid/images. An
        assembly goes to STL as one file, or with per_component=True as one file
        per component next to `path`; `files` lists what was written. A mesh
        keeps the document's coordinates (`frame`: 'part' or 'assembly', also
        per component), where SolidWorks would move an STL to positive space.
        """
        model = self._require_model()
        fmt = (file_format or os.path.splitext(path)[1].lstrip(".")).lower()
        if fmt not in EXPORT_FORMATS:
            raise SolidWorksError(
                f"Unknown export format '{fmt}'. Allowed: {sorted(EXPORT_FORMATS)}."
            )
        assembly_stl = fmt == "stl" and int(model.GetType()) == SW_DOC_ASSEMBLY
        if per_component and not assembly_stl:
            raise SolidWorksError("per_component is for an assembly exported to STL.")
        abs_path = os.path.abspath(path)
        result = {"ok": True, "path": abs_path, "format": fmt}
        files = [abs_path]
        if fmt in self._MESH_EXPORT_FORMATS:
            old = self._apply_stl_resolution(quality, deviation_mm, angle_deg)
            together = self._sw.GetUserPreferenceToggle(SW_TOGGLE_STL_ONE_FILE)
            in_place = self._sw.GetUserPreferenceToggle(SW_TOGGLE_STL_DONT_TRANSLATE)
            try:
                # a mesh measured back, or files per component, must line up with the model
                self._sw.SetUserPreferenceToggle(SW_TOGGLE_STL_DONT_TRANSLATE, True)
                if assembly_stl:  # SolidWorks' own setting would decide, and the user's may be per part
                    self._sw.SetUserPreferenceToggle(SW_TOGGLE_STL_ONE_FILE, not per_component)
                if per_component:
                    files = self._write_stl_per_component(abs_path)
                else:
                    self._write_via_saveas3(abs_path)
            finally:
                self._sw.SetUserPreferenceToggle(SW_TOGGLE_STL_DONT_TRANSLATE, in_place)
                self._sw.SetUserPreferenceToggle(SW_TOGGLE_STL_ONE_FILE, together)
                self._restore_stl_resolution(old)
            result["resolution"] = "custom" if deviation_mm is not None else quality
            result["frame"] = "assembly" if int(model.GetType()) == SW_DOC_ASSEMBLY else "part"
        else:
            self._write_via_saveas3(abs_path)
        result["files"] = files
        result["bytes"] = sum(os.path.getsize(f) for f in files)
        return result

    def _write_stl_per_component(self, abs_path: str) -> list:
        """SaveAs3 the assembly to STL one file per component; return the files
        that appeared or changed in the folder (SolidWorks names them itself)."""
        folder = os.path.dirname(abs_path)
        os.makedirs(folder, exist_ok=True)

        def stl_files():
            return {os.path.join(folder, f): os.path.getmtime(os.path.join(folder, f))
                    for f in os.listdir(folder) if f.lower().endswith(".stl")}

        before = stl_files()
        self._model.SaveAs3(abs_path, SW_SAVE_AS_CURRENT_VERSION, SW_SAVE_AS_OPTIONS_SILENT)
        written = sorted(f for f, mtime in stl_files().items() if before.get(f) != mtime)
        if not written:
            raise SolidWorksError(f"SolidWorks wrote no STL files into {folder}.")
        return written

    def screenshot(self, path: str, view: str = "iso", zoom_mm: list | None = None,
                   show_planes: bool = False, from_dir: list | None = None) -> dict:
        """Screenshot of the current part or assembly to PNG/JPG/TIF.

        view: 'iso' (default), 'front', 'back', 'left', 'right', 'top' or
        'bottom'; or from_dir = [x, y, z], the direction to look from (model
        axes), e.g. [-1, 1, -1] for an iso view from behind and below, with the
        model's +y kept up. Zoomed to fit, or onto the box zoom_mm = [[x1, y1, z1],
        [x2, y2, z2]] (model mm) to judge a detail. Reference planes and axes
        are left out unless show_planes=True; the part's own setting comes back
        afterwards. Writes via the same SaveAs3 path as `export`, so the return
        shape matches: {"ok", "path", "format", "bytes"} where "format" is the
        image extension.
        """
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        if ext not in {"png", "jpg", "tif"}:  # SaveAs3 to .bmp wrote nothing (returned 256)
            raise SolidWorksError(f"Screenshot extension '{ext}' is not supported (png/jpg/tif).")
        key = str(view).lower()
        if key not in VIEWS:
            raise SolidWorksError(f"Unknown view '{view}'. Use {', '.join(VIEWS)}.")
        if zoom_mm is not None and (len(zoom_mm) != 2 or any(len(corner) != 3 for corner in zoom_mm)):
            raise SolidWorksError(f"zoom_mm needs two corners [[x1, y1, z1], [x2, y2, z2]] (got {zoom_mm}).")
        rotation = None if from_dir is None else self._view_rotation(from_dir)
        model = self._require_model()
        busy = self._command_in_progress(False)  # the view must redraw for the picture
        screen = binding.wrap(model.ActiveView, self._mod.IModelView)
        stilled = screen is not None and not screen.EnableGraphicsUpdate  # by _quiet_ui
        if stilled:
            screen.EnableGraphicsUpdate = True
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        shown = {}
        try:
            if not show_planes:  # planes and axes run through the part and hide its shape
                for toggle in (SW_TOGGLE_DISPLAY_PLANES, SW_TOGGLE_DISPLAY_AXES):
                    shown[toggle] = bool(extension.GetUserPreferenceToggle(toggle, 0))
                    extension.SetUserPreferenceToggle(toggle, 0, False)
                model.GraphicsRedraw2()
            return self._take_screenshot(model, path, ext, key, zoom_mm, rotation)
        finally:
            for toggle, value in shown.items():
                extension.SetUserPreferenceToggle(toggle, 0, value)
            if shown:
                model.GraphicsRedraw2()
            if stilled:
                screen.EnableGraphicsUpdate = False
            self._command_in_progress(busy)

    def _take_screenshot(self, model, path, ext, key, zoom_mm, rotation=None) -> dict:
        if rotation is None:
            model.ShowNamedView2("", VIEWS[key])
        else:
            mathutil = binding.wrap(self._sw.GetMathUtility(), self._mod.IMathUtility)
            data = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                                           [*rotation, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0])  # no shift, scale 1
            view = binding.wrap(model.ActiveView, self._mod.IModelView)
            view.Orientation3 = binding.wrap(mathutil.CreateTransform(data), self._mod.IMathTransform)
        if zoom_mm is None:
            model.ViewZoomtofit2()
        else:
            # ViewZoomTo2 reads its corners along the screen's axes, which are
            # x, y, z only in the front view: turn the region's box first
            rotation = binding.wrap(model.ActiveView, self._mod.IModelView).Orientation3.ArrayData[:9]
            low, high = self._box_along_screen(zoom_mm, rotation)
            model.ViewZoomTo2(*(mm_to_m(c) for c in low), *(mm_to_m(c) for c in high))
        return self.export(path, ext)

    @staticmethod
    def _view_rotation(from_dir) -> tuple:
        """The Orientation3 rotation of a view looking from from_dir: its columns
        are the screen's x, y and z in model axes, z towards the viewer and the
        model's +y up; looking along y, -z or +z is up, as in SolidWorks' top
        and bottom views. Pure, unit-tested."""
        if len(from_dir) != 3 or not math.hypot(*from_dir):
            raise SolidWorksError(f"from_dir needs a direction [x, y, z] that is not zero (got {from_dir}).")
        length = math.hypot(*from_dir)
        z = [c / length for c in from_dir]
        up = [0.0, 1.0, 0.0] if abs(z[1]) < 1 - 1e-9 else [0.0, 0.0, -1.0 if z[1] > 0 else 1.0]
        along = sum(u * c for u, c in zip(up, z))
        y = [u - along * c for u, c in zip(up, z)]
        y = [c / math.hypot(*y) for c in y]
        x = [y[1] * z[2] - y[2] * z[1], y[2] * z[0] - y[0] * z[2], y[0] * z[1] - y[1] * z[0]]
        return tuple(axis[row] for row in range(3) for axis in (x, y, z))

    @staticmethod
    def _box_along_screen(corners_mm, rotation) -> tuple:
        """The box around a model-space box once turned into a view: (low, high)
        along the screen's x, y and z. rotation is the view's Orientation3, whose
        columns are the screen axes in model space; pure, unit-tested."""
        axes = [rotation[col::3] for col in range(3)]
        turned = [[sum(p * a for p, a in zip(corner, axis)) for axis in axes]
                  for corner in itertools.product(*zip(*corners_mm))]
        return [min(t[i] for t in turned) for i in range(3)], [max(t[i] for t in turned) for i in range(3)]

    def make_drawing(self, path: str) -> dict:
        """A 2D drawing of the current part, saved as PDF or as an editable .slddrw.

        Front, top and right views in European (first angle) projection plus an
        isometric view, on an A4 sheet at the scale SolidWorks picks, with the
        model's own dimensions, each shown once. The drawing refers to the part's
        file, so the part must have been saved; unsaved changes show in a PDF
        but not when a saved drawing is opened later.
        """
        fmt = os.path.splitext(path)[1].lstrip(".").lower()
        if fmt not in DRAWING_FORMATS:
            raise SolidWorksError(f"A drawing is saved as {' or '.join(sorted(DRAWING_FORMATS))} (got '{fmt}').")
        self._require_part()
        part_path = self._model.GetPathName()
        if not part_path:
            raise SolidWorksError("Save the part first (save_part): the drawing refers to its file.")
        template = self._sw.GetUserPreferenceStringValue(SW_PREF_DEFAULT_TEMPLATE_DRAWING)
        drawing = binding.wrap(self._sw.NewDocument(template, SW_DWG_PAPER_A4, 0.0, 0.0), self._mod.IModelDoc2)
        if drawing is None:
            raise SolidWorksError(f"SolidWorks could not start a drawing from the template '{template}'.")
        try:
            sheets = binding.wrap(drawing, self._mod.IDrawingDoc)
            if not sheets.Create1stAngleViews2(part_path):
                raise SolidWorksError("SolidWorks could not place the front, top and right views.")
            iso = binding.wrap(sheets.CreateDrawViewFromModelView3(part_path, "*Isometric", 0.24, 0.16, 0.0),
                               self._mod.IView)
            if iso is None:
                raise SolidWorksError("SolidWorks could not place the isometric view.")
            iso.UseSheetScale = True
            # DuplicateDims True leaves out repeats: each dimension in one view
            dimensions = sheets.InsertModelAnnotations3(SW_IMPORT_ENTIRE_MODEL, SW_INSERT_DIMENSIONS,
                                                        True, True, False, False) or ()
            views = []
            view = binding.wrap(sheets.GetFirstView(), self._mod.IView)  # the sheet itself comes first
            view = binding.wrap(view.GetNextView(), self._mod.IView)
            while view is not None:
                views.append(view.GetName2())
                view = binding.wrap(view.GetNextView(), self._mod.IView)
            scale = binding.wrap(sheets.GetCurrentSheet(), self._mod.ISheet).GetProperties2()[2:4]
            abs_path = os.path.abspath(path)
            self._write_via_saveas3(abs_path, drawing)
        finally:
            # saved, the drawing is named after the part ('holed_box - Sheet1'),
            # so the title it started with no longer closes it (verified)
            self._sw.CloseDoc(drawing.GetTitle())
        return {"ok": True, "path": abs_path, "bytes": os.path.getsize(abs_path), "views": views,
                "dimensions": len(dimensions), "scale": f"{scale[0]:g}:{scale[1]:g}"}

    # --- assemblies -----------------------------------------------------------
    #
    # Everything below drives an ASSEMBLY document (M6). Three API facts were
    # cracked empirically against this build and the code depends on all three:
    #
    # 1. AddComponent5 returns None unless the part is already LOADED, so each
    #    component is opened silently first and the assembly re-activated.
    # 2. AddComponent5's X/Y/Z do NOT place the part's origin: it drops the
    #    component with its bounding-box CENTRE at that point. Positioning
    #    therefore always goes through the component transform, which is written
    #    and then read back and compared.
    # 3. IMathTransform.ArrayData holds the rotation COLUMN-major (data[0:3] is
    #    the first column, not the first row) -- the transpose of the obvious
    #    reading, verified by rotating a component 90 deg and checking its box.
    #
    # A component's own faces come back in COMPONENT-local coordinates even when
    # the component is rotated, so a face selector like '-x' always means "the
    # part's own -X face", independent of how it is turned in the assembly.

    def new_assembly(self) -> dict:
        """Create a new empty assembly; it becomes the current document."""
        sw = self._ensure()
        template = sw.GetUserPreferenceStringValue(SW_PREF_DEFAULT_TEMPLATE_ASSEMBLY)
        model = None
        if template and os.path.isfile(template):
            model = binding.wrap(sw.NewDocument(template, 0, 0, 0), self._mod.IModelDoc2)
        if model is None:
            # Fallback avoids a "template not found" modal dialog, as new_part does.
            model = binding.wrap(sw.NewAssembly(), self._mod.IModelDoc2)
        if model is None:
            raise SolidWorksError(
                "Could not create a new assembly (template and NewAssembly both failed)."
            )
        self._model = model
        return {"ok": True, "title": model.GetTitle()}

    def open_assembly(self, path: str) -> dict:
        """Open an existing .sldasm, or import a STEP, IGES or Parasolid assembly;
        it becomes the current document. An import reports its component count."""
        abs_path, imported = self._native_or_neutral(path, "sldasm", "an assembly")
        sw = self._ensure()
        if imported:
            self._model = self._import_document(sw, abs_path, SW_DOC_ASSEMBLY)
            return {"ok": True, "title": self._model.GetTitle(), "path": abs_path, "imported": True,
                    "components": len(self._components(self._require_assembly()))}
        result = sw.OpenDoc6(abs_path, SW_DOC_ASSEMBLY, SW_OPEN_DOC_SILENT, "", 0, 0)
        doc = result[0] if isinstance(result, tuple) else result
        model = binding.wrap(doc, self._mod.IModelDoc2)
        if model is None:
            raise SolidWorksError(f"Could not open the assembly: {abs_path}")
        self._model = model
        return {"ok": True, "title": model.GetTitle(), "path": abs_path}

    def save_assembly(self, path: str) -> dict:
        """Save the current assembly to a native .sldasm file (silent)."""
        self._require_assembly()
        abs_path = os.path.abspath(path)
        if not abs_path.lower().endswith(".sldasm"):
            abs_path += ".sldasm"
        self._write_via_saveas3(abs_path)
        return {"ok": True, "path": abs_path, "bytes": os.path.getsize(abs_path)}

    # --- component placement (pure maths, unit-tested) ------------------------

    @staticmethod
    def _rotation_columns(rx_deg: float, ry_deg: float, rz_deg: float) -> list:
        """R = Rz*Ry*Rx as SolidWorks' COLUMN-major 9-float array; pure (no COM).

        Rotations are applied X first, then Y, then Z, about the assembly axes,
        and act on the component's own origin (p_assembly = R * p_part + t).
        """
        a, b, c = deg_to_rad(rx_deg), deg_to_rad(ry_deg), deg_to_rad(rz_deg)
        ca, sa = math.cos(a), math.sin(a)
        cb, sb = math.cos(b), math.sin(b)
        cc, sc = math.cos(c), math.sin(c)
        rows = [
            [cc * cb, cc * sb * sa - sc * ca, cc * sb * ca + sc * sa],
            [sc * cb, sc * sb * sa + cc * ca, sc * sb * ca - cc * sa],
            [-sb, cb * sa, cb * ca],
        ]
        return [rows[row][col] for col in range(3) for row in range(3)]

    @staticmethod
    def _euler_from_columns(columns) -> tuple:
        """Inverse of _rotation_columns -> (rx, ry, rz) in degrees; pure (no COM).

        At ry = +/-90 degrees the X and Z rotations become the same motion
        (gimbal lock); there we report rz = 0 and fold the whole rotation into
        rx, which still reproduces the matrix.
        """
        r = [[columns[col * 3 + row] for col in range(3)] for row in range(3)]
        ry = math.asin(max(-1.0, min(1.0, -r[2][0])))
        if abs(r[2][0]) > 1.0 - 1e-9:  # cos(ry) ~ 0: gimbal lock
            rx, rz = math.atan2(-r[1][2], r[1][1]), 0.0
        else:
            rx, rz = math.atan2(r[2][1], r[2][2]), math.atan2(r[1][0], r[0][0])
        return tuple(round(math.degrees(v), 6) for v in (rx, ry, rz))

    def _make_transform(self, x_mm, y_mm, z_mm, rx_deg, ry_deg, rz_deg):
        """Build an IMathTransform from a position (mm) and XYZ rotations (deg)."""
        data = self._rotation_columns(rx_deg, ry_deg, rz_deg) + [
            mm_to_m(x_mm), mm_to_m(y_mm), mm_to_m(z_mm),
            1.0,            # uniform scale
            0.0, 0.0, 0.0,  # unused
        ]
        mathutil = binding.wrap(self._sw.GetMathUtility(), self._mod.IMathUtility)
        coords = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, data)
        xform = binding.wrap(mathutil.CreateTransform(coords), self._mod.IMathTransform)
        if xform is None:
            raise SolidWorksError("CreateTransform returned None; could not build a transform.")
        return xform

    def _transform_data(self, comp) -> list:
        """The component's transform as the raw 16-float ArrayData."""
        xform = binding.wrap(comp.Transform2, self._mod.IMathTransform)
        if xform is None:
            raise SolidWorksError(f"Component '{comp.Name2}' has no readable transform.")
        return list(xform.ArrayData)

    def _placement(self, comp) -> dict:
        """Where a component sits: position (mm) + XYZ rotation (deg)."""
        data = self._transform_data(comp)
        rx, ry, rz = self._euler_from_columns(data[:9])
        return {
            "position_mm": [round(m_to_mm(v), 4) for v in data[9:12]],
            "rotation_deg": [rx, ry, rz],
        }

    # Read-back tolerances for a written transform. SolidWorks stores the matrix
    # as doubles and hands it back unchanged, so anything above round-off means
    # the write did NOT take (a fixed component, or a mate already driving it).
    _TRANSFORM_TOLERANCE_MM = 1e-6
    _ROTATION_TOLERANCE = 1e-9

    def _apply_transform(self, comp, x_mm, y_mm, z_mm, rx_deg, ry_deg, rz_deg) -> dict:
        """Write a component transform, then read it back and verify it stuck.

        A silently ignored transform is exactly the failure this repo refuses to
        pass on, so the written matrix is compared element by element with what
        SolidWorks reports afterwards.
        """
        wanted = self._rotation_columns(rx_deg, ry_deg, rz_deg) + [
            mm_to_m(x_mm), mm_to_m(y_mm), mm_to_m(z_mm)]
        comp.Transform2 = self._make_transform(x_mm, y_mm, z_mm, rx_deg, ry_deg, rz_deg)
        got = self._transform_data(comp)[:12]
        for i, (want, have) in enumerate(zip(wanted, got)):
            tol = self._ROTATION_TOLERANCE if i < 9 else mm_to_m(self._TRANSFORM_TOLERANCE_MM)
            if abs(want - have) > tol:
                raise SolidWorksError(
                    f"The transform of '{comp.Name2}' was not applied: requested position "
                    f"({x_mm:g}, {y_mm:g}, {z_mm:g}) mm, rotation ({rx_deg:g}, {ry_deg:g}, "
                    f"{rz_deg:g}) degrees; read-back position "
                    f"{[round(m_to_mm(v), 4) for v in got[9:12]]} mm. Element {i} is off by "
                    f"{abs(want - have):.3e}. Does an existing mate already constrain this component?"
                )
        return self._placement(comp)

    # --- components -----------------------------------------------------------

    def _component_dispatches(self, asm) -> list:
        """Top-level components of the assembly, as raw dispatches (never None)."""
        comps = asm.GetComponents(True)
        if not comps:
            return []
        return list(comps) if isinstance(comps, (list, tuple)) else [comps]

    def _components(self, asm) -> list:
        """Top-level components as early-bound IComponent2, in tree order."""
        return [binding.wrap(c, self._mod.IComponent2) for c in self._component_dispatches(asm)]

    def _component_by_name(self, asm, name: str):
        """Resolve a component by instance name ('Bed-1') or part name ('Bed'),
        or one inside a sub-assembly by its path ('Leg-1/Thigh-1').

        The short form is accepted only while it is unambiguous; with two copies
        inserted it raises and lists the instance names instead of guessing.
        """
        key = (name or "").strip().lower()
        if not key:
            raise SolidWorksError("Give a component name.")
        if "/" in key:  # SolidWorks names a component inside a sub-assembly by its path
            inside = self._nested_components(self._components(asm))
            found = [c for c in inside if c.Name2.lower() == key]
            if len(found) == 1:
                return found[0]
            raise SolidWorksError(f"Component '{name}' not found. Inside the sub-assemblies: "
                                  f"{[c.Name2 for c in inside if '/' in c.Name2]}.")
        comps = self._components(asm)
        exact = [c for c in comps if c.Name2.lower() == key]
        if len(exact) == 1:
            return exact[0]
        prefixed = [c for c in comps if c.Name2.lower().startswith(key + "-")]
        if len(prefixed) == 1:
            return prefixed[0]
        if len(prefixed) > 1:
            raise SolidWorksError(
                f"Component name '{name}' is not unique; candidates: "
                f"{sorted(c.Name2 for c in prefixed)}."
            )
        raise SolidWorksError(
            f"Component '{name}' not found. Present: {[c.Name2 for c in comps]}."
        )

    def _component_box(self, comp) -> dict | None:
        """The component's tight bounding box in ASSEMBLY coordinates (mm), or
        None when it holds no solid material (suppressed, or surfaces only).

        IComponent2.GetBox boxes the component's own box, turned with it: too
        big for a rotated round or slanted part (an arm was reported reaching
        y = 0 while it ended at -4.7). So each solid body's extreme points along
        the assembly axes are found in its part's frame and mapped through that
        part's transform; a sub-assembly joins the boxes of its parts.
        """
        return self._joined_box([self._part_box(part) for part in self._solid_parts(comp)])

    def _part_box(self, part) -> dict | None:
        bodies = self._solid_bodies(part)
        if not bodies:
            return None
        rotation, shift, scale = self._frame(part)
        low, high = [math.inf] * 3, [-math.inf] * 3
        for body in bodies:
            for axis in range(3):
                for sign in (1.0, -1.0):
                    # the assembly axis seen from the part: row `axis` of R (R transposed)
                    point = self._extreme_point(body, [sign * c for c in rotation[axis]])
                    coordinate = scale * sum(r * p for r, p in zip(rotation[axis], point)) + shift[axis]
                    low[axis], high[axis] = min(low[axis], coordinate), max(high[axis], coordinate)
        return self._joined_box([{"min_mm": [round(m_to_mm(v), 4) for v in low],
                                  "max_mm": [round(m_to_mm(v), 4) for v in high]}])

    @staticmethod
    def _joined_box(boxes) -> dict | None:
        """The box around `boxes` (min_mm / max_mm); None entries hold nothing."""
        boxes = [box for box in boxes if box]
        if not boxes:
            return None
        low = [min(box["min_mm"][axis] for box in boxes) for axis in range(3)]
        high = [max(box["max_mm"][axis] for box in boxes) for axis in range(3)]
        return {"min_mm": low, "max_mm": high, "size_mm": [round(h - l, 4) for l, h in zip(low, high)]}

    def _nested_components(self, comps) -> list:
        """The components and everything inside them, at any depth."""
        out = []
        for comp in comps:
            out.append(comp)
            out += self._nested_components([binding.wrap(c, self._mod.IComponent2) for c in comp.GetChildren() or ()])
        return out

    def _part_components(self, comp) -> list:
        """comp itself when it is a part, else the parts inside it at any depth.
        A part's transform is relative to the top assembly (verified), so each
        maps straight to assembly coordinates."""
        children = comp.GetChildren() or ()
        if not children:
            return [comp]
        return [part for child in children
                for part in self._part_components(binding.wrap(child, self._mod.IComponent2))]

    def _solid_parts(self, comp) -> list:
        """The parts holding comp's material. A suppressed part holds none; a
        lightweight one cannot be measured, as its geometry is not loaded."""
        parts = []
        for part in self._part_components(comp):
            state = part.GetSuppression2()
            if state in SW_COMPONENT_LIGHTWEIGHT_STATES:
                raise SolidWorksError(f"Component '{part.Name2}' is lightweight: SolidWorks has not loaded its "
                                      "geometry, so it cannot be measured. Set it to resolved.")
            if state != SW_COMPONENT_SUPPRESSED:
                parts.append(part)
        return parts

    def _solid_bodies(self, part) -> list:
        """The part component's solid bodies (part coordinates)."""
        bodies = part.GetBodies2(SW_BODY_SOLID) or ()
        if not isinstance(bodies, (list, tuple)):
            bodies = [bodies]
        return [binding.wrap(body, self._mod.IBody2) for body in bodies]

    def _frame(self, comp):
        """(rotation rows, shift in m, scale) of the component's transform, whose
        ArrayData holds the rotation column-major."""
        data = self._transform_data(comp)
        return [[data[col * 3 + row] for col in range(3)] for row in range(3)], data[9:12], data[12]

    @staticmethod
    def _extreme_point(body, direction) -> tuple:
        """The body's point furthest along `direction` (its own coordinates, m)."""
        result = body.GetExtremePoint(*direction)
        if not isinstance(result, tuple) or len(result) < 3 or (len(result) == 4 and not result[0]):
            raise SolidWorksError(f"SolidWorks found no extreme point of a body (got {result!r}).")
        return result[-3:]

    def _component_entry(self, comp) -> dict:
        return {
            "name": comp.Name2,
            "path": comp.GetPathName(),
            "fixed": bool(comp.IsFixed()),
            **self._placement(comp),
            "bounding_box_mm": self._component_box(comp),
        }

    def _fix_component(self, asm, comp) -> None:
        """Pin a component in place (FixComponent acts on the selection), and verify."""
        model = self._require_model()
        model.ClearSelection2(True)
        selmgr = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        if not comp.Select4(False, selmgr.CreateSelectData(), False):
            raise SolidWorksError(f"Could not select component '{comp.Name2}'.")
        asm.FixComponent()
        model.ClearSelection2(True)
        if not comp.IsFixed():
            raise SolidWorksError(f"Component '{comp.Name2}' could not be fixed.")

    def insert_component(self, path: str, x_mm: float = 0.0, y_mm: float = 0.0,
                         z_mm: float = 0.0, fixed: bool | None = None) -> dict:
        """Insert a part (.sldprt) or a sub-assembly (.sldasm) into the current
        assembly with its ORIGIN at (x, y, z) mm.

        The part's own origin lands on the given point (AddComponent5's own X/Y/Z
        would centre the bounding box there instead, so the position is applied
        as a transform and verified by reading it back).

        fixed: True pins the component in place, False leaves it free to be moved
        by mates. The default (None) fixes only the FIRST component, which is the
        ground the rest of the assembly is positioned against.
        """
        abs_path = os.path.abspath(path)
        doc_type = {".sldprt": SW_DOC_PART, ".sldasm": SW_DOC_ASSEMBLY}.get(os.path.splitext(abs_path)[1].lower())
        if doc_type is None:
            raise SolidWorksError(f"insert_component takes a .sldprt or a .sldasm (got '{path}').")
        asm = self._require_assembly()
        if not os.path.isfile(abs_path):
            raise SolidWorksError(f"Part not found: {abs_path}")
        if fixed is None:
            fixed = not self._component_dispatches(asm)

        # AddComponent5 gives None for a part that is not loaded, so open it
        # silently first and switch back to the assembly before inserting.
        title = self._model.GetTitle()
        self._sw.OpenDoc6(abs_path, doc_type, SW_OPEN_DOC_SILENT, "", 0, 0)
        self._sw.ActivateDoc3(title, True, 0, 0)

        comp = binding.wrap(
            asm.AddComponent5(abs_path, SW_ADD_COMPONENT_CURRENT_CONFIG, "", False, "",
                              0.0, 0.0, 0.0),
            self._mod.IComponent2,
        )
        if comp is None:
            raise SolidWorksError(
                f"AddComponent5 returned None for '{abs_path}'. Is it a valid "
                "SolidWorks part, and could SolidWorks load it?"
            )
        self._apply_transform(comp, x_mm, y_mm, z_mm, 0.0, 0.0, 0.0)
        if fixed:
            self._fix_component(asm, comp)
        return {"ok": True, "component": self._component_entry(comp)}

    def list_components(self) -> dict:
        """List the components (name, path, fixed, placement, bounding box) and
        the mates: name, SolidWorks type, the dimension of a distance or angle
        mate, and the error of a mate SolidWorks flags."""
        asm = self._require_assembly()
        comps = [self._component_entry(c) for c in self._components(asm)]
        return {"ok": True, "count": len(comps), "components": comps,
                "mates": [self._mate_entry(mate) for mate in self._mates()]}

    def _mate_entry(self, mate) -> dict:
        entry = {"name": mate.Name, "type": mate.GetTypeName2()}
        dimensions = self._dimension_entries(mate)
        if dimensions:
            entry["dimensions"] = [{key: d[key] for key in ("name", "value", "unit")} for d in dimensions]
        trouble = self._mate_trouble(mate)
        if trouble:
            entry["error"] = trouble
        return entry

    def _mate_trouble(self, mate) -> dict | None:
        """What is wrong with a mate: the error SolidWorks flags, or a face or
        edge it lost without one (a mate entity left without its reference)."""
        code, warning = mate.GetErrorCode2()
        if code:
            return self._error_entry(code, warning)
        details = binding.wrap(mate.GetSpecificFeature2(), self._mod.IMate2)
        if details is None or mate.IsSuppressed():  # suppressed, it holds nothing on purpose
            return None
        entities = [binding.wrap(details.MateEntity(i), self._mod.IMateEntity2) for i in range(details.GetMateEntityCount())]
        if any(entity is None or entity.Reference is None for entity in entities):
            return {"code": None, "warning": False, "cause": "broken: a face or edge it used is gone"}
        return None

    def _refuse_broken_mates(self) -> None:
        """Stepping a joint while a mate is broken lets the parts it should hold
        drift: a concentric mate on a hole that was made again held nothing,
        and a part slid off its axis through the next one."""
        self._model.ForceRebuild3(False)
        broken = [f"{mate.Name} ({trouble['cause']})" for mate in self._mates()
                  if (trouble := self._mate_trouble(mate)) and not trouble["warning"]]
        if broken:
            raise SolidWorksError(f"Mate(s) {', '.join(broken)}: the parts they should hold can drift, so stepping "
                                  "the joint means nothing. Repair them, or delete_mate and add them again.")

    def _mate_by_name(self, name: str):
        mates = self._mates()
        found = [m for m in mates if m.Name == name] or [m for m in mates if m.Name.lower() == str(name).lower()]
        if len(found) != 1:
            raise SolidWorksError(f"No mate '{name}' (there are: {', '.join(m.Name for m in mates) or 'none'}).")
        return found[0]

    def delete_mate(self, name: str) -> dict:
        """Delete a mate of the current assembly by name (list_components, or
        add_mate's 'mate'); its components are free to move again."""
        model = self._require_model()
        asm = self._require_assembly()
        mate = self._mate_by_name(name)
        title = mate.Name
        model.ClearSelection2(True)
        if not mate.Select2(False, 0):
            raise SolidWorksError(f"Could not select mate '{title}'.")
        if not binding.wrap(model.Extension, self._mod.IModelDocExtension).DeleteSelection2(SW_DELETE_ABSORBED):
            raise SolidWorksError(f"SolidWorks refused to delete mate '{title}'.")
        model.ClearSelection2(True)
        asm.EditRebuild()
        return {"ok": True, "deleted": title, "mates": [m.Name for m in self._mates()]}

    def suppress_mate(self, name: str, suppress: bool = True) -> dict:
        """Suppress a mate, so it stops holding its components while it stays in
        the tree, or bring it back with suppress=False."""
        self._require_model()
        asm = self._require_assembly()
        mate = self._mate_by_name(name)
        action = SW_SUPPRESS_FEATURE if suppress else SW_UNSUPPRESS_DEPENDENT
        if not mate.SetSuppression2(action, SW_THIS_CONFIGURATION, None):
            raise SolidWorksError(f"SolidWorks refused to {'suppress' if suppress else 'unsuppress'} mate '{mate.Name}'.")
        asm.EditRebuild()
        mate = self._mate_by_name(name)
        return {"ok": True, "mate": mate.Name, "suppressed": self._is_suppressed(mate)}

    def delete_component(self, component: str) -> dict:
        """Remove a component from the current assembly, with the mates that
        hold it. Returns the mates that went with it, the components left and the
        assembly's mass properties."""
        model = self._require_model()
        asm = self._require_assembly()
        comp = self._component_by_name(asm, component)
        name = comp.Name2
        mates_before = [m.Name for m in self._mates()]
        model.ClearSelection2(True)
        selmgr = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        if not comp.Select4(False, selmgr.CreateSelectData(), False):
            raise SolidWorksError(f"Could not select component '{name}'.")
        if not binding.wrap(model.Extension, self._mod.IModelDocExtension).DeleteSelection2(SW_DELETE_ABSORBED):
            raise SolidWorksError(f"SolidWorks refused to delete component '{name}'.")
        model.ClearSelection2(True)
        mates_after = {m.Name for m in self._mates()}
        return {"ok": True, "deleted": name, "mates_deleted": [m for m in mates_before if m not in mates_after],
                "components": [c.Name2 for c in self._components(asm)],
                "mass_properties": self.get_mass_properties()["mass_properties"]}

    def set_component_transform(self, name: str, x_mm: float, y_mm: float, z_mm: float,
                                rx_deg: float = 0.0, ry_deg: float = 0.0,
                                rz_deg: float = 0.0) -> dict:
        """Move/rotate a component: origin to (x, y, z) mm, rotated rx/ry/rz degrees.

        Rotations are applied X, then Y, then Z about the assembly axes, and work
        on a fixed component too (it simply becomes fixed at the new spot). The
        transform is read back and compared, so a write SolidWorks ignored fails
        loudly. Note that mates re-solve on the next rebuild and will override a
        manual move.
        """
        asm = self._require_assembly()
        comp = self._component_by_name(asm, name)
        placement = self._apply_transform(comp, x_mm, y_mm, z_mm, rx_deg, ry_deg, rz_deg)
        return {"ok": True, "component": comp.Name2, **placement,
                "bounding_box_mm": self._component_box(comp)}

    # --- faces inside a component --------------------------------------------

    def _component_faces(self, comp) -> list:
        """Every face of every solid body of the component as (face, part): a
        part component's own faces, or those of every part in a sub-assembly.
        Faces keep their part's coordinates. They come in a fixed order, by
        part, planar first, then centre and area, so an index stays the same
        face while the shape stays: SolidWorks' own order changed between two
        calls in one assembly, after a mate."""
        faces = []
        for part in self._solid_parts(comp):
            for body in self._solid_bodies(part):
                faces.extend((binding.wrap(face, self._mod.IFace2), part) for face in body.GetFaces() or ())
        if not faces:
            raise SolidWorksError(f"Component '{comp.Name2}' has no solid body to pick a face on.")
        return sorted(faces, key=lambda found: (found[1].Name2, *self._face_order(found[0])))

    def _face_order(self, face) -> tuple:
        surface = binding.wrap(face.GetSurface(), self._mod.ISurface)
        box = face.GetBox()
        centre = tuple(round(m_to_mm((box[j] + box[j + 3]) / 2), 3) for j in range(3))
        return (0 if surface is not None and surface.IsPlane() else 1, *centre, round(face.GetArea() * 1e6, 3))

    def _part_frame_in(self, comp, part) -> tuple:
        """(rotation rows, shift mm) taking `part` coordinates into the coordinates
        of `comp`, the component it sits in (identity for comp itself)."""
        if part.Name2 == comp.Name2:
            return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], [0.0, 0.0, 0.0]
        outer, inner = self._frame(comp), self._frame(part)
        rotation, shift = self._relative_frame((outer[0], outer[1]), (inner[0], inner[1]))
        return rotation, [m_to_mm(s) for s in shift]

    @staticmethod
    def _relative_frame(outer, inner) -> tuple:
        """(rotation rows, shift) taking `inner` coordinates into `outer`'s, both
        given as (rotation rows, shift) relative to one root: R_o^T R_i and
        R_o^T (t_i - t_o). Pure, unit-tested."""
        (r_o, t_o), (r_i, t_i) = outer, inner
        rotation = [[sum(r_o[k][row] * r_i[k][col] for k in range(3)) for col in range(3)] for row in range(3)]
        shift = [sum(r_o[k][row] * (t_i[k] - t_o[k]) for k in range(3)) for row in range(3)]
        return rotation, shift

    def _component_face(self, comp, selector: str):
        """Face '+x' / '-z:inner' of a component, face '#5' by its
        list_faces(component=...) index, or face '@x,y,z' through that point;
        returns (IFace2, the part it is on).

        The direction is read in the COMPONENT's own coordinate system (verified:
        a component's faces keep part coordinates however the component is
        turned), so '-x' is always the part's own -X face; in a sub-assembly, the
        sub-assembly's -X. ':inner' picks the cavity side of a hollow part -- the
        inside of a room wall, not its skin. An index reaches any face, such as a
        hole for a concentric mate; so does a point on it, in the same coordinates.
        """
        faces = self._component_faces(comp)
        if str(selector).strip().startswith("@"):
            return self._component_face_at(comp, faces, self._point_selector(selector))
        index = self._face_index(selector)
        if index is not None:
            if index >= len(faces):
                raise SolidWorksError(f"Component '{comp.Name2}' has faces #0 to #{len(faces) - 1}; "
                                      "list_faces(component=...) shows them.")
            face, part = faces[index]
            return binding.wrap(face, self._mod.IFace2), part
        normal, side = self._parse_face_selector(selector)
        if side not in self._FACE_SIDES:
            raise SolidWorksError(f"Unknown face side '{side}'. Use 'outer' or 'inner'.")
        facing = []
        for part in {part.Name2: part for _, part in faces}.values():
            rotation, shift = self._part_frame_in(comp, part)
            # the direction as the part sees it, and where its faces lie along it in comp's frame
            target = [sum(rotation[k][i] * normal[k] for k in range(3)) for i in range(3)]
            along = sum(s * n for s, n in zip(shift, normal))
            facing += [(face, position + along, part)
                       for face, position in self._planar_faces_facing([f for f, p in faces if p is part], target)]
        if not facing:
            raise SolidWorksError(
                f"Component '{comp.Name2}' has no planar face pointing {selector}."
            )
        face, _, part = (max if side == "outer" else min)(facing, key=lambda found: found[1])
        return face, part

    @staticmethod
    def _point_selector(selector) -> list:
        """'@0, 0, 20.2' -> [0.0, 0.0, 20.2]; pure, unit-tested."""
        try:
            point = [float(c) for c in str(selector).strip()[1:].split(",")]
        except ValueError:
            point = []
        if len(point) != 3:
            raise SolidWorksError(f"A face by a point on it is '@x,y,z', in mm (got {selector!r}).")
        return point

    _ON_COMPONENT_FACE_MM = 0.01

    def _component_face_at(self, comp, faces, point_mm):
        """The component's face through point_mm, in the component's own coordinates."""
        best = None
        for face, part in faces:
            rotation, shift = self._part_frame_in(comp, part)
            local = [sum(rotation[k][i] * (point_mm[k] - shift[k]) for k in range(3)) for i in range(3)]  # R^T
            nearest = face.GetClosestPointOn(*(mm_to_m(c) for c in local))
            gap = math.dist(local, [m_to_mm(c) for c in nearest[:3]])
            if best is None or gap < best[0]:
                best = (gap, face, part)
        gap, face, part = best
        if gap > self._ON_COMPONENT_FACE_MM:
            raise SolidWorksError(f"No face of '{comp.Name2}' goes through ({', '.join(f'{c:g}' for c in point_mm)}) "
                                  f"in its own coordinates: the nearest lies {gap:.3g} mm away.")
        return face, part

    def _face_plane_in_assembly(self, comp, face):
        """A planar component face as (point on its plane, normal) in ASSEMBLY
        coordinates, in metres.

        The face is reported in component coordinates, so both are pushed through
        the component transform: p' = R*p + t for the point, R*n for the normal
        (R is orthonormal here -- components are never scaled). The point is the
        plane's own root point: the box centre of a slanted face that is not
        symmetric lies off its plane (0.87 mm on a drafted wedge, measured).
        """
        data = self._transform_data(comp)
        rot, trans = data[:9], data[9:12]

        def rotate(v):
            # ArrayData is COLUMN-major: rot[3*col + row] is row `row` of column `col`.
            return [sum(rot[3 * col + row] * v[col] for col in range(3)) for row in range(3)]

        root = rotate(list(binding.wrap(face.GetSurface(), self._mod.ISurface).PlaneParams)[3:6])
        point = [root[i] + trans[i] for i in range(3)]
        return point, rotate(list(face.Normal))

    # --- mates ----------------------------------------------------------------

    # A mate that builds but resolves to the wrong side is a silent geometry
    # error, so every mate is measured back from the geometry afterwards: the
    # perpendicular distance between the two mated planes (coincident/distance),
    # the angle between their normals (parallel/perpendicular/angle), or how far
    # apart two cylinders' axes lie (concentric).
    _MATE_DISTANCE_TOLERANCE_MM = 1e-3
    _MATE_ANGLE_TOLERANCE_DEG = 0.01
    _MATE_EXPECTED_ANGLE_DEG = {"parallel": 0.0, "perpendicular": 90.0}

    def _measure_mate(self, comp_a, face_a, comp_b, face_b) -> tuple:
        """(perpendicular distance mm, angle between normals deg) after a rebuild.

        The angle is 0..180 degrees: normals pointing the same way are 0 apart."""
        point_a, normal_a = self._face_plane_in_assembly(comp_a, face_a)
        point_b, normal_b = self._face_plane_in_assembly(comp_b, face_b)
        gap = abs(sum((point_b[i] - point_a[i]) * normal_a[i] for i in range(3)))
        dot = sum(normal_a[i] * normal_b[i] for i in range(3))
        return m_to_mm(gap), math.degrees(math.acos(max(-1.0, min(1.0, dot))))

    def _axis_in_assembly(self, comp, face):
        """A cylindrical component face's axis as (point, unit direction) in
        ASSEMBLY coordinates, in metres."""
        params = binding.wrap(face.GetSurface(), self._mod.ISurface).CylinderParams
        rotation, shift, scale = self._frame(comp)
        point = [scale * sum(rotation[row][i] * params[i] for i in range(3)) + shift[row] for row in range(3)]
        direction = [sum(rotation[row][i] * params[3 + i] for i in range(3)) for row in range(3)]
        length = math.hypot(*direction)
        return point, [d / length for d in direction]

    def _axes_apart(self, comp_a, face_a, comp_b, face_b) -> tuple:
        """(distance mm of b's axis from a's, angle deg between the axes) for two
        cylindrical faces: both 0 when they share one axis."""
        point_a, axis_a = self._axis_in_assembly(comp_a, face_a)
        point_b, axis_b = self._axis_in_assembly(comp_b, face_b)
        offset = [b - a for a, b in zip(point_a, point_b)]
        along = sum(o * d for o, d in zip(offset, axis_a))
        across = math.dist(offset, [along * d for d in axis_a])
        dot = abs(sum(a * b for a, b in zip(axis_a, axis_b)))
        return m_to_mm(across), math.degrees(math.acos(max(-1.0, min(1.0, dot))))

    def _check_mate_faces(self, key, faces):
        """Concentric takes cylinders, every other type planar faces."""
        for selector, comp, face in faces:
            surface = binding.wrap(face.GetSurface(), self._mod.ISurface)
            if key == "concentric" and not surface.IsCylinder():
                raise SolidWorksError(f"A concentric mate takes cylindrical faces; {comp.Name2}:{selector} is not "
                                      "one. list_faces(component=...) shows the cylinders with their index.")
            if key != "concentric" and not surface.IsPlane():
                raise SolidWorksError(f"A {key} mate takes planar faces; {comp.Name2}:{selector} is not planar.")

    def _mates(self) -> list:
        """The assembly's mates (the MateGroup's sub-features), in tree order."""
        return [mate for feat in self._iter_features() if feat.GetTypeName2() == "MateGroup"
                for mate in self._sub_features(feat)]

    def _undo_mate(self, asm, mates_before: set, placements: list) -> None:
        """Delete the mates added since mates_before and put the components back,
        so a refused mate does not stay to fight the next attempt."""
        model = self._model
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        for mate in [m for m in self._mates() if m.Name not in mates_before]:
            model.ClearSelection2(True)
            if mate.Select2(False, 0):
                extension.DeleteSelection2(0)
        model.ClearSelection2(True)
        for comp, data in placements:
            comp.Transform2 = self._transform_from_data(data)
        asm.EditRebuild()

    def _transform_from_data(self, data):
        mathutil = binding.wrap(self._sw.GetMathUtility(), self._mod.IMathUtility)
        coords = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(data))
        return binding.wrap(mathutil.CreateTransform(coords), self._mod.IMathTransform)

    def _mate_problem(self, key, first, face_1, second, face_2, distance_mm, angle_deg) -> str | None:
        """Why the rebuilt mate does not hold what was asked, or None."""
        where = f"{first.Name2} and {second.Name2}"
        if key == "concentric":
            across_mm, tilt_deg = self._axes_apart(first, face_1, second, face_2)
            if across_mm > self._MATE_DISTANCE_TOLERANCE_MM or tilt_deg > self._MATE_ANGLE_TOLERANCE_DEG:
                return (f"Mate 'concentric' was built but the axes of {where} are {across_mm:.4f} mm and "
                        f"{tilt_deg:.4f} degrees apart. Check whether another mate works against it.")
            return None
        measured_mm, measured_deg = self._measure_mate(first, face_1, second, face_2)
        expected_mm = {"distance": distance_mm, "coincident": 0.0}.get(key)
        if expected_mm is not None and abs(measured_mm - expected_mm) > self._MATE_DISTANCE_TOLERANCE_MM:
            return (f"Mate '{key}' was built but gives {measured_mm:.4f} mm instead of {expected_mm:g} mm "
                    f"between {where}. Try flip=True, or check whether another mate works against it.")
        if key in self._MATE_EXPECTED_ANGLE_DEG:
            # parallel and perpendicular hold whichever way the normals point
            apart = min(measured_deg, 180.0 - measured_deg)
            if abs(apart - self._MATE_EXPECTED_ANGLE_DEG[key]) > self._MATE_ANGLE_TOLERANCE_DEG:
                return (f"Mate '{key}' was built but the faces of {where} are {apart:.4f} degrees apart "
                        f"instead of {self._MATE_EXPECTED_ANGLE_DEG[key]:g}.")
        if key == "angle" and abs(measured_deg - angle_deg) > self._MATE_ANGLE_TOLERANCE_DEG:
            return (f"Mate 'angle' was built but the faces of {where} are {measured_deg:.4f} degrees apart "
                    f"instead of {angle_deg:g}. Check whether another mate works against it.")
        return None

    def _mate_dimension(self, mate) -> str:
        """The name set_dimension takes for a distance or angle mate's value."""
        display = binding.wrap(binding.wrap(mate, self._mod.IMate2).DisplayDimension2(0), self._mod.IDisplayDimension)
        if display is None:
            raise SolidWorksError("The mate has no dimension to drive.")
        return binding.wrap(display.GetDimension2(0), self._mod.IDimension).GetNameForSelection()

    def add_mate(self, comp_a: str, face_a: str, comp_b: str, face_b: str,
                 mate_type: str = "coincident", distance_mm: float = 0.0,
                 angle_deg: float = 0.0, flip: bool = False) -> dict:
        """Mate a face of one component to a face of another.

        comp_a/comp_b are component names ('Bed' or 'Bed-1'); face_a/face_b are
        direction selectors in each component's OWN frame ('+x', '-z', or
        '+y:inner' for the cavity side of a hollow part), or a face index from
        list_faces(component=...) ('#5'). mate_type: 'coincident', 'distance'
        (distance_mm), 'parallel', 'perpendicular' or 'angle' (angle_deg, the
        angle between the faces' normals) between planar faces; 'concentric'
        between two cylindrical faces (a pin in a hole, a hinge), which leaves
        the turn about the axis free. flip takes the other solution: the
        mirror side of a distance, the other turning direction of an angle.

        A distance or angle mate returns its `dimension`, which set_dimension
        and check_motion drive: a joint angle as one number. After the rebuild
        the result is measured back from the geometry; a mate that does not
        hold what was asked is removed again and the components put back.
        """
        key = (mate_type or "").lower().strip()
        if key not in MATE_TYPES:
            raise SolidWorksError(
                f"Unknown mate type '{mate_type}'. Use one of: {sorted(MATE_TYPES)}."
            )
        if key == "distance" and distance_mm < 0:
            raise SolidWorksError(f"distance must be >= 0 (got {distance_mm}).")
        if key == "angle" and not 0.0 < angle_deg < 180.0:
            raise SolidWorksError(f"angle must be between 0 and 180 degrees (got {angle_deg}); "
                                  "0 and 180 are a parallel mate.")
        if (comp_a or "").strip().lower() == (comp_b or "").strip().lower():
            raise SolidWorksError("A mate constrains two DIFFERENT components.")
        asm = self._require_assembly()
        model = self._model

        first = self._component_by_name(asm, comp_a)
        second = self._component_by_name(asm, comp_b)
        # a face in a sub-assembly lies on one of its parts, whose transform places it
        face_1, part_1 = self._component_face(first, face_a)
        face_2, part_2 = self._component_face(second, face_b)
        self._check_mate_faces(key, ((face_a, first, face_1), (face_b, second, face_2)))
        placements = [(comp, self._transform_data(comp)) for comp in (first, second)]
        mates_before = {m.Name for m in self._mates()}

        # Both mate entities go in with selection mark 1 (cracked empirically).
        model.ClearSelection2(True)
        selmgr = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        select_data = selmgr.CreateSelectData()
        select_data.Mark = 1
        for entity, comp, selector in ((face_1, first, face_a), (face_2, second, face_b)):
            if not binding.wrap(entity, self._mod.IEntity).Select4(True, select_data):
                raise SolidWorksError(
                    f"Could not select face {selector} of component '{comp.Name2}'."
                )

        distance_m = mm_to_m(distance_mm) if key == "distance" else 0.0
        angle_rad = math.radians(angle_deg) if key == "angle" else 0.0
        result = asm.AddMate5(
            MATE_TYPES[key],           # MateTypeFromEnum
            SW_MATE_ALIGN_CLOSEST,     # AlignFromEnum (components are pre-positioned)
            bool(flip),                # Flip
            distance_m,                # Distance
            distance_m, distance_m,    # DistanceAbsUpperLimit, DistanceAbsLowerLimit
            1.0, 1.0,                  # GearRatioNumerator, GearRatioDenominator
            angle_rad,                 # Angle
            angle_rad, angle_rad,      # AngleAbsUpperLimit, AngleAbsLowerLimit
            False,                     # ForPositioningOnly
            False,                     # LockRotation
            0,                         # WidthMateOption
            0,                         # ErrorStatus (out)
        )
        model.ClearSelection2(True)
        mate, status = (result[0], result[-1]) if isinstance(result, tuple) else (result, None)
        if mate is None or (status is not None and int(status) != SW_ADD_MATE_NO_ERROR):
            # An over-defining mate is added all the same, in error, and flags the
            # mates it fights (verified), so name those before it goes again.
            fought = [m.Name for m in self._mates() if m.Name in mates_before and m.GetErrorCode2()[0]]
            self._undo_mate(asm, mates_before, placements)
            raise SolidWorksError(
                f"Mate '{key}' between {first.Name2}:{face_a} and {second.Name2}:{face_b} "
                f"failed (AddMate5 status {status}). "
                + (f"It contradicts {', '.join(fought)}." if fought else
                   "Are the faces in a position that allows this mate, without contradicting existing mates?")
            )

        asm.EditRebuild()
        problem = self._mate_problem(key, part_1, face_1, part_2, face_2, distance_mm, angle_deg)
        if problem:
            self._undo_mate(asm, mates_before, placements)
            raise SolidWorksError(problem)
        entry = {
            "ok": True,
            "mate": next((m.Name for m in self._mates() if m.Name not in mates_before), None),
            "mate_type": key,
            "components": [first.Name2, second.Name2],
            "faces": [face_a, face_b],
            "placements": {c.Name2: self._placement(c) for c in (first, second)},
        }
        if key == "concentric":
            entry["axis_offset_mm"] = round(self._axes_apart(part_1, face_1, part_2, face_2)[0], 6)
        else:
            measured_mm, measured_deg = self._measure_mate(part_1, face_1, part_2, face_2)
            entry.update(distance_mm=round(measured_mm, 6), angle_deg=round(measured_deg, 6))
        if key in ("distance", "angle"):
            entry["dimension"] = self._mate_dimension(mate)
        return entry

    # --- interference ---------------------------------------------------------

    def check_interference(self) -> dict:
        """Find components whose solids overlap; volumes in mm^3, per pair.

        Touching faces are NOT an interference (a bed standing on the floor is
        fine); only real overlapping material counts. SolidWorks reports each
        disjoint overlapping lump separately, so the lumps are summed per
        component pair and counted as `regions`.
        """
        asm = self._require_assembly()
        manager = binding.wrap(asm.InterferenceDetectionManager,
                               self._mod.IInterferenceDetectionMgr)
        if manager is None:
            raise SolidWorksError("Could not open the InterferenceDetectionManager.")
        manager.TreatCoincidenceAsInterference = False
        manager.TreatSubAssembliesAsComponents = True
        manager.IncludeMultibodyPartInterferences = False
        pairs = {}
        try:
            found = manager.GetInterferences()
            for item in (found or []):
                interference = binding.wrap(item, self._mod.IInterference)
                components = interference.Components
                names = tuple(sorted(
                    binding.wrap(c, self._mod.IComponent2).Name2 for c in (components or [])
                ))
                entry = pairs.setdefault(
                    names, {"components": list(names), "volume_mm3": 0.0, "regions": 0})
                entry["volume_mm3"] += interference.Volume * 1e9
                entry["regions"] += 1
        finally:
            manager.Done()
        result = sorted(pairs.values(), key=lambda e: -e["volume_mm3"])
        for entry in result:
            entry["volume_mm3"] = round(entry["volume_mm3"], 4)
        return {"ok": True, "count": len(result), "interferences": result}

    def measure_distance(self, component_a: str, component_b: str | None = None,
                         point_mm: list | None = None, axis_mm: list | None = None) -> dict:
        """The smallest distance (mm) from a component of the current assembly to
        another one, to a point (assembly mm, with the nearest point on the
        component), or to an axis: the endless line axis_mm = [[x, y, z], [dx,
        dy, dz]] through a point along a direction, such as a bolt's axis. 0
        where they touch or overlap; check_interference tells which. A point in
        the material comes back with inside: true and its distance to the surface.
        """
        if sum(target is not None for target in (component_b, point_mm, axis_mm)) != 1:
            raise SolidWorksError("Give one of component_b, point_mm or axis_mm.")
        if point_mm is not None and len(point_mm) != 3:
            raise SolidWorksError(f"point_mm needs [x, y, z] (got {point_mm}).")
        line = None if axis_mm is None else self._axis_line(axis_mm)
        asm = self._require_assembly()
        comp = self._component_by_name(asm, component_a)
        if component_b is not None:
            return self._distance_between(comp, self._component_by_name(asm, component_b))
        if line is not None:
            return self._distance_to_line(comp, *line)
        return self._distance_to_point(comp, [float(c) for c in point_mm])

    @staticmethod
    def _axis_line(axis_mm) -> tuple:
        """[[x, y, z], [dx, dy, dz]] -> (point, unit direction); pure, unit-tested."""
        try:
            point, direction = [[float(c) for c in vector] for vector in axis_mm]
        except (TypeError, ValueError):
            raise SolidWorksError(f"axis_mm needs [[x, y, z], [dx, dy, dz]] (got {axis_mm}).") from None
        length = math.hypot(*direction)
        if len(point) != 3 or len(direction) != 3 or length < 1e-9:
            raise SolidWorksError(f"axis_mm needs a point and a direction that is not zero (got {axis_mm}).")
        return point, [d / length for d in direction]

    def _distance_to_line(self, comp, point, direction) -> dict:
        """Measured against a temporary 3D sketch line that reaches well past the
        component either way, deleted again afterwards."""
        model = self._model
        box = self._component_box(comp)
        if box is None:
            raise SolidWorksError(f"Component '{comp.Name2}' has no solid body to measure to.")
        centre = [(lo + hi) / 2 for lo, hi in zip(box["min_mm"], box["max_mm"])]
        reach = math.dist(box["min_mm"], box["max_mm"]) + math.dist(centre, point) + 10.0
        ends = [[mm_to_m(p + sign * reach * d) for p, d in zip(point, direction)] for sign in (-1.0, 1.0)]
        before = {f.Name for f in self._iter_features()}
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.Insert3DSketch(True)
        try:
            sk.AddToDB = True
            line = sk.CreateLine(*ends[0], *ends[1])
            sk.AddToDB = False
        finally:
            sk.Insert3DSketch(True)
        helpers = [f for f in self._iter_features() if f.Name not in before]
        try:
            if line is None:
                raise SolidWorksError("Could not draw the axis line to measure to.")
            selmgr = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
            model.ClearSelection2(True)
            if not (comp.Select4(False, selmgr.CreateSelectData(), False)
                    and binding.wrap(line, self._mod.ISketchSegment).Select4(True, selmgr.CreateSelectData())):
                raise SolidWorksError(f"Could not select '{comp.Name2}' and the axis line.")
            measure = binding.wrap(binding.wrap(model.Extension, self._mod.IModelDocExtension).CreateMeasure(),
                                   self._mod.IMeasure)
            if measure is None or not measure.Calculate(None) or not (measure.IsIntersect or measure.Distance >= 0):
                raise SolidWorksError(f"SolidWorks could not measure from '{comp.Name2}' to the axis.")
            distance = 0.0 if measure.IsIntersect else measure.Distance
        finally:
            extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
            for helper in helpers:
                model.ClearSelection2(True)
                if helper.Select2(False, 0):
                    extension.DeleteSelection2(0)
            model.ClearSelection2(True)
        return {"ok": True, "from": comp.Name2, "axis_mm": [point, direction], "distance_mm": round(m_to_mm(distance), 4)}

    def _distance_between(self, first, second) -> dict:
        """The smallest distance between the parts of the two: SolidWorks would
        not measure to a sub-assembly as a whole."""
        distance = min(self._measured_apart(a, b) for a in self._solid_parts(first) for b in self._solid_parts(second))
        return {"ok": True, "between": [first.Name2, second.Name2], "distance_mm": round(m_to_mm(distance), 4)}

    def _measured_apart(self, first, second) -> float:
        """IMeasure between two part components, in m."""
        model = self._require_model()
        selmgr = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        model.ClearSelection2(True)
        try:
            if not (first.Select4(False, selmgr.CreateSelectData(), False)
                    and second.Select4(True, selmgr.CreateSelectData(), False)):
                raise SolidWorksError(f"Could not select '{first.Name2}' and '{second.Name2}'.")
            measure = binding.wrap(binding.wrap(model.Extension, self._mod.IModelDocExtension).CreateMeasure(),
                                   self._mod.IMeasure)
            if measure is None or not measure.Calculate(None) or not (measure.IsIntersect or measure.Distance >= 0):
                raise SolidWorksError(f"SolidWorks could not measure between '{first.Name2}' and '{second.Name2}'.")
            # touching or overlapping: SolidWorks reports no distance then, only the meeting (verified)
            return 0.0 if measure.IsIntersect else measure.Distance
        finally:
            model.ClearSelection2(True)

    def _distance_to_point(self, comp, point_mm) -> dict:
        """Nearest point on the faces of the component's parts, each searched in
        its own frame."""
        best = None  # (gap in assembly m, face, nearest and point in part m, part frame)
        for part in self._solid_parts(comp):
            rotation, shift, scale = self._frame(part)
            relative = [mm_to_m(p) - s for p, s in zip(point_mm, shift)]
            local = [sum(rotation[row][i] * relative[row] for row in range(3)) / scale for i in range(3)]  # R^T (p - t)
            for body in self._solid_bodies(part):
                for face_dispatch in body.GetFaces() or ():
                    face = binding.wrap(face_dispatch, self._mod.IFace2)
                    nearest = face.GetClosestPointOn(*local)
                    if not nearest or len(nearest) < 3:
                        raise SolidWorksError(f"SolidWorks found no nearest point on a face of '{part.Name2}'.")
                    gap = math.dist(local, nearest[:3]) * scale
                    if best is None or gap < best[0]:
                        best = (gap, face, nearest[:3], local, (rotation, shift, scale))
        if best is None:
            raise SolidWorksError(f"Component '{comp.Name2}' has no solid body to measure to.")
        gap, face, nearest, local, (rotation, shift, scale) = best
        on_component = [scale * sum(rotation[row][i] * nearest[i] for i in range(3)) + shift[row] for row in range(3)]
        return {"ok": True, "from": comp.Name2, "point_mm": point_mm, "distance_mm": round(m_to_mm(gap), 4),
                "nearest_mm": [round(m_to_mm(c), 4) for c in on_component],
                "inside": self._on_material_side(face, nearest, local)}

    def _on_material_side(self, face, nearest, point) -> bool:
        """Whether `point` lies behind `face` at its nearest point `nearest`."""
        surface = binding.wrap(face.GetSurface(), self._mod.ISurface)
        normal = surface.EvaluateAtPoint(*nearest)[:3]
        # With FaceInSurfaceSense the surface normal points INTO the material
        # (verified on planes and cylinders, with both senses).
        outward = [-n for n in normal] if face.FaceInSurfaceSense() else list(normal)
        return sum((p - q) * n for p, q, n in zip(point, nearest, outward)) < 0

    def check_motion(self, dimension_name: str, values: list, distances: list | None = None) -> dict:
        """Step a dimension through `values` and check the assembly at every
        step: an angle mate's joint angle (degrees) or a distance mate's travel
        (mm), as add_mate returns them.

        Each step gives the overlapping component pairs with their volume and
        the distance between each pair in `distances` ([["Rod", "Bolt"], ...]);
        the summary gives whether every step is clash-free and each pair's
        smallest distance with the value where it occurs, and `moving` the
        components that moved. Nothing moving fails: the mate the dimension
        belongs to holds nothing any more (suppressed, or a face it used is
        gone). The dimension goes back to its value afterwards.
        """
        if not values:
            raise SolidWorksError("Give the values to step through, e.g. [30, 60, 90].")
        pairs = [list(pair) for pair in distances or ()]
        if any(len(pair) != 2 for pair in pairs):
            raise SolidWorksError(f"distances takes component pairs [[a, b], ...] (got {distances}).")
        asm = self._require_assembly()
        model = self._model
        for name in {name for pair in pairs for name in pair}:
            self._component_by_name(asm, name)  # a wrong name fails before anything moves
        self._refuse_broken_mates()
        dim = self._dimension(dimension_name)
        unit, to_system, from_system = self._dimension_unit(dim)
        original = dim.SystemValue
        everything = self._nested_components(self._components(asm))  # a joint inside a sub-assembly moves its parts
        start = {c.Name2: self._transform_data(c) for c in everything}
        moving, steps = set(), []
        try:
            for value in values:
                target = self._joint_angle(dim, unit, value)
                rebuilt = self._write_dimension(dim, to_system(target))
                applied = from_system(dim.SystemValue)
                if abs(applied - target) > 1e-6:
                    raise SolidWorksError(f"'{dimension_name}' did not take {value:g} {unit} (it reads "
                                          f"{applied:g}): is it a driven dimension?")
                moving |= {c.Name2 for c in everything if self._moved(start.get(c.Name2), c)}
                steps.append({
                    f"value_{unit}": value,
                    "rebuild_ok": rebuilt,
                    "interferences": self.check_interference()["interferences"],
                    "distances": [self.measure_distance(a, b) for a, b in pairs],
                })
        finally:
            self._write_dimension(dim, original)
        if not moving and any(abs(to_system(value) - original) > 1e-12 for value in values):
            raise SolidWorksError(f"Stepping '{dimension_name}' through {values} moved no component: its mate holds "
                                  "nothing any more (suppressed, or a face it used is gone); list_components shows it.")
        # what moved inside a sub-assembly that moved as a whole goes with it
        moving = {name for name in moving if not any(name.startswith(other + "/") for other in moving)}
        for step in steps:
            step["distances"] = [{"between": d["between"], "distance_mm": d["distance_mm"]} for d in step["distances"]]
        smallest = []
        for i in range(len(pairs)):
            closest = min(steps, key=lambda s: s["distances"][i]["distance_mm"])
            smallest.append({**closest["distances"][i], f"at_{unit}": closest[f"value_{unit}"]})
        return {"ok": True, "dimension": dimension_name, "steps": steps, "moving": sorted(moving),
                "clash_free": not any(step["interferences"] for step in steps), "smallest_distances": smallest}

    def _moved(self, before, comp) -> bool:
        """Whether the component's transform left `before` (a new one has moved)."""
        if before is None:
            return True
        now = self._transform_data(comp)
        return (any(abs(a - b) > self._ROTATION_TOLERANCE for a, b in zip(before[:9], now[:9]))
                or any(abs(a - b) > mm_to_m(self._TRANSFORM_TOLERANCE_MM) for a, b in zip(before[9:12], now[9:12])))

    def swept_region(self, component: str, dimension: str, values: list, heights_mm: list,
                     axis: str = "z", margin_mm: float = 0.0, tolerance_mm: float = 0.2,
                     frame: str | None = None) -> dict:
        """What a component covers in a plane while a joint moves.

        Steps `dimension` through `values` (as check_motion), cuts the component
        at every step where `axis` = each of heights_mm, and outlines all of it
        together, margin_mm wider and within about tolerance_mm. Give a layer a
        few heights inside it, not on its faces. Coordinates are those of the
        `frame` component (the part to cut it from), else the assembly's; each
        region's outline runs counter-clockwise in the plane's other two axes,
        (x, y) across z, ready for add_sketch as a spline. The dimension goes
        back to its value afterwards.
        """
        if not values:
            raise SolidWorksError("Give the values to step through, e.g. [30, 60, 90].")
        if not heights_mm:
            raise SolidWorksError("Give the heights to cut at, e.g. [-19.2] or a few inside a layer.")
        key = str(axis).lower()
        if key not in ("x", "y", "z"):
            raise SolidWorksError(f"Unknown axis '{axis}'. Use 'x', 'y' or 'z'.")
        asm = self._require_assembly()
        model = self._model
        comp = self._component_by_name(asm, component)
        framer = self._component_by_name(asm, frame) if frame else None
        self._refuse_broken_mates()
        pieces = [(part, [tri[:3] for body in self._solid_bodies(part) for face in self._body_faces(body)
                          for tri in self._face_triangles(binding.wrap(face, self._mod.IFace2))])
                  for part in self._solid_parts(comp)]  # in each part's own coordinates, read once
        dim = self._dimension(dimension)
        unit, to_system, from_system = self._dimension_unit(dim)
        original = dim.SystemValue
        start = {part.Name2: self._transform_data(part) for part, _ in pieces}
        sections, moved = [], False
        try:
            for value in values:
                target = self._joint_angle(dim, unit, value)
                self._write_dimension(dim, to_system(target))
                applied = from_system(dim.SystemValue)
                if abs(applied - target) > 1e-6:
                    raise SolidWorksError(f"'{dimension}' did not take {value:g} {unit} (it reads {applied:g}): "
                                          "is it a driven dimension?")
                for part, triangles in pieces:
                    moved = moved or self._moved(start[part.Name2], part)
                    place = self._placement_into(part, framer)
                    placed = [tuple(place(p) for p in tri) for tri in triangles]
                    sections += [loops for height in heights_mm if (loops := section(placed, key, height))]
        finally:
            self._write_dimension(dim, original)
        if not moved and any(abs(to_system(value) - original) > 1e-12 for value in values):
            raise SolidWorksError(f"Stepping '{dimension}' through {values} moved no part of '{comp.Name2}': its "
                                  "mate holds nothing (suppressed, or a face it used is gone), or it moves something else.")
        if not sections:
            raise SolidWorksError(f"'{comp.Name2}' does not reach {key} = {', '.join(f'{h:g}' for h in heights_mm)} "
                                  f"mm in {framer.Name2 if framer else 'the assembly'}'s coordinates at any step.")
        regions = swept_outline(sections, tolerance_mm, margin_mm)
        return {"ok": True, "component": comp.Name2, "frame": framer.Name2 if framer else "assembly",
                "dimension": dimension, f"values_{unit}": list(values), "axis": key, "heights_mm": list(heights_mm),
                "plane_axes": [a for a in ("x", "y", "z") if a != key], "margin_mm": margin_mm,
                "tolerance_mm": tolerance_mm, "count": len(regions),
                "regions": [{**r, "outline_mm": [list(p) for p in r["outline_mm"]],
                             "holes_mm": [[list(p) for p in hole] for hole in r["holes_mm"]]} for r in regions]}

    def _placement_into(self, part, framer):
        """A function taking a point of the part (mm, its own coordinates) to
        the frame component's coordinates, or the assembly's without one."""
        rotation, shift, scale = self._frame(part)
        shift = [m_to_mm(s) for s in shift]
        if framer is None:
            back, origin, size = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], [0.0] * 3, 1.0
        else:
            back, origin, size = self._frame(framer)
            origin = [m_to_mm(s) for s in origin]

        def place(p):
            world = [scale * sum(rotation[row][i] * p[i] for i in range(3)) + shift[row] for row in range(3)]
            offset = [w - o for w, o in zip(world, origin)]
            return tuple(sum(back[i][row] * offset[i] for i in range(3)) / size for row in range(3))  # R^T: the inverse
        return place

    def get_assembly_bounding_box(self) -> dict:
        """Bounding box of the whole assembly (min/max/size in mm)."""
        self._require_assembly()
        return {"ok": True, "bounding_box_mm": self._bounding_box()}
