#!/usr/bin/env python3
"""Generate Muni+ bus and cable car station data from the official SFMTA GTFS feed.

    python3 tools/build_bus_data.py [--gtfs PATH_OR_URL]

Writes appdata/bus/<route>.json (one file per route, for readable diffs) and
appdata/bus.json (every route merged, for the app to load), using the same
schema as appdata/data.json. Covers every surface route the metro file does not:
all 58 bus routes plus the 3 cable car lines.

Station identity is one namespace shared with the metro file:

  * appdata/data.json is authoritative. A bus stop that shares a stop code with
    a metro platform, or that sits at a street-level metro station, is filed
    under that station's existing id, so the loader merges them.
  * Underground stations are never merged into - data.json models a surface
    stop next to an underground one as a separate station reached by
    transferStations (church / churchMarket), so we emit that link instead.
  * Every id ever minted is frozen in tools/station-ids.json and reused on
    later builds, because a shipped id lives in people's favourites.

Route files carry only the platforms and lines their own route serves; the loader
unions platforms by stop code and unions lines, so a station served by the J and
the 22 ends up with one record and both lines.
"""

import argparse, collections, csv, io, json, math, os, re, sys, urllib.request, zipfile

GTFS_URL = 'https://muni-gtfs.apps.sfmta.com/data/muni_gtfs-current.zip'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METRO = os.path.join(ROOT, 'appdata', 'data.json')
ID_MAP = os.path.join(ROOT, 'tools', 'station-ids.json')
OVERRIDES = os.path.join(ROOT, 'tools', 'station-overrides.json')
OUT_DIR = os.path.join(ROOT, 'appdata', 'bus')
OUT_ALL = os.path.join(ROOT, 'appdata', 'bus.json')

# Which routes we cover: every one data.json does not already have a line for. That
# is all 58 buses, the 3 cable cars, and the F Market & Wharves - which is route_type
# 0 like the metro, but has never been in data.json. Self-maintaining: add the F to
# data.json one day and this drops it automatically.
MERGE_RADIUS = 75               # m: bus stop <-> street-level metro station
TRANSFER_RADIUS = 200           # m: bus station -> nearby metro station link
CLUSTER_RADIUS = 250            # m: two stops at one intersection
TERMINAL_SHARE = 0.25           # a stop is a terminal if this share of trips start there
AXIS_RADIUS = 600               # m: how far to look for the run of a stop's own street
AXIS_DOMINANCE = 2.0            # how lopsided that run must be to count as axis-aligned

M_PER_DEG_LAT = 110540.0
M_PER_DEG_LON = 111320.0 * math.cos(math.radians(37.76))    # fixed reference latitude for SF


def metres(a, b):
    return math.hypot((a[0] - b[0]) * M_PER_DEG_LON, (a[1] - b[1]) * M_PER_DEG_LAT)


# --------------------------------------------------------------- street names
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
    """True if two names denote the same intersection."""
    ka, kb = match_key(a), match_key(b)
    return bool(ka) and bool(kb) and (ka <= kb or kb <= ka)


def streets(name):
    """'16th St& Rhode Island St' -> ['16th', 'rhode island']"""
    name = re.sub(r'\s*(SE|NE|SW|NW)-(FS|NS|MB)\s*/?\s*BZ\s*$', '', name).strip()
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
    return ' & '.join(phrase(street) for street in streets(name))


def title_case(s):
    return phrase(s)


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


# ---------------------------------------------------------------------- input
def read_gtfs(src, covered=frozenset()):
    if src.startswith(('http://', 'https://')):
        sys.stderr.write(f'fetching {src}\n')
        with urllib.request.urlopen(src) as r:
            blob = r.read()
    else:
        blob = open(src, 'rb').read()
    zf = zipfile.ZipFile(io.BytesIO(blob))
    def table(name):
        with zf.open(name) as f:
            return list(csv.DictReader(io.TextIOWrapper(f, 'utf-8-sig')))
    routes = {r['route_id']: r for r in table('routes.txt')
              if r['route_id'] not in covered}
    stops = {s['stop_id']: s for s in table('stops.txt')}
    trips = {t['trip_id']: t for t in table('trips.txt') if t['route_id'] in routes}
    order = collections.defaultdict(list)
    with zf.open('stop_times.txt') as f:
        for row in csv.DictReader(io.TextIOWrapper(f, 'utf-8-sig')):
            if row['trip_id'] in trips:
                order[row['trip_id']].append((int(row['stop_sequence']), row['stop_id']))
    patterns = collections.Counter()
    for tid, seq in order.items():
        seq.sort()
        t = trips[tid]
        patterns[(t['route_id'], t['direction_id'], tuple(s for _, s in seq))] += 1
    return routes, stops, patterns


