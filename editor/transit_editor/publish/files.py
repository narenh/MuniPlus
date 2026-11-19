"""Writing the published files: ``manifest.json`` and the hash-named data files.

Layout, under the output root (the ``static`` branch's ``public/`` when the
editor publishes):

    data/1/manifest.json
    data/1/network.<hash>.json
    data/1/shapes.<hash>.json

``<hash>`` is the first 16 hex characters of the file's SHA-256. The data files
are written before the manifest, and the manifest is replaced atomically, so a
reader never sees a manifest naming a file that is not there yet.
"""

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..data.network import Network
from ..data.validate import validate
from ..data.loader import read
from .build import FORMAT, PublishError, build_network, build_shapes
from .check import check

HASH_CHARS = 16
NAMES = ("network", "shapes")
HASHED = re.compile(rf"^({'|'.join(NAMES)})\.[0-9a-f]{{{HASH_CHARS}}}\.json$")


def encode(document: dict) -> bytes:
    """The one serialisation: compact, UTF-8, keys in the order they were built.

    ``build`` constructs every object in a fixed key order, so this needs no
    ``sort_keys``, and the files read in the spec's order."""
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class Published:
    directory: Path
    manifest: dict
    network: dict
    shapes: dict
    warnings: list[str]
    """The editor validator's warnings: published anyway, but worth a look."""


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def source_of(root: Path) -> tuple[str | None, str | None]:
    """The data checkout's commit and its commit time (UTC, ``…Z``), if it is a
    git checkout. The commit time rather than the clock, so publishing the same
    commit twice gives the same manifest."""
    commit = _git(root, "rev-parse", "HEAD")
    when = _git(root, "log", "-1", "--format=%cI", "HEAD") if commit else None
    if when:
        when = datetime.fromisoformat(when).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return commit, when


def build(data_root: Path, version: str = "") -> tuple[dict, dict, list[str]]:
    """``(network.json, shapes.json, validator warnings)`` for the checkout at
    ``data_root``. Raises ``PublishError`` if the editor's validator reports an
    error or the documents fail ``check``."""
    curation, snapshots = read(data_root)
    validation = validate(curation, snapshots)
    if validation.errors:
        raise PublishError(
            "The data has validation errors:\n" + "\n".join(f"  {i.code}: {i.message}" for i in validation.errors)
        )
    network = Network(curation, snapshots, version)
    doc, shapes = build_network(network, curation), build_shapes(network)
    if problems := check(doc, shapes):
        raise PublishError("The published files would be inconsistent:\n" + "\n".join(f"  {p}" for p in problems))
    return doc, shapes, [f"{i.code}: {i.message}" for i in validation.warnings]


def _write_atomic(path: Path, body: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def publish(data_root: Path, out_root: Path, *, published: str | None = None, prune: bool = True) -> Published:
    """Build and write the files for the checkout at ``data_root`` into
    ``out_root/data/1/``. Returns what was written.

    ``published`` overrides the manifest's timestamp (default: the data commit's
    time, or now if ``data_root`` is not a git checkout). With ``prune``, hashed
    files the new manifest does not name are removed from the directory.
    """
    commit, when = source_of(data_root)
    doc, shapes, warnings = build(data_root, commit or "")

    directory = out_root / "data" / str(FORMAT)
    directory.mkdir(parents=True, exist_ok=True)
    entries = {}
    for name, document in (("network", doc), ("shapes", shapes)):
        body = encode(document)
        if json.loads(body) != document:
            raise PublishError(f"{name}.json does not round-trip through JSON")
        sha = digest(body)
        path = f"{name}.{sha[:HASH_CHARS]}.json"
        if not (directory / path).exists() or (directory / path).read_bytes() != body:
            _write_atomic(directory / path, body)
        entries[name] = {"path": path, "sha256": sha, "bytes": len(body)}

    manifest = {
        "format": FORMAT,
        "published": published or when or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": commit,
        "files": entries,
    }
    _write_atomic(directory / "manifest.json", json.dumps(manifest, indent=2).encode() + b"\n")

    if prune:
        current = {e["path"] for e in entries.values()}
        for f in directory.iterdir():
            if HASHED.match(f.name) and f.name not in current:
                f.unlink()

    return Published(directory=directory, manifest=manifest, network=doc, shapes=shapes, warnings=warnings)
