"""Reading a network off disk.

The only place that turns an sf-transit checkout into a ``Network``. Files are read
through ``app.models.files``, the one module that knows their layout.
"""

from collections.abc import Iterable
from pathlib import Path

from ..models import files
from ..models.curation import Curation
from ..models.snapshot import Snapshot
from .network import Network
from .repo import Repo


def read(root: Path, operators: Iterable[str] | None = None) -> tuple[Curation, dict[str, Snapshot]]:
    """The curation and the snapshots under ``root``.

    ``operators`` limits which snapshots are read (``settings.operators``); None
    reads every one on disk. An operator asked for but with no snapshot is simply
    absent: its curated platforms then read as not live, which the validator
    reports, rather than the whole network failing to load.
    """
    on_disk = files.snapshot_operators(root)
    wanted = on_disk if operators is None else [op for op in on_disk if op in set(operators)]
    return files.read_curation(root), {op: files.read_snapshot(root, op) for op in wanted}


def load(root: Path, version: str, operators: Iterable[str] | None = None) -> Network:
    """Build the network from the files under ``root``, labelled ``version``."""
    curation, snapshots = read(root, operators)
    return Network(curation, snapshots, version)


def load_checkout(repo: Repo, operators: Iterable[str] | None = None) -> Network:
    """Build the network from the sf-transit checkout, labelled with its HEAD."""
    return load(repo.path, repo.head(), operators)
