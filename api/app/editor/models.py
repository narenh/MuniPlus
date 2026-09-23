"""Shapes the editor server needs that ``app.models.editor`` does not have yet.

They belong in the contract; they live here until it takes them, so that no
track edits ``app/models/`` behind wave 0's back.
"""

import datetime as dt
from typing import Literal

from ..ingest.propose import Proposal
from ..models.api import Problem
from ..models.base import Wire
from ..models.editor import Validation
from ..models.ids import Heading, LineId, Mode, Operator, PlatformId, StationId


class HistoryEntry(Wire):
    sha: str
    subject: str
    """The commit message's first line, which is what a list of commits can show."""
    author: str
    date: str
    """Author date, ISO 8601 with the author's offset, as git prints it."""
    url: str | None
    """The commit on GitHub, which is the diff. Null when ``DATA_REPO`` is not a
    GitHub URL (a local path in tests, another host)."""


class HistoryResponse(Wire):
    commits: list[HistoryEntry]
    """Newest first."""


class InvalidSave(Problem):
    """422: the posted curation has validation errors, so nothing was written."""

    validation: Validation


class SaveConflict(Problem):
    """409: sf-transit moved in a way this save cannot be applied on top of. The
    editor should reload the state and redo the edit; nothing was written."""

    version: str
    """sf-transit HEAD after the refusal, the version to reload."""
    paths: list[str]
    """The curation files that changed underneath the edit, where known."""


# MARK: - Review queue


class UnassignedStop(Wire):
    """A 511 stop in no station and not ignored, with what the proposer suggests.

    Accepting it is an ordinary curation edit followed by the normal save, so this
    carries everything that edit needs:

    * ``proposal.station`` set: append ``{id: platform, heading}`` to that
      station's ``platforms`` (and clear its ``verified``, as any change to a
      platform list does);
    * ``proposal.newStation`` set: create ``stations[newStation.id]`` named
      ``newStation.name`` unless the edit already has, and append the platform.
      Stops proposed together at one intersection share one ``newStation``, so
      accepting all of them builds one station;
    * ``proposal.heading`` null (no line stops here, so there is no direction of
      travel to read one from): the editor has to ask, since a platform needs one;
    * rejecting it: add ``ignored[platform] = {note}``, or place it by hand.

    A proposed new id is free in the curation this was computed from. If the edit
    has since created a different station with that id, pick another.
    """

    platform: PlatformId
    stop_name: str
    """511's name for the stop."""
    lat: float
    lon: float
    lines: list[LineId]
    """Every line with a pattern stopping here (``derived.platforms``), in line
    order. Empty for a stop no line serves in the service period."""
    proposal: Proposal


class DeadPlatform(Wire):
    """A curated platform 511 no longer lists (``platform-not-in-snapshot``)."""

    platform: PlatformId
    station: StationId
    station_name: str
    heading: Heading


class DeadStation(Wire):
    """A station none of whose platforms 511 lists (``station-has-no-live-platforms``).
    It has no coordinate, so the public API leaves it out."""

    station: StationId
    name: str
    platforms: list[PlatformId]


class ReviewResponse(Wire):
    """``GET /editor/api/review``: where 511 and the curation disagree."""

    version: str
    """sf-transit HEAD this was computed from. Proposals are only good against the
    curation at this version: fetch the review again after a save."""
    unassigned: list[UnassignedStop]
    """In platform id order."""
    dead_platforms: list[DeadPlatform]
    """By station id, then in the station's own platform order. Includes the
    platforms of ``deadStations``."""
    dead_stations: list[DeadStation]
    """By station id."""


# MARK: - Snapshot refresh


class ServicePeriod(Wire):
    service_from: dt.date
    service_to: dt.date


class DriftStop(Wire):
    platform: PlatformId
    name: str
    lat: float
    lon: float


class StopMove(Wire):
    platform: PlatformId
    name: str
    """The new name."""
    metres: float
    """Rounded to the metre."""
    old_lat: float
    old_lon: float
    lat: float
    lon: float


class StopRename(Wire):
    platform: PlatformId
    old_name: str
    name: str


class DriftLine(Wire):
    line: LineId
    short_name: str
    long_name: str
    mode: Mode


class FieldChange(Wire):
    field: Literal["shortName", "longName", "mode", "color", "textColor"]
    old: str | None
    new: str | None


class LineChange(Wire):
    line: LineId
    changes: list[FieldChange]


class PatternSummary(Wire):
    headsign: str
    trips: int
    stops: list[PlatformId]


class PatternChange(Wire):
    """The most-run pattern of one line and direction, where its stops or headsign
    changed. That pattern is the line's diagram and names its trips in realtime; a
    change in trip count alone is not reported, since every new period has one."""

    line: LineId
    direction: Literal[0, 1]
    old: PatternSummary | None
    """Null when the line had no trips in this direction before."""
    new: PatternSummary | None
    """Null when it has none now."""
    stops_added: list[PlatformId]
    stops_removed: list[PlatformId]


class SnapshotDrift(Wire):
    """The pending snapshot against the checkout's current one for the same operator.

    Lists are sorted by id (patterns by line, then direction). Only lines in both
    snapshots appear in ``patternsChanged``: a new or dropped line is already in
    ``linesAdded`` or ``linesRemoved``.
    """

    operator: Operator
    old: ServicePeriod | None
    """Null when the checkout has no snapshot for this operator yet."""
    new: ServicePeriod
    same_zip: bool
    """The GTFS zip's sha256 is unchanged."""

    stops_added: list[DriftStop]
    stops_removed: list[DriftStop]
    stops_moved: list[StopMove]
    """Moved more than 10 m."""
    stops_renamed: list[StopRename]

    lines_added: list[DriftLine]
    lines_removed: list[DriftLine]
    lines_changed: list[LineChange]
    patterns_changed: list[PatternChange]

    dead_platforms: list[DeadPlatform]
    """Curated platforms live now that the new snapshot drops."""
    dead_stations: list[DeadStation]
    """Stations with a live platform now that would have none."""
    new_unassigned: list[UnassignedStop]
    """Stops that would join the review queue, proposed against the new snapshot and
    the curation at ``baseVersion``. Stops already in the queue are not repeated."""


class SnapshotFetchRequest(Wire):
    operator: Operator | None = None
    """Defaults to the first of ``settings.operators`` (``SF``)."""


class SnapshotFetchResponse(Wire):
    """``POST /editor/api/snapshot/fetch``. Nothing is committed."""

    pending: str
    """Names the pending snapshot for ``snapshot/commit``. A newer fetch replaces it."""
    base_version: str
    """sf-transit HEAD the drift was computed against; send it back to commit."""
    fetched_at: dt.datetime
    unchanged: bool
    """Committing would change nothing: the files would be identical apart from
    ``meta.fetchedAt``, so ``snapshot/commit`` would return ``commit: null``."""
    drift: SnapshotDrift


class SnapshotCommitRequest(Wire):
    pending: str
    base_version: str
    """The ``baseVersion`` from the fetch; it must be a commit in sf-transit's
    history, as a save's must. The commit is refused (409) if this operator's
    snapshot was committed again after the version the drift was computed against,
    which the server recorded with the pending snapshot, so a newer ``baseVersion``
    cannot hide such a refresh. Curation edits since do not matter: a snapshot
    commit touches only ``snapshot/``."""
