"""Suggest a station and a heading for 511 stops that no station claims yet.

A port of ``tools/build_bus_data.py`` (read ``tools/README.md`` for the evidence
behind every rule here). The old generator decided station identity and headings
for every surface stop on each build. In the new system the curation already
assigns every stop that exists, so this only runs when 511 adds stops: it
*suggests*, and a person accepts or rejects the suggestion in the editor's review
queue. Nothing here writes a file.

How the old generator's inputs map onto the new model:

  * ``data.json`` was authoritative; now the **curation** is. Existing
    assignments and headings are never changed. They feed the heuristics instead:
    a stop listed under a station pulls the rest of its intersection with it, and
    every station is a corner to match against.
  * The frozen id map (``station-ids.json``) is now simply the set of existing
    station ids, plus every ``formerIds`` entry. A minted id never reuses one.
  * "Underground stations are never merged into" relied on ``kind: underground``,
    which no longer exists. The substitute is a station in a
    ``curation.stations.subways`` group that is not named for a corner; see
    ``_below_ground``.
  * ``station-overrides.json`` is now plain curation (an assignment with a
    ``note``), and ``exclude`` is ``ignored.json``. Ignored stops are never
    proposed, and are removed from every stop pattern before anything else runs,
    as excluded stops were.
"""

import collections
import math
import re
from collections.abc import Iterable

from app.models.base import Wire
from app.models.curation import Curation, Station
from app.models.ids import Heading, StopId, StationId, upstream_of
from app.models.snapshot import Snapshot, SnapshotStop

MERGE_RADIUS = 75               # m: new stop <-> a station at the same corner
CLUSTER_RADIUS = 250            # m: two stops at one intersection
TERMINAL_SHARE = 0.25           # a stop is a terminal if this share of trips start there
AXIS_RADIUS = 600               # m: how far to look for the run of a stop's own street
AXIS_DOMINANCE = 2.0            # how lopsided that run must be to count as axis-aligned

M_PER_DEG_LAT = 110540.0
M_PER_DEG_LON = 111320.0 * math.cos(math.radians(37.76))    # fixed reference latitude for SF


def metres(a, b):
    return math.hypot((a[0] - b[0]) * M_PER_DEG_LON, (a[1] - b[1]) * M_PER_DEG_LAT)


# MARK: - What gets returned


class NewStation(Wire):
    id: StationId
    name: str


class Proposal(Wire):
    """One suggestion for one unassigned stop. Exactly one of ``station`` and
    ``new_station`` is set. Stops proposed together that form one intersection
    share a ``new_station``."""

    stop: StopId
    heading: Heading | None
    """``None`` only for a stop no pattern visits, which has no direction of travel."""
    station: StationId | None = None
    new_station: NewStation | None = None
    reason: str


# MARK: - Street names

SUFFIX = {'st', 'street', 'ave', 'avenue', 'blvd', 'boulevard', 'rd', 'road', 'dr', 'drive'}
RENAME = {'bay shore': 'bayshore'}
FIXUP = {'Mcallister': 'McAllister'}
ACRONYM = {'sf', 'bart', 'ucsf', 'sfsu', 'ccsf', 'usf', 'va', 'vamc', 'sfo', 'mlk', 'ymca'}
MINOR = {'at', 'of', 'the', 'and', 'to', 'via', 'on'}       # lowercase unless first
ORDINAL = re.compile(r'\d+(st|nd|rd|th)$')
ORD_SUFFIX = re.compile(r'(st|nd|rd|th)$')


# Matching needs a harsher normalisation than display does. SFMTA and data.json spell
# the same street several ways: "Third St" / "3rd St", "The Embarcadero" / "Embarcadero",
# "San Leandro Way" / "San Leandro", plus two typos in data.json we must not edit.
# Without this, requiring the same corner wrongly split 27 stations that are
# genuinely one place. The station names in curation came from data.json, typos
# included, so the aliases still earn their keep.
MATCH_SUFFIX = SUFFIX | {'way', 'ter', 'terrace', 'cir', 'circle', 'ln', 'lane', 'pl',
                         'place', 'ct', 'court', 'hwy', 'highway', 'plz', 'plaza', 'path'}
