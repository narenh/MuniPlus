"""Thinning shapes for drawing."""

from app.data.shapes import simplify
from app.ingest.propose import M_PER_DEG_LAT, M_PER_DEG_LON

# One metre, in degrees, at SF's latitude.
LON_M, LAT_M = 1 / M_PER_DEG_LON, 1 / M_PER_DEG_LAT
O = (-122.4, 37.77)


def at(east_m: float, north_m: float) -> tuple[float, float]:
    return (O[0] + east_m * LON_M, O[1] + north_m * LAT_M)


def test_a_straight_run_keeps_its_ends():
    run = [at(x, 0) for x in range(0, 500, 5)]
    assert simplify(run) == [run[0], run[-1]]


def test_a_wobble_under_the_tolerance_is_dropped_and_one_over_is_kept():
    assert simplify([at(0, 0), at(50, 0.4), at(100, 0)]) == [at(0, 0), at(100, 0)]
    assert simplify([at(0, 0), at(50, 0.6), at(100, 0)]) == [at(0, 0), at(50, 0.6), at(100, 0)]


def test_a_corner_is_kept():
    shape = [at(0, 0), at(50, 0), at(100, 0), at(100, 50), at(100, 100)]
    assert simplify(shape) == [at(0, 0), at(100, 0), at(100, 100)]


def test_a_loop_back_to_its_start_is_not_collapsed():
    loop = [at(0, 0), at(100, 0), at(100, 100), at(0, 100), at(0, 0)]
    assert simplify(loop) == loop


def test_short_shapes_are_returned_as_they_are():
    assert simplify([]) == []
    assert simplify([at(0, 0)]) == [at(0, 0)]
    assert simplify([at(0, 0), at(1, 0)]) == [at(0, 0), at(1, 0)]
