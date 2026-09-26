"""Fully define the sketches the tools draw.

SolidWorks marks an under-defined sketch with (-), and a stray drag then moves
its geometry. So every tool constrains what it draws, the way a designer would:
relations only where the input already satisfies them exactly (so nothing
moves), and one dimension, measured from the origin, for every remaining
degree of freedom. Profiles with many points, typically traced from a mesh,
are fixed instead (see MAX_DIMENSIONED_VERTICES).

plan_sketch decides what each point gets and is pure (unit-tested);
SketchDefiner carries a plan out in the open sketch over COM.
"""

from dataclasses import dataclass

import pythoncom
import win32com.client

from . import binding
from .constants import (
    SW_CONSTRAINT_COINCIDENT,
    SW_CONSTRAINT_FIXED,
    SW_CONSTRAINT_HORIZONTAL,
    SW_CONSTRAINT_HORIZONTAL_POINTS,
    SW_CONSTRAINT_VERTICAL,
    SW_CONSTRAINT_VERTICAL_POINTS,
    SW_FULLY_CONSTRAINED,
    SW_SKETCH_ARC,
    SW_SKETCH_LINE,
)
from .errors import SolidWorksError
from .units import m_to_mm

# Input coordinates (mm) this close count as equal: exactly axis-aligned
# segments, points exactly on an origin axis. Anything further apart is real
# geometry -- a sliced mesh wall is often a few nanometres off vertical, and a
# vertical relation would move it.
EXACT_MM = 1e-9

# Every dimension makes SolidWorks re-solve the sketch, so dimensioning grows
# quadratically: 40 points took 15 s (7 s with solving paused), 120 points 90 s.
# Sketches with more points than this are fixed instead; nobody edits that many
# dimensions, and such profiles usually come from a mesh.
MAX_DIMENSIONED_VERTICES = 24


@dataclass(frozen=True)
class SketchPlan:
    """How to fully define a sketch. Indices refer to the planned points/segments.

    coincident: (duplicate, point) pairs of distinct points at one location.
    horizontal / vertical: segments that get that relation.
    at_origin: the point made coincident with the origin.
    origin_x / origin_y: points on the origin's vertical / horizontal line (a
    relation, because SolidWorks cannot make a 0 mm dimension).
    x_dims / y_dims: (point, coordinate) dimensioned from the origin.
    fixed: too many points to dimension; fix everything instead.
    """
    fixed: bool = False
    coincident: tuple = ()
    horizontal: tuple = ()
    vertical: tuple = ()
    at_origin: int | None = None
    origin_x: tuple = ()
    origin_y: tuple = ()
    x_dims: tuple = ()
    y_dims: tuple = ()


