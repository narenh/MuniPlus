"""The one validator, run by the server for the loader and for every editor save.

Errors block a save; warnings are shown and never block. Every ``Issue.code`` is a
stable identifier the editor groups by and the tests assert on, and the full set
is the list in PLAN.md ("Validation"). What the models already enforce (id
shapes, headings, non-blank names, unknown keys) is not checked again here.

Issues come out in a fixed order (stations and lines by id, platforms in file
order) so the same curation always gives the same list.
"""

from collections.abc import Mapping

from ..models.curation import Curation
from ..models.editor import Issue, Validation
from ..models.snapshot import Snapshot
from .network import _most_run, terminal_stops
from .shapes import ANCHOR_M, draw_shapes
from ..ingest.propose import metres

PLATFORM_SPREAD_M = 25.0
"""How far a platform's extra stop may be from its primary before the validator
asks whether they are really one place."""


def validate(curation: Curation, snapshots: Mapping[str, Snapshot]) -> Validation:
    errors: list[Issue] = []
    warnings: list[Issue] = []

    def error(code: str, message: str, **where) -> None:
        errors.append(Issue(level="error", code=code, message=message, **where))

    def warn(code: str, message: str, **where) -> None:
        warnings.append(Issue(level="warning", code=code, message=message, **where))

    stations = dict(sorted(curation.stations.stations.items()))
    subways = dict(sorted(curation.stations.subways.items()))
    ignored = curation.ignored.root
    stops = {pid: stop for snap in snapshots.values() for pid, stop in snap.stops.root.items()}
    known_lines = {lid for snap in snapshots.values() for lid in snap.lines.root}

    # MARK: Station ids

    # Stations are a JSON object, so the same id twice cannot survive parsing (the
    # last one silently wins, in Python's json and in Pydantic alike). What can
    # survive is two ids that differ only in case, ``powell`` and ``Powell``: two
    # stations to this server, one to anything that folds case, and never what a
    # person meant.
    by_folded: dict[str, list[str]] = {}
    for sid in stations:
        by_folded.setdefault(sid.casefold(), []).append(sid)
    for same in by_folded.values():
        for sid in same[1:]:
            error(
                "duplicate-station-id",
                f"Station ids {same[0]!r} and {sid!r} differ only in case.",
                station=sid,
            )

    # MARK: Former ids

    claimed_by: dict[str, str] = {}
    for sid, station in stations.items():
        for former in station.former_ids:
            if former in stations:
                error(
                    "former-id-collides",
                    f"{sid!r} lists former id {former!r}, which is a current station id.",
                    station=sid,
                )
            elif former in claimed_by:
                other = claimed_by[former]
                where = "twice" if other == sid else f"and so does {other!r}"
                error(
                    "former-id-collides",
                    f"{sid!r} lists former id {former!r} {where}; it can redirect to only one station.",
                    station=sid,
                )
            else:
                claimed_by[former] = sid

    # MARK: Platforms and their stops

    owner: dict[str, str] = {}
    for sid, station in stations.items():
        if not station.platforms:
            error("station-has-no-platforms", f"{station.name} ({sid}) has no platforms.", station=sid)
            continue
        seen: set[str] = set()
        for platform in station.platforms:
            for pid in platform.all_stops:
                if pid in seen:
                    error(
                        "stop-listed-twice",
                        f"{pid} is listed twice in {station.name} ({sid}).",
                        station=sid,
                        stop=pid,
                    )
                    continue
                seen.add(pid)
                if pid in owner:
                    first = owner[pid]
                    error(
                        "stop-in-two-stations",
                        f"{pid} is in both {stations[first].name} ({first}) and {station.name} ({sid}).",
                        station=sid,
                        stop=pid,
                    )
                else:
                    owner[pid] = sid
                if pid in ignored:
                    error(
                        "stop-assigned-and-ignored",
                        f"{pid} is in {station.name} ({sid}) and also in ignored.json.",
                        station=sid,
                        stop=pid,
                    )
                if pid not in stops:
                    warn(
                        "stop-not-in-snapshot",
                        f"{pid} in {station.name} ({sid}) is not in 511's current data, so the API leaves it out.",
                        station=sid,
                        stop=pid,
                    )
            # The stops of one platform are one place a rider stands. Two ids for one
            # shelter sit metres apart (5.4 and 5.8 m at Duboce & Church); much further
            # and it is more likely two places. A warning: 511 moving a pole must never
            # block a save.
            primary = stops.get(platform.id)
            for pid in platform.stops:
                other = stops.get(pid)
                if primary and other and (far := metres((primary.lon, primary.lat), (other.lon, other.lat))) > PLATFORM_SPREAD_M:
                    warn(
                        "platform-stops-far-apart",
                        f"{pid} is {far:.0f} m from {platform.id}, the platform it is listed in at "
                        f"{station.name} ({sid}): more than one place to stand?",
                        station=sid,
                        stop=pid,
                    )
        if not any(pid in stops for pid in seen):
            warn(
                "station-has-no-live-platforms",
                f"None of {station.name} ({sid})'s platforms are in 511's current data, "
                "so it has no coordinate and the API leaves it out.",
                station=sid,
            )

    # MARK: Platforms' former ids

    # A former id is an alias homes and favourites resolve through. One that is also
    # a current stop, or the former id of two platforms, would send them to the
    # wrong place, so both are errors.
    former_of: dict[str, str] = {}
    for sid, station in stations.items():
        for platform in station.platforms:
            for former in platform.former_ids:
                where = f"platform {former} was, now {platform.id} in {station.name} ({sid})"
                if former in owner:
                    error(
                        "platform-former-id-collides",
                        f"{former} is a former id of platform {platform.id} in {station.name} ({sid}), "
                        f"but it is a current stop in {stations[owner[former]].name}.",
                        station=sid, stop=platform.id,
                    )
                elif former in former_of:
                    error(
                        "platform-former-id-collides",
                        f"{former} is a former id of two platforms: {former_of[former]} and {platform.id}.",
                        station=sid, stop=platform.id,
                    )
                else:
                    former_of[former] = platform.id

    # MARK: References

    seen_pairs: dict[frozenset[str], int] = {}
    for i, transfer in enumerate(curation.stations.transfers):
        a, b = transfer.between
        pair = " and ".join(repr(x) for x in sorted(transfer.between))
        for end in dict.fromkeys(transfer.between):
            if end not in stations:
                error(
                    "unknown-station",
                    f"The transfer between {pair} names {end!r}, which is not a station.",
                    station=end if a == b else (b if end == a else a),
                )
        if a == b:
            error("transfer-to-itself", f"A transfer joins {a!r} to itself.", station=a)
            continue
        key = frozenset(transfer.between)
        if key in seen_pairs:
            # Both would be served, and a mode change to one would leave the other.
            error("duplicate-transfer", f"The transfer between {pair} is listed twice.", station=min(a, b))
        else:
            seen_pairs[key] = i
    for subway_id, subway in subways.items():
        for sid in subway.stations:
            if sid not in stations:
                error(
                    "unknown-station",
                    f"Subway {subway.name} ({subway_id}) lists {sid!r}, which is not a station.",
                )

    for line_id, override in sorted(curation.lines.root.items()):
        if line_id not in known_lines:
            warn(
                "unknown-line-override",
                f"lines.json has an override for {line_id}, which 511 does not list.",
                line=line_id,
            )
        for replaced in override.replaces:
            if replaced not in known_lines:
                # A warning, not an error: 511 dropping a line in a service change
                # must never block every save until someone edits lines.json.
                warn(
                    "unknown-line",
                    f"{line_id} replaces {replaced}, which 511 does not list.",
                    line=line_id,
                )

    # MARK: Shape patches

    # Warnings: a refresh that moves 511's track away from a patch's ends, or drops
    # its line, must not block every save until someone redraws the patch.
    shapes = {sid: pts for snap in snapshots.values() for sid, pts in snap.shapes.root.items()}
    patterns = {lid: ps for snap in snapshots.values() for lid, ps in snap.patterns.root.items()}
    most_run = {lid: list(_most_run(patterns.get(lid, [])).values()) for lid in known_lines}
    drawn = {lid: [p.shape for p in ps if p.shape in shapes] for lid, ps in most_run.items()}
    drawn_stops: dict[str, list[str]] = {}
    for ps in most_run.values():
        for p in ps:
            if p.shape in shapes:
                drawn_stops.setdefault(p.shape, p.stops)
    _, unmatched = draw_shapes(curation.shapes, drawn, terminal_stops(drawn_stops, stops, curation), shapes)
    for patch_id, line_id in unmatched:
        if line_id not in known_lines:
            warn(
                "shape-patch-unknown-line",
                f"Shape patch {patch_id!r} is for {line_id}, which 511 does not list.",
                line=line_id,
            )
        else:
            warn(
                "shape-patch-unmatched",
                f"Shape patch {patch_id!r} does not fit {line_id}: 511's shape for it no longer passes "
                f"within {ANCHOR_M:g} m of both ends of the patch, or of one end and the line's own end "
                "for an extension, so 511's path is drawn there instead.",
                line=line_id,
            )

    # MARK: Review queue

    for pid in sorted(stops.keys() - owner.keys() - ignored.keys()):
        warn(
            "unassigned-stop",
            f"{pid} ({stops[pid].name}) is in 511's data but in no station, and not ignored.",
            stop=pid,
        )

    return Validation(errors=errors, warnings=warnings)