MATCH_ALIAS = {'third': '3rd', 'fourth': '4th', 'fifth': '5th', 'sixth': '6th',
               'seventh': '7th', 'eighth': '8th', 'ninth': '9th', 'tenth': '10th',
               'vincente': 'vicente',       # data.json spells Vicente St with an n
               '42th': '42nd',              # and 42nd Ave as 42th
               'bay shore': 'bayshore'}


def match_key(name):
    """Street set for comparing two names for the same corner. '/' counts as either:
    a stop at 'Ocean & Dorado' belongs to data.json's 'Ocean & Jules/Dorado'."""
    name = re.sub(r'\([^)]*\)', ' ', name)              # drop "(SF State)" etc.
    out = set()
    for part in re.split(r'[&/]', name):
        toks = [t for t in re.split(r'[\s.]+', part.strip().lower()) if t]
        if toks and toks[0] == 'the':
            toks = toks[1:]
        while len(toks) > 1 and toks[-1] in MATCH_SUFFIX:
            toks.pop()
        if toks:
            joined = ' '.join(toks)
            out.add(MATCH_ALIAS.get(joined, joined))
    return out


def same_corner(a, b):
    """True if two names denote the same intersection.

    Every street in one name must appear in the other. One shared street is not
    enough: the 37's poles at '14th St & Church St' are a signalled crossing away
    from the J and the 22 on Church and the F on Market, so they are their own
    station even though all four names contain "Church"."""
    return _same_keys(match_key(a), match_key(b))


def _same_keys(ka, kb):
    return bool(ka) and bool(kb) and (ka <= kb or kb <= ka)


def streets(name):
    """'16th St& Rhode Island St' -> ['16th', 'rhode island']"""
    # SFMTA's stop-position code. Case-insensitive, unlike the old generator: 511
    # title-cases it ('Mission Bay South & 4th St Se-Fs/ Bz', SFMTA's 'SE-FS/ BZ').
    name = re.sub(r'\s*(SE|NE|SW|NW)-(FS|NS|MB)\s*/?\s*BZ\s*$', '', name, flags=re.I).strip()
    out = []
    for part in name.split('&'):
        toks = [t for t in re.split(r'[\s.]+', part.strip().lower()) if t]
        if len(toks) > 1 and toks[0] == 'the':      # "The Embarcadero" -> Embarcadero
            toks = toks[1:]
        while len(toks) > 1 and toks[-1] in SUFFIX:
            toks.pop()
        if toks:
            joined = ' '.join(toks)
            out.append(RENAME.get(joined, joined))
    return out


def titled(word):
    """Title-case one word, keeping ordinals, acronyms and O'Names intact."""
    for sep in ('-', '/'):
        if sep in word:
            return sep.join(titled(w) for w in word.split(sep))
    if not word or ORDINAL.fullmatch(word):
        return word
    if word.lower() in ACRONYM:
        return word.upper()
    m = re.fullmatch(r"([a-z])'([a-z].*)", word, re.I)      # o'shaughnessy, not joseph's
    if m:
        word = f"{m.group(1).upper()}'{m.group(2)[:1].upper()}{m.group(2)[1:]}"
    else:
        word = word[:1].upper() + word[1:]
    return FIXUP.get(word, word)


def phrase(text):
    words = text.lower().split()
    return ' '.join(w if i and w in MINOR else titled(w) for i, w in enumerate(words))


def pretty(name):
    """A station's display name from a 511 stop name, keeping SFMTA's street order:
    'Geary Blvd & Fillmore St' -> 'Geary & Fillmore'."""
    return ' & '.join(phrase(street) for street in streets(name))