def drop_excluded(patterns, stops, excluded):
    """Remove stops that will never return a prediction, before clustering sees them."""
    out = collections.Counter()
    for (route, direction, seq), trips in patterns.items():
        kept = tuple(s for s in seq if stops[s]['stop_code'] not in excluded)
        if len(kept) > 1:
            out[(route, direction, kept)] += trips
    return out


# ------------------------------------------------------------------ headings
def compute_headings(patterns, stops, metro, head_overrides):
    """One heading per stop code, from travel through it across every route.

    Where data.json already describes a stop, its heading wins: those are
    hand-set and sometimes line-relative rather than geometric (the J is
    labelled southbound at Dolores & 30th while physically running east).
    """
    def xy(sid):
        return float(stops[sid]['stop_lon']), float(stops[sid]['stop_lat'])

    def unit(a, b):
        dx = (b[0] - a[0]) * M_PER_DEG_LON
        dy = (b[1] - a[1]) * M_PER_DEG_LAT
        n = math.hypot(dx, dy)
        return (dx / n, dy / n) if n else (0.0, 0.0)

    vec = collections.defaultdict(lambda: [0.0, 0.0])
    total = collections.Counter()
    start = {}
    for (route, direction, seq), trips in patterns.items():
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
    # Only stops NAMED FOR a street tell you how it runs. 'Market St & 3rd St' is a
    # stop on Market, and counting it as a 3rd St stop drags 3rd's axis east-west.
    on_street = collections.defaultdict(list)
    for s in vec:
        named = streets(stops[s]['stop_name'])
        if named:
            on_street[named[0]].append((frozenset(named), xy(s)))

    def street_axis(s):
        """'x' if this stop's own street clearly runs east-west here, 'y' if clearly
        north-south, None if it runs diagonally - SoMa and Mission Bay sit on a grid
        rotated about 45 degrees, where neither answer is more right than the other."""
        named = streets(stops[s]['stop_name'])
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

    curated = {p['id']: p['heading'] for st in metro['stations'] for p in st['platforms']}
    for s in head:
        if stops[s]['stop_code'] in curated:
            head[s] = curated[stops[s]['stop_code']]
    for s in head:                               # hand-verified corrections win outright
        if stops[s]['stop_code'] in head_overrides:
            head[s] = head_overrides[stops[s]['stop_code']]
    return head, snapped


