#!/usr/bin/env python3
"""Write the reference-data snapshot an app bundles for CanopyTransitKit.

    python3 make_snapshot.py --base-url https://muni-staging.canopysf.com \
        --out ../../../../MuniUnderground/Shared/canopy-snapshot.json

Asks the API for /stations, /lines and every line's /lines/{id}, and records
each body with the ETag it came with, in the format `Snapshot.swift` reads. A
first launch with no network shows this; the first revalidation sends its ETags,
so a snapshot that is still current costs a 304 and no download.

Standard library only. Needs no key: the API is public.
"""

import argparse
import gzip
import json
import sys
import time
import urllib.parse
import urllib.request

FORMAT = 1


def fetch(base: str, path: str) -> tuple[dict, str | None]:
    url = base.rstrip("/") + "/api/v1/" + path
    # Cloudflare turns away urllib's own User-Agent with a 403.
    headers = {"Accept": "application/json", "Accept-Encoding": "gzip", "User-Agent": "CanopyTransitKit-snapshot/1"}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return json.loads(body), response.headers.get("ETag")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True, help="the Muni+ API, without /api/v1")
    parser.add_argument("--out", required=True, help="where to write the snapshot")
    parser.add_argument("--lines", default="all", help="line ids whose diagrams to include, comma-separated, or 'all'")
    args = parser.parse_args()

    stations, stations_tag = fetch(args.base_url, "stations")
    lines, lines_tag = fetch(args.base_url, "lines")
    if not stations["stations"] or not lines["lines"]:
        print("the API sent no stations or no lines; not writing a snapshot", file=sys.stderr)
        return 1
    if stations["version"] != lines["version"]:
        # The data changed between the two requests. Harmless, but a rerun gives one version.
        print(f"warning: stations are {stations['version']}, lines {lines['version']}", file=sys.stderr)

    wanted = [line["id"] for line in lines["lines"]] if args.lines == "all" else args.lines.split(",")
    details = {}
    for line_id in wanted:
        body, tag = fetch(args.base_url, "lines/" + urllib.parse.quote(line_id, safe=":"))
        details[line_id] = {"etag": tag, "body": body}

    snapshot = {
        "format": FORMAT,
        "createdAt": int(time.time()),
        "baseURL": args.base_url,
        "stations": {"etag": stations_tag, "body": stations},
        "lines": {"etag": lines_tag, "body": lines},
        "lineDetails": details,
    }
    with open(args.out, "w", encoding="utf-8") as out:
        json.dump(snapshot, out, ensure_ascii=False, separators=(",", ":"))
        out.write("\n")
    print(f"{args.out}: {len(stations['stations'])} stations, {len(lines['lines'])} lines, "
          f"{len(details)} line diagrams, data {stations['version'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
