"""Shapes for the editor (``/editor/api/*``, behind login) and the public map
(``/map/api/state``).

The two pages are one frontend. ``/map`` loads the same ``EditorState`` with
``readOnly`` set and no validation or repo status, and hides everything that
edits. Nothing here is secret: sf-transit is a public repo, so the curation
(notes and verification dates included) is already public.
"""

from typing import Literal

from .api import LineSummary
from .base import Wire
from .curation import Curation, NonBlank
from .ids import LineId, Mode, Operator, PlatformId, StationId
from .snapshot import Snapshot

# MARK: - Derived values


class DerivedPlatform(Wire):
    live: bool
    """False when 511 no longer lists this stop. It is dropped from the public API
    and flagged, never deleted automatically."""
    lines: list[LineId]
    lat: float | None
    lon: float | None
    stop_name: str | None


class DerivedStation(Wire):
    lat: float | None
    lon: float | None
    """Centroid of live platforms. Null only when none are live, which is a warning."""
    lines: list[LineId]
    modes: list[Mode]


class Derived(Wire):
    """Everything computed from snapshot x curation, so the frontend never has to
    re-implement the rules. Recomputed by the server after every validate call."""

    stations: dict[StationId, DerivedStation]
    platforms: dict[PlatformId, DerivedPlatform]
    lines: dict[LineId, LineSummary]


# MARK: - Validation


class Issue(Wire):
    level: Literal["error", "warning"]
    code: str
    """Stable, machine-readable (``platform-in-two-stations``), so the UI can group
    and the tests can assert without matching prose."""
    message: str
    station: StationId | None = None
    platform: PlatformId | None = None
    line: LineId | None = None


class Validation(Wire):
    errors: list[Issue]
    """Block a save."""
    warnings: list[Issue]
    """Shown, never block."""


# MARK: - State


class RepoStatus(Wire):
    branch: str
    head: str
    dirty: bool
    ahead: int
    behind: int


class EditorState(Wire):
    version: str
    """sf-transit HEAD commit the state was read from."""
    read_only: bool
    read_only_reason: str | None
    curation: Curation
    snapshots: dict[Operator, Snapshot]
    derived: Derived
    validation: Validation | None
    """Null on ``/map``."""
    repo: RepoStatus | None
    """Null on ``/map``."""


# MARK: - Requests


class LoginRequest(Wire):
    password: str


class ValidateRequest(Wire):
    curation: Curation


class ValidateResponse(Wire):
    validation: Validation
    derived: Derived


class SaveRequest(Wire):
    curation: Curation
    message: NonBlank
    base_version: str
    """The version this edit started from. If sf-transit has moved on since and the
    rebase does not apply cleanly, the save is refused with a 409 rather than
    overwriting someone else's commit."""


class SaveResponse(Wire):
    version: str
    commit: str | None
    """Null when the save changed nothing: an unchanged document makes no commit."""
    pushed: bool
    push_error: str | None
    validation: Validation
