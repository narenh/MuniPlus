"""An editor save: validate, check for conflicts, write, commit, push, reload.

The editor posts the whole curation, not a patch, so a save replaces every
curation file. That is what makes concurrency the hard part: if sf-transit's
curation moved after the editor loaded it, writing the posted document would
silently revert that change. So a save is refused (409) unless the curation at
the fetched HEAD is still the one the edit started from (``baseVersion``). A
change to ``snapshot/`` alone does not count: the editor never writes it, so
nothing is reverted.

The push can still race another writer between our fetch and our push. Then the
commit is rebased once onto what arrived and pushed again. That rebase is
textual, so the combination is validated before it is pushed: two edits that
each pass can together put one platform in two stations.

Everything after validation (the lock, fetch, base check, push, retry and network
swap) is shared with a snapshot commit, ``refresh.commit``, which differs only in
which files it writes and which paths it guards.
"""

import logging
import re
from collections.abc import Callable
from typing import TypeVar

from fastapi import FastAPI

from ..data.repo import RepoError
from ..data.validate import validate
from ..models.editor import SaveRequest, SaveResponse
from .checkout import PushResult, RebaseConflict
from .errors import ProblemError
from .models import InvalidSave, SaveConflict
from .state import Editor, loaded

log = logging.getLogger(__name__)

T = TypeVar("T")

SHA = re.compile(r"[0-9a-f]{7,64}")
"""A commit id, abbreviated or full (SHA-1 or SHA-256). Checked before it reaches
git, which would read a value starting with ``-`` as an option."""


def conflict(editor: Editor, message: str, paths: list[str] | None = None) -> ProblemError:
    version = editor.checkout.head()
    body = SaveConflict(error="conflict", message=message, version=version, paths=paths or [])
    return ProblemError(409, "conflict", message, body=body)


def save(editor: Editor, app: FastAPI, request: SaveRequest) -> SaveResponse:
    check_writable(editor, request.base_version)

    # 1. Validate before touching git. Errors never depend on the snapshot (PLAN.md,
    # "Validation"), so the checkout's current snapshot is good enough to reject on,
    # even if a newer one is about to be fetched.
    validation = validate(request.curation, loaded(editor).snapshots)
    if validation.errors:
        message = f"The curation has {len(validation.errors)} validation error(s); nothing was saved."
        body = InvalidSave(error="bad-request", message=message, validation=validation)
        raise ProblemError(422, "bad-request", message, body=body)

    # 2. One save at a time.
    return run_locked(editor, app, lambda: _save(editor, request))


def check_writable(editor: Editor, base_version: str) -> None:
    """Refuse before any work if the editor cannot write, or ``baseVersion`` is not
    even shaped like a commit."""
    if reason := editor.read_only_reason():
        raise ProblemError(503, "unavailable", f"The editor is read-only: {reason}.")
    if not SHA.fullmatch(base_version):
        raise ProblemError(400, "bad-request", f"baseVersion {base_version!r} is not a commit id.")


def run_locked(editor: Editor, app: FastAPI, work: Callable[[], T]) -> T:
    """Run ``work`` under the save lock, then swap the network if HEAD moved.

    Everything that writes the checkout goes through here (saves and snapshot
    commits), so two writers never interleave, and the public API always ends up
    serving whatever the checkout holds."""
    with editor.checkout.save_lock:
        try:
            return work()
        finally:
            # 8. Whatever happened, HEAD may have moved (a commit, or only the
            # fast-forward before a refusal): the public API serves the checkout.
            reload_network(editor, app)


def _save(editor: Editor, request: SaveRequest) -> SaveResponse:
    checkout = editor.checkout

    # 3. Fetch, and fast-forward (or replay an earlier save that never got pushed).
    head = fetch_and_rebase(editor)

    # 4. Refuse if the curation changed underneath the edit.
    require_base(editor, request.base_version)
    if changed := checkout.curation_changed(request.base_version, head):
        raise conflict(
            editor,
            "The curation changed since this edit started. Reload, then redo the edit.",
            changed,
        )

    # 5 and 6. Write through ``files`` and commit what changed.
    commit = checkout.commit_curation(request.curation, request.message)

    # 7. Push whatever the remote lacks.
    return push_and_respond(editor, before=head, commit=commit)


def fetch_and_rebase(editor: Editor) -> str:
    """Bring the checkout up to the remote; returns the new HEAD. Callers hold the
    save lock."""
    checkout = editor.checkout
    try:
        checkout.fetch()
    except RepoError as err:
        # Without the remote there is nothing to check baseVersion against, and a
        # commit made blind could conflict on every later save. Refuse; write nothing.
        raise ProblemError(503, "unavailable", f"Could not fetch sf-transit: {err}") from None
    try:
        checkout.rebase_onto_upstream()
    except RebaseConflict:
        raise conflict(
            editor,
            "An earlier save that was never pushed conflicts with sf-transit. "
            "It is still committed in the server's checkout and needs resolving by hand.",
        ) from None
    return checkout.head()


def require_base(editor: Editor, base_version: str) -> None:
    if not editor.checkout.is_commit(base_version):
        raise conflict(editor, f"baseVersion {base_version} is not in sf-transit's history. Reload.")


def push_and_respond(editor: Editor, *, before: str, commit: str | None) -> SaveResponse:
    """Push whatever the remote lacks: this commit, or one left from an earlier save
    whose push failed. ``before`` is the fetched HEAD this commit was made on, where
    the checkout goes back to if a retry has to be refused."""
    checkout = editor.checkout
    pushed, push_error = True, None
    if checkout.ahead() > 0:
        result = checkout.push()
        if result.rejected:
            result = _rebase_and_retry(editor, before)
        pushed, push_error = result.ok, result.error
        if not result.ok:
            # Kept locally: the public API serves it now and the next save pushes it.
            log.warning("sf-transit push failed; keeping the local commit: %s", push_error)

    at_head = checkout.loaded()
    return SaveResponse(
        version=at_head.version,
        commit=at_head.version if commit else None,
        pushed=pushed,
        push_error=push_error,
        validation=at_head.validation(),
    )


def _rebase_and_retry(editor: Editor, before: str) -> PushResult:
    """The remote moved after our fetch. Rebase once onto it and push again; if the
    rebase does not apply, or its result does not validate, put the checkout back
    to ``before`` (the fetched HEAD, before this save's commit) and refuse."""
    checkout = editor.checkout
    try:
        checkout.fetch()
    except RepoError as err:
        return PushResult(ok=False, error=f"Could not fetch sf-transit to retry the push: {err}")
    with checkout.lock:
        try:
            checkout.rebase_onto_upstream()
        except RebaseConflict:
            checkout.reset(before)
            raise conflict(editor, "Someone else changed the same files at the same time. Reload.") from None
        if checkout.loaded().validation().errors:
            checkout.reset(before)
            raise conflict(
                editor,
                "This save and one made at the same time are each valid but not together. Reload.",
            )
    return checkout.push()


def reload_network(editor: Editor, app: FastAPI) -> None:
    """Replace ``app.state.network`` whole if the checkout is at another version."""
    checkout = editor.checkout
    try:
        current = getattr(app.state, "network", None)
        at_head = checkout.loaded()
        if current is None or current.version != at_head.version:
            app.state.network = at_head.network()
    except Exception:
        # The checkout holds files this server cannot load (a hand edit pushed
        # straight to GitHub, say). Serving the last good network beats serving none.
        log.exception("Could not reload the network from the sf-transit checkout")
