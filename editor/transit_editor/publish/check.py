"""Checks on the documents about to be published.

The editor's validator (``transit_editor.data.validate``) checks the curation.
These check what the app will actually read: that every reference in the
published files resolves, and that no id is claimed twice. A failure here is a
bug in the publisher or data the validator does not catch, and publishing stops.
"""

from collections import Counter


def check(network: dict, shapes: dict) -> list[str]:
    """Every problem with the pair of documents, in words. Empty means publishable."""
    problems: list[str] = []

    def dupes(kind: str, ids) -> None:
        for value, n in sorted(Counter(ids).items()):
            if n > 1:
                problems.append(f"{kind} id {value!r} appears {n} times")

    lines = {line["id"]: line for line in network["lines"]}
    stations = {s["id"]: s for s in network["stations"]}
    platforms = {p["id"]: s["id"] for s in network["stations"] for p in s["platforms"]}

    dupes("line", [line["id"] for line in network["lines"]])
    dupes("station", [s["id"] for s in network["stations"]])
    dupes("platform", [p["id"] for s in network["stations"] for p in s["platforms"]])
    dupes("subway", [s["id"] for s in network["subways"]])

    # Former ids: never a current id, and never claimed by two stations or platforms,
    # or a favourite could move to the wrong place.
    former_stations = [f for s in network["stations"] for f in s["formerIds"]]
    dupes("former station", former_stations)
    for f in former_stations:
        if f in stations:
            problems.append(f"former station id {f!r} is a current station id")
    former_platforms = [f for s in network["stations"] for p in s["platforms"] for f in p["formerIds"]]
    dupes("former platform", former_platforms)
    for f in former_platforms:
        if f in platforms:
            problems.append(f"former platform id {f!r} is a current platform id")

    for line in network["lines"]:
        lid = line["id"]
        if line["style"] not in network["styles"]:
            problems.append(f"line {lid} uses style {line['style']!r}, which is not in styles")
        if line["replaces"] is not None and line["replaces"] not in lines:
            problems.append(f"line {lid} replaces {line['replaces']!r}, which is not a published line")
        dupes(f"line {lid} direction", [d["id"] for d in line["directions"]])
        for d in line["directions"]:
            if not d["platforms"]:
                problems.append(f"line {lid} direction {d['id']} has no platforms")
            for pid in d["platforms"]:
                if pid not in platforms:
                    problems.append(f"line {lid} direction {d['id']} lists platform {pid!r}, which no station has")

    for s in network["stations"]:
        sid = s["id"]
        if not s["platforms"]:
            problems.append(f"station {sid} has no platforms")
        for t in s["transfers"]:
            if t["station"] not in stations:
                problems.append(f"station {sid} transfers to {t['station']!r}, which is not a published station")
        for p in s["platforms"]:
            for lid in p["lines"]:
                if lid not in lines:
                    problems.append(f"platform {p['id']} at {sid} lists line {lid!r}, which is not published")
                elif not lines[lid]["listedAtStations"]:
                    problems.append(f"platform {p['id']} at {sid} lists line {lid}, which is not listed at stations")

    for subway in network["subways"]:
        for sid in subway["stations"]:
            if sid not in stations:
                problems.append(f"subway {subway['id']} lists station {sid!r}, which is not published")

    for lid, directions in shapes["lines"].items():
        if lid not in lines:
            problems.append(f"shapes has line {lid!r}, which is not published")
            continue
        known = {d["id"] for d in lines[lid]["directions"]}
        for did, path in directions.items():
            if did not in known:
                problems.append(f"shapes has line {lid} direction {did!r}, which the line does not have")
            if len(path) < 2:
                problems.append(f"shape for line {lid} direction {did} has fewer than two points")

    return problems
