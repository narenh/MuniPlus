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


# MARK: - Curated patches

from app.data.network import Network
from app.data.shapes import patch_shapes, splice
from app.data.validate import validate
from app.models.curation import ShapePatch, ShapesFile

# A street running east, 0-400 m, with a dogleg 511 put in at 200 m.
STREET = [at(0, 0), at(100, 0), at(200, 0), at(200, 30), at(210, 30), at(210, 0), at(300, 0), at(400, 0)]
STRAIGHT = [at(150, 0), at(250, 0)]


def test_a_patch_replaces_the_stretch_between_its_ends():
    assert splice(STREET, STRAIGHT) == [at(0, 0), at(100, 0), at(150, 0), at(250, 0), at(300, 0), at(400, 0)]


def test_a_shape_running_the_other_way_takes_the_patch_reversed():
    back = splice(STREET[::-1], STRAIGHT)
    assert back == [at(400, 0), at(300, 0), at(250, 0), at(150, 0), at(100, 0), at(0, 0)]


def test_an_end_off_the_shape_leaves_it_alone():
    assert splice(STREET, [at(150, 0), at(250, 40)]) is None


def test_an_end_on_a_vertex_is_not_doubled():
    out = splice(STREET, [at(100, 0), at(300, 0)])
    assert out == [at(0, 0), at(100, 0), at(300, 0), at(400, 0)]


def test_patches_apply_per_line_and_report_what_did_not_fit():
    shapes = {"SF:1": STREET, "SF:2": STREET[::-1], "SF:9": [at(0, 500), at(400, 500)]}
    patches = ShapesFile({
        "dogleg": ShapePatch(lines=["SF:A", "SF:B"], path=STRAIGHT),
        "elsewhere": ShapePatch(lines=["SF:C"], path=STRAIGHT),
    })
    out, unmatched = patch_shapes(patches, {"SF:A": ["SF:1", "SF:2"], "SF:B": ["SF:9"], "SF:C": []}, shapes)
    assert out["SF:1"] == splice(STREET, STRAIGHT)
    assert out["SF:2"] == splice(STREET[::-1], STRAIGHT)
    assert out["SF:9"] == shapes["SF:9"]
    assert unmatched == [("dogleg", "SF:B"), ("elsewhere", "SF:C")]


# The fixture's F runs 17th & Castro -> corner -> Market & Church (shape SF:F1).
# The patch bows its middle 90 m off that line, so thinning keeps it.
F_PATCH = ShapePatch(lines=["SF:F"], path=[(-122.434979, 37.762576), (-122.4331, 37.7650), (-122.429214, 37.76725)])


def test_the_network_serves_patched_shapes(curation, snapshots):
    before = Network(curation, snapshots, "v").shapes()[1]
    curation.shapes = ShapesFile({"f-corner": F_PATCH})
    shapes, etag = Network(curation, snapshots, "v").shapes()
    assert shapes.shapes["SF:F1"] == list(F_PATCH.path)
    # The ETag follows the patch, so a client fetches the corrected shapes.
    assert etag != before


def test_patch_warnings(curation, snapshots):
    curation.shapes = ShapesFile({
        "f-corner": F_PATCH,
        "gone": ShapePatch(lines=["SF:Q"], path=F_PATCH.path),
        "moved": ShapePatch(lines=["SF:F"], path=[(-122.44, 37.75), (-122.43, 37.75)]),
    })
    warnings = [(w.code, w.line) for w in validate(curation, snapshots).warnings if w.code.startswith("shape-")]
    assert warnings == [("shape-patch-unknown-line", "SF:Q"), ("shape-patch-unmatched", "SF:F")]
