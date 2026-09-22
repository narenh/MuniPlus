"""Differential: the ported heading rule against the original, stop for stop, on
511's GTFS for SF."""

import pytest

from app.ingest import propose as port

from .conftest import ref


@pytest.fixture(scope="session")
def ignored(overrides):
    return {ref(c) for c in overrides["exclude"]}


@pytest.fixture(scope="session")
def ported(snapshot, ignored):
    return port.compute_headings(port.stop_patterns(snapshot, ignored), snapshot.stops.root)


def original_headings(original, gtfs, excluded, covered=frozenset()):
    """The original's headings before curated ones are laid over them, keyed by platform id."""
    routes, stops, patterns = gtfs
    patterns = {k: v for k, v in patterns.items() if k[0] not in covered}
    # main() always runs drop_excluded, which also drops one-stop patterns
    patterns = original.drop_excluded(patterns, stops, excluded)
    head, snapped = original.compute_headings(patterns, stops, {"stations": []}, {})
    return ({ref(stops[s]["stop_code"]): h for s, h in head.items()},
            {ref(stops[s]["stop_code"]): h for s, h in snapped.items()})


def test_same_patterns_give_identical_headings(original, gtfs, overrides, ported):
    want, want_snapped = original_headings(original, gtfs, overrides["exclude"])
    got, got_snapped = ported

    assert len(got) == 3232              # all 3,240 stops in the zip, less the 8 ignored
    assert got == want
    # the street-axis snap overrode the direction of travel at the same stops too
    assert got_snapped == want_snapped
    assert len(got_snapped) == 159


def test_seeing_rail_lines_changes_two_bus_stops(original, gtfs, cerf, overrides, ported):
    """The original only ever read the lines data.json did not cover; the port reads
    every line. On the 3,086 stops those bus lines serve, that changes two headings,
    both on streets rail also runs along."""
    covered = {line["id"] for line in cerf["lines"]}
    want, _ = original_headings(original, gtfs, overrides["exclude"], covered)
    got, _ = ported

    assert len(want) == 3086
    diff = {s: (want[s], got[s]) for s in want if want[s] != got[s]}
    assert diff == {
        # Bayshore Blvd & Arleta Ave. The T's platforms become two of the three
        # nearest stops named for Bayshore (17399, 74 m, and 17395, 76 m), in
        # place of the 8/9's poles at Leland. 17399 is 'Bayshore Blvd & Areta
        # Ave', a typo in 511, so it does not count as this stop's own corner.
        # With them, the run is lopsided enough to snap east-west. Without them,
        # it is too diagonal to snap, and travel (south) stands. Bayshore runs
        # NW-SE here, so neither answer is clearly wrong. The curated heading, from
        # SFMTA's feed, is southbound.
        "SF:13772": ("southbound", "westbound"),
        # Market St & Sanchez St. The original saw only the owl and bus-substitute
        # trips here (92). The F adds 284, which tip the diagonal Market St vector
        # from west to south. This stop is curated as an F platform, so it is never
        # proposed.
        "SF:15690": ("westbound", "southbound"),
    }