ORD_WORD = {'1st': 'first', '2nd': 'second', '3rd': 'third', '4th': 'fourth', '5th': 'fifth',
            '6th': 'sixth', '7th': 'seventh', '8th': 'eighth', '9th': 'ninth', '10th': 'tenth',
            '11th': 'eleventh', '12th': 'twelfth', '13th': 'thirteenth', '14th': 'fourteenth',
            '15th': 'fifteenth', '16th': 'sixteenth', '17th': 'seventeenth', '18th': 'eighteenth',
            '19th': 'nineteenth', '20th': 'twentieth', '21st': 'twentyFirst',
            '22nd': 'twentySecond', '23rd': 'twentyThird', '24th': 'twentyFourth',
            '25th': 'twentyFifth', '26th': 'twentySixth', '27th': 'twentySeventh',
            '28th': 'twentyEighth', '29th': 'twentyNinth', '30th': 'thirtieth',
            '31st': 'thirtyFirst', '32nd': 'thirtySecond', '33rd': 'thirtyThird',
            '34th': 'thirtyFourth', '35th': 'thirtyFifth', '36th': 'thirtySixth',
            '37th': 'thirtySeventh', '38th': 'thirtyEighth', '39th': 'thirtyNinth',
            '40th': 'fortieth', '41st': 'fortyFirst', '42nd': 'fortySecond',
            '43rd': 'fortyThird', '44th': 'fortyFourth', '45th': 'fortyFifth',
            '46th': 'fortySixth', '47th': 'fortySeventh', '48th': 'fortyEighth'}


def mint_id(name):
    """camelCase id in the style of data.json: churchMarket, church24, fourthKing."""
    pieces = []
    for i, street in enumerate(streets(name)):
        words = street.split()
        if len(words) == 1 and ORDINAL.fullmatch(words[0]):
            # a leading numbered street is spelled out; a trailing one stays a number
            pieces.append(ORD_WORD.get(words[0], words[0]) if i == 0
                          else ORD_SUFFIX.sub('', words[0]))
            continue
        clean = [re.sub(r'[^a-z0-9]', '', w) for w in words]
        pieces.append(clean[0] + ''.join(w.capitalize() for w in clean[1:]))
    pieces = [p for p in pieces if p] or ['stop']
    out = pieces[0] + ''.join(p[0].upper() + p[1:] for p in pieces[1:])
    return out[0].lower() + out[1:]


def heading_of(dx, dy):
    if abs(dy) >= abs(dx):
        return 'northbound' if dy > 0 else 'southbound'
    return 'eastbound' if dx > 0 else 'westbound'


# MARK: - Patterns


def stop_patterns(snapshot: Snapshot, ignored: Iterable[str] = ()) -> collections.Counter:
    """Trips per distinct ``(line, direction, stops)``, with ignored stops removed.

    Ignored stops come out before clustering or headings see them, as the old
    generator's excluded stops did: a stop that will never return a prediction
    should not bend its neighbours' direction of travel either. A pattern left with
    one stop has no direction and is dropped, as it was there.

    Patterns are re-keyed without the headsign. Two patterns that differ only in
    headsign are one sequence of stops, and the terminal rule below compares one
    pattern's trips against a stop's total: split by headsign, a real terminal
    could fall under ``TERMINAL_SHARE``.
    """
    ignored = set(ignored)
    out = collections.Counter()
    for line, patterns in snapshot.patterns.root.items():
        for p in patterns:
            kept = tuple(s for s in p.stops if s not in ignored)
            if len(kept) > 1:
                out[(line, p.direction, kept)] += p.trips
    return out


# MARK: - Headings


