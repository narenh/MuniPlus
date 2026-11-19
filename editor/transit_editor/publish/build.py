"""The published documents (docs/data-format.md), built from a loaded ``Network``.

Everything here is a pure function of the network and the curation it was built
from: no clock, no git, no file system. The same data always gives the same
documents, key for key, which is what makes the file hashes stable.

Keys are written in the order the spec lists them, and every collection keyed by
data (``styles``, the lines in ``shapes.json``) is sorted, so the byte order never
depends on dict insertion order upstream.
"""

from ..models.curation import Curation, LineStyle
from ..models.api import LineDetail, StationDetail
from ..models.ids import upstream_of
from ..data.network import Network

FORMAT = 1

AGENCY = {"id": "SF", "name": "Muni"}

MODES = {
    "metro": "lightRail",
    "streetcar": "streetcar",
    "bus": "bus",
    "cableway": "cableCar",
}
"""Editor mode (511's ``TransportMode``, or curation's) -> published ``mode``."""

AGENCIES = {"BA": "bart", "CT": "caltrain"}
"""511 operator code -> published agency. Any other code is published lower-cased."""

COORDINATE_PLACES = 6


class PublishError(ValueError):
    """The data cannot be published as it stands. The message says why."""


def _id(ref: str) -> str:
    """``SF:16992`` -> ``16992``: the agency is given once, at the top."""
    return upstream_of(ref)


def _coordinate(lat: float, lon: float) -> dict:
    return {"lat": round(lat, COORDINATE_PLACES), "lon": round(lon, COORDINATE_PLACES)}


def _color_pair(light: str, dark: str) -> dict:
    return {"light": light, "dark": dark}


def _style(style: LineStyle) -> dict:
    out = {
        "fill": _color_pair(style.fill.light, style.fill.dark),
        "text": _color_pair(style.text.light, style.text.dark),
    }
    if style.text_edge:
        out["textEdge"] = _color_pair(style.text_edge.light, style.text_edge.dark)
    return out


def _own_style(line: LineDetail) -> tuple[str, dict]:
    """The style a line is drawn with when it replaces nothing: (style id, style).

    A curated style (the Metro, the F, the S) is the palette's, keyed by the
    line's id. Every other line (buses, cable cars) gets one of its own from 511's
    colours, the same in light and dark.

    To move buses onto shared styles (``bus-local``, ``bus-rapid``) later, return
    the shared id and style here for ``line.mode == "bus"``: nothing else changes.
    """
    key = _id(line.id)
    if line.style:
        return key, _style(line.style)
    if not line.color or not line.text_color:
        raise PublishError(f"Line {line.id} has no style and no 511 colours to make one from.")
    return key, {
        "fill": _color_pair(line.color, line.color),
        "text": _color_pair(line.text_color, line.text_color),
    }


def build_network(network: Network, curation: Curation) -> dict:
    """``network.json``."""
    derived = network.derived()
    # Hidden lines are never published, and nothing published may list them.
    lines = [line for line in derived.lines.values() if not line.hidden]
    published_lines = {line.id for line in lines}
    listed = {line.id for line in lines if line.listed_at_stations}

    # Styles: each line's own, then a replacement takes its first replaced line's.
    styles: dict[str, dict] = {}
    own: dict[str, str] = {}
    for line in lines:
        key, style = _own_style(line)
        own[line.id] = key
        styles.setdefault(key, style)

    def replaced(line: LineDetail) -> str | None:
        # The spec has one string; curation allows a list. The first one is
        # published, and only if it is a published line.
        first = line.replaces[0] if line.replaces else None
        return first if first in published_lines else None

    out_lines = []
    for line in lines:
        if line.mode not in MODES:
            raise PublishError(f"Line {line.id} has mode {line.mode!r}, which the format has no value for.")
        target = replaced(line)
        directions = []
        for d in line.directions:
            platforms: list[str] = []
            for stop in d.stops:
                # A direction lists 511 stops; a platform may own several of them.
                pid = network.platform_of(stop)
                if pid is None:
                    continue
                pid = _id(pid)
                if not platforms or platforms[-1] != pid:
                    platforms.append(pid)
            directions.append({"id": str(d.direction), "headsign": d.headsign, "platforms": platforms})
        out_lines.append({
            "id": _id(line.id),
            "name": line.name,
            "badge": line.short_name,
            "mode": MODES[line.mode],
            "style": own[target] if target else own[line.id],
            "drawn": "outlined" if target else "solid",
            "replaces": _id(target) if target else None,
            "listedAtStations": line.listed_at_stations,
            "owl": line.owl,
            "directions": directions,
        })
    used = {line["style"] for line in out_lines}
    styles = {key: styles[key] for key in sorted(used)}

    curated = curation.stations.stations
    in_subway = {sid for subway in curation.stations.subways.values() for sid in subway.stations}
    out_stations = []
    for summary in sorted(network.stations().stations, key=lambda s: s.id):
        detail: StationDetail = network.station(summary.id)
        station = curated[summary.id]
        out_stations.append({
            "id": detail.id,
            "name": detail.name,
            "kind": "underground" if detail.id in in_subway else "streetLevel",
            "coordinate": _coordinate(detail.lat, detail.lon),
            "formerIds": list(station.former_ids),
            "agencies": [AGENCIES.get(op, op.lower()) for op in station.transfer_agencies],
            "transfers": [{"station": t.to, "mode": t.mode} for t in detail.transfers],
            "platforms": [
                {
                    "id": _id(p.id),
                    "stops": [_id(s) for s in p.stops],
                    "formerIds": [_id(f) for f in p.former_ids],
                    "heading": p.heading,
                    "coordinate": _coordinate(p.lat, p.lon),
                    "lines": [_id(lid) for lid in p.lines if lid in listed],
                    "label": p.name,
                    "terminus": p.terminus,
                }
                for p in detail.platforms
            ],
        })

    return {
        "format": FORMAT,
        "agency": dict(AGENCY),
        "styles": styles,
        "lines": out_lines,
        "stations": out_stations,
        "subways": [
            {"id": s.id, "name": s.name, "stations": list(s.stations)} for s in network.stations().subways
        ],
    }


def build_shapes(network: Network) -> dict:
    """``shapes.json``: each published line direction's path, as ``[lat, lon]``.

    The editor's shapes are ``[lon, lat]`` (GeoJSON order) and simplified to half
    a metre; this swaps them and rounds to six places. A direction with no shape
    (a hand-entered line such as the S) is left out.
    """
    paths, _ = network.shapes()
    out: dict[str, dict[str, list]] = {}
    for line in network.derived().lines.values():
        if line.hidden:
            continue
        for d in line.directions:
            if d.shape and d.shape in paths.shapes:
                out.setdefault(_id(line.id), {})[str(d.direction)] = [
                    [round(lat, COORDINATE_PLACES), round(lon, COORDINATE_PLACES)] for lon, lat in paths.shapes[d.shape]
                ]
    return {"format": FORMAT, "lines": {lid: dict(sorted(dirs.items())) for lid, dirs in sorted(out.items())}}