# ------------------------------------------------------------------ identity
def resolve_stations(patterns, stops, metro, frozen, overrides):
    """Assign every bus stop to a station in the shared namespace."""
    served = {s for _, _, seq in patterns for s in seq}
    pt_of = {s: (float(stops[s]['stop_lon']), float(stops[s]['stop_lat'])) for s in served}

    metro_by_code = {p['id']: st for st in metro['stations'] for p in st['platforms']}
    metro_ids = {st['id'] for st in metro['stations']}
    metro_pts = [(st, (float(st['longitude']), float(st['latitude']))) for st in metro['stations']]

    def platforms_of(st):
        return [(float(p['longitude']), float(p['latitude'])) for p in st['platforms']]

    def candidates(members):
        """Metro stations this group of stops could belong to."""
        out = {}
        for s in members:
            hit = metro_by_code.get(stops[s]['stop_code'])
            if hit:
                out[hit['id']] = hit
            for st in metro['stations']:
                if st['kind'] == 'streetLevel' \
                        and same_corner(st['name'], stops[s]['stop_name']) \
                        and min(metres(pt_of[s], mp) for mp in platforms_of(st)) <= MERGE_RADIUS:
                    out[st['id']] = st
        return out

    def primary_of(members):
        """The stop whose name spelling most stops here agree on."""
        votes = collections.Counter(stops[s]['stop_name'] for s in members)
        return min(members, key=lambda s: (-votes[stops[s]['stop_name']], stops[s]['stop_code']))

    # group stops into intersections: same cross streets, then within CLUSTER_RADIUS
    groups = collections.defaultdict(list)
    for s in served:
        groups[frozenset(match_key(stops[s]['stop_name']))].append(s)
    clusters = []
    for key, members in groups.items():
        members.sort(key=lambda s: int(stops[s]['stop_id']))
        built = []
        for s in members:
            for c in built:
                if min(metres(pt_of[s], pt_of[o]) for o in c) <= CLUSTER_RADIUS:
                    c.append(s)
                    break
            else:
                built.append([s])
        clusters.extend(built)

    # data.json is authoritative about how finely an intersection is split, not just
    # about names: it models Church & Duboce as two stations, one on each street. Where
    # a cluster straddles two of them, split it the same way - by which street the stop
    # is named for first, then by distance.
    split = []
    for members in clusters:
        pinned = collections.defaultdict(list)
        for s in list(members):
            sid = overrides.get(stops[s]['stop_code'])
            if sid:
                pinned[sid].append(s)
                members = [m for m in members if m != s]
        split.extend((ms, sid) for sid, ms in pinned.items())
        if not members:
            continue
        cands = candidates(members)
        if len(cands) < 2:
            split.append((members, next(iter(cands), None)))
            continue
        buckets = collections.defaultdict(list)
        for s in members:
            pin = metro_by_code.get(stops[s]['stop_code'])
            if pin and pin['id'] in cands:
                buckets[pin['id']].append(s)
                continue
            ours = [next(iter(match_key(streets(stops[s]['stop_name'])[0])), '')]
            best = min(cands.values(), key=lambda st: (
                0 if [next(iter(match_key(streets(st['name'])[0])), '')] == ours else 1,
                min(metres(pt_of[s], mp) for mp in platforms_of(st))))
            buckets[best['id']].append(s)
        split.extend((ms, sid) for sid, ms in buckets.items())

    station_of, records, taken = {}, {}, set(metro_ids)
    notes = collections.Counter()
    for members, forced in sorted(split, key=lambda c: int(stops[c[0][0]]['stop_id'])):
        primary = primary_of(members)
        # a hand-verified override beats everything, the frozen map included
        sid = next((overrides[stops[s]['stop_code']] for s in members
                    if stops[s]['stop_code'] in overrides), None)
        if sid:
            notes['hand-verified override'] += 1
        # then an id frozen by an earlier build - it is in people's favourites
        elif (was := next((frozen[stops[s]['stop_code']] for s in members
                           if stops[s]['stop_code'] in frozen), None)):
            sid = was
            notes['frozen'] += 1
        elif forced:
            sid = forced
            notes['merged into a metro station'] += 1
        else:
            base = mint_id(stops[primary]['stop_name'])          # never reuse an id
            sid, n = base, 2                                     # that means somewhere else
            while sid in taken:
                sid, n = f'{base}{n}', n + 1
            notes['new'] += 1

        taken.add(sid)
        rec = records.get(sid)
        if rec is None:
            records[sid] = {'members': list(members),
                            'metro': next((st for st in metro['stations']
                                           if st['id'] == sid), None),
                            'name': pretty(stops[primary]['stop_name'])}
        else:
            rec['members'] += members
        for s in members:
            station_of[s] = sid

    # nearby metro stations we deliberately did not merge into become transfers
    for sid, rec in records.items():
        n = len(rec['members'])
        centre = (sum(pt_of[m][0] for m in rec['members']) / n,
                  sum(pt_of[m][1] for m in rec['members']) / n)
        rec['centre'] = centre
        rec['transfers'] = [] if rec['metro'] else sorted(
            st['id'] for st, p in metro_pts
            if metres(centre, p) <= TRANSFER_RADIUS and st['id'] != sid)
    return station_of, records, notes