class _Groups:
    """Points that must share one coordinate; a group is 'held' once constrained."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))
        self._held = [False] * n

    def _root(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def hold(self, i: int) -> None:
        self._held[self._root(i)] = True

    def merge(self, a: int, b: int) -> None:
        ra, rb = self._root(a), self._root(b)
        if ra != rb:
            self._parent[ra] = rb
            self._held[rb] = self._held[rb] or self._held[ra]

    def join(self, a: int, b: int) -> bool:
        """Merge for a relation between a and b; False if they already share a group."""
        if self._root(a) == self._root(b):
            return False
        self.merge(a, b)
        return True

    def free_groups(self) -> list:
        """Members of every group nothing holds yet, ordered by their first point."""
        members = {}
        for i in range(len(self._parent)):
            members.setdefault(self._root(i), []).append(i)
        return [m for root, m in sorted(members.items(), key=lambda kv: kv[1][0]) if not self._held[root]]


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= EXACT_MM


def plan_sketch(points_mm, segments) -> SketchPlan:
    """Plan relations and dimensions for points joined by straight segments.

    points_mm: [(x, y), ...] in sketch coordinates relative to the origin (mm).
    segments: [(i, j), ...] point index pairs, one per line in the sketch.
    Removes exactly two degrees of freedom per point.
    """
    pts = [(float(x), float(y)) for x, y in points_mm]
    n = len(pts)
    if n > MAX_DIMENSIONED_VERTICES:
        return SketchPlan(fixed=True)
    xs, ys = _Groups(n), _Groups(n)

    at_origin = next((i for i, (x, y) in enumerate(pts) if _same(x, 0.0) and _same(y, 0.0)), None)
    if at_origin is not None:
        xs.hold(at_origin)
        ys.hold(at_origin)

    # distinct points at one spot (e.g. a centreline end on a profile corner):
    # tie them first, while each duplicate is still completely free
    alias = list(range(n))
    coincident = []
    for k in range(n):
        twin = next((i for i in range(k) if alias[i] == i
                     and _same(pts[i][0], pts[k][0]) and _same(pts[i][1], pts[k][1])), None)
        if twin is not None:
            alias[k] = twin
            coincident.append((k, twin))
            xs.merge(k, twin)
            ys.merge(k, twin)

    horizontal, vertical = [], []
    for s, (a, b) in enumerate(segments):
        a, b = alias[a], alias[b]
        (x1, y1), (x2, y2) = pts[a], pts[b]
        if _same(y1, y2) and not _same(x1, x2):
            if ys.join(a, b):
                horizontal.append(s)
        elif _same(x1, x2) and not _same(y1, y2):
            if xs.join(a, b):
                vertical.append(s)

    def anchor(groups, axis):
        on_origin, dims = [], []
        for members in groups.free_groups():
            point = members[0]
            value = pts[point][axis]
            if _same(value, 0.0):
                on_origin.append(point)
            else:
                dims.append((point, value))
        return tuple(on_origin), tuple(dims)

    origin_x, x_dims = anchor(xs, 0)
    origin_y, y_dims = anchor(ys, 1)
    return SketchPlan(False, tuple(coincident), tuple(horizontal), tuple(vertical), at_origin,
                      origin_x, origin_y, x_dims, y_dims)


def plan_profile(points_mm, closed: bool) -> SketchPlan:
    """plan_sketch for a polyline: segment i runs from point i to point i+1."""
    n = len(points_mm)
    if closed and n < 3:
        raise SolidWorksError(f"A closed profile needs at least 3 points (got {n}).")
    count = n if closed else max(n - 1, 0)
    return plan_sketch(points_mm, [(i, (i + 1) % n) for i in range(count)])


_TEXT_OFFSET_M = 0.002  # dimension text sits this far from what it measures


def _entities(*objects):
    """The SAFEARRAY of dispatch pointers AddRelation expects."""
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [o._oleobj_ for o in objects])


class SketchDefiner:
    """Fully defines the sketch being edited, following plan_sketch.

    Relations go through the sketch's RelationManager, which returns the new
    relation, so a refusal fails loud. Dimensions are measured from the model
    origin, which SolidWorks projects onto any sketch plane. Automatic solving
    and dimension display are paused meanwhile: that halves the time per
    dimension.
    """

    def __init__(self, model, mod, sketch_manager, sketch, origin_point) -> None:
        self._model = model
        self._mod = mod
        self._sk = sketch_manager
        self._sketch = sketch
        self._origin = origin_point
        self._relations = binding.wrap(sketch.RelationManager, mod.ISketchRelationManager)

    def define(self, lines=(), circles=(), names=None, origin_mm=(0.0, 0.0)) -> dict:
        """Constrain `lines` and `circles`; return {role: dimension name}.

        Points are numbered in the order the lines (start, end) and then the
        circle centres first reach them, so a polyline keeps its vertex order.
        names maps ('x', point), ('y', point) or ('diameter', circle) to a role
        name; the default is 'x3', 'y3', 'diameter'. origin_mm is where the
        model origin lies in this sketch's coordinates (non-zero on a face
        sketch that does not contain it).
        """
        names = names or {}
        lines = [binding.wrap(line, self._mod.ISketchLine) for line in lines]
        circles = [binding.wrap(circle, self._mod.ISketchArc) for circle in circles]
        points, index = [], {}

        def number(point_dispatch):
            point = binding.wrap(point_dispatch, self._mod.ISketchPoint)
            key = tuple(point.GetID())
            if key not in index:
                index[key] = len(points)
                points.append(point)
            return index[key]

        segments = [(number(line.GetStartPoint2()), number(line.GetEndPoint2())) for line in lines]
        for circle in circles:
            number(circle.GetCenterPoint2())
        u0, v0 = origin_mm
        plan = plan_sketch([(m_to_mm(p.X) - u0, m_to_mm(p.Y) - v0) for p in points], segments)
        if plan.fixed:
            self.fix([*lines, *circles])
            return {}

        paused = self._pause()
        try:
            for duplicate, twin in plan.coincident:
                self._relate(SW_CONSTRAINT_COINCIDENT, "coincident", points[duplicate], points[twin])
            for s in plan.horizontal:
                self._relate(SW_CONSTRAINT_HORIZONTAL, "horizontal", lines[s])
            for s in plan.vertical:
                self._relate(SW_CONSTRAINT_VERTICAL, "vertical", lines[s])
            if plan.at_origin is not None:
                self._relate(SW_CONSTRAINT_COINCIDENT, "on-origin", points[plan.at_origin], self._origin)
            for i in plan.origin_x:
                self._relate(SW_CONSTRAINT_VERTICAL_POINTS, "above-origin", points[i], self._origin)
            for i in plan.origin_y:
                self._relate(SW_CONSTRAINT_HORIZONTAL_POINTS, "level-with-origin", points[i], self._origin)
            dims = {}
            for i, _ in plan.x_dims:
                role = names.get(("x", i), f"x{i}")
                dims[role] = self._dimension(self._model.AddHorizontalDimension2, points[i], role)
            for i, _ in plan.y_dims:
                role = names.get(("y", i), f"y{i}")
                dims[role] = self._dimension(self._model.AddVerticalDimension2, points[i], role)
            for c, circle in enumerate(circles):
                role = names.get(("diameter", c), "diameter" if len(circles) == 1 else f"diameter{c}")
                dims[role] = self._diameter(circle, role)
            return dims
        finally:
            self._resume(paused)

    def fix(self, segments) -> None:
        """Freeze lines, arcs and splines: fully defined, nothing to edit.

        A fixed line or arc still lets its end points slide along it (an open
        path stays under-defined), so for those every point is fixed instead:
        ends, and centres of arcs. A spline is fixed as a whole.
        """
        paused = self._pause()
        try:
            seen = set()
            for segment in segments:
                kind = binding.wrap(segment, self._mod.ISketchSegment).GetType()
                if kind == SW_SKETCH_LINE:
                    line = binding.wrap(segment, self._mod.ISketchLine)
                    points = [line.GetStartPoint2(), line.GetEndPoint2()]
                elif kind == SW_SKETCH_ARC:
                    arc = binding.wrap(segment, self._mod.ISketchArc)
                    points = [arc.GetStartPoint2(), arc.GetEndPoint2(), arc.GetCenterPoint2()]
                else:
                    self._relate(SW_CONSTRAINT_FIXED, "fix", segment)
                    continue
                for point_dispatch in points:
                    if point_dispatch is None:  # a full circle has no end points
                        continue
                    point = binding.wrap(point_dispatch, self._mod.ISketchPoint)
                    key = tuple(point.GetID())
                    if key not in seen:
                        seen.add(key)
                        self._relate(SW_CONSTRAINT_FIXED, "fix", point)
        finally:
            self._resume(paused)

    def fully_defined(self) -> bool:
        return self._sketch.GetConstrainedStatus() == SW_FULLY_CONSTRAINED

    def _pause(self) -> tuple:
        paused = (self._sk.AutoSolve, self._sk.DisplayWhenAdded)
        self._sk.AutoSolve = False
        self._sk.DisplayWhenAdded = False
        return paused

    def _resume(self, paused: tuple) -> None:
        self._sk.AutoSolve, self._sk.DisplayWhenAdded = paused
        self._model.ClearSelection2(True)

    def _relate(self, kind: int, label: str, *entities) -> None:
        if self._relations.AddRelation(_entities(*entities), kind) is None:
            raise SolidWorksError(f"SolidWorks refused a {label} relation while defining the sketch.")

    def _dimension(self, add, point, role: str) -> str:
        if not (point.Select4(False, None) and self._origin.Select4(True, None)):
            raise SolidWorksError(f"Could not select a sketch point and the origin for dimension '{role}'.")
        display = add(point.X + _TEXT_OFFSET_M, point.Y + _TEXT_OFFSET_M, 0.0)
        if display is None:
            raise SolidWorksError(f"SolidWorks refused the dimension '{role}'.")
        return self._named(display, role)

    def _diameter(self, circle, role: str) -> str:
        if not binding.wrap(circle, self._mod.ISketchSegment).Select4(False, None):
            raise SolidWorksError(f"Could not select the circle for dimension '{role}'.")
        centre = binding.wrap(circle.GetCenterPoint2(), self._mod.ISketchPoint)
        offset = circle.GetRadius() + _TEXT_OFFSET_M
        display = self._model.AddDiameterDimension2(centre.X + offset, centre.Y + offset, 0.0)
        if display is None:
            raise SolidWorksError(f"SolidWorks refused the dimension '{role}'.")
        return self._named(display, role)

    def _named(self, display, role: str) -> str:
        """Rename the dimension to its role; return what set_dimension takes, e.g. 'width@Sketch1'."""
        dim = binding.wrap(binding.wrap(display, self._mod.IDisplayDimension).GetDimension2(0), self._mod.IDimension)
        dim.Name = role
        return dim.GetNameForSelection()
