"""Differential: station grouping from scratch.

The curation holds only the hand-curated metro stations (cerf's data.json). The
port proposes a station for every other stop, and the resulting partition of
stops into stations is compared with the original ``resolve_stations`` run with
no frozen ids and no overrides. Partitions are compared as sets of stops, so the
station ids do not have to agree. They are checked separately.
"""

import collections

import pytest

from app.ingest import propose as port

from .conftest import ref, to_curation


def blocks(label):
    """stop -> the set of stops sharing its station."""
    members = collections.defaultdict(set)
    for s, station in label.items():
        members[station].add(s)
    return {s: frozenset(members[station]) for s, station in label.items()}


@pytest.fixture(scope="session")
def metro_curation(cerf, overrides):
    return to_curation([cerf], overrides["exclude"])


@pytest.fixture(scope="session")
def proposed(snapshot, metro_curation):
    assigned = {p.id for st in metro_curation.stations.stations.values() for p in st.platforms}
    todo = [s for s in snapshot.stops.root if s not in assigned and s not in metro_curation.ignored.root]
    assert len(todo) == 2949
    props = port.propose(snapshot, metro_curation, todo)
    assert [p.platform for p in props] == todo
    return props


def compare(original, gtfs, metro_for_original, overrides, metro_curation, proposed, covered):
    routes, stops, patterns = gtfs
    patterns = original.drop_excluded({k: v for k, v in patterns.items() if k[0] not in covered},
                                      stops, overrides["exclude"])
    station_of, records, _ = original.resolve_stations(patterns, stops, metro_for_original, {}, {})

    # data.json was loaded before bus.json, so a curated platform the original never
    # saw (served only by a line it skipped) is still in its curated station
    curated = {p.id: sid for sid, st in metro_curation.stations.stations.items() for p in st.platforms}
    want = dict(curated)
    want.update({ref(stops[s]["stop_code"]): sid for s, sid in station_of.items()})
    got = dict(curated)
    got.update({p.platform: p.station or f"new:{p.new_station.id}" for p in proposed})

    common = set(want) & set(got)
    bw, bg = blocks({s: want[s] for s in common}), blocks({s: got[s] for s in common})
    split = {s: (want[s], got[s]) for s in sorted(common) if bw[s] != bg[s]}

    minted = {s: records[want[s]] for s in common
              if want[s] in records and records[want[s]]["metro"] is None}
    new = {p.platform: p.new_station for p in proposed if p.new_station}
    both_new = set(minted) & set(new)
    names = {s: (minted[s]["name"], new[s].name) for s in sorted(both_new) if minted[s]["name"] != new[s].name}
    ids = {s: (want[s], new[s].id) for s in sorted(both_new) if want[s] != new[s].id}
    return common, split, len(both_new), names, ids, set(got) - set(want)


def test_same_lines_give_the_same_stations(original, gtfs, metro_for_original, overrides, metro_curation, proposed):
    """Fed every line, as the port is, the original groups every stop identically."""
    common, split, n_new, names, ids, _ = compare(original, gtfs, metro_for_original, overrides,
                                                  metro_curation, proposed, covered=frozenset())
    assert len(common) == 3232
    assert split == {}
    # 1,671 new stations in the original, holding 2,826 stops, all minted by the port too
    assert len({p.new_station.id for p in proposed if p.new_station}) == 1671
    assert n_new == 2826
    # ...with the same name and the same id, bar one stop. 511 title-cases SFMTA's
    # stop-position code 'SE-FS/ BZ' as 'Se-Fs/ Bz', which the original's
    # case-sensitive strip misses. The port strips it whatever the case.
    assert names == {"SF:17766": ("Mission Bay South & 4th St Se-Fs/ Bz", "Mission Bay South & 4th")}
    assert ids == {"SF:17766": ("missionBaySouth4thStSefsBz", "missionBaySouth4")}


def test_bus_lines_only_as_the_original_ran(original, gtfs, cerf, metro_for_original, overrides, metro_curation,
                                            proposed):
    """As the original actually ran, it read only the lines data.json did not cover.
    A curated platform served only by rail was then invisible to its clustering, so
    it could not pull a bus stop with the same name into its station. The port sees
    every line, so it can. That is the only source of disagreement: 19 stops, at 6
    intersections."""
    covered = frozenset(line["id"] for line in cerf["lines"])
    common, split, _, names, _, extra = compare(original, gtfs, metro_for_original, overrides,
                                                metro_curation, proposed, covered)
    assert len(common) == 3225
    assert {s: got for s, (_, got) in split.items()} == {
        # 18th & Church: the 33's poles on 18th are named like the J's 13987
        # 'Church St & 18th St' (same streets, other order).
        "SF:13322": "doloresPark18", "SF:13323": "doloresPark18",
        "SF:13987": "doloresPark18", "SF:16213": "doloresPark18",
        # 19th Ave & Junipero Serra: the M's 13361 shares the 28's name.
        "SF:13361": "juniperoSerra19", "SF:13362": "juniperoSerra19",
        "SF:13363": "juniperoSerra19", "SF:13376": "juniperoSerra19",
        # Stonestown: the M's 13403 and 17449 share the 28/28R/91's names.
        "SF:13402": "winston19", "SF:13403": "winston19",
        "SF:17448": "winston19", "SF:17449": "winston19",
        # 30th & Church: the J's 14000 is 'Church St & 30th St', like the 24's poles. This is the exact
        # trap tools/station-overrides.json records for 13535/13536 ("any rule that
        # matches on platform names would wrongly merge these"). In sf-transit that
        # override is a curated assignment, so it only bites a new stop here, and
        # a person reviews every proposal.
        "SF:13535": "churchDay", "SF:13536": "churchDay",
        "SF:14000": "churchDay", "SF:14004": "churchDay",
        # San Jose & Geneva: the J/K's 17778 shares its name with the K bus
        # substitute's 16260 and the 714's 18080.
        "SF:16260": "balboaPark", "SF:17778": "balboaPark", "SF:18080": "balboaPark",
    }
    # At all six, the curation (bus.json) keeps the bus stops in a station of
    # their own, as the original did. The names do agree, but the curated
    # convention is to keep a rail stop and its neighbouring bus stops apart. The
    # drop-10% test measures what this costs when the bus stations exist.
    assert names == {"SF:17766": ("Mission Bay South & 4th St Se-Fs/ Bz", "Mission Bay South & 4th")}
    # stops served only by lines the original skipped, and not curated either
    assert extra == {"SF:15239", "SF:15418", "SF:16269", "SF:17164", "SF:17219", "SF:17396", "SF:17869"}


def test_the_underground_substitute_is_exactly_the_old_kind(metro_curation, underground):
    assert port._below_ground(metro_curation) == underground
    assert len(underground) == 12
    # subway membership alone would add the Central Subway's two surface stops
    in_subway = {sid for sub in metro_curation.stations.subways.values() for sid in sub.stations}
    assert in_subway - underground == {"fourthBrannan", "fourthKingT"}