# -------------------------------------------------------------------- output
HEAD_ORDER = {'northbound': 0, 'southbound': 1, 'eastbound': 2, 'westbound': 3}


def route_order(route, patterns, station_of):
    """Station ids in outbound order, with short-line-only stops spliced in."""
    mine = sorted(((d, seq, n) for (r, d, seq), n in patterns.items() if r == route),
                  key=lambda t: (-len(t[1]), -t[2]))
    order = []
    for direction, seq, _ in mine:
        chain = [station_of[s] for s in seq]
        if direction == '1':
            chain = chain[::-1]
        at, pending = None, []
        for sid in chain:
            if sid in order:
                at = order.index(sid)
                for k, p in enumerate(pending):
                    order.insert(at + k, p)
                at += len(pending)
                pending = []
            elif at is None:
                pending.append(sid)
            else:
                at += 1
                order.insert(at, sid)
        order.extend(pending)
    return order


def station_record(sid, rec, stops, head, codes, lines):
    """One station as seen by a set of routes: their platforms, their lines."""
    members = sorted((s for s in rec['members'] if stops[s]['stop_code'] in codes),
                     key=lambda s: (HEAD_ORDER[head[s]], s))
    metro = rec['metro']
    if metro:
        name, kind = metro['name'], metro['kind']
        lat, lon = metro['latitude'], metro['longitude']
        transfers, agencies = [], []
    else:
        name, kind = rec['name'], 'streetLevel'
        lat = round(sum(float(stops[s]['stop_lat']) for s in members) / len(members), 6)
        lon = round(sum(float(stops[s]['stop_lon']) for s in members) / len(members), 6)
        transfers, agencies = rec.get('transfers', []), []
    return {
        'id': sid,
        'name': name,
        'kind': kind,
        'lines': lines,
        'latitude': lat,
        'longitude': lon,
        'transferStations': transfers,
        'transferAgencies': agencies,
        'platforms': [{
            'id': stops[s]['stop_code'],
            'heading': head[s],
            'name': None,
            'latitude': float(stops[s]['stop_lat']),
            'longitude': float(stops[s]['stop_lon']),
        } for s in members],
    }


def line_record(route, routes, station_ids):
    r = routes[route]
    return {
        'id': route,
        'name': f"{r['route_short_name']} {title_case(r['route_long_name'])}",
        'shortName': r['route_short_name'],
        'color': '#' + r['route_color'].strip().upper(),
        'stationIds': station_ids,
    }


def write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(doc, f, indent=2)
        f.write('\n')


