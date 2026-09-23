"""Refreshing a snapshot from 511: fetch, show the drift, then commit on request.

``POST snapshot/fetch`` makes the two 511 calls a refresh takes (PLAN.md, "Data"),
builds the snapshot into a pending area under ``settings.data_dir``, outside the
checkout, and reports how it differs from the committed one. Nothing is
committed: a new service period can drop platforms people rely on, and someone
should see that before the public API does. ``POST snapshot/commit`` then writes
it through ``files.write_snapshot`` and commits it by the same path as a save
(``save.run_locked`` and friends), guarding ``snapshot/<operator>/`` where a save
guards ``curation/``.

One pending snapshot at a time, on disk, so it survives a restart between the
fetch and the commit. A new fetch replaces it. The id is random and changes on
every fetch, so a commit can never pick up a snapshot other than the one whose
drift the person was shown.
"""

import asyncio
import csv
import json
import secrets
import shutil
import threading
import time
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, FastAPI, Request
from starlette.concurrency import run_in_threadpool

from ..data.network import Network
from ..data.validate import validate
from ..db import Database
from ..ingest.gtfs_static import GtfsError, build_snapshot
from ..ingest.propose import metres
from ..models import files
from ..models.api import Problem
from ..models.editor import SaveResponse
from ..models.snapshot import Pattern, Snapshot
from ..settings import Settings
from ..upstream.client511 import BudgetExhausted, Client511, NoApiKey, UpstreamError
from .api import UNAUTHORIZED, SessionRoute
from .checkout import Checkout, Loaded
from .errors import ProblemError, json_response
from .models import (
    DriftLine,
    DriftStop,
    FieldChange,
    LineChange,
    PatternChange,
    PatternSummary,
    SaveConflict,
    ServicePeriod,
    SnapshotCommitRequest,
    SnapshotDrift,
    SnapshotFetchRequest,
    SnapshotFetchResponse,
    StopMove,
    StopRename,
)
from .review import codes_of, dead_stations, dead_stops, unassigned_stops
from .save import check_writable, conflict, fetch_and_rebase, push_and_respond, require_base, run_locked
from .state import Editor, editor_of, loaded

PENDING_DIR = "snapshot-pending"
PENDING_META = "pending.json"

CALLS_PER_REFRESH = 2
MOVE_METRES = 10.0
"""A stop that moved less than this is 511 re-surveying a pole, not moving it. 511
publishes six decimal places (about 10 cm), so this is well clear of rounding."""

LINE_FIELDS = (
    ("shortName", "short_name"),
    ("longName", "long_name"),
    ("mode", "mode"),
    ("color", "color"),
    ("textColor", "text_color"),
)


# MARK: - Pending state


