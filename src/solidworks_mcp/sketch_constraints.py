"""Plan how to fully define the sketches the tools draw.

SolidWorks marks an under-defined sketch with (-), and a stray drag then moves
its geometry. The plan constrains a sketch the way a designer would: relations
only where the input already satisfies them exactly (so nothing moves), and one
dimension, measured from the origin, for every remaining degree of freedom.
Profiles with many points, typically traced from a mesh, are fixed instead
(see MAX_DIMENSIONED_VERTICES).
"""

from dataclasses import dataclass

from .errors import SolidWorksError

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

