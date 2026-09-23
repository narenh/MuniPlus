"""Thinning a GTFS shape for drawing.

A feed's shape carries a point every few metres along straight track, where two
would draw the same line. Douglas-Peucker drops every point within ``TOLERANCE_M``
of the line through its neighbours, so a long straight street keeps its two ends
and a curve keeps as many points as its bend needs.
"""

import math

from ..ingest.propose import M_PER_DEG_LAT, M_PER_DEG_LON

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