def compute_headings(patterns, stops: dict[str, SnapshotStop]):
    """One heading per stop, from travel through it across every line.

    Returns ``(head, snapped)``: the heading of every stop some pattern visits,
    and, for the stops where snapping to the street changed it, what the direction
    of travel alone said. Curated headings are not consulted: they win outright
    where they exist (they are hand-set, and sometimes line-relative rather than
    geometric: the J is labelled southbound at Dolores & 30th while physically
    running east), and this only ever runs for stops that have none.

    Every line counts, rail included. The old generator saw only the lines
    data.json did not cover, but at every pole a train and a bus share, they
    travel the same physical direction (tools/README.md checked all 88).
    """
    def xy(sid):
        return stops[sid].lon, stops[sid].lat

    def unit(a, b):
        dx = (b[0] - a[0]) * M_PER_DEG_LON
        dy = (b[1] - a[1]) * M_PER_DEG_LAT
        n = math.hypot(dx, dy)
        return (dx / n, dy / n) if n else (0.0, 0.0)

    vec = collections.defaultdict(lambda: [0.0, 0.0])
    total = collections.Counter()
    start = {}
    for (line, direction, seq), trips in patterns.items():
        pts = [xy(s) for s in seq]
        last = len(seq) - 1
        for i, s in enumerate(seq):
            # blend a local and a wider window so a stop just before a turn is
            # labelled by the street it actually sits on
            near = unit(pts[max(i - 1, 0)], pts[min(i + 1, last)])
            wide = unit(pts[max(i - 2, 0)], pts[min(i + 2, last)])
            vec[s][0] += trips * (2 * near[0] + wide[0])
            vec[s][1] += trips * (2 * near[1] + wide[1])
            total[s] += trips
        if last > 0 and (seq[0] not in start or start[seq[0]][0] < trips):
            start[seq[0]] = (trips, unit(pts[0], pts[1]))

    # The direction of travel alone mislabels a stop just before a turn: the 22
    # at 16th & Church is still running west on 16th, and only turns onto Church
    # after leaving. So snap each heading to the axis of the street the stop is
    # named for, using nearby stops on that same street to find which way it runs.
    # SFMTA names a stop '<street it is on> & <cross street>'. This corrected 157 of
    # 3,092 stops (5%) in the old generator.
    # Only stops NAMED FOR a street tell you how it runs. 'Market St & 3rd St' is a
    # stop on Market, and counting it as a 3rd St stop drags 3rd's axis east-west,
    # which flipped every 3rd St heading downtown before it was fixed.
    on_street = collections.defaultdict(list)
    for s in vec:
        named = streets(stops[s].name)
        if named:
            on_street[named[0]].append((frozenset(named), xy(s)))

    def street_axis(s):
        """'x' if this stop's own street clearly runs east-west here, 'y' if clearly
        north-south, None if it runs diagonally - SoMa and Mission Bay sit on a grid
        rotated about 45 degrees, where neither answer is more right than the other.
        Consecutive 3rd St stops there differ by dx=245, dy=-240: north versus west
        is a coin flip that rounding decides, so the travel direction stands."""
        named = streets(stops[s].name)
        if not named:
            return None
        here, pt = frozenset(named), xy(s)
        near = sorted(((metres(pt, p), p) for k, p in on_street[named[0]] if k != here),
                      key=lambda t: t[0])[:3]
        near = [p for d, p in near if d <= AXIS_RADIUS]
        if not near:
            return None
        run_x = sum(abs(p[0] - pt[0]) for p in near) * M_PER_DEG_LON
        run_y = sum(abs(p[1] - pt[1]) for p in near) * M_PER_DEG_LAT
        lo, hi = sorted((run_x, run_y))
        if hi < lo * AXIS_DOMINANCE:
            return None
        return 'x' if run_x > run_y else 'y'

    head, snapped = {}, {}
    for s in vec:
        # label a terminal by where the bus leaves for, but only when a real share
        # of the service starts there - a rare short-line origin is not a terminal
        if s in start and start[s][0] >= TERMINAL_SHARE * total[s]:
            dx, dy = start[s][1]
        else:
            dx, dy = vec[s]
        plain = heading_of(dx, dy)
        axis = street_axis(s)
        if axis == 'x' and dx:
            head[s] = 'eastbound' if dx > 0 else 'westbound'
        elif axis == 'y' and dy:
            head[s] = 'northbound' if dy > 0 else 'southbound'
        else:
            head[s] = plain
        if head[s] != plain:
            snapped[s] = plain
    return head, snapped


# MARK: - Identity


def _below_ground(curation: Curation) -> set[str]:
    """Stations a stop is never merged into by corner matching.

    The old rule was ``kind: underground``: data.json models a surface stop next
    to an underground one as a separate station reached by a transfer (``church``
    vs ``churchMarket``), and that convention is preserved. Without it, the
    subset rule in ``same_corner`` would file 'Church St & Market St' under the
    station called just 'Church'.

    ``kind`` no longer exists, so this takes the stations in a subway group that
    are not named for a corner. Subway membership alone is 14 stations on the data
    this was ported from: the 12 underground ones plus ``fourthBrannan`` and
    ``fourthKingT``, where the Central Subway has surfaced and its stops are named
    for their corner like any street stop. The old generator did merge into those
    two, and treating them as underground cost two stops in the differential test:
    the 30/45's pole '4th St & Brannan St' was proposed as a new station
    'fourthBrannan2' beside the existing 'fourthBrannan', and '4th St & King St/
    Caltrain' went to the N's King St station instead of the T's on 4th. An
    underground station is named for the place (Church, Powell, Yerba Buena -
    Moscone), which is also exactly the name the subset rule over-matches.

    This is a name heuristic standing in for a fact the curation does not record.
    When platforms get levels (the planned ``layout``), use those instead.
    """
    return {sid for subway in curation.stations.subways.values() for sid in subway.stations
            if sid in curation.stations.stations
            and len(match_key(curation.stations.stations[sid].name)) < 2}


