"""SolidWorks operations.

A thin, stateful wrapper over the COM API. Every method here must run on the COM
worker thread (see com_worker). Methods return plain JSON-serialisable dicts so
the MCP tools can hand them straight back to the agent.

The call sequences (enum values, FeatureExtrusion3 argument order, the
language-independent plane walk, forced-SI mass properties) are the ones proven
green by scripts/m1_block.py and scripts/m2_parametric.py.
"""

import math
import os

import pythoncom
import win32com.client

from . import binding
from .constants import (
    EXPORT_FORMATS,
    MATE_TYPES,
    SW_ADD_COMPONENT_CURRENT_CONFIG,
    SW_ADD_MATE_NO_ERROR,
    SW_BODY_SOLID,
    SW_BOUNDING_BOX_SOLID_ONLY,
    SW_CHAMFER_ANGLE_DISTANCE,
    SW_DOC_ASSEMBLY,
    SW_DOC_PART,
    SW_END_COND_BLIND,
    SW_END_COND_THROUGH_ALL,
    SW_FILLET_OPT_UNIFORM_RADIUS,
    SW_FILLET_TYPE_SIMPLE,
    SW_MATE_ALIGN_CLOSEST,
    SW_OPEN_DOC_SILENT,
    SW_PREF_DEFAULT_TEMPLATE_ASSEMBLY,
    SW_PREF_DEFAULT_TEMPLATE_PART,
    SW_REF_PLANE_DISTANCE,
    SW_SAVE_AS_CURRENT_VERSION,
    SW_SAVE_AS_OPTIONS_SILENT,
    SW_SLOT_CREATION_LINE,
    SW_SLOT_LENGTH_CENTER,
    SW_START_SKETCH_PLANE,
    SW_STL_ANGLE_TOLERANCE,
    SW_STL_DEVIATION,
    SW_STL_QUALITY,
    SW_STL_QUALITY_COARSE,
    SW_STL_QUALITY_CUSTOM,
    SW_STL_QUALITY_FINE,
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
        if self._model is None:
            raise SolidWorksError("Geen actief document. Roep eerst 'new_part' of 'new_assembly' aan.")
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
                f"Het huidige document '{model.GetTitle()}' is geen part maar een assembly. "
                "Roep 'new_part' of 'open_part' aan, of gebruik de assembly-tools."
            )
        return binding.wrap(model, self._mod.IPartDoc)

    def _require_assembly(self):
        """The current document as an IAssemblyDoc; raises if it is a part."""
        model = self._require_model()
        if int(model.GetType()) != SW_DOC_ASSEMBLY:
            raise SolidWorksError(
                f"Het huidige document '{model.GetTitle()}' is geen assembly. "
                "Roep eerst 'new_assembly' of 'open_assembly' aan."
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
        """Close the current document (part or assembly). CloseDoc never prompts."""
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
            raise SolidWorksError("Geen solid body; bouw eerst geometrie (bv. add_box).")
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

    def _pick_planar_face(self, faces, target, side: str):
        """Extreme PLANAR face along `target`: 'outer' = max, 'inner' = min position.

        Non-planar faces (a cylinder left by a hole, a fillet surface) are skipped
        so the result is always a valid sketch base. Returns (IFace2, position_mm
        along `target`) or (None, None) if no planar face faces that way.
        """
        if side not in self._FACE_SIDES:
            raise SolidWorksError(f"Onbekende vlakzijde '{side}'. Gebruik 'outer' of 'inner'.")
        tx, ty, tz = target
        best, best_pos = None, None
        for face_dispatch in faces:
            face = binding.wrap(face_dispatch, self._mod.IFace2)
            surface = binding.wrap(face.GetSurface(), self._mod.ISurface)
            if surface is None or not surface.IsPlane():
                continue  # only sketch on flat faces
            nx, ny, nz = face.Normal
            if nx * tx + ny * ty + nz * tz < 0.999:  # ~2.6 degrees
                continue
            box = face.GetBox()  # planar face -> its box centre lies in the plane
            pos = sum((box[i] + box[i + 3]) / 2.0 * target[i] for i in range(3))
            if best is None or (pos > best_pos if side == "outer" else pos < best_pos):
                best, best_pos = face, pos
        return best, (None if best_pos is None else m_to_mm(best_pos))

    def _planar_face_by_normal(self, body, target, side: str = "outer"):
        """The body's outermost (default) or innermost planar face facing `target`."""
        faces = body.GetFaces()
        if not faces:
            return None
        if not isinstance(faces, (list, tuple)):
            faces = [faces]
        return self._pick_planar_face(faces, target, side)[0]

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

    def _parse_face_selector(self, token: str):
        """'+z' or '+z:inner' -> ((0,0,1), 'outer'|'inner'); pure, unit-tested.

        The optional ':inner' suffix asks for the cavity-side face instead of the
        outside skin -- the only way to address the inner wall of a hollow part
        (a shelled box, a room), where several faces share the same normal.
        """
        text = (token or "").lower().strip()
        direction, _, side = text.partition(":")
        side = side.strip() or "outer"
        if side not in self._FACE_SIDES:
            raise SolidWorksError(
                f"Onbekende vlakzijde ':{side}' in '{token}'. Gebruik ':outer' (standaard) of ':inner'."
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
            raise SolidWorksError(f"pad heeft minstens 2 verschillende punten nodig (kreeg {len(clean)}).")
        if len(clean) == 2:
            return [("line", clean[0], clean[1])]
        if radius_mm <= 0:
            raise SolidWorksError("bend_radius moet > 0 zijn voor een pad met hoeken.")

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
                    f"pad keert terug op zichzelf bij punt {i}; een sweep-pad mag niet 180 graden terugvouwen."
                )
            setback = radius_mm / math.tan(theta / 2.0)
            if setback > la - 1e-9 or setback > lb - 1e-9:
                raise SolidWorksError(f"bend_radius {radius_mm} te groot voor het segment bij punt {i}.")
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
                    f"bend_radius {radius_mm} te groot: bochten bij punt {prev['i']} en {nxt['i']} "
                    "overlappen op het tussensegment."
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
    def _draw_polygon_segments(sk, pts_m) -> None:
        """Draw closed-polygon CreateLine segments from 2D points in METRES.

        The sketch must already be open. Shared by the +Z polygon path and the
        any-face path (which supplies transformed sketch coordinates).
        """
        n = len(pts_m)
        for i in range(n):
            x1, y1 = pts_m[i]
            x2, y2 = pts_m[(i + 1) % n]
            if not sk.CreateLine(x1, y1, 0.0, x2, y2, 0.0):
                raise SolidWorksError(f"Kon lijnsegment {i} niet maken.")

    def _sketch_closed_polygon(self, sk, points_mm) -> None:
        """Open a sketch and draw a closed polygon from [x, y] points (mm).

        Tolerant of open and explicitly-closed rings (see _clean_polygon).
        """
        pts_m = [(mm_to_m(x), mm_to_m(y)) for x, y in self._clean_polygon(points_mm)]
        sk.InsertSketch(True)
        self._draw_polygon_segments(sk, pts_m)
        self._model.ClearSelection2(True)
        sk.InsertSketch(True)  # close the sketch

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
                f"Kon geen sketch openen op het {face}-vlak (InsertSketch gaf geen actieve sketch). "
                "Stond er nog een sketch open van een eerdere mislukte bewerking?"
            )
        return sketch

    def _select_planar_face(self, body, normal, label: str, side: str = "outer"):
        """Select the outermost (default) or innermost planar face facing `normal`."""
        face = self._planar_face_by_normal(body, normal, side)
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
            raise SolidWorksError(f"depth moet > 0 zijn (kreeg {depth_mm}).")
        pts = self._clean_polygon(points_mm)  # >= 3 distinct points

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("Geen reference plane gevonden in de feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Kon de reference plane niet selecteren.")

        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        coords = []
        for x, y in pts + [pts[0]]:  # repeat the first point to close the spline
            coords += [mm_to_m(x), mm_to_m(y), 0.0]
        point_data = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, coords)
        spline = sk.CreateSpline2(point_data, False)  # SimulateNaturalEnds=False
        model.ClearSelection2(True)
        sk.InsertSketch(True)  # close the sketch
        if not spline:
            raise SolidWorksError("Spline-sketch mislukte: CreateSpline2 gaf niets terug.")

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
            raise SolidWorksError("FeatureExtrusion3 mislukte (None). Is de spline gesloten en niet zelfsnijdend?")
        return self._finish_feature(extrude, name)

    def add_disc(self, diameter_mm: float, thickness_mm: float, name: str = "Disc") -> dict:
        """Create a disc / puck / flange: a circle extruded along +Z, centred at origin.

        Unlike add_cylinder (revolve, axis Y), the disc's flat faces are +Z/-Z, so
        add_hole and add_circular_pattern compose with it directly -- this is how
        round-flange bolt circles are built. Centred at the origin (x, y in
        [-r, r]). Returns mass properties (volume = pi * r^2 * thickness).
        """
        model = self._require_model()
        if diameter_mm <= 0 or thickness_mm <= 0:
            raise SolidWorksError("diameter en thickness moeten > 0 zijn.")

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("Geen reference plane gevonden in de feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Kon de reference plane niet selecteren.")

        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        circle = sk.CreateCircleByRadius(0.0, 0.0, 0.0, mm_to_m(diameter_mm / 2.0))
        model.ClearSelection2(True)
        sk.InsertSketch(True)
        if not circle:
            raise SolidWorksError("Cirkel-sketch mislukte: CreateCircleByRadius gaf niets terug.")

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        extrude = feat_mgr.FeatureExtrusion3(
            True, False, False,
            SW_END_COND_BLIND, 0,
            mm_to_m(thickness_mm), 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False,
            True, True, True,
            SW_START_SKETCH_PLANE, 0.0, False,
        )
        if extrude is None:
            raise SolidWorksError("FeatureExtrusion3 mislukte (None).")
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

    def add_revolved_profile(self, profile_mm: list, angle_deg: float = 360.0,
                             name: str = "Revolve") -> dict:
        """Revolve a closed (radius, height) profile about the axis at radius 0.

        profile_mm = [[r, z], ...] in mm: r is the distance from the revolve axis,
        z the position along it. The polygon is auto-closed and spun `angle_deg`
        (default 360) about r=0. Points touching the axis (r=0) give a solid like
        add_cone; a profile offset from the axis gives a ring/torus cross-section.
        The profile may not cross the axis (no negative r). Returns mass properties.
        """
        model = self._require_model()
        pts = self._clean_polygon(profile_mm)
        if any(r < -1e-9 for r, _ in pts):
            raise SolidWorksError("radius (eerste coord) mag niet negatief zijn -- het profiel mag de as niet kruisen.")
        if all(abs(r) < 1e-9 for r, _ in pts):
            raise SolidWorksError("profiel ligt volledig op de as (alle radii 0).")
        if not 0.0 < angle_deg <= 360.0:
            raise SolidWorksError(f"angle moet in (0, 360] liggen (kreeg {angle_deg}).")

        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("Geen reference plane gevonden in de feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Kon de reference plane niet selecteren.")

        z_vals = [z for _, z in pts]
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        self._draw_polygon_segments(sk, [(mm_to_m(r), mm_to_m(z)) for r, z in pts])
        sk.CreateCenterLine(0.0, mm_to_m(min(z_vals)), 0.0, 0.0, mm_to_m(max(z_vals)), 0.0)
        sk.InsertSketch(True)

        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        revolve = feat_mgr.FeatureRevolve2(
            True, True, False, False, False, False,
            SW_END_COND_BLIND, 0, deg_to_rad(angle_deg), 0.0,
            False, False, 0.0, 0.0, 0, 0.0, 0.0, True, True, True,
        )
        if revolve is None:
            raise SolidWorksError("FeatureRevolve2 mislukte (None). Is het profiel gesloten en geldig?")
        return self._finish_feature(revolve, name)

    def _draw_path_on_front(self, model, path_mm, bend_radius_mm) -> str:
        """Draw a rounded polyline path on the Front plane; return the new sketch name.

        Shared by the sweep builders. Pins the sketch to the Front plane (every
        builder selects its plane explicitly), rounds interior corners with
        bend_radius_mm (see _round_polyline), and identifies the just-drawn sketch
        via a ProfileFeature before/after diff.
        """
        segs = self._round_polyline(path_mm, bend_radius_mm)
        plane = self._first_ref_plane()
        if plane is None:
            raise SolidWorksError("Geen reference plane gevonden in de feature tree.")
        if not plane.Select2(False, 0):
            raise SolidWorksError("Kon de reference plane niet selecteren.")

        before = self._profile_feature_names()
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        for s in segs:
            if s[0] == "line":
                (x1, y1), (x2, y2) = s[1], s[2]
                if not sk.CreateLine(mm_to_m(x1), mm_to_m(y1), 0.0, mm_to_m(x2), mm_to_m(y2), 0.0):
                    raise SolidWorksError("Kon een padlijn niet maken.")
            else:
                _, c, p1, p2, direction = s
                if sk.CreateArc(mm_to_m(c[0]), mm_to_m(c[1]), 0.0,
                                mm_to_m(p1[0]), mm_to_m(p1[1]), 0.0,
                                mm_to_m(p2[0]), mm_to_m(p2[1]), 0.0, direction) is None:
                    raise SolidWorksError("Kon een padboog niet maken.")
        sk.InsertSketch(True)  # close the path sketch

        new_names = self._profile_feature_names() - before
        if len(new_names) != 1:
            raise SolidWorksError(
                f"Kon het zojuist getekende pad niet identificeren (verwachtte 1 nieuwe sketch, vond {len(new_names)})."
            )
        return new_names.pop()

    def add_swept_pipe(self, path_mm: list, diameter_mm: float,
                       bend_radius_mm: float = 0.0, name: str = "Pipe") -> dict:
        """Sweep a circular profile (pipe/tube/rod) along a 2D path on the Front plane.

        path_mm = [[x, y], ...] in mm: the pipe centreline. Interior corners are
        rounded with bend_radius_mm (required when the path has corners; a 2-point
        straight path needs none). diameter_mm is the outer diameter; the round
        profile is generated perpendicular to the path automatically. Returns mass
        properties (volume = pi*(d/2)^2 * path_length). Use new_part first.
        """
        model = self._require_model()
        if diameter_mm <= 0:
            raise SolidWorksError(f"diameter moet > 0 zijn (kreeg {diameter_mm}).")

        path_name = self._draw_path_on_front(model, path_mm, bend_radius_mm)
        model.ClearSelection2(True)
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        if not ext.SelectByID2(path_name, "SKETCH", 0.0, 0.0, 0.0, False, 4, None, 0):  # mark 4 = sweep path
            raise SolidWorksError(f"Kon het pad '{path_name}' niet selecteren.")

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
                "InsertProtrusionSwept4 mislukte (None). Is het pad geldig "
                "(geen overlappende bochten, radius past)?"
            )
        return self._finish_feature(pipe, name)

    @staticmethod
    def _require_path_starts_along_x(path_mm) -> None:
        """Fail-fast: a swept-profile path must start at the origin heading +X.

        The cross-section sits on the Right plane (normal +X), so the path's start
        tangent must be +X for the profile to be perpendicular to it. Pure helper.
        """
        raw = [(float(x), float(y)) for x, y in path_mm]
        if len(raw) < 2:
            raise SolidWorksError("path heeft minstens 2 punten nodig.")
        p0 = raw[0]
        p1 = next((p for p in raw[1:] if abs(p[0] - p0[0]) > 1e-9 or abs(p[1] - p0[1]) > 1e-9), None)
        if p1 is None:
            raise SolidWorksError("path heeft minstens 2 verschillende punten nodig.")
        if math.hypot(p0[0], p0[1]) > 1e-6:
            raise SolidWorksError("path moet bij de oorsprong (0,0) beginnen.")
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        if dx / math.hypot(dx, dy) < 1.0 - 1e-6:  # start tangent not +X
            raise SolidWorksError("path moet bij de start langs +X lopen (eerste segment richting +X).")

    def add_swept_profile(self, profile_mm: list, path_mm: list,
                          bend_radius_mm: float = 0.0, name: str = "Sweep") -> dict:
        """Sweep an arbitrary closed PROFILE (cross-section) along a 2D PATH.

        profile_mm = [[u, v], ...] in mm: the closed cross-section, drawn on the
        Right plane (u along world +Y, v along world +Z), centred near the origin.
        path_mm = [[x, y], ...] in mm on the Front plane; it MUST start at the
        origin heading +X (so the profile is perpendicular to the path there).
        Interior path corners are rounded with bend_radius_mm. Volume =
        profile_area * path_length (Pappus). For non-round extrusions along a path
        (rails, gaskets, trim, channels). Returns mass properties. Use new_part first.
        """
        model = self._require_model()
        prof = self._clean_polygon(profile_mm)  # >= 3 distinct points
        self._require_path_starts_along_x(path_mm)

        planes = self._ref_planes()
        if len(planes) < 3:
            raise SolidWorksError("Geen Right-vlak gevonden (verwacht Front/Top/Right).")
        right = planes[2]  # tree order: Front, Top, Right

        # profile (cross-section) on the Right plane, perpendicular to the +X start
        if not right.Select2(False, 0):
            raise SolidWorksError("Kon het Right-vlak niet selecteren.")
        before = self._profile_feature_names()
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        self._draw_polygon_segments(sk, [(mm_to_m(u), mm_to_m(v)) for u, v in prof])
        model.ClearSelection2(True)
        sk.InsertSketch(True)
        profile_names = self._profile_feature_names() - before
        if len(profile_names) != 1:
            raise SolidWorksError("Kon het profiel niet identificeren na het tekenen.")
        profile_name = profile_names.pop()

        # path on the Front plane (reuses the rounded-polyline path builder)
        path_name = self._draw_path_on_front(model, path_mm, bend_radius_mm)

        model.ClearSelection2(True)
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        if not ext.SelectByID2(profile_name, "SKETCH", 0.0, 0.0, 0.0, False, 1, None, 0):  # mark 1 = profile
            raise SolidWorksError(f"Kon het profiel '{profile_name}' niet selecteren.")
        if not ext.SelectByID2(path_name, "SKETCH", 0.0, 0.0, 0.0, True, 4, None, 0):  # mark 4 = path
            raise SolidWorksError(f"Kon het pad '{path_name}' niet selecteren.")

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
                "InsertProtrusionSwept4 mislukte (None). Ligt het profiel op het Right-vlak "
                "en start het pad bij de oorsprong langs +X?"
            )
        return self._finish_feature(sweep, name)

    def add_lofted_solid(self, profiles_mm: list, heights_mm: list, name: str = "Loft") -> dict:
        """Loft (blend) 2+ closed polygon profiles on parallel planes stacked along +Z.

        profiles_mm: a list of profiles, each a list of [x, y] vertices (mm) in the
        Front-plane coordinate system (same convention as add_extruded_profile).
        heights_mm: the +Z offset (mm) of each profile's plane; same length as
        profiles_mm, strictly increasing, starting at 0. Each profile is auto-closed.
        A 2-profile loft is a ruled transition; 3+ profiles blend smoothly through
        the intermediate ones. Give profiles in a consistent vertex order/orientation
        to avoid a twisted blend. Use for non-rotational transitions (revolve/cone
        already cover round shapes). Returns mass properties. Use new_part first.
        """
        model = self._require_model()
        if len(profiles_mm) != len(heights_mm):
            raise SolidWorksError("profiles_mm en heights_mm moeten even lang zijn.")
        if len(profiles_mm) < 2:
            raise SolidWorksError("loft heeft minstens 2 profielen nodig.")
        if heights_mm[0] != 0:
            raise SolidWorksError("heights_mm[0] moet 0 zijn (eerste profiel op de Front plane).")
        for lo, hi in zip(heights_mm, heights_mm[1:]):
            if hi <= lo:
                raise SolidWorksError("heights_mm moet strikt oplopend zijn.")
        cleaned = [self._clean_polygon(p) for p in profiles_mm]  # validates >= 3 distinct pts

        base = self._first_ref_plane()
        if base is None:
            raise SolidWorksError("Geen reference plane gevonden in de feature tree.")
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)

        sketch_names = []
        helper_planes = []
        for poly, height in zip(cleaned, heights_mm):
            if not base.Select2(False, 0):
                raise SolidWorksError("Kon de Front plane niet selecteren.")
            if height != 0:
                if feat_mgr.InsertRefPlane(SW_REF_PLANE_DISTANCE, mm_to_m(height), 0, 0.0, 0, 0.0) is None:
                    raise SolidWorksError(f"Kon geen offsetvlak maken op z={height}.")
                # InsertRefPlane's return is a generic dispatch without Select2; take
                # the new plane from the tree instead.
                plane = self._last_ref_plane()
                if plane is None or not plane.Select2(False, 0):
                    raise SolidWorksError(f"Kon het offsetvlak op z={height} niet selecteren.")
                helper_planes.append(plane)
            before = self._profile_feature_names()
            sk.InsertSketch(True)
            self._draw_polygon_segments(sk, [(mm_to_m(x), mm_to_m(y)) for x, y in poly])
            sk.InsertSketch(True)
            new_names = self._profile_feature_names() - before
            if len(new_names) != 1:
                raise SolidWorksError(f"Kon het profiel op z={height} niet identificeren.")
            sketch_names.append(new_names.pop())

        model.ClearSelection2(True)
        ext = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        for sketch_name in sketch_names:
            if not ext.SelectByID2(sketch_name, "SKETCH", 0.0, 0.0, 0.0, True, 1, None, 0):  # mark 1, append
                raise SolidWorksError(f"Kon profiel '{sketch_name}' niet selecteren.")

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
                "InsertProtrusionBlend mislukte (None). Liggen de profielen geldig gestapeld?"
            )
        # The offset planes are construction geometry; hide them so they don't
        # clutter screenshots.
        for plane in helper_planes:
            if plane.Select2(False, 0):
                model.BlankRefGeom()
        model.ClearSelection2(True)
        return self._finish_feature(loft, name)

    def _cut_circle_on_z(self, model, diameter_mm: float, x_mm: float, y_mm: float,
                         through: bool, depth_mm: float = 0.0):
        """Cut one circle on the +Z face -- through-all or blind to depth_mm.

        Returns the raw FeatureCut4 feature (or None on failure) so callers attach
        their own error message. Shared by add_hole and add_counterbore_hole;
        sketching on the selected +Z face is what makes the cut direction
        unambiguous. (x_mm, y_mm) are in add_box coordinates.
        """
        body = self._solid_body()
        self._select_planar_face(body, (0.0, 0.0, 1.0), "+Z")
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)  # the sketch is created on the selected face
        circle = sk.CreateCircleByRadius(
            mm_to_m(x_mm), mm_to_m(y_mm), 0.0, mm_to_m(diameter_mm / 2.0))
        sk.InsertSketch(True)  # close the sketch
        if not circle:
            raise SolidWorksError("Cirkel-sketch mislukte: CreateCircleByRadius gaf niets terug.")

        t1 = SW_END_COND_THROUGH_ALL if through else SW_END_COND_BLIND
        d1 = 0.0 if through else mm_to_m(depth_mm)
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        return feat_mgr.FeatureCut4(
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
            raise SolidWorksError(f"diameter moet > 0 zijn (kreeg {diameter_mm}).")
        cut = self._cut_circle_on_z(model, diameter_mm, x_mm, y_mm, through=True)
        if cut is None:
            raise SolidWorksError(
                "FeatureCut4 mislukte (None). Ligt (x, y) binnen het materiaal van het part?"
            )
        return self._finish_feature(cut, name)

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
            raise SolidWorksError("diameters moeten > 0 zijn.")
        if cbore_diameter_mm <= clearance_diameter_mm:
            raise SolidWorksError("cbore_diameter moet groter zijn dan clearance_diameter.")
        if cbore_depth_mm <= 0:
            raise SolidWorksError(f"cbore_depth moet > 0 zijn (kreeg {cbore_depth_mm}).")

        # Through clearance shank first (clean +Z face), then the blind pocket: the
        # pocket removes the annular ring around the already-cut shank.
        shank = self._cut_circle_on_z(model, clearance_diameter_mm, x_mm, y_mm, through=True)
        if shank is None:
            raise SolidWorksError(
                "Clearance-gat (FeatureCut4) mislukte (None). Ligt (x, y) binnen het materiaal?"
            )
        cbore = self._cut_circle_on_z(model, cbore_diameter_mm, x_mm, y_mm,
                                      through=False, depth_mm=cbore_depth_mm)
        if cbore is None:
            raise SolidWorksError("Counterbore-pocket (FeatureCut4) mislukte (None).")
        return self._finish_feature(cbore, name)

    # A point given to add_hole_on_face / cut_profile_on_face must LIE on the
    # chosen face. ModelToSketchTransform's out-of-plane component (local[2]) is
    # exactly 0 for an on-face point (verified) and equals the off-face distance
    # otherwise; without this guard the 2D projection silently relocates the
    # feature onto the face. 1 um catches any real mistake by orders of magnitude
    # while absorbing transform round-off.
    _ON_FACE_TOLERANCE_MM = 1e-3

    def _model_to_sketch_uv(self, sketch, x_m, y_m, z_m, face):
        """Map a 3D model point (m) to the active sketch's local 2D (u, v) (m).

        Via ISketch.ModelToSketchTransform. The point must be a proper SAFEARRAY
        VARIANT -- a plain Python list is mis-marshalled by CreatePoint.

        The point must LIE on the sketch's face: local[2] is its perpendicular
        distance to the face plane, which we reject past _ON_FACE_TOLERANCE_MM so
        an off-face point fails fast instead of being silently projected onto the
        face (which would place the feature at the wrong spot). `face` names the
        face in the error.
        """
        xform = binding.wrap(sketch.ModelToSketchTransform, self._mod.IMathTransform)
        mathutil = binding.wrap(self._sw.GetMathUtility(), self._mod.IMathUtility)
        coords = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x_m, y_m, z_m])
        p = binding.wrap(mathutil.CreatePoint(coords), self._mod.IMathPoint)
        local = binding.wrap(p.MultiplyTransform(xform), self._mod.IMathPoint).ArrayData
        off_mm = m_to_mm(local[2])
        if abs(off_mm) > self._ON_FACE_TOLERANCE_MM:
            raise SolidWorksError(
                f"Punt ({m_to_mm(x_m):g}, {m_to_mm(y_m):g}, {m_to_mm(z_m):g}) mm ligt niet "
                f"op het {face}-vlak: het staat {off_mm:.3f} mm buiten het vlak. Geef een "
                f"punt op het vlak (loodrechte afstand moet ~0 zijn)."
            )
        return local[0], local[1]

    def add_hole_on_face(self, diameter_mm: float, face: str,
                         x_mm: float, y_mm: float, z_mm: float, name: str = "Hole") -> dict:
        """Drill a through-hole on any planar face, centred at 3D point (x, y, z).

        face is a direction '+x'/'-x'/'+y'/'-y'/'+z'/'-z' selecting the planar
        face -- add ':inner' (e.g. '+z:inner') for the cavity-side face of a
        hollow part; (x_mm, y_mm, z_mm) is the hole centre in global coordinates
        and must lie on that face. The hole runs through all material along the
        face normal. (add_hole is the +Z 2D convenience version of this.)
        """
        model = self._require_model()
        if diameter_mm <= 0:
            raise SolidWorksError(f"diameter moet > 0 zijn (kreeg {diameter_mm}).")

        body = self._solid_body()
        normal, side = self._parse_face_selector(face)
        self._select_planar_face(body, normal, face, side)
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sketch = self._open_face_sketch(sk, face)
        try:
            u, v = self._model_to_sketch_uv(sketch, mm_to_m(x_mm), mm_to_m(y_mm),
                                            mm_to_m(z_mm), face)
            circle = sk.CreateCircleByRadius(u, v, 0.0, mm_to_m(diameter_mm / 2.0))
        finally:
            sk.InsertSketch(True)  # close the sketch, also when the point is rejected
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

    def cut_profile_on_face(self, points_mm: list, face: str,
                            depth_mm: float | None = None, name: str = "Cut") -> dict:
        """Cut a polygon pocket/slot on ANY planar face, blind or through.

        points_mm is a list of 3D [x, y, z] vertices (mm) that lie on the chosen
        `face` ('+x'/'-x'/...); each is mapped into the face-sketch via the
        model->sketch transform. The polygon is auto-closed; cut blind by depth_mm
        or through when depth_mm is None. Returns mass properties.
        """
        model = self._require_model()
        if not points_mm:
            raise SolidWorksError("Geen profielpunten opgegeven.")

        body = self._solid_body()
        normal, side = self._parse_face_selector(face)
        self._select_planar_face(body, normal, face, side)
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sketch = self._open_face_sketch(sk, face)
        try:
            uv_m = [self._model_to_sketch_uv(sketch, mm_to_m(p[0]), mm_to_m(p[1]),
                                             mm_to_m(p[2]), face)
                    for p in points_mm]
            self._draw_polygon_segments(sk, self._clean_polygon(uv_m))
            model.ClearSelection2(True)
        finally:
            sk.InsertSketch(True)  # close the sketch, also when a point is rejected

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
            raise SolidWorksError(f"FeatureCut4 mislukte (None). Liggen de punten op het {face}-vlak?")
        return self._finish_feature(cut, name)

    def cut_slot(self, length_mm: float, width_mm: float, x_mm: float, y_mm: float,
                 angle_deg: float = 0.0, depth_mm: float | None = None, name: str = "Slot") -> dict:
        """Cut a straight slotted hole (obround) on the +Z face, blind or through.

        Centred at (x_mm, y_mm); length_mm is centre-to-centre of the end arcs,
        width_mm the slot width, angle_deg the orientation in the +Z plane. Cut
        blind by depth_mm or through (None). Returns mass properties.
        """
        model = self._require_model()
        if length_mm <= 0 or width_mm <= 0:
            raise SolidWorksError("length en width moeten > 0 zijn.")

        rad = deg_to_rad(angle_deg)
        ax, ay = math.cos(rad), math.sin(rad)      # slot axis direction
        px, py = -math.sin(rad), math.cos(rad)     # perpendicular (width side)
        half = length_mm / 2.0
        c1 = (x_mm - half * ax, y_mm - half * ay)
        c2 = (x_mm + half * ax, y_mm + half * ay)
        edge = (x_mm + (width_mm / 2.0) * px, y_mm + (width_mm / 2.0) * py)

        body = self._solid_body()
        self._select_planar_face(body, (0.0, 0.0, 1.0), "+Z")
        sk = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sk.InsertSketch(True)
        seg = sk.CreateSketchSlot(
            SW_SLOT_CREATION_LINE, SW_SLOT_LENGTH_CENTER, mm_to_m(width_mm),
            mm_to_m(c1[0]), mm_to_m(c1[1]), 0.0,
            mm_to_m(c2[0]), mm_to_m(c2[1]), 0.0,
            mm_to_m(edge[0]), mm_to_m(edge[1]), 0.0,
            1, False,
        )
        model.ClearSelection2(True)
        sk.InsertSketch(True)
        if not seg:
            raise SolidWorksError("Slot-sketch mislukte: CreateSketchSlot gaf niets terug.")

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
            raise SolidWorksError("FeatureCut4 mislukte (None). Past de sleuf op het +Z-vlak?")
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
            normal, side = self._parse_face_selector(opened)
            self._select_planar_face(body, normal, opened, side)

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
        part = self._require_part()
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
        # IModelDoc2 has no GetBox, so the call depends on the document type: a
        # part measures via IPartDoc.GetPartBox(NoConversion=True), an assembly
        # via IAssemblyDoc.GetBox. Both return system units (metres).
        # Best-effort: a bbox failure must not break the core measurement.
        model = self._require_model()
        try:
            if int(model.GetType()) == SW_DOC_ASSEMBLY:
                assembly = binding.wrap(model, self._mod.IAssemblyDoc)
                # IAssemblyDoc.GetBox is STALE until the assembly is rebuilt: after
                # moving a component it still reports the previous extents
                # (verified). Rebuild first rather than hand back an old number.
                assembly.EditRebuild()
                box = assembly.GetBox(SW_BOUNDING_BOX_SOLID_ONLY)
            else:
                box = binding.wrap(model, self._mod.IPartDoc).GetPartBox(True)
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

    _MESH_EXPORT_FORMATS = {"stl", "3mf"}

    def _apply_stl_resolution(self, quality: str, deviation_mm, angle_deg) -> dict:
        """Set the global STL/3MF tessellation prefs; return the prior values.

        quality 'coarse'|'fine'; or pass deviation_mm (+ optional angle_deg) for a
        reproducible Custom resolution (overrides quality). Caller MUST restore the
        returned values afterwards -- these are application-wide preferences.
        """
        if deviation_mm is not None and deviation_mm <= 0:
            raise SolidWorksError(f"deviation_mm moet > 0 zijn (kreeg {deviation_mm}).")
        if angle_deg is not None and angle_deg <= 0:
            raise SolidWorksError(f"angle_deg moet > 0 zijn (kreeg {angle_deg}).")
        levels = {"coarse": SW_STL_QUALITY_COARSE, "fine": SW_STL_QUALITY_FINE}
        if deviation_mm is None and quality not in levels:
            raise SolidWorksError(f"quality moet 'coarse' of 'fine' zijn (kreeg '{quality}').")

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
               deviation_mm: float | None = None, angle_deg: float | None = None) -> dict:
        """Export the current part (STEP/STL/IGES/Parasolid/3MF/image) via SaveAs3.

        Silent (no overwrite prompt). Success is verified by checking the file
        actually appears on disk, because SaveAs3's return code is unreliable.

        For STL/3MF, tessellation resolution is applied first (and restored after):
        quality 'coarse'|'fine' (default 'fine' for print quality), or pass
        deviation_mm (+ optional angle_deg) for a reproducible Custom resolution
        (overrides quality). Ignored for STEP/IGES/Parasolid/images.
        """
        self._require_model()
        fmt = (file_format or os.path.splitext(path)[1].lstrip(".")).lower()
        if fmt not in EXPORT_FORMATS:
            raise SolidWorksError(
                f"Onbekend exportformaat '{fmt}'. Toegestaan: {sorted(EXPORT_FORMATS)}."
            )
        abs_path = os.path.abspath(path)
        result = {"ok": True, "path": abs_path, "format": fmt}
        if fmt in self._MESH_EXPORT_FORMATS:
            old = self._apply_stl_resolution(quality, deviation_mm, angle_deg)
            try:
                self._write_via_saveas3(abs_path)
            finally:
                self._restore_stl_resolution(old)
            result["resolution"] = "custom" if deviation_mm is not None else quality
        else:
            self._write_via_saveas3(abs_path)
        result["bytes"] = os.path.getsize(abs_path)
        return result

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
                "Kon geen nieuwe assembly maken (template + NewAssembly faalden)."
            )
        self._model = model
        return {"ok": True, "title": model.GetTitle()}

    def open_assembly(self, path: str) -> dict:
        """Open an existing .sldasm; it becomes the current document."""
        sw = self._ensure()
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            raise SolidWorksError(f"Bestand niet gevonden: {abs_path}")
        result = sw.OpenDoc6(abs_path, SW_DOC_ASSEMBLY, SW_OPEN_DOC_SILENT, "", 0, 0)
        doc = result[0] if isinstance(result, tuple) else result
        model = binding.wrap(doc, self._mod.IModelDoc2)
        if model is None:
            raise SolidWorksError(f"Kon de assembly niet openen: {abs_path}")
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
            raise SolidWorksError("CreateTransform gaf None terug; kon geen transform bouwen.")
        return xform

    def _transform_data(self, comp) -> list:
        """The component's transform as the raw 16-float ArrayData."""
        xform = binding.wrap(comp.Transform2, self._mod.IMathTransform)
        if xform is None:
            raise SolidWorksError(f"Component '{comp.Name2}' heeft geen leesbare transform.")
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
                    f"Transform van '{comp.Name2}' is niet toegepast: gevraagd positie "
                    f"({x_mm:g}, {y_mm:g}, {z_mm:g}) mm rotatie ({rx_deg:g}, {ry_deg:g}, "
                    f"{rz_deg:g}) graden, teruggelezen positie "
                    f"{[round(m_to_mm(v), 4) for v in got[9:12]]} mm. Element {i} wijkt "
                    f"{abs(want - have):.3e} af. Legt een bestaande mate deze component al vast?"
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
        """Resolve a component by instance name ('Bed-1') or part name ('Bed').

        The short form is accepted only while it is unambiguous; with two copies
        inserted it raises and lists the instance names instead of guessing.
        """
        key = (name or "").strip().lower()
        if not key:
            raise SolidWorksError("Geef een componentnaam op.")
        comps = self._components(asm)
        exact = [c for c in comps if c.Name2.lower() == key]
        if len(exact) == 1:
            return exact[0]
        prefixed = [c for c in comps if c.Name2.lower().startswith(key + "-")]
        if len(prefixed) == 1:
            return prefixed[0]
        if len(prefixed) > 1:
            raise SolidWorksError(
                f"Componentnaam '{name}' is niet uniek; kandidaten: "
                f"{sorted(c.Name2 for c in prefixed)}."
            )
        raise SolidWorksError(
            f"Component '{name}' niet gevonden. Aanwezig: {[c.Name2 for c in comps]}."
        )

    def _component_box(self, comp) -> dict:
        """The component's bounding box in ASSEMBLY coordinates (mm)."""
        box = comp.GetBox(False, False)  # no reference planes, no sketches
        if not box or len(box) < 6:
            return None
        xmin, ymin, zmin, xmax, ymax, zmax = (m_to_mm(v) for v in box[:6])
        return {
            "min_mm": [round(xmin, 4), round(ymin, 4), round(zmin, 4)],
            "max_mm": [round(xmax, 4), round(ymax, 4), round(zmax, 4)],
            "size_mm": [round(xmax - xmin, 4), round(ymax - ymin, 4), round(zmax - zmin, 4)],
        }

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
            raise SolidWorksError(f"Kon component '{comp.Name2}' niet selecteren.")
        asm.FixComponent()
        model.ClearSelection2(True)
        if not comp.IsFixed():
            raise SolidWorksError(f"Component '{comp.Name2}' kon niet vastgezet worden (fixed).")

    def insert_component(self, path: str, x_mm: float = 0.0, y_mm: float = 0.0,
                         z_mm: float = 0.0, fixed: bool | None = None) -> dict:
        """Insert a part into the current assembly with its ORIGIN at (x, y, z) mm.

        The part's own origin lands on the given point (AddComponent5's own X/Y/Z
        would centre the bounding box there instead, so the position is applied
        as a transform and verified by reading it back).

        fixed: True pins the component in place, False leaves it free to be moved
        by mates. The default (None) fixes only the FIRST component, which is the
        ground the rest of the assembly is positioned against.
        """
        asm = self._require_assembly()
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            raise SolidWorksError(f"Part niet gevonden: {abs_path}")
        if fixed is None:
            fixed = not self._component_dispatches(asm)

        # AddComponent5 gives None for a part that is not loaded, so open it
        # silently first and switch back to the assembly before inserting.
        title = self._model.GetTitle()
        self._sw.OpenDoc6(abs_path, SW_DOC_PART, SW_OPEN_DOC_SILENT, "", 0, 0)
        self._sw.ActivateDoc3(title, True, 0, 0)

        comp = binding.wrap(
            asm.AddComponent5(abs_path, SW_ADD_COMPONENT_CURRENT_CONFIG, "", False, "",
                              0.0, 0.0, 0.0),
            self._mod.IComponent2,
        )
        if comp is None:
            raise SolidWorksError(
                f"AddComponent5 gaf None voor '{abs_path}'. Is het een geldig "
                "SolidWorks-part en kon SolidWorks het laden?"
            )
        self._apply_transform(comp, x_mm, y_mm, z_mm, 0.0, 0.0, 0.0)
        if fixed:
            self._fix_component(asm, comp)
        return {"ok": True, "component": self._component_entry(comp)}

    def list_components(self) -> dict:
        """List the components: name, path, fixed, placement, bounding box."""
        asm = self._require_assembly()
        comps = [self._component_entry(c) for c in self._components(asm)]
        return {"ok": True, "count": len(comps), "components": comps}

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
        """Every face of every solid body of the component (component coordinates)."""
        bodies = comp.GetBodies2(SW_BODY_SOLID)
        if not bodies:
            raise SolidWorksError(
                f"Component '{comp.Name2}' heeft geen solid body om een vlak op te kiezen."
            )
        if not isinstance(bodies, (list, tuple)):
            bodies = [bodies]
        faces = []
        for body_dispatch in bodies:
            body_faces = binding.wrap(body_dispatch, self._mod.IBody2).GetFaces()
            if not body_faces:
                continue
            faces.extend(body_faces if isinstance(body_faces, (list, tuple)) else [body_faces])
        return faces

    def _component_face(self, comp, selector: str):
        """Face '+x' / '-z:inner' of a component; returns (IFace2, position_mm).

        The direction is read in the COMPONENT's own coordinate system (verified:
        a component's faces keep part coordinates however the component is
        turned), so '-x' is always the part's own -X face. ':inner' picks the
        cavity side of a hollow part -- the inside of a room wall, not its skin.
        """
        normal, side = self._parse_face_selector(selector)
        face, position_mm = self._pick_planar_face(self._component_faces(comp), normal, side)
        if face is None:
            raise SolidWorksError(
                f"Component '{comp.Name2}' heeft geen planair vlak dat naar {selector} wijst."
            )
        return face, position_mm

    def _face_plane_in_assembly(self, comp, face):
        """A component face as (point, normal) in ASSEMBLY coordinates, in metres.

        The face is reported in component coordinates, so both are pushed through
        the component transform: p' = R*p + t for the point, R*n for the normal
        (R is orthonormal here -- components are never scaled).
        """
        data = self._transform_data(comp)
        rot, trans = data[:9], data[9:12]

        def rotate(v):
            # ArrayData is COLUMN-major: rot[3*col + row] is row `row` of column `col`.
            return [sum(rot[3 * col + row] * v[col] for col in range(3)) for row in range(3)]

        box = face.GetBox()
        centre = rotate([(box[i] + box[i + 3]) / 2.0 for i in range(3)])
        point = [centre[i] + trans[i] for i in range(3)]
        return point, rotate(list(face.Normal))

    # --- mates ----------------------------------------------------------------

    # A mate that builds but resolves to the wrong side is a silent geometry
    # error, so every mate is measured back from the geometry afterwards: the
    # perpendicular distance between the two mated planes (coincident/distance)
    # or the angle between their normals (parallel/perpendicular).
    _MATE_DISTANCE_TOLERANCE_MM = 1e-3
    _MATE_ANGLE_TOLERANCE_DEG = 0.01
    _MATE_EXPECTED_ANGLE_DEG = {"parallel": 0.0, "perpendicular": 90.0}

    def _measure_mate(self, comp_a, face_a, comp_b, face_b) -> tuple:
        """(perpendicular distance mm, angle between normals deg) after a rebuild."""
        point_a, normal_a = self._face_plane_in_assembly(comp_a, face_a)
        point_b, normal_b = self._face_plane_in_assembly(comp_b, face_b)
        gap = abs(sum((point_b[i] - point_a[i]) * normal_a[i] for i in range(3)))
        dot = abs(sum(normal_a[i] * normal_b[i] for i in range(3)))
        return m_to_mm(gap), math.degrees(math.acos(max(-1.0, min(1.0, dot))))

    def add_mate(self, comp_a: str, face_a: str, comp_b: str, face_b: str,
                 mate_type: str = "coincident", distance_mm: float = 0.0,
                 flip: bool = False) -> dict:
        """Mate a planar face of one component to a planar face of another.

        comp_a/comp_b are component names ('Bed' or 'Bed-1'); face_a/face_b are
        direction selectors in each component's OWN frame ('+x', '-z', or
        '+y:inner' for the cavity side of a hollow part). mate_type is
        'coincident', 'distance', 'parallel' or 'perpendicular'; distance_mm
        applies to 'distance'. flip swaps the solution when SolidWorks lands on
        the mirror side.

        After the rebuild the result is measured back from the geometry, and the
        mate is rejected if it did not deliver what was asked.
        """
        asm = self._require_assembly()
        model = self._model
        key = (mate_type or "").lower().strip()
        if key not in MATE_TYPES:
            raise SolidWorksError(
                f"Onbekend mate-type '{mate_type}'. Gebruik: {sorted(MATE_TYPES)}."
            )
        if key == "distance" and distance_mm < 0:
            raise SolidWorksError(f"distance moet >= 0 zijn (kreeg {distance_mm}).")
        if (comp_a or "").strip().lower() == (comp_b or "").strip().lower():
            raise SolidWorksError("Een mate legt twee VERSCHILLENDE componenten vast.")

        first = self._component_by_name(asm, comp_a)
        second = self._component_by_name(asm, comp_b)
        face_1, _ = self._component_face(first, face_a)
        face_2, _ = self._component_face(second, face_b)

        # Both mate entities go in with selection mark 1 (cracked empirically).
        model.ClearSelection2(True)
        selmgr = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        select_data = selmgr.CreateSelectData()
        select_data.Mark = 1
        for entity, comp, selector in ((face_1, first, face_a), (face_2, second, face_b)):
            if not binding.wrap(entity, self._mod.IEntity).Select4(True, select_data):
                raise SolidWorksError(
                    f"Kon vlak {selector} van component '{comp.Name2}' niet selecteren."
                )

        distance_m = mm_to_m(distance_mm) if key == "distance" else 0.0
        result = asm.AddMate5(
            MATE_TYPES[key],           # MateTypeFromEnum
            SW_MATE_ALIGN_CLOSEST,     # AlignFromEnum (components are pre-positioned)
            bool(flip),                # Flip
            distance_m,                # Distance
            distance_m, distance_m,    # DistanceAbsUpperLimit, DistanceAbsLowerLimit
            1.0, 1.0,                  # GearRatioNumerator, GearRatioDenominator
            0.0, 0.0, 0.0,             # Angle, AngleAbsUpperLimit, AngleAbsLowerLimit
            False,                     # ForPositioningOnly
            False,                     # LockRotation
            0,                         # WidthMateOption
            0,                         # ErrorStatus (out)
        )
        model.ClearSelection2(True)
        mate, status = (result[0], result[-1]) if isinstance(result, tuple) else (result, None)
        if mate is None or (status is not None and int(status) != SW_ADD_MATE_NO_ERROR):
            raise SolidWorksError(
                f"Mate '{key}' tussen {first.Name2}:{face_a} en {second.Name2}:{face_b} "
                f"mislukte (AddMate5 status {status}). Staan de vlakken in een stand die "
                "deze mate toelaat, en spreekt hij bestaande mates niet tegen?"
            )

        asm.EditRebuild()
        measured_mm, angle_deg = self._measure_mate(first, face_1, second, face_2)
        expected_mm = distance_mm if key == "distance" else (0.0 if key == "coincident" else None)
        if (expected_mm is not None
                and abs(measured_mm - expected_mm) > self._MATE_DISTANCE_TOLERANCE_MM):
            raise SolidWorksError(
                f"Mate '{key}' is gebouwd maar levert {measured_mm:.4f} mm in plaats van "
                f"{expected_mm:g} mm tussen {first.Name2}:{face_a} en {second.Name2}:{face_b}. "
                "Probeer flip=True, of controleer of een andere mate deze tegenwerkt."
            )
        expected_angle = self._MATE_EXPECTED_ANGLE_DEG.get(key)
        if (expected_angle is not None
                and abs(angle_deg - expected_angle) > self._MATE_ANGLE_TOLERANCE_DEG):
            raise SolidWorksError(
                f"Mate '{key}' is gebouwd maar de vlakken staan {angle_deg:.4f} graden uit "
                f"elkaar in plaats van {expected_angle:g}."
            )
        return {
            "ok": True,
            "mate_type": key,
            "components": [first.Name2, second.Name2],
            "faces": [face_a, face_b],
            "distance_mm": round(measured_mm, 6),
            "angle_deg": round(angle_deg, 6),
            "placements": {c.Name2: self._placement(c) for c in (first, second)},
        }

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
            raise SolidWorksError("Kon de InterferenceDetectionManager niet openen.")
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

    def get_assembly_bounding_box(self) -> dict:
        """Bounding box of the whole assembly (min/max/size in mm)."""
        self._require_assembly()
        return {"ok": True, "bounding_box_mm": self._bounding_box()}
