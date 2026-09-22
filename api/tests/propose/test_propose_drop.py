"""Drop 10%: forget some of the curation's bus stop assignments, then ask for them back.

The curation assigns every stop (cerf's data.json, then bus.json folded in the
way the app loads them). A seeded-random 10% of the assignments that only
bus.json makes are removed. A station left with no platforms goes entirely. Each
dropped stop should then come back:

  * to the same station, where that station still exists; or
  * to a new station with the old station's name, shared with exactly the
    other dropped stops of that station, where it was removed entirely.
"""

import collections
import random

import pytest

from app.ingest import propose as port
from app.models.curation import StationsFile

from .conftest import ref, to_curation

SEEDS = (1, 2, 3, 4, 5)


@pytest.fixture(scope="session")
def full(cerf, bus, overrides):
    return to_curation([cerf, bus], overrides["exclude"])


def run(snapshot, cerf, full, seed):
    cerf_codes = {ref(p["id"]) for st in cerf["stations"] for p in st["platforms"]}
    home = {p.id: sid for sid, st in full.stations.stations.items() for p in st.platforms}
    heading = {p.id: p.heading for st in full.stations.stations.values() for p in st.platforms}
    # bus.json still lists 4 stops that 511 no longer has; those cannot be proposed
    pool = sorted(s for s in home if s not in cerf_codes and s in snapshot.stops.root)
    assert len(pool) == 2936
    dropped = set(random.Random(seed).sample(pool, round(len(pool) * 0.1)))

    kept = {}
    for sid, st in full.stations.stations.items():
        platforms = [p for p in st.platforms if p.id not in dropped]
        if platforms:
            kept[sid] = st.model_copy(update={"platforms": platforms})
    curation = full.model_copy(update={"stations": StationsFile(subways=full.stations.subways, stations=kept)})
    proposals = port.propose(snapshot, curation, sorted(dropped))
    assert sorted(p.platform for p in proposals) == sorted(dropped)

    minted = collections.defaultdict(set)
    for p in proposals:
        if p.new_station:
            minted[p.new_station.id].add(p.platform)
    removed = collections.defaultdict(set)
    for s in dropped:
        if home[s] not in kept:
            removed[home[s]].add(s)

    misses, back, reminted = {}, set(), set()
    for p in proposals:
        old = home[p.platform]
        if old in kept:
            if p.station != old:
                misses[p.platform] = p.station or f"new:{p.new_station.id}"
        elif (p.new_station is None or p.new_station.name != full.stations.stations[old].name
              or minted[p.new_station.id] != removed[old]):
            misses[p.platform] = p.station or f"new:{p.new_station.id}"
        else:
            back.add(old)
            if p.new_station.id == old:
                reminted.add(old)
    headings = {p.platform: p.heading for p in proposals if p.heading != heading[p.platform]}
    return len(dropped), len(removed), misses, headings, back, reminted


# Every miss, across all five seeds, by cause. The causes, measured against
# the SFMTA feed that bus.json was built from (api/.local/upstream/gtfs-sfmta.zip):
#
# RENAMED: 511 names 157 stops differently from SFMTA. It appends landmarks
#   ('Townsend St & Lusk St' -> 'Townsend St & Lusk St/Caltrain') and relabels
#   bays ('Daly City BART Station' -> 'Daly City BART Stop B3'). The old station
#   was named, and its stops clustered, from SFMTA's spelling. Given 511's
#   spelling, the original makes the same proposal: the grouping differential
#   shows the two agree on every stop.
# FROZEN: bus.json's marketMontgomery exists only because its id was frozen
#   before data.json's market2 became 'Market & 2nd/Montgomery'. Run from scratch
#   on 511, the original also puts 15684 into market2.
# RAIL: the M's 13361 '19th Ave & Junipero Serra Blvd' (juniperoSerra19) is 78 m
#   from the 28's 13363, and the 28's other pole there is 82 m. The original
#   never saw 13361, because the M was one of data.json's lines.
RENAMED, FROZEN, RAIL = "renamed", "frozen", "rail"
EXPECTED = {
    1: {"SF:18109": RENAMED, "SF:18110": RENAMED},          # Townsend & Lusk -> '.../Caltrain'
    2: {"SF:16927": RENAMED,                                # Washington & Powell -> '.../Chinatown'
        "SF:17321": RENAMED},                               # 3rd & Warriors Way -> '... / Chase Center'
    3: {"SF:13363": RAIL,
        "SF:14766": RENAMED, "SF:16516": RENAMED,           # Geary & Stockton: one pole now '.../Union Square'
        "SF:15684": FROZEN,
        "SF:16299": RENAMED},                               # Sacramento & Grant -> '.../Chinatown'
    4: {"SF:13363": RAIL,
        "SF:14341": RENAMED,                                # 'Daly City BART Stop B3'
        "SF:14712": RENAMED,                                # 'Font Blvd & Tapia Dr' -> 'Font & Holloway/Tapia-Sfsu'
        "SF:14769": RENAMED},                               # Geary & Webster -> '.../Japantown'
    5: {"SF:15837": RENAMED},                               # Pacific & Grant -> '.../Chinatown'
}


# Proposed headings that differ from the curated one, which bus.json computed
# with this same rule from SFMTA's feed.
EXPECTED_HEADINGS = {
    # 511 renamed SFMTA's 'Mason St & Washington St' to 'Washington St & Mason St /
    # Cable Car Museum'. The stop is now named for Washington, which runs east-west,
    # so the snap follows Washington instead of Mason.
    1: {"SF:15372": "eastbound"},
    # Bayshore & Arleta: the T's platforms change how Bayshore's axis reads; see
    # test_propose_headings.py.
    3: {"SF:13772": "westbound"},
}
# How many stations were removed entirely, and how many of them came back whole,
# with the same name, and with the same id too, since that id was free again.
EXPECTED_REMOVED = {1: (80, 79), 2: (90, 88), 3: (85, 82), 4: (72, 71), 5: (83, 82)}


@pytest.mark.parametrize("seed", SEEDS)
def test_dropped_assignments_come_back(snapshot, cerf, full, seed):
    n, removed, misses, headings, back, reminted = run(snapshot, cerf, full, seed)
    print(f"\nseed {seed}: {n} dropped, {len(misses)} missed: {misses}; "
          f"{removed} stations removed entirely, {len(back)} back; headings differ: {headings}")
    assert n == 294
    # No tolerance. Every miss is listed, with its cause, above.
    assert set(misses) == set(EXPECTED[seed])
    assert headings == EXPECTED_HEADINGS.get(seed, {})
    assert (removed, len(back)) == EXPECTED_REMOVED[seed]
    assert reminted == back


def test_overall_recovery():
    """1,470 stop assignments dropped across the five seeds, 1,456 recovered (99.0%)."""
    dropped = 294 * len(SEEDS)
    missed = sum(len(m) for m in EXPECTED.values())
    assert (dropped, missed) == (1470, 14)
    assert sum(1 for m in EXPECTED.values() for cause in m.values() if cause == RENAMED) == 11
