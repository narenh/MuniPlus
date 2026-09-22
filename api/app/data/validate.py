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

    # MARK: Platforms

    owner: dict[str, str] = {}
    for sid, station in stations.items():
        if not station.platforms:
            error("station-has-no-platforms", f"{station.name} ({sid}) has no platforms.", station=sid)
            continue
        seen: set[str] = set()
        for platform in station.platforms:
            pid = platform.id
            if pid in seen:
                error(
                    "platform-listed-twice",
                    f"{pid} is listed twice in {station.name} ({sid}).",
                    station=sid,
                    platform=pid,
                )
                continue
            seen.add(pid)
            if pid in owner:
                first = owner[pid]
                error(
                    "platform-in-two-stations",
                    f"{pid} is in both {stations[first].name} ({first}) and {station.name} ({sid}).",
                    station=sid,
                    platform=pid,
                )
            else:
                owner[pid] = sid
            if pid in ignored:
                error(
                    "platform-assigned-and-ignored",
                    f"{pid} is in {station.name} ({sid}) and also in ignored.json.",
                    station=sid,
                    platform=pid,
                )
            if pid not in stops:
                warn(
                    "platform-not-in-snapshot",
                    f"{pid} in {station.name} ({sid}) is not in 511's current data, so the API leaves it out.",
                    station=sid,
                    platform=pid,
                )
        if not any(pid in stops for pid in seen):
            warn(
                "station-has-no-live-platforms",
                f"None of {station.name} ({sid})'s platforms are in 511's current data, "
                "so it has no coordinate and the API leaves it out.",
                station=sid,
            )

    # MARK: References

    for sid, station in stations.items():
        for transfer in station.transfers:
            if transfer.to not in stations:
                error(
                    "unknown-station",
                    f"{station.name} ({sid}) has a transfer to {transfer.to!r}, which is not a station.",
                    station=sid,
                )
            elif transfer.mode == "indoor" and not any(
                back.to == sid and back.mode == "indoor" for back in stations[transfer.to].transfers
            ):
                # Street links may be one-way on purpose; an indoor passage cannot be.
                error(
                    "indoor-transfer-not-reciprocated",
                    f"{station.name} ({sid}) has an indoor transfer to {transfer.to!r}, "
                    "which has no indoor transfer back.",
                    station=sid,
                )
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
                error(
                    "unknown-line",
                    f"{line_id} replaces {replaced}, which 511 does not list.",
                    line=line_id,
                )

    # MARK: Review queue

    for pid in sorted(stops.keys() - owner.keys() - ignored.keys()):
        warn(
            "unassigned-stop",
            f"{pid} ({stops[pid].name}) is in 511's data but in no station, and not ignored.",
            platform=pid,
        )

    return Validation(errors=errors, warnings=warnings)
