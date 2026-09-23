"""The machine layer: what lives in sf-transit's ``snapshot/<operator>/``.

Written only by ingest, from 511, never by hand. A refresh overwrites these files
in place; git history keeps the old ones, and the diff of a refresh commit is
exactly what 511 changed.

For SF a refresh is two 511 calls: the GTFS zip (``/transit/datafeeds``) for
stops, lines, colours, stop patterns and the shapes vehicles drive, and ``/transit/lines`` for 511's own
``TransportMode``, which the zip does not carry.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import Field, RootModel, model_serializer

from .base import FileModel
from .ids import Color, LineId, Mode, Operator, StopId, ShapeId


class SnapshotMeta(FileModel):
    """``meta.json``"""

    operator: Operator
    service_from: date
    service_to: date
    """511's service period for this data. It is the snapshot's identity: a new
    period is what makes a refresh worth doing."""
    fetched_at: datetime
    source: str
    sha256: str
    """Of the GTFS zip, so two refreshes of an unchanged feed are recognisably the same."""


class SnapshotStop(FileModel):
    name: str
    """511's name for the stop ("Metro Embarcadero Station"). Not the station name."""
    lat: float
    lon: float


class SnapshotStops(RootModel[dict[StopId, SnapshotStop]]):
    """``stops.json``: every stop 511 lists, keyed by the id the realtime feeds use."""

    root: dict[StopId, SnapshotStop] = {}

    @model_serializer(mode="wrap")
    def _sorted(self, handler):
        return dict(sorted(handler(self).items()))


class SnapshotLine(FileModel):
    short_name: str
    long_name: str
    mode: Mode
    """511's ``TransportMode`` for the line, verbatim."""
    route_type: int
    """GTFS ``route_type``, kept for reference. ``mode`` is what we use."""
    color: Color | None = None
    text_color: Color | None = None


class SnapshotLines(RootModel[dict[LineId, SnapshotLine]]):
    """``lines.json``. GTFS calls these routes; they are renamed at ingest."""

    root: dict[LineId, SnapshotLine] = {}

    @model_serializer(mode="wrap")
    def _sorted(self, handler):
        return dict(sorted(handler(self).items()))


class Pattern(FileModel):
    """One distinct stop sequence a line runs in one direction."""

    direction: Literal[0, 1]
    """GTFS ``direction_id``, the same 0/1 the realtime feeds use."""
    headsign: str
    trips: int = Field(ge=1)
    """How many trips in the service period run exactly this sequence. The most-run
    pattern per direction is the line's diagram."""
    stops: list[StopId]
    shape: ShapeId | None = None
    """The GTFS shape most of those trips drive, a key into ``shapes.json``. None
    when the feed has no shapes."""


class SnapshotPatterns(RootModel[dict[LineId, list[Pattern]]]):
    """``patterns.json``: per line, sorted by direction then most trips first."""

    root: dict[LineId, list[Pattern]] = {}

    @model_serializer(mode="wrap")
    def _sorted(self, handler):
        out = handler(self)
        return {
            line: sorted(patterns, key=lambda p: (p["direction"], -p["trips"], p["stops"]))
            for line, patterns in sorted(out.items())
        }


Point = tuple[float, float]
"""``(lon, lat)``, GeoJSON's order, so a shape is a LineString's coordinates as is."""


class SnapshotShapes(RootModel[dict[ShapeId, list[Point]]]):
    """``shapes.json``: the path of every shape a pattern names, as the feed draws it.

    Only shapes a pattern names are kept; the rest of ``shapes.txt`` describes trips
    no pattern counts. Points are the feed's, unsimplified: simplifying is a choice
    about drawing, made where the shapes are served."""

    root: dict[ShapeId, list[Point]] = {}

    @model_serializer(mode="wrap")
    def _sorted(self, handler):
        return dict(sorted(handler(self).items()))


class Snapshot(FileModel):
    """One operator's snapshot, as one value."""

    meta: SnapshotMeta
    stops: SnapshotStops
    lines: SnapshotLines
    patterns: SnapshotPatterns
    shapes: SnapshotShapes = Field(default_factory=SnapshotShapes, exclude=True)
    """Left out when a whole snapshot is serialised, as ``EditorState`` does: they are
    most of its bytes and the map fetches them once from ``GET /api/shapes``.
    ``files`` writes each part on its own, so ``shapes.json`` is unaffected."""
