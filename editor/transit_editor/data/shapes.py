"""Thinning a GTFS shape for drawing, and splicing in curated corrections.

A feed's shape carries a point every few metres along straight track, where two
would draw the same line. Douglas-Peucker drops every point within ``TOLERANCE_M``
of the line through its neighbours, so a long straight street keeps its two ends
and a curve keeps as many points as its bend needs.
"""

import math
from collections.abc import Mapping

from ..ingest.propose import M_PER_DEG_LAT, M_PER_DEG_LON
from ..models.curation import ShapesFile

TOLERANCE_M = 0.5
"""Four pixels at the map's z20, where the line itself is ten wide. On a 2024 SFMTA
feed the 138 shapes the line directions name went from 25,874 points to 18,257
(103 KB gzipped); a metre would save another 30 KB and bend visibly at z20."""


def simplify(points: list[tuple[float, float]], tolerance_m: float = TOLERANCE_M) -> list[tuple[float, float]]:
    """``(lon, lat)`` points in, the subset that stays within ``tolerance_m`` out.
    The first and last points are always kept."""
    if len(points) < 3:
        return list(points)
    # Flat metres at SF's latitude: across the city that is within a fraction of a
    # percent of the true distance, far finer than the tolerance.
    xy = [(lon * M_PER_DEG_LON, lat * M_PER_DEG_LAT) for lon, lat in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    # Iterative, not recursive: a long bus route is thousands of points, and a
    # recursion that deep is a stack limit away from a crash.
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        (ax, ay), (bx, by) = xy[first], xy[last]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        worst, worst_i = 0.0, -1
        for i in range(first + 1, last):
            px, py = xy[i]
            if length == 0:
                # A loop that ends where it starts: measure from the point itself.
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dx * (ay - py) - dy * (ax - px)) / length
            if d > worst:
                worst, worst_i = d, i
        if worst > tolerance_m:
            keep[worst_i] = True
            stack.append((first, worst_i))
            stack.append((worst_i, last))
    return [p for p, k in zip(points, keep) if k]


# MARK: - Curated patches

ANCHOR_M = 25.0
"""How far a patch's end may be from 511's shape and still be on it. Wide enough
for an end placed by eye on the map, narrower than the half-block that would let
it catch a parallel street."""


def _position(points: list[tuple[float, float]], at: tuple[float, float]) -> tuple[int, float, float]:
    """Where ``at`` falls along ``points``: the segment it is nearest, how far
    along that segment (0-1), and how far off the line, in metres."""
    px, py = at[0] * M_PER_DEG_LON, at[1] * M_PER_DEG_LAT
    best = (0, 0.0, math.inf)
    for i in range(len(points) - 1):
        ax, ay = points[i][0] * M_PER_DEG_LON, points[i][1] * M_PER_DEG_LAT
        bx, by = points[i + 1][0] * M_PER_DEG_LON, points[i + 1][1] * M_PER_DEG_LAT
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy
        t = 0.0 if span == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / span))
        d = math.hypot(px - ax - t * dx, py - ay - t * dy)
        if d < best[2]:
            best = (i, t, d)
    return best