class Refresher:
    """The pending snapshot and what fetching one needs. Lives on
    ``app.state.snapshot_refresh``; tests put their own there, with a mock 511."""

    def __init__(
        self,
        settings: Settings,
        *,
        db: Database | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.root = settings.data_dir / PENDING_DIR
        self.clock = clock
        self._db = db
        self._transport = transport
        # A second fetch while one runs would spend two more calls for the same data.
        self.fetching = asyncio.Lock()
        # Guards the pending directory: replaced by a fetch, read and cleared by a commit.
        self._lock = threading.Lock()

    def client(self) -> Client511:
        if self._db is None:
            # The same SQLite file as the realtime pollers' ledger, so a refresh and
            # the pollers count against one budget.
            self._db = Database(self.settings.db_path)
        return Client511(self.settings, self._db, transport=self._transport, clock=self.clock)

    def stage(
        self, operator: str, gtfs_zip: bytes, lines_json: bytes, fetched_at: datetime, base_version: str
    ) -> tuple[str, Snapshot]:
        """Build the snapshot and make it the pending one, recording the sf-transit
        version its drift is computed against. Returns its id."""
        pending = secrets.token_hex(8)
        staging = self.root.parent / f".{PENDING_DIR}-{pending}"
        staging.mkdir(parents=True)
        try:
            zip_path, lines_path = staging / "datafeed.zip", staging / "lines.json"
            zip_path.write_bytes(gtfs_zip)
            lines_path.write_bytes(lines_json)
            try:
                snapshot = build_snapshot(zip_path, lines_path, operator, fetched_at)
            except (GtfsError, zipfile.BadZipFile, KeyError, csv.Error, ValueError) as err:
                # KeyError is a table missing from the zip; ValueError covers JSON
                # that is not JSON and a value a model refuses. Either way 511 sent
                # something this code was not written against: say what, keep nothing.
                raise ProblemError(
                    503, "unavailable", f"511's {operator} feed could not be built into a snapshot: {err}"
                ) from None
            # The raw download is 8 MB and the built files hold everything a commit
            # needs, so it is not kept.
            zip_path.unlink()
            lines_path.unlink()
            files.write_snapshot(staging, snapshot)
            (staging / PENDING_META).write_text(json.dumps({"id": pending, "operator": operator, "baseVersion": base_version}) + "\n")
            with self._lock:
                shutil.rmtree(self.root, ignore_errors=True)
                staging.rename(self.root)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return pending, snapshot

    def load(self, pending: str) -> tuple[Snapshot, str]:
        """The pending snapshot called ``pending`` and the version it was compared
        against, or a 404 if that is not the one pending."""
        with self._lock:
            meta = self._meta()
            if meta is None or meta.get("id") != pending:
                raise ProblemError(
                    404, "not-found", "That pending snapshot has been committed or replaced. Fetch again."
                )
            return files.read_snapshot(self.root, meta["operator"]), meta["baseVersion"]

    def clear(self, pending: str) -> None:
        """Drop the pending snapshot, unless a newer fetch has already replaced it."""
        with self._lock:
            meta = self._meta()
            if meta is not None and meta.get("id") == pending:
                shutil.rmtree(self.root, ignore_errors=True)

    def _meta(self) -> dict | None:
        try:
            return json.loads((self.root / PENDING_META).read_text())
        except (FileNotFoundError, ValueError):
            return None


def refresher_of(app: FastAPI) -> Refresher:
    refresher = getattr(app.state, "snapshot_refresh", None)
    if refresher is None:
        realtime = getattr(app.state, "realtime", None)
        refresher = Refresher(app.state.editor.settings, db=getattr(realtime, "db", None))
        app.state.snapshot_refresh = refresher
    return refresher


# MARK: - Fetch


def refusal(settings: Settings) -> str | None:
    """Why this server must not call 511 for a snapshot, if it must not."""
    if settings.fixtures:
        # FIXTURES=1 promises a server that replays recordings and never calls out,
        # whatever key happens to be in the environment.
        return "the server is replaying fixtures (FIXTURES=1) and never calls 511"
    if not settings.api_511_key:
        return "no 511 API key is configured (API_511_KEY)"
    return None


async def fetch(editor: Editor, app: FastAPI, body: SnapshotFetchRequest) -> SnapshotFetchResponse:
    settings = editor.settings
    if reason := refusal(settings):
        raise ProblemError(503, "unavailable", f"Cannot fetch a snapshot: {reason}.")
    operator = body.operator or settings.operators[0]
    if operator not in settings.operators:
        raise ProblemError(400, "bad-request", f"{operator} is not one of this server's operators.")
    # Before spending any budget: a checkout that cannot be read has nothing to
    # compare against, and the refresh could not be committed either.
    await run_in_threadpool(loaded, editor)

    refresher = refresher_of(app)
    if refresher.fetching.locked():
        raise ProblemError(409, "conflict", "A snapshot fetch is already running.")
    async with refresher.fetching:
        client = refresher.client()
        try:
            # Checked up front so a refresh never spends one call and then finds no
            # room for the second. The pollers can still take the last call in
            # between; then the second call is refused like any other.
            if (left := client.remaining()) < CALLS_PER_REFRESH:
                raise ProblemError(
                    503,
                    "unavailable",
                    f"A refresh takes {CALLS_PER_REFRESH} calls to 511 and the hourly budget has {left} left. "
                    "Try again later.",
                )
            gtfs_zip = await client.fetch_static("datafeeds", operator)
            lines_json = await client.fetch_static("lines", operator)
        except (NoApiKey, BudgetExhausted) as err:
            raise ProblemError(503, "unavailable", f"Not calling 511: {err}.") from None
        except UpstreamError as err:
            raise ProblemError(503, "unavailable", f"511 did not answer: {err}") from None
        finally:
            await client.aclose()

        # Whole seconds: meta.json is read by people, and the realtime feeds' own
        # clocks are whole seconds too.
        fetched_at = datetime.fromtimestamp(int(refresher.clock()), UTC)
        # Off the event loop: the build streams the whole of stop_times.txt (73 MB
        # for SF) and the pollers share this loop.
        return await run_in_threadpool(
            _stage_and_compare, editor, refresher, operator, gtfs_zip, lines_json, fetched_at
        )


def _stage_and_compare(
    editor: Editor, refresher: Refresher, operator: str, gtfs_zip: bytes, lines_json: bytes, fetched_at: datetime
) -> SnapshotFetchResponse:
    # Read again rather than reuse the read from before the calls: the checkout may
    # have moved while 511 answered, and the drift is against what it holds now.
    at_head = loaded(editor)
    pending, snapshot = refresher.stage(operator, gtfs_zip, lines_json, fetched_at, at_head.version)
    old = at_head.snapshots.get(operator)
    return SnapshotFetchResponse(
        pending=pending,
        base_version=at_head.version,
        fetched_at=fetched_at,
        unchanged=same_content(old, snapshot),
        drift=drift(at_head, old, snapshot),
    )


# MARK: - Drift


def same_content(old: Snapshot | None, new: Snapshot) -> bool:
    """Whether writing ``new`` would change nothing but ``meta.fetchedAt``.

    That field changes on every fetch. A commit carrying only it would say "511 was
    checked at this time" in sf-transit's history, which is not what the history is
    for, so it is not made."""
    if old is None:
        return False
    restamped = new.model_copy(update={"meta": new.meta.model_copy(update={"fetched_at": old.meta.fetched_at})})
    # Compared as the bytes ``write_snapshot`` would write, so "same" means exactly
    # what the commit would see.
    parts = files.snapshot_paths(new.meta.operator)
    return all(files.dumps(getattr(old, part)) == files.dumps(getattr(restamped, part)) for part in parts)


def drift(at_head: Loaded, old: Snapshot | None, new: Snapshot) -> SnapshotDrift:
    operator = new.meta.operator
    old_stops = old.stops.root if old else {}
    new_stops = new.stops.root
    old_lines = old.lines.root if old else {}
    new_lines = new.lines.root

    moved, renamed = [], []
    for pid in sorted(old_stops.keys() & new_stops.keys()):
        a, b = old_stops[pid], new_stops[pid]
        # ``metres`` takes (lon, lat), and is the proposer's flat-earth distance at
        # SF's latitude: within a metre of the great circle at these distances.
        distance = metres((a.lon, a.lat), (b.lon, b.lat))
        if distance > MOVE_METRES:
            moved.append(
                StopMove(
                    stop=pid,
                    name=b.name,
                    metres=round(distance),
                    old_lat=a.lat,
                    old_lon=a.lon,
                    lat=b.lat,
                    lon=b.lon,
                )
            )
        if a.name != b.name:
            renamed.append(StopRename(stop=pid, old_name=a.name, name=b.name))

    changed_lines = []
    for lid in sorted(old_lines.keys() & new_lines.keys()):
        a, b = old_lines[lid], new_lines[lid]
        changes = [
            FieldChange(field=wire, old=getattr(a, attr), new=getattr(b, attr))
            for wire, attr in LINE_FIELDS
            if getattr(a, attr) != getattr(b, attr)
        ]
        if changes:
            changed_lines.append(LineChange(line=lid, changes=changes))

    # What the curation would see: the review warnings with the new snapshot in
    # place of the old, less the ones it has already.
    snapshots = {**at_head.snapshots, operator: new}
    curation = at_head.curation
    before = codes_of(at_head.validation())
    after = codes_of(validate(curation, snapshots))
    newly = {code: after[code] - before[code] for code in after}
    new_unassigned = []
    if newly["unassigned-stop"]:
        network = Network(curation, snapshots, at_head.version)
        # Proposed together with the stops already waiting, as the review queue
        # does, so a new stop at the corner of a queued one is proposed into the
        # same new station.
        queue = unassigned_stops(curation, snapshots, network, after["unassigned-stop"])
        new_unassigned = [u for u in queue if u.stop in newly["unassigned-stop"]]

    return SnapshotDrift(
        operator=operator,
        old=ServicePeriod(service_from=old.meta.service_from, service_to=old.meta.service_to) if old else None,
        new=ServicePeriod(service_from=new.meta.service_from, service_to=new.meta.service_to),
        same_zip=old is not None and old.meta.sha256 == new.meta.sha256,
        stops_added=[_stop(pid, new_stops[pid]) for pid in sorted(new_stops.keys() - old_stops.keys())],
        stops_removed=[_stop(pid, old_stops[pid]) for pid in sorted(old_stops.keys() - new_stops.keys())],
        stops_moved=moved,
        stops_renamed=renamed,
        lines_added=[_line(lid, new_lines[lid]) for lid in sorted(new_lines.keys() - old_lines.keys())],
        lines_removed=[_line(lid, old_lines[lid]) for lid in sorted(old_lines.keys() - new_lines.keys())],
        lines_changed=changed_lines,
        patterns_changed=_pattern_changes(old, new) if old else [],
        dead_stops=dead_stops(curation, newly["stop-not-in-snapshot"]),
        dead_stations=dead_stations(curation, newly["station-has-no-live-platforms"]),
        new_unassigned=new_unassigned,
    )


def _stop(pid, stop) -> DriftStop:
    return DriftStop(stop=pid, name=stop.name, lat=stop.lat, lon=stop.lon)


def _line(lid, line) -> DriftLine:
    return DriftLine(line=lid, short_name=line.short_name, long_name=line.long_name, mode=line.mode)


def most_run(patterns: list[Pattern]) -> dict[int, Pattern]:
    """The most-run pattern per direction, tie-broken the way the network does once
    the snapshot is on disk: ``patterns.json`` is sorted most trips first, then by
    stop sequence, and the network takes the first. A freshly built snapshot is in
    the zip's order, so the order is imposed here rather than inherited."""
    best: dict[int, Pattern] = {}
    for pattern in sorted(patterns, key=lambda p: (-p.trips, p.stops)):
        best.setdefault(pattern.direction, pattern)
    return best


def _pattern_changes(old: Snapshot, new: Snapshot) -> list[PatternChange]:
    out = []
    old_patterns, new_patterns = old.patterns.root, new.patterns.root
    for lid in sorted(old.lines.root.keys() & new.lines.root.keys()):
        before, after = most_run(old_patterns.get(lid, [])), most_run(new_patterns.get(lid, []))
        for direction in sorted(before.keys() | after.keys()):
            a, b = before.get(direction), after.get(direction)
            if a and b and (a.stops, a.headsign) == (b.stops, b.headsign):
                continue
            old_stops, new_stops = (a.stops if a else []), (b.stops if b else [])
            out.append(
                PatternChange(
                    line=lid,
                    direction=direction,
                    old=_summary(a),
                    new=_summary(b),
                    stops_added=[s for s in new_stops if s not in set(old_stops)],
                    stops_removed=[s for s in old_stops if s not in set(new_stops)],
                )
            )
    return out


def _summary(pattern: Pattern | None) -> PatternSummary | None:
    if pattern is None:
        return None
    return PatternSummary(headsign=pattern.headsign, trips=pattern.trips, stops=list(pattern.stops))


# MARK: - Commit


def commit(editor: Editor, app: FastAPI, body: SnapshotCommitRequest) -> SaveResponse:
    check_writable(editor, body.base_version)
    refresher = refresher_of(app)
    snapshot, compared_with = refresher.load(body.pending)
    result = run_locked(editor, app, lambda: _commit(editor, snapshot, body.base_version, compared_with))
    refresher.clear(body.pending)
    return result


def _commit(editor: Editor, snapshot: Snapshot, base_version: str, compared_with: str) -> SaveResponse:
    checkout = editor.checkout
    head = fetch_and_rebase(editor)
    require_base(editor, base_version)
    # Only this operator's snapshot is guarded. A curation edit since the fetch is
    # not a conflict: this commit does not touch curation/, so it reverts nothing,
    # and the drift's consequences for curation are recomputed by the validation
    # in the response. Another refresh committed since is: writing ours would
    # silently replace it with whatever this one fetched, and the drift the person
    # accepted would no longer be the change being committed. So the guard runs
    # from the version the drift was computed against, recorded with the pending
    # snapshot, not from ``baseVersion``: an editor that saved a curation edit in
    # between may send its newer version, and that must not hide a refresh.
    guarded = f"{files.SNAPSHOT_DIR}/{snapshot.meta.operator}/"
    require_base(editor, compared_with)
    if changed := _changed(checkout, compared_with, head, guarded):
        raise conflict(editor, "The snapshot was refreshed since this fetch. Fetch again.", changed)
    made = commit_snapshot(checkout, snapshot)
    return push_and_respond(editor, before=head, commit=made)


def _changed(checkout: Checkout, base: str, head: str, prefix: str) -> list[str]:
    # ``Checkout.curation_changed`` for another prefix. It is not generalised there
    # because checkout.py is not this track's (see the track J report).
    return checkout.repo._git("diff", "--name-only", base, head, "--", prefix).splitlines()


def commit_message(snapshot: Snapshot) -> str:
    meta = snapshot.meta
    return f"Refresh {meta.operator} snapshot for service {meta.service_from} to {meta.service_to}"


def commit_snapshot(checkout: Checkout, snapshot: Snapshot) -> str | None:
    """Write the snapshot's files and commit the ones that changed; ``commit_curation``
    for ``snapshot/``. None when nothing but ``fetchedAt`` would change."""
    with checkout.lock:
        operator = snapshot.meta.operator
        on_disk = operator in files.snapshot_operators(checkout.path)
        current = files.read_snapshot(checkout.path, operator) if on_disk else None
        if same_content(current, snapshot):
            return None
        before = checkout.head()
        try:
            # Inside the try: the files are written one by one, and a failure
            # after the first must not leave the checkout dirty.
            changed = files.write_snapshot(checkout.path, snapshot)
            if not changed:
                return None
            checkout.repo._git("add", "--", *changed)
            # Only these paths, as a save commits only its own.
            checkout.repo._git(
                "commit", "-q", "--no-verify", "-m", commit_message(snapshot), "--", *changed,
                env=checkout._author_env(),
            )  # fmt: skip
        except BaseException:
            checkout.reset(before)
            raise
        return checkout.head()


# MARK: - Routes

router = APIRouter(prefix="/editor/api", route_class=SessionRoute, tags=["editor"])


@router.post(
    "/snapshot/fetch",
    response_model=SnapshotFetchResponse,
    responses={**UNAUTHORIZED, 409: {"model": Problem}, 503: {"model": Problem}},
)
async def snapshot_fetch(request: Request, body: SnapshotFetchRequest | None = None):
    return json_response(await fetch(editor_of(request), request.app, body or SnapshotFetchRequest()))


@router.post(
    "/snapshot/commit",
    response_model=SaveResponse,
    responses={**UNAUTHORIZED, 404: {"model": Problem}, 409: {"model": SaveConflict}, 503: {"model": Problem}},
)
def snapshot_commit(body: SnapshotCommitRequest, request: Request):
    return json_response(commit(editor_of(request), request.app, body))