def _id_order(pid):
    """GTFS stop ids are numeric strings for SF, and the old generator ordered by
    their value. Anything non-numeric sorts after, by text."""
    up = upstream_of(pid)
    return (0, int(up), pid) if up.isdigit() else (1, 0, pid)


def _first_street(name):
    # min(), not next(iter()): for a 'Jules/Dorado' street the old generator's pick
    # depended on the process's string hash seed.
    streets_ = streets(name)
    key = match_key(streets_[0]) if streets_ else set()
    return min(key) if key else ''


def _clusters(universe, stops):
    """Group stops into intersections: same cross streets, then within CLUSTER_RADIUS.

    Greedy and single-link, in stop id order, exactly as before: a stop joins the
    first cluster it is near, and two clusters are never merged afterwards."""
    groups = collections.defaultdict(list)
    for s in universe:
        groups[frozenset(match_key(stops[s].name))].append(s)
    clusters = []
    for key, members in groups.items():
        members.sort(key=_id_order)
        built = []
        for s in members:
            for c in built:
                if min(metres(_pt(stops, s), _pt(stops, o)) for o in c) <= CLUSTER_RADIUS:
                    c.append(s)
                    break
            else:
                built.append([s])
        clusters.extend(built)
    return clusters


def _pt(stops, s):
    return stops[s].lon, stops[s].lat


def _primary_of(members, stops):
    """The stop whose name spelling most stops here agree on. That is how a
    station keeps SFMTA's street order: 'Geary & Fillmore', not 'Fillmore & Geary'."""
    votes = collections.Counter(stops[s].name for s in members)
    return min(members, key=lambda s: (-votes[stops[s].name], _id_order(s)))


class _Placement:
    __slots__ = ('station', 'new', 'reason')

    def __init__(self, station=None, new=None, reason=''):
        self.station, self.new, self.reason = station, new, reason


