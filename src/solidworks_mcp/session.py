"""SolidWorks operations.

A thin, stateful wrapper over the COM API. Every method here must run on the COM
worker thread (see com_worker). Methods return plain JSON-serialisable dicts so
the MCP tools can hand them straight back to the agent.

The call sequences (enum values, FeatureExtrusion3 argument order, the
language-independent plane walk, forced-SI mass properties) are the ones proven
green by scripts/m1_block.py and scripts/m2_parametric.py.
"""

import os

import pythoncom
import win32com.client

from . import binding
from .constants import (
    EXPORT_FORMATS,
    SW_BODY_SOLID,
    SW_CHAMFER_ANGLE_DISTANCE,
    SW_DOC_PART,
    SW_END_COND_BLIND,
    SW_END_COND_THROUGH_ALL,
    SW_FILLET_OPT_UNIFORM_RADIUS,
    SW_FILLET_TYPE_SIMPLE,
    SW_PREF_DEFAULT_TEMPLATE_PART,
    SW_SAVE_AS_CURRENT_VERSION,
    SW_SAVE_AS_OPTIONS_SILENT,
    SW_START_SKETCH_PLANE,
    SW_TOGGLE_INPUT_DIM_VAL_ON_CREATE,
    SW_VIEW_ISOMETRIC,
)
from .errors import SolidWorksError
from .units import deg_to_rad, m_to_mm, mm_to_m