# ------------------------------------------------------------------ validate
def validate(docs, metro, stops, head):
    problems = []
    code_to_station, code_to_head, id_to_place = {}, {}, {}
    for st in metro['stations']:
        id_to_place[st['id']] = (float(st['longitude']), float(st['latitude']))
        for p in st['platforms']:
            code_to_station[p['id']] = st['id']
            code_to_head[p['id']] = p['heading']

    for doc in docs:
        known = {s['id'] for s in doc['stations']}
        for line in doc['lines']:
            for sid in line['stationIds']:
                if sid not in known:
                    problems.append(f"{line['id']}: stationId {sid} has no station record")
        for st in doc['stations']:
            place = (st['longitude'], st['latitude'])
            if st['id'] in id_to_place and metres(place, id_to_place[st['id']]) > CLUSTER_RADIUS:
                problems.append(f"station id {st['id']} used for two places "
                                f"{round(metres(place, id_to_place[st['id']]))}m apart")
            id_to_place.setdefault(st['id'], place)
            for p in st['platforms']:
                if code_to_station.setdefault(p['id'], st['id']) != st['id']:
                    problems.append(f"stop {p['id']} filed under both "
                                    f"{code_to_station[p['id']]} and {st['id']}")
                if code_to_head.setdefault(p['id'], p['heading']) != p['heading']:
                    problems.append(f"stop {p['id']} has two headings: "
                                    f"{code_to_head[p['id']]} and {p['heading']}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gtfs', default=GTFS_URL, help='GTFS zip path or URL')
    args = ap.parse_args()

    metro = json.load(open(METRO))
    frozen = json.load(open(ID_MAP)) if os.path.exists(ID_MAP) else {}
    ov = json.load(open(OVERRIDES)) if os.path.exists(OVERRIDES) else {}
    real = lambda m: {k: v for k, v in m.items() if not k.startswith('//')}   # skip comments
    overrides = {k: v['station'] for k, v in real(ov.get('stations', {})).items()}
    head_overrides = {k: v['heading'] for k, v in real(ov.get('headings', {})).items()}
    excluded = real(ov.get('exclude', {}))
    routes, stops, patterns = read_gtfs(args.gtfs, {l['id'] for l in metro['lines']})
    served_before = {stops[s]['stop_code'] for _, _, seq in patterns for s in seq}
    patterns = drop_excluded(patterns, stops, excluded)
    head, snapped = compute_headings(patterns, stops, metro, head_overrides)
    station_of, records, notes = resolve_stations(patterns, stops, metro, frozen, overrides)

    route_stations = {r: route_order(r, patterns, station_of) for r in routes}
    station_lines = collections.defaultdict(list)
    for r in sorted(routes, key=lambda x: (len(x), x)):
        for sid in route_stations[r]:
            station_lines[sid].append(r)

    docs = []
    for route in sorted(routes, key=lambda x: (len(x), x)):
        order = route_stations[route]
        codes = {stops[s]['stop_code'] for _, _, seq in
                 [(a, b, c) for (a, b, c) in patterns if a == route] for s in seq}
        doc = {
            'lines': [line_record(route, routes, order)],
            'subways': [],
            'stations': [station_record(sid, records[sid], stops, head, codes, [route])
                         for sid in order],
        }
        write(os.path.join(OUT_DIR, f'{route}.json'), doc)
        docs.append(doc)

    # merged document: every route, stations unioned
    merged_stations = {}
    for doc in docs:
        for st in doc['stations']:
            cur = merged_stations.get(st['id'])
            if cur is None:
                merged_stations[st['id']] = {**st, 'platforms': list(st['platforms'])}
                continue
            cur['lines'] = sorted(set(cur['lines']) | set(st['lines']), key=lambda x: (len(x), x))
            have = {p['id'] for p in cur['platforms']}
            cur['platforms'] += [p for p in st['platforms'] if p['id'] not in have]
    for st in merged_stations.values():
        st['platforms'].sort(key=lambda p: (HEAD_ORDER[p['heading']], p['id']))
    merged = {
        'lines': [line_record(r, routes, route_stations[r])
                  for r in sorted(routes, key=lambda x: (len(x), x))],
        'subways': [],
        'stations': list(merged_stations.values()),
    }
    write(OUT_ALL, merged)

    problems = validate(docs + [merged], metro, stops, head)
    known = {stops[s]['stop_code'] for s in station_of}
    for code in list(overrides) + list(head_overrides):
        if code not in known:
            problems.append(f'override names stop {code}, which no route serves')
    for code in excluded:
        if code not in served_before:
            problems.append(f'exclude names stop {code}, which no route serves anyway')
        elif code in known:
            problems.append(f'excluded stop {code} is still in the output')
    for code, sid in overrides.items():
        if code in station_of and station_of.get(
                next(s for s in station_of if stops[s]['stop_code'] == code)) != sid:
            problems.append(f'override for stop {code} did not take effect')
    print(f'{len(routes)} routes, {len(merged_stations)} stations, '
          f'{sum(len(s["platforms"]) for s in merged_stations.values())} stops'
          + (f', {len(excluded)} stops excluded' if excluded else ''))
    print('station identity: ' + ', '.join(f'{v} {k}' for k, v in sorted(notes.items())))
    if problems:
        print(f'\n{len(problems)} PROBLEM(S):')
        for p in problems[:40]:
            print('  ' + p)
        print('\ntools/station-ids.json NOT updated - a frozen id is permanent, so a '
              'build that fails validation must not add to it.')
        return 1

    # only now, once everything checks out, freeze the ids
    for sid, rec in records.items():
        for s in rec['members']:
            frozen[stops[s]['stop_code']] = sid
    write(ID_MAP, dict(sorted(frozen.items())))
    print('validation: ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