def splice(points: list[tuple[float, float]], path: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
    """``points`` with the stretch between ``path``'s two ends replaced by
    ``path``, or None when either end is not on the shape.

    Works in either direction: a shape that meets the end before the start is
    running the other way along the same track, and takes the path reversed.
    Where a shape passes an end twice (a terminal loop) the nearer pass is used.
    """
    if len(points) < 2:
        return None
    start, end = _position(points, path[0]), _position(points, path[-1])
    if start[2] > ANCHOR_M or end[2] > ANCHOR_M:
        return None
    if start[:2] > end[:2]:
        start, end, path = end, start, path[::-1]
    # Up to the vertex before the start, the path, then from the vertex after the end.
    joined = [*points[: start[0] + 1], *path, *points[end[0] + 1 :]]
    # An end placed exactly on a vertex would otherwise leave that point in twice.
    return [p for i, p in enumerate(joined) if i == 0 or p != joined[i - 1]]


def extend(points: list[tuple[float, float]], path: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
    """``points`` with ``path`` joined on at one end, or None when it does not fit.

    One end of ``path`` must be within ANCHOR_M of the shape's first or last point
    and the other must be off the shape: a line carried on past where its trips
    end, to meet its station (the J and K at Balboa Park end 75 m short of the
    M's platform, across the same station). A path with both ends on the shape
    is a splice, not this.
    """
    if len(points) < 2:
        return None
    first, last = points[0], points[-1]

    def near(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return math.hypot((a[0] - b[0]) * M_PER_DEG_LON, (a[1] - b[1]) * M_PER_DEG_LAT) <= ANCHOR_M

    for p in (path, path[::-1]):
        if _position(points, p[0])[2] <= ANCHOR_M:
            continue  # the free end must be off the shape
        if near(p[-1], first):
            return [*p[:-1], *points]
        if near(p[-1], last):
            return [*points, *p[-2::-1]]
    return None


def draw_shapes(
    patches: ShapesFile,
    drawn: Mapping[str, list[str]],
    drawn_stops: Mapping[str, list[tuple[float, float]]],
    shapes: Mapping[str, list[tuple[float, float]]],
) -> tuple[dict[str, list[tuple[float, float]]], list[tuple[str, str]]]:
    """The shapes each line draws (``drawn``: line -> shape ids), as served:

    1. every curated patch with both ends on a shape spliced in;
    2. cut to the shape's first and last stop riders use (``drawn_stops``: shape
       id -> the stops of the pattern that draws it, ignored ones left out);
    3. every other patch joined on as an extension, after the cut, which would
       otherwise remove it again.

    Also every (patch, line) whose patch fits none of that line's shapes, which
    the validator reports. Only the shapes named in ``drawn`` are returned: those
    are the only ones served."""
    out = {sid: shapes[sid] for sids in drawn.values() for sid in sids if sid in shapes}
    fitted: set[tuple[str, str]] = set()
    ordered = sorted(patches.root.items())
    for patch_id, patch in ordered:
        for line in patch.lines:
            for sid in drawn.get(line, []):
                if sid in out and (spliced := splice(out[sid], list(patch.path))) is not None:
                    out[sid] = spliced
                    fitted.add((patch_id, line))
    for sid, stops in drawn_stops.items():
        if sid in out:
            out[sid] = clip_to_stops(out[sid], stops)
    for patch_id, patch in ordered:
        for line in patch.lines:
            if (patch_id, line) in fitted:
                continue
            for sid in drawn.get(line, []):
                if sid in out and (extended := extend(out[sid], list(patch.path))) is not None:
                    out[sid] = extended
                    fitted.add((patch_id, line))
    unmatched = [(patch_id, line) for patch_id, patch in ordered for line in patch.lines if (patch_id, line) not in fitted]
    return out, unmatched


# MARK: - Terminal tails

STOP_M = 50.0
"""How far a stop may be from its line's shape and still be placed on it. Stops
sit at the kerb, and a subway platform can be tens of metres from the track's
centre line in the feed."""


def _segments(points: list[tuple[float, float]], at: tuple[float, float]) -> list[tuple[float, float]]:
    """Each segment's (t, distance) from ``at``, in metres."""
    px, py = at[0] * M_PER_DEG_LON, at[1] * M_PER_DEG_LAT
    out = []
    for i in range(len(points) - 1):
        ax, ay = points[i][0] * M_PER_DEG_LON, points[i][1] * M_PER_DEG_LAT
        bx, by = points[i + 1][0] * M_PER_DEG_LON, points[i + 1][1] * M_PER_DEG_LAT
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy
        t = 0.0 if span == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / span))
        out.append((t, math.hypot(px - ax - t * dx, py - ay - t * dy)))
    return out


def stop_positions(points: list[tuple[float, float]], stops: list[tuple[float, float]]) -> list[tuple[int, float] | None]:
    """Where each stop falls along the shape, in order: (segment, t), or None for
    a stop too far from it.

    Each stop is looked for only from where the one before it was found, and at
    the first pass that comes within STOP_M, so a route that passes the same
    corner twice places each stop on the right pass. The nearest point overall
    would not: the 36's last stop is also nearest a point six kilometres earlier.
    """
    out: list[tuple[int, float] | None] = []
    at = (0, 0.0)
    for stop in stops:
        segs = _segments(points, stop)
        found = None
        for i in range(at[0], len(segs)):
            t, d = segs[i]
            if i == at[0] and t < at[1]:
                t = at[1]  # not behind the stop before
            if d <= STOP_M:
                # This pass: follow it to its closest point.
                while i + 1 < len(segs) and segs[i + 1][1] < d:
                    i += 1
                    t, d = segs[i]
                found = (i, t)
                break
        out.append(found)
        if found:
            at = found
    return out


def clip_to_stops(points: list[tuple[float, float]], stops: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The shape from its first stop to its last.

    A GTFS shape is where the vehicle drives, which can run on past the last
    stop to where it turns: the J, K, L and M go 600 m beyond Embarcadero to
    their turnaround. A line map ends a line where riders' trips end.
    """
    placed = [p for p in stop_positions(points, stops) if p]
    if len(placed) < 2 or placed[0] >= placed[-1]:
        return points

    def at(seg: int, t: float) -> tuple[float, float]:
        (ax, ay), (bx, by) = points[seg], points[seg + 1]
        return (round(ax + (bx - ax) * t, 6), round(ay + (by - ay) * t, 6))

    (s0, t0), (s1, t1) = placed[0], placed[-1]
    out = [at(s0, t0), *points[s0 + 1 : s1 + 1], at(s1, t1)]
    return [p for i, p in enumerate(out) if i == 0 or p != out[i - 1]]
