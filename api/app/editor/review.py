"""``GET /editor/api/review``: the review queue.

Three lists, each the list form of one validator warning, so the queue and the
editor's warnings can never disagree about what needs looking at:

* ``unassigned``: ``unassigned-stop``, with a proposal from ``app.ingest.propose``;
* ``deadPlatforms``: ``stop-not-in-snapshot``;
* ``deadStations``: ``station-has-no-live-platforms``.

Accepting a proposal has no endpoint: the editor applies it to its curation and
saves as usual (``UnassignedStop`` says how), so a stop is assigned by the same
validated, conflict-checked path as any other edit.
"""

from collections.abc import Mapping

from fastapi import APIRouter, Request

from ..data.network import Network
from ..ingest.propose import propose
from ..models.curation import Curation
from ..models.editor import Validation
from ..models.ids import operator_of
from ..models.snapshot import Snapshot
from .api import UNAUTHORIZED, SessionRoute
from .errors import json_response
from .models import DeadStop, DeadStation, ReviewResponse, UnassignedStop
from .state import current_network, editor_of, loaded

router = APIRouter(prefix="/editor/api", route_class=SessionRoute, tags=["editor"])


@router.get("/review", response_model=ReviewResponse, responses=UNAUTHORIZED)
def review(request: Request):
    editor = editor_of(request)
    at_head = loaded(editor)
    cached = getattr(request.app.state, "review_cache", None)
    if cached is not None and cached.version == at_head.version:
        return json_response(cached)
    # The app's network when it is this version, as ``build_state`` does, rather
    # than building a second one for the platforms' lines.
    network = current_network(request.app)
    if network is None or network.version != at_head.version:
        network = at_head.network()
    result = build_review(at_head.version, at_head.curation, at_head.snapshots, network, at_head.validation())
    # One entry, keyed by version: the proposer runs over every snapshot stop, and
    # the editor asks again after each save, most of which move nothing it looks at.
    request.app.state.review_cache = result
    return json_response(result)


def build_review(
    version: str,
    curation: Curation,
    snapshots: Mapping[str, Snapshot],
    network: Network,
    validation: Validation,
) -> ReviewResponse:
    codes = codes_of(validation)
    return ReviewResponse(
        version=version,
        unassigned=unassigned_stops(curation, snapshots, network, codes["unassigned-stop"]),
        dead_stops=dead_stops(curation, codes["stop-not-in-snapshot"]),
        dead_stations=dead_stations(curation, codes["station-has-no-live-platforms"]),
    )


REVIEW_CODES = ("unassigned-stop", "stop-not-in-snapshot", "station-has-no-live-platforms")


def codes_of(validation: Validation) -> dict[str, set[str]]:
    """What each review warning names: a stop id for the first two (a dead stop's
    station is in the curation), a station id for the last."""
    out: dict[str, set[str]] = {code: set() for code in REVIEW_CODES}
    for issue in validation.warnings:
        if issue.code == "station-has-no-live-platforms":
            out[issue.code].add(issue.station)
        elif issue.code in out:
            out[issue.code].add(issue.stop)
    return out


def unassigned_stops(
    curation: Curation,
    snapshots: Mapping[str, Snapshot],
    network: Network,
    unassigned: set[str],
) -> list[UnassignedStop]:
    derived = network.derived().stops
    by_operator: dict[str, list[str]] = {}
    for pid in sorted(unassigned):
        by_operator.setdefault(operator_of(pid), []).append(pid)
    out = []
    for operator, pids in sorted(by_operator.items()):
        snapshot = snapshots[operator]
        # One call for all of them, never one per stop: the proposer clusters the
        # stops it is given, so two new poles at one corner get one new station,
        # and two corners that would mint the same id are numbered consistently.
        for proposal in propose(snapshot, curation, pids):
            stop = snapshot.stops.root[proposal.stop]
            out.append(
                UnassignedStop(
                    stop=proposal.stop,
                    name=stop.name,
                    lat=stop.lat,
                    lon=stop.lon,
                    lines=derived[proposal.stop].lines if proposal.stop in derived else [],
                    proposal=proposal,
                )
            )
    return sorted(out, key=lambda u: u.stop)


def dead_stops(curation: Curation, dead: set[str]) -> list[DeadStop]:
    out = []
    seen: set[str] = set()
    for sid, station in sorted(curation.stations.stations.items()):
        for platform in station.platforms:
            for pid in platform.all_stops:
                # A stop listed twice is an error the validator reports; here it is
                # shown once, under the first station, as the network does.
                if pid in dead and pid not in seen:
                    seen.add(pid)
                    out.append(DeadStop(
                        stop=pid, platform=platform.id, station=sid, station_name=station.name,
                        heading=platform.heading,
                    ))
    return out


def dead_stations(curation: Curation, stations: set[str]) -> list[DeadStation]:
    return [
        DeadStation(station=sid, name=station.name, stops=[s for p in station.platforms for s in p.all_stops])
        for sid, station in sorted(curation.stations.stations.items())
        if sid in stations
    ]
