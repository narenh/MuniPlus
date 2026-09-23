"""Reading and writing sf-transit's files.

The on-disk format is part of the contract, because it is what makes the git
history of sf-transit readable:

* two-space indent, UTF-8 (no ``\\u`` escapes), trailing newline;
* keys in model order, and every map keyed by id sorted by that id;
* optional fields that were never stated are omitted, not written as null.

Writing what was read gives back the same bytes, so a one-field edit is a
one-line diff and an unchanged save writes nothing at all.
"""

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel

from .curation import Curation, IgnoredFile, LinesFile, ShapesFile, StationsFile
from .snapshot import Snapshot, SnapshotLines, SnapshotMeta, SnapshotPatterns, SnapshotShapes, SnapshotStops

STATIONS = "curation/stations.json"
LINES = "curation/lines.json"
IGNORED = "curation/ignored.json"
SHAPES = "curation/shapes.json"
SNAPSHOT_DIR = "snapshot"


def snapshot_paths(operator: str) -> dict[str, str]:
    base = f"{SNAPSHOT_DIR}/{operator}"
    return {
        "meta": f"{base}/meta.json",
        "stops": f"{base}/stops.json",
        "lines": f"{base}/lines.json",
        "patterns": f"{base}/patterns.json",
        "shapes": f"{base}/shapes.json",
    }


def dumps(model: BaseModel) -> str:
    data = model.model_dump(mode="json", exclude_defaults=True)
    if isinstance(model, SnapshotShapes):
        return _dumps_shapes(data)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _dumps_shapes(data: dict[str, list[list[float]]]) -> str:
    """One point per line. The general format would spend four lines on each point
    (SFMTA's own 2024 feed has ~44,000); this keeps the file a quarter of that while a
    refresh that re-surveys a stretch of track still diffs as just those points."""
    if not data:
        return "{}\n"
    parts = []
    for shape_id, points in data.items():
        body = ",\n".join(f"    {json.dumps(p)}" for p in points)
        parts.append(f"  {json.dumps(shape_id)}: [\n{body}\n  ]" if points else f"  {json.dumps(shape_id)}: []")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def write_if_changed(path: Path, text: str) -> bool:
    """Atomically replace ``path`` with ``text``, unless it already holds exactly that.

    Returns whether anything was written. The temp file sits in the same directory
    so the rename cannot cross a filesystem.
    """
    data = text.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    # mkstemp creates 0600, and os.replace would carry that onto the data file.
    # These files are public and read by other processes on the volume (git, a
    # shell, a backup), so they get an ordinary file's 0644, or keep the mode the
    # file already had.
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


class DuplicateKeyError(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    # json keeps the last of two identical keys and says nothing, so a hand-edit
    # that pastes a station in twice would lose one copy on the next save without
    # anyone seeing it. Refusing the file is the only place this can be caught:
    # once parsed, the duplicate is already gone.
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise DuplicateKeyError(f"duplicate key {key!r}")
        out[key] = value
    return out


def loads(text: str | bytes) -> object:
    """``json.loads`` that refuses duplicate keys. Use it for anything hand-edited."""
    return json.loads(text, object_pairs_hook=_no_duplicate_keys)


def _read(root: Path, rel: str, model: type[BaseModel], *, missing_ok: bool = False):
    path = root / rel
    if missing_ok and not path.exists():
        return model()
    try:
        data = loads(path.read_bytes())
    except DuplicateKeyError as err:
        raise DuplicateKeyError(f"{rel}: {err}") from None
    return model.model_validate(data)


# MARK: - Curation


def read_curation(root: Path) -> Curation:
    """``lines.json``, ``ignored.json`` and ``shapes.json`` may be absent and read
    as empty; ``stations.json`` may not."""
    return Curation(
        stations=_read(root, STATIONS, StationsFile),
        lines=_read(root, LINES, LinesFile, missing_ok=True),
        ignored=_read(root, IGNORED, IgnoredFile, missing_ok=True),
        shapes=_read(root, SHAPES, ShapesFile, missing_ok=True),
    )


def write_curation(root: Path, curation: Curation) -> list[str]:
    """Write every curation file whose content changed. Returns the paths written,
    relative to ``root``, which is exactly what a commit should stage."""
    changed = []
    parts = ((STATIONS, curation.stations), (LINES, curation.lines), (IGNORED, curation.ignored), (SHAPES, curation.shapes))
    for rel, model in parts:
        if rel == SHAPES and not model.root and not (root / rel).exists():
            # Newer than the other files: a save with no patches must not add an
            # empty one to every checkout that predates it.
            continue
        if write_if_changed(root / rel, dumps(model)):
            changed.append(rel)
    return changed


# MARK: - Snapshot


def snapshot_operators(root: Path) -> list[str]:
    base = root / SNAPSHOT_DIR
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if (p / "meta.json").is_file())


def read_snapshot(root: Path, operator: str) -> Snapshot:
    paths = snapshot_paths(operator)
    return Snapshot(
        meta=_read(root, paths["meta"], SnapshotMeta),
        stops=_read(root, paths["stops"], SnapshotStops),
        lines=_read(root, paths["lines"], SnapshotLines),
        patterns=_read(root, paths["patterns"], SnapshotPatterns),
        # Absent from snapshots committed before shapes were ingested. Those read as
        # no shapes, and the map draws through the platforms until the next refresh.
        shapes=_read(root, paths["shapes"], SnapshotShapes, missing_ok=True),
    )


def write_snapshot(root: Path, snapshot: Snapshot) -> list[str]:
    paths = snapshot_paths(snapshot.meta.operator)
    changed = []
    for key in paths:
        if key == "shapes" and not snapshot.shapes.root and not (root / paths[key]).exists():
            # A feed without shapes, into a snapshot that never had any: an empty
            # file would say nothing that its absence does not.
            continue
        if write_if_changed(root / paths[key], dumps(getattr(snapshot, key))):
            changed.append(paths[key])
    return changed