class SolidWorksSession:
    """Holds the SolidWorks connection and the current part document."""

    def __init__(self) -> None:
        self._sw = None       # early-bound ISldWorks
        self._mod = None      # generated wrapper module
        self._model = None    # current IModelDoc2

    # --- connection -----------------------------------------------------------

    def _ensure(self):
        if self._sw is None:
            self.connect()
        return self._sw

    def connect(self) -> dict:
        """Attach to the running SolidWorks instance and configure it for automation."""
        self._mod = binding.module()
        self._sw = binding.connect()
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
        if self._model is None:
            raise SolidWorksError("Geen actief part. Roep eerst 'new_part' aan.")
        return self._model

    # --- status ---------------------------------------------------------------

    def get_status(self) -> dict:
        sw = self._ensure()
        active = binding.wrap(sw.ActiveDoc, self._mod.IModelDoc2)
        active_title = active.GetTitle() if active is not None else None
        return {
            "ok": True,
            "connected": True,
            "revision": self._revision(),
            "active_document": active_title,
            "current_part": self._model.GetTitle() if self._model is not None else None,
        }

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
            raise SolidWorksError("Kon geen nieuw part-document maken (template + NewPart faalden).")
        self._model = model
        return {"ok": True, "title": model.GetTitle()}

    def close_part(self, save: bool = False) -> dict:
        """Close the current part. CloseDoc never prompts; save is not implemented yet."""
        model = self._require_model()
        if save:
            raise SolidWorksError("Opslaan bij sluiten is nog niet ondersteund; gebruik 'export'.")
        title = model.GetTitle()
        self._sw.CloseDoc(title)
        self._model = None
        return {"ok": True, "closed": title}

    def _write_via_saveas3(self, abs_path: str) -> None:
        """SaveAs3 to abs_path (silent) and verify the file was actually (re)written.

        Checks the modification time advanced, so a silent SaveAs3 failure over a
        pre-existing file (locked/read-only target) is not reported as success.
        """
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        before = os.path.getmtime(abs_path) if os.path.isfile(abs_path) else None
        result = self._model.SaveAs3(abs_path, SW_SAVE_AS_CURRENT_VERSION, SW_SAVE_AS_OPTIONS_SILENT)
        if not os.path.isfile(abs_path) or (before is not None and os.path.getmtime(abs_path) == before):
            raise SolidWorksError(
                f"Schrijven mislukt: '{abs_path}' is niet (her)schreven; SaveAs3 gaf {result}. "
                "Is het bestand open of vergrendeld?"
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
        """Open an existing .sldprt; it becomes the current part."""
        sw = self._ensure()
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            raise SolidWorksError(f"Bestand niet gevonden: {abs_path}")
        result = sw.OpenDoc6(abs_path, SW_DOC_PART, 0, "", 0, 0)
        doc = result[0] if isinstance(result, tuple) else result
        model = binding.wrap(doc, self._mod.IModelDoc2)
        if model is None:
            raise SolidWorksError(f"Kon het part niet openen: {abs_path}")
        self._model = model
        return {"ok": True, "title": model.GetTitle(), "path": abs_path}

    # --- geometry -------------------------------------------------------------

    def _first_ref_plane(self):
        """First reference plane via tree walk (language-independent: 'RefPlane').

        Avoids SelectByID2('Front Plane', ...), which breaks on non-English
        installs. In a fresh part the first RefPlane is the Front plane.
        """
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
        part = binding.wrap(self._model, self._mod.IPartDoc)
        bodies = part.GetBodies2(SW_BODY_SOLID, True)
        if not bodies:
            raise SolidWorksError("Geen solid body; bouw eerst geometrie (bv. add_box).")
        if not isinstance(bodies, (list, tuple)):
            bodies = [bodies]
        return binding.wrap(bodies[0], self._mod.IBody2)

    def _planar_face_by_normal(self, body, target):
        """Return the PLANAR face whose outward normal best matches `target`.

        The reusable selection primitive: e.g. target (0,0,1) is the top face.
        Non-planar faces (a cylinder left by a hole, a fillet surface) are skipped
        so the result is always a valid sketch base. Returns the early-bound
        IFace2 facing closest to `target`, or None if no planar face faces it.
        """
        faces = body.GetFaces()
        if not faces:
            return None
        if not isinstance(faces, (list, tuple)):
            faces = [faces]
        tx, ty, tz = target
        best = None
        best_dot = 0.999  # floor: must face essentially toward `target`; closest wins
        for face_dispatch in faces:
            face = binding.wrap(face_dispatch, self._mod.IFace2)
            surface = binding.wrap(face.GetSurface(), self._mod.ISurface)
            if surface is None or not surface.IsPlane():
                continue  # only sketch on flat faces
            nx, ny, nz = face.Normal
            dot = nx * tx + ny * ty + nz * tz
            if dot > best_dot:
                best_dot = dot
                best = face
        return best

    _DIRECTIONS = {
        "+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0),
        "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
        "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
    }

    def _parse_direction(self, token: str):
        """'+z'/'-x'/... -> a unit vector tuple. Raises on an unknown token."""
        key = (token or "").lower().strip()
        if key not in self._DIRECTIONS:
            raise SolidWorksError(f"Onbekende richting '{token}'. Gebruik +x/-x/+y/-y/+z/-z.")
        return self._DIRECTIONS[key]

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
        that world axis (for an add_box block, 'z' is the depth edges); or explicit
        indices as [2, 5] or '2,5' (into list_edges order).
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
                    raise SolidWorksError(f"Edge-index {idx} buiten bereik (0..{len(edges) - 1}).")
                if binding.wrap(edges[idx], self._mod.IEntity).Select4(True, None):
                    count += 1
            return count

        sel = str(selector).lower()
        if sel != "all" and sel not in self._EDGE_AXES:
            raise SolidWorksError(
                f"Onbekende edge-selector '{selector}'. Gebruik 'all', 'x'/'y'/'z' of indices als '2,5'."
            )
        target = self._EDGE_AXES.get(sel)
        for edge_dispatch in edges:
            if target is not None and not self._edge_parallel_to(edge_dispatch, target):
                continue
            if binding.wrap(edge_dispatch, self._mod.IEntity).Select4(True, None):
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

    def add_box(self, width_mm: float, height_mm: float, depth_mm: float,
                name: str = "BlockExtrude") -> dict:
        """Sketch a rectangle on the first plane and extrude it; returns mass props.

        The extrude feature gets the stable name `name` so its depth dimension is
        addressable as 'D1@<name>' (used by set_dimension) regardless of language.
        NOTE: only the depth is parametric in v0. The rectangle width/height are
        not driven dimensions, so they cannot be changed via set_dimension yet;
        rebuild the box to resize them.
        """
        model = self._require_model()
        for value, label in ((width_mm, "width"), (height_mm, "height"), (depth_mm, "depth")):
            if value <= 0:
                raise SolidWorksError(f"{label} moet > 0 zijn (kreeg {value}).")

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("Geen reference plane gevonden in de feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Kon de reference plane niet selecteren.")

        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        rect = sk.CreateCornerRectangle(0.0, 0.0, 0.0, mm_to_m(width_mm), mm_to_m(height_mm), 0.0)
        model.ClearSelection2(True)
        sk.InsertSketch(True)  # close the sketch (it stays selected for the extrude)
        if not rect:
            # Fail at the true root cause (empty sketch) instead of later at the extrude.
            raise SolidWorksError("Rechthoek-sketch mislukte: CreateCornerRectangle gaf geen segmenten.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        extrude = feat_mgr.FeatureExtrusion3(
            True, False, False,        # Sd (single dir), Flip, Dir
            SW_END_COND_BLIND, 0,      # T1, T2 (end conditions)
            mm_to_m(depth_mm), 0.0,    # D1 (depth), D2
            False, False,              # Dchk1, Dchk2
            False, False,              # Ddir1, Ddir2
            0.0, 0.0,                  # Dang1, Dang2 (draft, radians)
            False, False,              # OffsetReverse1, OffsetReverse2
            False, False,              # TranslateSurface1, TranslateSurface2
            True,                      # Merge
            True,                      # UseFeatScope
            True,                      # UseAutoSelect
            SW_START_SKETCH_PLANE,     # T0 (start condition)
            0.0,                       # StartOffset
            False,                     # FlipStartOffset
        )
        if extrude is None:
            raise SolidWorksError("FeatureExtrusion3 mislukte (None). Is de sketch geldig?")
        result = self._finish_feature(extrude, name)
        result["depth_dimension"] = f"D1@{result['feature']}"
        return result

    @staticmethod
    def _clean_polygon(points_mm) -> list:
        """Distinct polygon vertices [(x, y), ...]; pure (no COM), unit-testable.

        Drops coincident consecutive points and a trailing point equal to the
        first (so open and explicitly-closed rings both work). Raises if fewer
        than 3 distinct vertices remain.
        """
        cleaned = []
        for x, y in points_mm:
            p = (float(x), float(y))
            if not cleaned or abs(p[0] - cleaned[-1][0]) > 1e-9 or abs(p[1] - cleaned[-1][1]) > 1e-9:
                cleaned.append(p)
        if (len(cleaned) >= 2 and abs(cleaned[0][0] - cleaned[-1][0]) < 1e-9
                and abs(cleaned[0][1] - cleaned[-1][1]) < 1e-9):
            cleaned.pop()  # drop an explicit closing point
        if len(cleaned) < 3:
            raise SolidWorksError(
                f"Profiel heeft minstens 3 verschillende punten nodig (kreeg {len(cleaned)})."
            )
        return cleaned

    def _sketch_closed_polygon(self, sk, points_mm) -> None:
        """Open a sketch and draw a closed polygon from [x, y] points (mm).

        Tolerant of open and explicitly-closed rings (see _clean_polygon); fails
        loudly on a bad segment.
        """
        cleaned = self._clean_polygon(points_mm)
        sk.InsertSketch(True)
        n = len(cleaned)
        for i in range(n):
            x1, y1 = cleaned[i]
            x2, y2 = cleaned[(i + 1) % n]
            if not sk.CreateLine(mm_to_m(x1), mm_to_m(y1), 0.0, mm_to_m(x2), mm_to_m(y2), 0.0):
                raise SolidWorksError(f"Kon lijnsegment {i} ({x1},{y1})->({x2},{y2}) niet maken.")
        self._model.ClearSelection2(True)
        sk.InsertSketch(True)  # close the sketch

    def _select_planar_face(self, body, normal, label: str):
        """Select the planar face whose outward normal matches `normal`; raise if none."""
        face = self._planar_face_by_normal(body, normal)
        if face is None:
            raise SolidWorksError(f"Geen planair {label}-vlak gevonden.")
        self._model.ClearSelection2(True)
        if not binding.wrap(face, self._mod.IEntity).Select4(False, None):
            raise SolidWorksError(f"Kon het {label}-vlak niet selecteren.")
        return face

    def add_extruded_profile(self, points_mm: list, depth_mm: float,
                             name: str = "Extrude") -> dict:
        """Extrude a closed polygon profile into a solid on the first plane.

        points_mm is a list of [x, y] vertices (mm) in the first-plane coordinate
        system (same as add_box); the polygon is auto-closed and extruded by
        depth_mm along the plane normal. Unlocks arbitrary prismatic shapes
        (L-brackets, T-sections, polygons, ...). Returns mass properties
        (volume = polygon area * depth).
        """
        model = self._require_model()
        if depth_mm <= 0:
            raise SolidWorksError(f"depth moet > 0 zijn (kreeg {depth_mm}).")
        if not points_mm:
            raise SolidWorksError("Geen profielpunten opgegeven.")

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("Geen reference plane gevonden in de feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Kon de reference plane niet selecteren.")

        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        self._sketch_closed_polygon(sk, points_mm)

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        extrude = feat_mgr.FeatureExtrusion3(
            True, False, False,
            SW_END_COND_BLIND, 0,
            mm_to_m(depth_mm), 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False,
            True, True, True,
            SW_START_SKETCH_PLANE, 0.0, False,
        )
        if extrude is None:
            raise SolidWorksError("FeatureExtrusion3 mislukte (None). Is het profiel gesloten en niet zelfsnijdend?")
        return self._finish_feature(extrude, name)

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
                raise SolidWorksError(f"{label} moet > 0 zijn (kreeg {value}).")

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("Geen reference plane gevonden in de feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Kon de reference plane niet selecteren.")

        radius = mm_to_m(diameter_mm / 2.0)
        height = mm_to_m(height_mm)
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        sk.CreateCornerRectangle(0.0, 0.0, 0.0, radius, height, 0.0)
        sk.CreateCenterLine(0.0, 0.0, 0.0, 0.0, height, 0.0)  # axis at x=0
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
            raise SolidWorksError("FeatureRevolve2 mislukte (None). Is het profiel geldig?")
        return self._finish_feature(revolve, name)

    def add_cone(self, bottom_diameter_mm: float, top_diameter_mm: float,
                 height_mm: float, name: str = "Revolve") -> dict:
        """Create a cone/frustum by revolving a trapezoidal profile 360 deg.

        top_diameter_mm = 0 gives a full cone. Reuses the add_cylinder revolve
        plumbing (a closed profile + a centerline axis). Returns mass properties
        (volume = pi*h/3 * (rb^2 + rb*rt + rt^2)).
        """
        model = self._require_model()
        if bottom_diameter_mm <= 0 or height_mm <= 0:
            raise SolidWorksError("bottom_diameter en height moeten > 0 zijn.")
        if top_diameter_mm < 0:
            raise SolidWorksError("top_diameter mag niet negatief zijn.")
        if top_diameter_mm >= bottom_diameter_mm:
            raise SolidWorksError("top_diameter moet kleiner zijn dan bottom_diameter (anders: add_cylinder).")

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("Geen reference plane gevonden in de feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Kon de reference plane niet selecteren.")

        rb = mm_to_m(bottom_diameter_mm / 2.0)
        rt = mm_to_m(top_diameter_mm / 2.0)
        h = mm_to_m(height_mm)
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        sk.CreateLine(0.0, 0.0, 0.0, rb, 0.0, 0.0)   # bottom edge
        sk.CreateLine(rb, 0.0, 0.0, rt, h, 0.0)      # slant edge (to apex if rt=0)
        if rt > 1e-9:
            sk.CreateLine(rt, h, 0.0, 0.0, h, 0.0)   # top edge (omitted for a full cone)
        sk.CreateLine(0.0, h, 0.0, 0.0, 0.0, 0.0)    # axis edge (closes the profile)
        sk.CreateCenterLine(0.0, 0.0, 0.0, 0.0, h, 0.0)  # revolve axis at x=0
        sk.InsertSketch(True)

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        revolve = feat_mgr.FeatureRevolve2(
            True, True, False, False, False, False,
            SW_END_COND_BLIND, 0, deg_to_rad(360.0), 0.0,
            False, False, 0.0, 0.0, 0, 0.0, 0.0, True, True, True,
        )
        if revolve is None:
            raise SolidWorksError("FeatureRevolve2 mislukte (None). Is het profiel gesloten?")
        return self._finish_feature(revolve, name)

    def add_hole(self, diameter_mm: float, x_mm: float, y_mm: float,
                 name: str = "Hole") -> dict:
        """Cut a circular through-hole at (x, y), straight through the depth axis.

        Selects the +Z face (the face parallel to add_box's width x height
        profile) and cuts through all material to the opposite face -- i.e. a hole
        through a plate's thickness, along the extrude direction. (x_mm, y_mm) are
        in add_box's coordinate system, so the centre of a 40x20 profile is x=20,
        y=10. Returns the resulting mass properties.

        Sketching on this face -- rather than on a reference plane coincident with
        the opposite face -- is what makes the cut direction unambiguous.
        """
        model = self._require_model()
        if diameter_mm <= 0:
            raise SolidWorksError(f"diameter moet > 0 zijn (kreeg {diameter_mm}).")

        body = self._solid_body()
        self._select_planar_face(body, (0.0, 0.0, 1.0), "+Z")

        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)  # the sketch is created on the selected face
        circle = sk.CreateCircleByRadius(
            mm_to_m(x_mm), mm_to_m(y_mm), 0.0, mm_to_m(diameter_mm / 2.0))
        sk.InsertSketch(True)  # close the sketch
        if not circle:
            raise SolidWorksError("Cirkel-sketch mislukte: CreateCircleByRadius gaf niets terug.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        cut = feat_mgr.FeatureCut4(
            True, False, False,                # Sd, Flip, Dir
            SW_END_COND_THROUGH_ALL, 0,        # T1 (through all, into the material), T2
            0.0, 0.0,                          # D1, D2 (ignored for through-all)
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
        if cut is None:
            raise SolidWorksError(
                "FeatureCut4 mislukte (None). Ligt (x, y) binnen het materiaal van het part?"
            )
        return self._finish_feature(cut, name)

    def _model_to_sketch_uv(self, sketch, x_m, y_m, z_m):
        """Map a 3D model point (m) to the active sketch's local 2D (u, v) (m).

        Via ISketch.ModelToSketchTransform. The point must be a proper SAFEARRAY
        VARIANT -- a plain Python list is mis-marshalled by CreatePoint.
        """
        xform = binding.wrap(sketch.ModelToSketchTransform, self._mod.IMathTransform)
        mathutil = binding.wrap(self._sw.GetMathUtility(), self._mod.IMathUtility)
        coords = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x_m, y_m, z_m])
        p = binding.wrap(mathutil.CreatePoint(coords), self._mod.IMathPoint)
        local = binding.wrap(p.MultiplyTransform(xform), self._mod.IMathPoint).ArrayData
        return local[0], local[1]

    def add_hole_on_face(self, diameter_mm: float, face: str,
                         x_mm: float, y_mm: float, z_mm: float, name: str = "Hole") -> dict:
        """Drill a through-hole on any planar face, centred at 3D point (x, y, z).

        face is a direction '+x'/'-x'/'+y'/'-y'/'+z'/'-z' selecting the planar
        face; (x_mm, y_mm, z_mm) is the hole centre in global (add_box) coordinates
        and must lie on that face. The hole runs through all material along the
        face normal. (add_hole is the +Z 2D convenience version of this.)
        """
        model = self._require_model()
        if diameter_mm <= 0:
            raise SolidWorksError(f"diameter moet > 0 zijn (kreeg {diameter_mm}).")

        body = self._solid_body()
        self._select_planar_face(body, self._parse_direction(face), face)
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        sketch = binding.wrap(sk.ActiveSketch, self._mod.ISketch)
        u, v = self._model_to_sketch_uv(sketch, mm_to_m(x_mm), mm_to_m(y_mm), mm_to_m(z_mm))
        circle = sk.CreateCircleByRadius(u, v, 0.0, mm_to_m(diameter_mm / 2.0))
        sk.InsertSketch(True)
        if not circle:
            raise SolidWorksError("Cirkel-sketch mislukte: CreateCircleByRadius gaf niets terug.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        cut = feat_mgr.FeatureCut4(
            True, False, False, SW_END_COND_THROUGH_ALL, 0, 0.0, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, False, True, True, False, False, False,
            SW_START_SKETCH_PLANE, 0.0, False, False,
        )
        if cut is None:
            raise SolidWorksError(
                f"FeatureCut4 mislukte (None). Ligt ({x_mm}, {y_mm}, {z_mm}) op het {face}-vlak?"
            )
        return self._finish_feature(cut, name)

    def cut_profile(self, points_mm: list, depth_mm: float | None = None,
                    name: str = "Cut") -> dict:
        """Cut a polygonal pocket/slot from the +Z face, blind or through.

        points_mm is a list of [x, y] vertices (mm) in add_box coordinates. The
        polygon is auto-closed and cut into the part: blind by depth_mm, or all
        the way through when depth_mm is None. Returns mass properties.
        """
        model = self._require_model()
        if not points_mm:
            raise SolidWorksError("Geen profielpunten opgegeven.")

        body = self._solid_body()
        self._select_planar_face(body, (0.0, 0.0, 1.0), "+Z")
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        self._sketch_closed_polygon(sk, points_mm)

        if depth_mm is None:
            t1, d1 = SW_END_COND_THROUGH_ALL, 0.0
        else:
            if depth_mm <= 0:
                raise SolidWorksError(f"depth moet > 0 zijn (kreeg {depth_mm}).")
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
            raise SolidWorksError("FeatureCut4 mislukte (None). Ligt het profiel op het +Z-vlak?")
        return self._finish_feature(cut, name)

    def add_fillet(self, radius_mm: float, edges: str = "all", name: str = "Fillet") -> dict:
        """Round edges of the part's solid body with one constant radius.

        edges: 'all' (default); a world axis 'x'|'y'|'z' (straight edges parallel
        to it, e.g. 'z' = the depth edges of an add_box block); or explicit indices
        like '2,5' from list_edges. Returns how many edges were filleted and the
        resulting mass properties (volume drops as convex edges are rounded off).
        """
        model = self._require_model()
        if radius_mm <= 0:
            raise SolidWorksError(f"radius moet > 0 zijn (kreeg {radius_mm}).")

        body = self._solid_body()
        edge_count = self._select_edges(body, edges)
        if edge_count == 0:
            raise SolidWorksError(f"Geen randen gevonden voor selector '{edges}'.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        fillet = feat_mgr.FeatureFillet3(
            SW_FILLET_OPT_UNIFORM_RADIUS,      # Options (uniform R1; no propagation)
            mm_to_m(radius_mm),                # R1 (uniform radius)
            0.0, 0.0,                          # R2, Rho
            SW_FILLET_TYPE_SIMPLE,             # Ftyp
            0, 0,                              # OverflowType, ConicRhoType
            None, None, None, None,            # Radii, Dist2Arr, RhoArr, SetBackDistances
            None, None, None,                  # PointRadius/Dist2/Rho arrays
        )
        if fillet is None:
            raise SolidWorksError(
                "FeatureFillet3 mislukte (None). Is de radius te groot voor de geometrie?"
            )
        return self._finish_feature(fillet, name, edges_filleted=edge_count)

    def add_chamfer(self, distance_mm: float, edges: str = "all", name: str = "Chamfer") -> dict:
        """Chamfer edges of the part's solid body at 45 degrees (equal distance).

        edges: 'all' (default); a world axis 'x'|'y'|'z'; or explicit indices like
        '2,5' from list_edges. Returns how many edges were chamfered and the
        resulting mass properties.
        """
        model = self._require_model()
        if distance_mm <= 0:
            raise SolidWorksError(f"distance moet > 0 zijn (kreeg {distance_mm}).")

        body = self._solid_body()
        edge_count = self._select_edges(body, edges)
        if edge_count == 0:
            raise SolidWorksError(f"Geen randen gevonden voor selector '{edges}'.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        chamfer = feat_mgr.InsertFeatureChamfer(
            0,                                   # Options (no tangent propagation)
            SW_CHAMFER_ANGLE_DISTANCE,           # ChamferType (distance + angle)
            mm_to_m(distance_mm),                # Width (the setback distance)
            deg_to_rad(45.0),                    # Angle (45 deg -> symmetric chamfer)
            0.0,                                 # OtherDist
            0.0, 0.0, 0.0,                       # Vertex chamfer distances
        )
        if chamfer is None:
            raise SolidWorksError(
                "InsertFeatureChamfer mislukte (None). Is de afstand te groot voor de geometrie?"
            )
        return self._finish_feature(chamfer, name, edges_chamfered=edge_count)

    def add_shell(self, thickness_mm: float, open_face: str = "+z") -> dict:
        """Hollow the part to a wall of `thickness_mm`, optionally opening one face.

        open_face: a direction '+z'/'-z'/'+x'/... selects the planar face to remove
        (an open shell); 'none' makes a fully closed hollow. Returns mass
        properties (volume drops to just the walls). InsertFeatureShell returns no
        feature object, so there is no feature name.
        """
        model = self._require_model()
        if thickness_mm <= 0:
            raise SolidWorksError(f"thickness moet > 0 zijn (kreeg {thickness_mm}).")

        body = self._solid_body()
        opened = (open_face or "none").lower()
        if opened == "none":
            model.ClearSelection2(True)
        else:
            self._select_planar_face(body, self._parse_direction(opened), opened)

        # Outward=False: the wall grows inward, so the outer size is unchanged.
        model.InsertFeatureShell(mm_to_m(thickness_mm), False)
        rebuilt_ok = bool(model.ForceRebuild3(False))
        props = self.get_mass_properties()["mass_properties"]
        if abs(props["volume_mm3"]) < 1e-6:
            raise SolidWorksError("Shell verwijderde al het materiaal; is de wanddikte te groot?")
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
                "Geen patroonbaar feature gevonden; geef feature_name expliciet op."
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
            raise SolidWorksError(f"count moet >= 2 zijn (kreeg {count}).")
        if spacing_mm <= 0:
            raise SolidWorksError(f"spacing moet > 0 zijn (kreeg {spacing_mm}).")

        dvec = self._parse_direction(direction)
        body = self._solid_body()
        p1, p2, edge_dispatch = self._first_edge_along(body, dvec)
        if p1 is None:
            raise SolidWorksError(f"Geen rechte rand evenwijdig aan {direction} gevonden.")
        along = (p2[0] - p1[0]) * dvec[0] + (p2[1] - p1[1]) * dvec[1] + (p2[2] - p1[2]) * dvec[2]
        flip = along < 0  # pattern follows the edge's p1->p2 dir; flip to match `direction`

        seed = feature_name or self._last_feature_name()
        selmgr = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        model.ClearSelection2(True)
        select_data = binding.wrap(selmgr.CreateSelectData(), self._mod.ISelectData)
        select_data.Mark = 1
        if not binding.wrap(edge_dispatch, self._mod.IEntity).Select4(False, select_data):
            raise SolidWorksError("Kon de richting-rand niet selecteren.")
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        if not ext.SelectByID2(seed, "BODYFEATURE", 0.0, 0.0, 0.0, True, 4, None, 0):
            raise SolidWorksError(f"Kon de seed-feature '{seed}' niet selecteren.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        pattern = feat_mgr.FeatureLinearPattern(count, mm_to_m(spacing_mm), 1, 0.0,
                                                flip, False, "", "")
        if pattern is None:
            raise SolidWorksError("FeatureLinearPattern mislukte (None). Passen alle instances op het part?")
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
            raise SolidWorksError(f"count moet >= 2 zijn (kreeg {count}).")

        body = self._solid_body()
        face = self._cylindrical_face_near(body, center_x_mm, center_y_mm)
        if face is None:
            raise SolidWorksError(
                f"Geen cilindrisch vlak bij ({center_x_mm}, {center_y_mm}) gevonden voor de as. "
                "Boor daar eerst een centraal gat."
            )
        seed = feature_name or self._last_feature_name()
        selmgr = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        model.ClearSelection2(True)
        select_data = binding.wrap(selmgr.CreateSelectData(), self._mod.ISelectData)
        select_data.Mark = 1
        if not binding.wrap(face, self._mod.IEntity).Select4(True, select_data):
            raise SolidWorksError("Kon het as-vlak niet selecteren.")
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        if not ext.SelectByID2(seed, "BODYFEATURE", 0.0, 0.0, 0.0, True, 4, None, 0):
            raise SolidWorksError(f"Kon de seed-feature '{seed}' niet selecteren.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        pattern = feat_mgr.FeatureCircularPattern(count, deg_to_rad(360.0) / count, False, "")
        if pattern is None:
            raise SolidWorksError("FeatureCircularPattern mislukte (None).")
        return self._finish_feature(pattern, "CircularPattern", instances=count,
                                    seed=seed, center_mm=[center_x_mm, center_y_mm])

    # --- parametric edit ------------------------------------------------------

    def set_dimension(self, dimension_name: str, value_mm: float) -> dict:
        """Set a named driving dimension (e.g. 'D1@BlockExtrude'), rebuild, remeasure."""
        model = self._require_model()
        dim = binding.wrap(model.Parameter(dimension_name), self._mod.IDimension)
        if dim is None:
            raise SolidWorksError(
                f"Dimensie '{dimension_name}' niet gevonden. "
                "Gebruik de 'D1@<feature>'-notatie."
            )
        old_mm = m_to_mm(dim.SystemValue)
        dim.SystemValue = mm_to_m(value_mm)
        rebuilt_ok = bool(model.ForceRebuild3(False))
        # Read the value back: a driven/reference or equation-controlled dimension
        # ignores the write silently, so the applied value can differ from the
        # request. Report the actual value so the agent's loop sees a no-op.
        applied_mm = m_to_mm(dim.SystemValue)
        return {
            "ok": True,
            "dimension": dimension_name,
            "old_value_mm": round(old_mm, 6),
            "requested_value_mm": value_mm,
            "new_value_mm": round(applied_mm, 6),
            "applied": abs(applied_mm - value_mm) < 1e-6,
            "rebuild_ok": rebuilt_ok,
            "mass_properties": self.get_mass_properties()["mass_properties"],
        }

    def set_equation(self, equation: str) -> dict:
        """Add a global equation linking dimensions, then rebuild and remeasure.

        equation is a SolidWorks equation string, e.g.
        '"D1@BlockExtrude" = 25' or '"D1@BlockExtrude" = 2 * "D1@Sketch1"'.
        Unlike set_dimension (a one-off value), this persists a relation in the
        model. Returns the resulting mass properties.
        """
        model = self._require_model()
        eqmgr = binding.wrap(model.GetEquationMgr(), self._mod.IEquationMgr)
        if eqmgr is None:
            raise SolidWorksError("Geen EquationManager beschikbaar.")
        count = eqmgr.GetCount()
        count = count() if callable(count) else count
        index = eqmgr.Add2(int(count), equation, True)  # append, solve immediately
        if index < 0:
            raise SolidWorksError(
                f"Equation toevoegen mislukt (Add2 gaf {index}). Controleer de syntax, "
                "bv. '\"D1@BlockExtrude\" = 25'."
            )
        rebuilt_ok = bool(model.ForceRebuild3(False))
        return {
            "ok": True,
            "equation": equation,
            "index": index,
            "rebuild_ok": rebuilt_ok,
            "mass_properties": self.get_mass_properties()["mass_properties"],
        }

    def set_material(self, name: str, database: str = "") -> dict:
        """Assign a material by name so mass/density reflect a real material.

        name: a material in the SolidWorks database, e.g. '6061 Alloy',
        'AISI 1020', 'ABS', 'Plain Carbon Steel'. database: path to a .sldmat, or
        '' for the default databases. Returns mass properties (with density).
        """
        model = self._require_model()
        part = binding.wrap(model, self._mod.IPartDoc)
        part.SetMaterialPropertyName2("", database, name)
        rebuilt_ok = bool(model.ForceRebuild3(False))
        # Verify by reading the applied name back (robust to re-assignment and to
        # materials near 1000 kg/m^3, where a density heuristic would lie).
        applied = part.GetMaterialPropertyName2("")
        applied_name = applied[0] if isinstance(applied, (list, tuple)) else applied
        if (applied_name or "").strip().lower() != name.strip().lower():
            raise SolidWorksError(
                f"Materiaal '{name}' niet toegepast (actief: '{applied_name}'). "
                "Controleer de exacte naam, bv. '6061 Alloy', 'AISI 1020', 'ABS'."
            )
        return {
            "ok": True,
            "material": applied_name,
            "rebuild_ok": rebuilt_ok,
            "mass_properties": self.get_mass_properties()["mass_properties"],
        }

    def rebuild(self, top_only: bool = False) -> dict:
        model = self._require_model()
        rebuilt_ok = bool(model.ForceRebuild3(top_only))
        return {"ok": True, "rebuild_ok": rebuilt_ok}

    # --- measurement ----------------------------------------------------------

    def get_mass_properties(self) -> dict:
        """Volume/mass/area/centre-of-mass plus bounding box, all in SI->mm, forced SI."""
        model = self._require_model()
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        mp = binding.wrap(ext.CreateMassProperty(), self._mod.IMassProperty)
        if mp is None:
            raise SolidWorksError("CreateMassProperty gaf None terug.")
        # Force SI (m, kg) regardless of document units. This is coupled to the
        # fixed 1e9/1e6/m_to_mm factors below, so do NOT swallow a failure here:
        # silently wrong units would be worse than a loud error.
        mp.UseSystemUnits = True
        if not mp.UseSystemUnits:
            raise SolidWorksError("Kon mass properties niet in SI forceren (UseSystemUnits=False).")
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
        # Tight part box via IPartDoc.GetPartBox(NoConversion=True). IModelDoc2
        # has no GetBox; that lives on IAssemblyDoc/IComponent/IFace. We QI the
        # model to IPartDoc. True = no unit conversion -> system units (metres).
        # Best-effort: a bbox failure must not break the core measurement.
        try:
            part = binding.wrap(self._model, self._mod.IPartDoc)
            box = part.GetPartBox(True)
        except pythoncom.com_error:
            return None
        if not box or len(box) < 6:
            return None
        xmin, ymin, zmin, xmax, ymax, zmax = (m_to_mm(v) for v in box[:6])
        return {
            "min_mm": [round(xmin, 4), round(ymin, 4), round(zmin, 4)],
            "max_mm": [round(xmax, 4), round(ymax, 4), round(zmax, 4)],
            "size_mm": [round(xmax - xmin, 4), round(ymax - ymin, 4), round(zmax - zmin, 4)],
        }

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

    def list_faces(self) -> dict:
        """Inspect the solid body's faces: index, planar?, normal, area, centre.

        Lets an agent see the geometry before choosing one. Indices are positional
        in the body's face list and shift as features are added.
        """
        self._require_model()
        body = self._solid_body()
        faces = body.GetFaces()
        if not isinstance(faces, (list, tuple)):
            faces = [faces]
        out = []
        for i, face_dispatch in enumerate(faces):
            face = binding.wrap(face_dispatch, self._mod.IFace2)
            surface = binding.wrap(face.GetSurface(), self._mod.ISurface)
            planar = bool(surface is not None and surface.IsPlane())
            box = face.GetBox()
            center = None
            if box and len(box) >= 6:
                center = [round(m_to_mm((box[j] + box[j + 3]) / 2), 3) for j in range(3)]
            entry = {
                "index": i,
                "type": "planar" if planar else "curved",
                "area_mm2": round(face.GetArea() * 1e6, 3),
                "center_mm": center,
            }
            if planar:
                nx, ny, nz = face.Normal
                entry["normal"] = [round(nx, 4), round(ny, 4), round(nz, 4)]
            out.append(entry)
        return {"ok": True, "count": len(out), "faces": out}

    def list_edges(self) -> dict:
        """Inspect the solid body's edges: index, type; lines also give axis/length/midpoint."""
        self._require_model()
        body = self._solid_body()
        edges = body.GetEdges()
        if not isinstance(edges, (list, tuple)):
            edges = [edges]
        out = []
        for i, edge_dispatch in enumerate(edges):
            edge = binding.wrap(edge_dispatch, self._mod.IEdge)
            curve = binding.wrap(edge.GetCurve(), self._mod.ICurve)
            is_line = bool(curve is not None and curve.IsLine())
            is_circle = bool(curve is not None and not is_line and curve.IsCircle())
            entry = {"index": i, "type": "line" if is_line else ("circle" if is_circle else "curve")}
            if is_line:
                start = edge.GetStartVertex()
                end = edge.GetEndVertex()
                if start is not None and end is not None:
                    p1 = binding.wrap(start, self._mod.IVertex).GetPoint()
                    p2 = binding.wrap(end, self._mod.IVertex).GetPoint()
                    dx, dy, dz = p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2]
                    length = (dx * dx + dy * dy + dz * dz) ** 0.5
                    entry["length_mm"] = round(m_to_mm(length), 3)
                    entry["midpoint_mm"] = [round(m_to_mm((p1[k] + p2[k]) / 2), 3) for k in range(3)]
                    entry["axis"] = self._axis_of(dx, dy, dz, length)
            out.append(entry)
        return {"ok": True, "count": len(out), "edges": out}

    # --- output ---------------------------------------------------------------

    def export(self, path: str, file_format: str | None = None) -> dict:
        """Export the current part (STEP/STL/IGES/Parasolid/3MF/image) via SaveAs3.

        Silent (no overwrite prompt). Success is verified by checking the file
        actually appears on disk, because SaveAs3's return code is unreliable.
        """
        self._require_model()
        fmt = (file_format or os.path.splitext(path)[1].lstrip(".")).lower()
        if fmt not in EXPORT_FORMATS:
            raise SolidWorksError(
                f"Onbekend exportformaat '{fmt}'. Toegestaan: {sorted(EXPORT_FORMATS)}."
            )
        abs_path = os.path.abspath(path)
        self._write_via_saveas3(abs_path)
        return {"ok": True, "path": abs_path, "format": fmt, "bytes": os.path.getsize(abs_path)}

    def screenshot(self, path: str) -> dict:
        """Isometric, zoom-to-fit screenshot of the current part to PNG/BMP/JPG.

        Writes via the same SaveAs3 path as `export`, so the return shape matches:
        {"ok", "path", "format", "bytes"} where "format" is the image extension.
        """
        model = self._require_model()
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        if ext not in {"png", "bmp", "jpg", "tif"}:
            raise SolidWorksError(f"Screenshot-extensie '{ext}' niet ondersteund (png/bmp/jpg/tif).")
        try:
            model.ShowNamedView2("", SW_VIEW_ISOMETRIC)  # best-effort orientation
        except pythoncom.com_error:
            pass
        model.ViewZoomtofit2()
        return self.export(path, ext)