def _place(stops: dict[str, SnapshotStop], curation: Curation, universe: set[str]) -> dict[str, _Placement]:
    """Place every unassigned stop in ``universe`` in a station, existing or new.

    ``universe`` is every stop that takes part in clustering: the served ones and
    any others being proposed. Assigned stops in it only pull their neighbours;
    they are never moved.
    """
    stations: dict[str, Station] = curation.stations.stations
    station_of = {s: sid for sid, st in stations.items() for p in st.platforms for s in p.all_stops}
    below = _below_ground(curation)

    # A station's stops are where 511 says they are: coordinates are never curated.
    # A stop 511 no longer lists has nowhere to be.
    platform_pts = {sid: [_pt(stops, x) for p in st.platforms for x in p.all_stops if x in stops]
                    for sid, st in stations.items()}
    corner_keys = {sid: match_key(st.name) for sid, st in stations.items()
                   if sid not in below and platform_pts[sid]}

    def nearest(s, sid):
        return min(metres(_pt(stops, s), p) for p in platform_pts[sid]) if platform_pts[sid] \
            else math.inf

    def candidates(members):
        """Stations this group of stops could belong to, and why."""
        out = {}
        for s in members:
            # the curation is authoritative: a stop it lists belongs to that station,
            # full stop, and brings the rest of its intersection with it
            if s in station_of:
                out.setdefault(station_of[s], ('listed', s))
        for s in members:
            key = match_key(stops[s].name)
            for sid, skey in corner_keys.items():
                if sid not in out and _same_keys(skey, key) and nearest(s, sid) <= MERGE_RADIUS:
                    out[sid] = ('corner', s)
        return out

    placed: dict[str, _Placement] = {}
    to_mint = []
    for members in _clusters(universe, stops):
        loose = [s for s in members if s not in station_of]
        if not loose:
            continue
        cands = candidates(members)
        if not cands:
            to_mint.append(members)
            continue
        if len(cands) == 1:
            ((sid, (how, via)),) = cands.items()
            for s in loose:
                placed[s] = _Placement(station=sid, reason=_join_reason(sid, how, via, s, stops, stations,
                                                                        station_of, members))
            continue
        # The curation is authoritative about how finely an intersection is split,
        # not just about names: data.json modelled Church & Duboce as two stations,
        # churchDuboceJ on Church and duboceChurchN on Duboce. Where a cluster
        # straddles two of them, split it the same way - by which street the stop is
        # named for first, then by distance. Without this the 22's Church St stops
        # were filed under the N's Duboce St station.
        firsts = {sid: _first_street(stations[sid].name) for sid in cands}
        for s in loose:
            ours = _first_street(stops[s].name)
            best = min(cands, key=lambda sid: (0 if firsts[sid] == ours else 1, nearest(s, sid)))
            by = 'named for the same street first' if firsts[best] == ours else 'nearest'
            placed[s] = _Placement(
                station=best,
                reason=f"intersection spans {', '.join(sorted(cands))}; {best} is {by} "
                       f"({nearest(s, best):.0f} m)")

    # Ids are permanent once committed and live in people's favourites, so a new
    # one never reuses an id that already means somewhere else, live or former.
    taken = set(stations) | {f for st in stations.values() for f in st.former_ids}
    for members in sorted(to_mint, key=lambda c: _id_order(c[0])):
        primary = _primary_of(members, stops)
        base = mint_id(stops[primary].name)
        sid, n = base, 2
        while sid in taken:
            sid, n = f'{base}{n}', n + 1
        taken.add(sid)
        new = NewStation(id=sid, name=pretty(stops[primary].name))
        others = len(members) - 1
        reason = 'no station at this corner' + (f'; new with {others} other stop{"s" * (others != 1)}'
                                                if others else '; new')
        for s in members:
            placed[s] = _Placement(new=new, reason=reason)
    return placed


def _join_reason(sid, how, via, s, stops, stations, station_of, members):
    if how == 'listed':
        # name the nearest sibling the station already has, which is the evidence
        sib = min((m for m in members if station_of.get(m) == sid),
                  key=lambda m: metres(_pt(stops, s), _pt(stops, m)))
        return (f"same intersection as {sib} in {sid} "
                f"({metres(_pt(stops, s), _pt(stops, sib)):.0f} m)")
    return f"same corner as '{stations[sid].name}'" + (
        '' if via == s else f' (via {via})')


# MARK: - Entry point


def propose(snapshot: Snapshot, curation: Curation, stop_ids: Iterable[str]) -> list[Proposal]:
    """A proposal for each given stop that is in the snapshot, in no station and not
    ignored, in the order given. Assigned and ignored stops are skipped: an existing
    assignment is never second-guessed here. A stop 511 does not list is an error.

    Stops are proposed against every other unassigned stop, not just the ones
    asked about, so a new id is the same whichever subset of the review queue is
    asked for: two new stops at one corner get one new station, and two new
    intersections that would mint the same id are numbered in stop id order.
    """
    stops = snapshot.stops.root
    ignored = set(curation.ignored.root)
    assigned = {s for st in curation.stations.stations.values() for p in st.platforms for s in p.all_stops}

    wanted = []
    for pid in dict.fromkeys(stop_ids):
        if pid not in stops:
            raise ValueError(f'{pid} is not in the snapshot')
        if pid not in ignored and pid not in assigned:
            wanted.append(pid)
    if not wanted:
        return []

    patterns = stop_patterns(snapshot, ignored)
    head, snapped = compute_headings(patterns, stops)
    served = {s for _, _, seq in patterns for s in seq}
    placed = _place(stops, curation, served | set(wanted))

    out = []
    for pid in wanted:
        p = placed[pid]
        reason = p.reason
        if pid not in head:
            reason += '; no line stops here, so no heading'
        elif pid in snapped:
            reason += f'; heading snapped to its street (travel alone says {snapped[pid]})'
        out.append(Proposal(stop=pid, heading=head.get(pid), station=p.station,
                            new_station=p.new, reason=reason))
    return out
