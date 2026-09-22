"""Shapes the editor server needs that ``app.models.editor`` does not have yet.

They belong in the contract; they live here until it takes them, so that no
track edits ``app/models/`` behind wave 0's back.
"""

from ..models.api import Problem
from ..models.base import Wire
from ..models.editor import Validation


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
