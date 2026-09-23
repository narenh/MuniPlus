"""The hand-curated layer: what lives in sf-transit's ``curation/``.

The rule for this layer is that it holds only what a person asserts. Anything 511
already publishes (stop names, coordinates, which lines serve a platform, route
colours, stop order) comes from the snapshot and is never restated here; where a
person deliberately disagrees with 511, that is an explicit override field.

Every optional field defaults to "not stated" and is omitted when written, so a
file only ever contains what someone actually decided.
"""

from datetime import date

from typing import Annotated

from pydantic import Field, RootModel, StringConstraints, model_serializer

from .base import FileModel
from .ids import Color, Heading, LineId, Mode, Operator, PlatformId, StationId, SubwayId, TransferMode

NonBlank = Annotated[str, StringConstraints(pattern=r"\S")]
"""At least one non-space character. Not stripped: what is on disk is what round-trips."""


class Platform(FileModel):
    """One pole or platform, identified by the stop id 511 uses for it.

    A platform belongs to exactly one station. Its coordinates, 511 stop name and
    the lines serving it all come from the snapshot.
    """

    id: PlatformId
    heading: Heading
    name: str | None = None
    """Signage, where the platform has any ("Platform 1", "To Castro"). Not the 511 stop name."""
    note: str | None = None
    """Why this platform is here, when that is not obvious: a verified-on-the-street
    reassignment, a pole with two ids. The record of what was checked and how."""


class Transfer(FileModel):
    """A walk from this station to another.

    Deliberately one-way: a link from A to B says nothing about B to A, and many
    are left unreciprocated on purpose. Only ``indoor`` links must be mutual,
    because a passage cannot be walkable in one direction only.
    """

    to: StationId
    mode: TransferMode
    note: str | None = None


class Station(FileModel):
    name: NonBlank
    platforms: list[Platform]
    """Flat, in the order the app should list them. Levels and exits arrive later
    as an optional ``layout`` field that assigns these platforms to levels; this
    list does not change when they do."""
    transfers: list[Transfer] = []
    transfer_agencies: list[Operator] = []
    """Operators reachable here that have no platforms in the data yet (``BA`` at
    Embarcadero). A stopgap: once an operator's platforms are ingested, its
    presence is derived from them and these entries are removed."""
    former_ids: list[StationId] = []
    """Ids this station used to have. The API redirects them and publishes the map,
    so the app can migrate saved favourites."""
    verified: date | None = None
    """When a person last checked this station on the map. Cleared whenever its
    platform list changes; renames and heading fixes leave it alone."""
    hub: bool = False
    """A major interchange between metro lines (4th & King, Union Square, Balboa
    Park), drawn as the map's largest white station. A person's call: a rule on
    line counts also catches every stop where two branches happen to meet."""
    note: str | None = None


class Subway(FileModel):
    name: NonBlank
    stations: list[StationId]
    """In order along the subway, which is why subways are listed here rather than
    as a field on each station: a per-station field would lose the order."""


class StationsFile(FileModel):
    """``curation/stations.json``: everything hand-curated about stations, in one place."""

    subways: dict[SubwayId, Subway] = {}
    stations: dict[StationId, Station]

    @model_serializer(mode="wrap")
    def _sorted(self, handler):
        # Sorted by id, so where an entry sits in the file is never a change in itself.
        out = handler(self)
        for key in ("subways", "stations"):
            if key in out:
                out[key] = dict(sorted(out[key].items()))
        return out


class LineOverride(FileModel):
    """Where a person overrides or adds to what 511 says about a line."""

    name: str | None = None
    """Display name. Default is ``"{short} {Title Case long}"`` from the snapshot."""
    color: Color | None = None
    text_color: Color | None = None
    mode: Mode | None = None
    """``streetcar`` for the F. Otherwise 511's mode stands."""
    hidden: bool = False
    replaces: list[LineId] = []
    """Lines this one substitutes for: ``SF:LOWL`` replaces ``SF:L``. Line-level
    only; there is no mapping of replacement stops to stations."""
    note: str | None = None


class LinesFile(RootModel[dict[LineId, LineOverride]]):
    """``curation/lines.json``: overrides only. A line with nothing to override is absent."""

    root: dict[LineId, LineOverride] = {}

    @model_serializer(mode="wrap")
    def _sorted(self, handler):
        return dict(sorted(handler(self).items()))


class IgnoredStop(FileModel):
    note: NonBlank
    """Required: an ignored stop with no reason is indistinguishable from a mistake."""


class IgnoredFile(RootModel[dict[PlatformId, IgnoredStop]]):
    """``curation/ignored.json``: 511 stops deliberately assigned to no station, so
    they stop appearing in the review queue. Not station data, hence its own file."""

    root: dict[PlatformId, IgnoredStop] = {}

    @model_serializer(mode="wrap")
    def _sorted(self, handler):
        return dict(sorted(handler(self).items()))


PatchId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]+$")]

CuratedPoint = tuple[float, float]
"""``(lon, lat)``, as in a snapshot's shapes."""


class ShapePatch(FileModel):
    """A stretch of track 511's shapes draw wrongly, and the path it really takes.

    ``path`` runs from one point on 511's shape to another. Every shape of the
    listed lines that passes both ends has what lies between them replaced by
    ``path``, reversed for a direction that runs the other way. Anchored to the
    geometry rather than to shape ids, which 511 may renumber in a new service
    period: a refresh leaves the patch applying wherever the track still runs.
    """

    lines: list[LineId] = Field(min_length=1)
    path: list[CuratedPoint] = Field(min_length=2)
    note: str | None = None
    """What is wrong with 511's version, and how the curated path was checked."""


class ShapesFile(RootModel[dict[PatchId, ShapePatch]]):
    """``curation/shapes.json``: patches to 511's line shapes, keyed by a name for
    the stretch (``t-market-4th``). Applied in key order."""

    root: dict[PatchId, ShapePatch] = {}

    @model_serializer(mode="wrap")
    def _sorted(self, handler):
        return dict(sorted(handler(self).items()))


class Curation(FileModel):
    """All the curation files, as one value. What the editor loads and saves."""

    stations: StationsFile
    lines: LinesFile = LinesFile()
    ignored: IgnoredFile = IgnoredFile()
    shapes: ShapesFile = ShapesFile()
