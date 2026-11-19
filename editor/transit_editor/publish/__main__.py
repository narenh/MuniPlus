"""``python -m transit_editor.publish --data <checkout> --out <dir>``

Builds the published files from a data checkout (``curation/`` and
``snapshot/``) and writes them to ``<dir>/data/1/``. Exits 1, writing nothing,
if the data fails the editor's validator or the publisher's own checks.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from .build import PublishError
from .files import publish


def summary(result) -> str:
    net = result.network
    modes = Counter(line["mode"] for line in net["lines"])
    platforms = sum(len(s["platforms"]) for s in net["stations"])
    lines = [
        f"wrote {result.directory}",
        f"  source {result.manifest['source']}  published {result.manifest['published']}",
        f"  {len(net['stations'])} stations, {platforms} platforms, {len(net['subways'])} subways, {len(net['styles'])} styles",
        f"  {len(net['lines'])} lines: " + ", ".join(f"{n} {m}" for m, n in sorted(modes.items())),
        f"  {sum(len(d) for d in result.shapes['lines'].values())} shapes",
    ]
    for entry in result.manifest["files"].values():
        lines.append(f"  {entry['path']}  {entry['bytes']:,} bytes")
    if result.warnings:
        lines.append(f"  {len(result.warnings)} validator warnings (published anyway):")
        lines.extend(f"    {w}" for w in result.warnings)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m transit_editor.publish", description=__doc__.split("\n\n")[1])
    parser.add_argument("--data", type=Path, required=True, help="the data checkout (holds curation/ and snapshot/)")
    parser.add_argument("--out", type=Path, required=True, help="output root; files go in <out>/data/1/")
    parser.add_argument("--published", help="the manifest's timestamp (default: the data commit's time)")
    parser.add_argument("--keep-old", action="store_true", help="keep hashed files the new manifest no longer names")
    args = parser.parse_args(argv)
    try:
        result = publish(args.data, args.out, published=args.published, prune=not args.keep_old)
    except PublishError as err:
        print(f"not published: {err}", file=sys.stderr)
        return 1
    print(summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
