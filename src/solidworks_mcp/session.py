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

from . import binding
from .constants import (
    EXPORT_FORMATS,
    SW_BODY_SOLID,
    SW_CHAMFER_ANGLE_DISTANCE,
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

    def _select_edges(self, body, selector: str = "all") -> int:
        """Append-select body edges matching `selector`; return how many.

        selector: 'all' = every edge; 'x'|'y'|'z' = straight edges parallel to
        that world axis (for an add_box block, 'z' is the depth/extrude edges).
        """
        selector = (selector or "all").lower()
        if selector != "all" and selector not in self._EDGE_AXES:
            raise SolidWorksError(
                f"Onbekende edge-selector '{selector}'. Gebruik 'all', 'x', 'y' of 'z'."
            )
        edges = body.GetEdges()
        if not edges:
            return 0
        if not isinstance(edges, (list, tuple)):
            edges = [edges]
        target = self._EDGE_AXES.get(selector)
        self._model.ClearSelection2(True)
        count = 0
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
        face = self._planar_face_by_normal(body, (0.0, 0.0, 1.0))
        if face is None:
            raise SolidWorksError("Geen +Z-vlak gevonden om in te boren.")
        entity = binding.wrap(face, self._mod.IEntity)
        model.ClearSelection2(True)
        if not entity.Select4(False, None):
            raise SolidWorksError("Kon het +Z-vlak niet selecteren.")

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

    def add_fillet(self, radius_mm: float, edges: str = "all", name: str = "Fillet") -> dict:
        """Round edges of the part's solid body with one constant radius.

        edges: 'all' (default) or a world axis 'x'|'y'|'z' to round only the
        straight edges parallel to that axis ('z' = the depth edges of an add_box
        block). Returns how many edges were filleted and the resulting mass
        properties (volume drops as convex edges are rounded off).
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

        edges: 'all' (default) or a world axis 'x'|'y'|'z' to chamfer only the
        straight edges parallel to that axis. Returns how many edges were
        chamfered and the resulting mass properties.
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

    # --- output ---------------------------------------------------------------

    def export(self, path: str, file_format: str | None = None) -> dict:
        """Export the current part (STEP/STL/IGES/Parasolid/3MF/image) via SaveAs3.

        Silent (no overwrite prompt). Success is verified by checking the file
        actually appears on disk, because SaveAs3's return code is unreliable.
        """
        model = self._require_model()
        fmt = (file_format or os.path.splitext(path)[1].lstrip(".")).lower()
        if fmt not in EXPORT_FORMATS:
            raise SolidWorksError(
                f"Onbekend exportformaat '{fmt}'. Toegestaan: {sorted(EXPORT_FORMATS)}."
            )
        abs_path = os.path.abspath(path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        result = model.SaveAs3(abs_path, SW_SAVE_AS_CURRENT_VERSION, SW_SAVE_AS_OPTIONS_SILENT)
        if not os.path.isfile(abs_path):
            raise SolidWorksError(
                f"Export mislukt: bestand niet aangemaakt ({abs_path}). SaveAs3 gaf {result}."
            )
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
