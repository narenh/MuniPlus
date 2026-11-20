"""The publisher (docs/data-format.md), against tests/fixtures/transit."""

import json
from pathlib import Path

import pytest

from transit_editor.data import loader
from transit_editor.data.network import Network
from transit_editor.models.curation import LineOverride, LineStyle
from transit_editor.publish import PublishError, build, build_network, build_shapes, publish
from transit_editor.publish.__main__ import main
from transit_editor.publish.check import check
from transit_editor.publish.files import digest, encode

FIXTURE = Path(__file__).parent.parent / "fixtures" / "transit"


@pytest.fixture
def curation():
    return loader.read(FIXTURE)[0]


@pytest.fixture
def snapshots():
    return loader.read(FIXTURE)[1]


def documents(curation, snapshots) -> tuple[dict, dict]:
    network = Network(curation, snapshots, "v")
    return build_network(network, curation), build_shapes(network)


@pytest.fixture
def doc(curation, snapshots) -> dict:
    return documents(curation, snapshots)[0]


def by_id(items: list[dict]) -> dict[str, dict]:
    return {item["id"]: item for item in items}


# MARK: - network.json


def test_fixture_publishes_clean(curation, snapshots):
    network, shapes = documents(curation, snapshots)
    assert check(network, shapes) == []
    assert network["format"] == 1
    assert network["agency"] == {"id": "SF", "name": "Muni"}


def test_ids_drop_the_agency_prefix(doc):
    ids = [line["id"] for line in doc["lines"]]
    ids += [p["id"] for s in doc["stations"] for p in s["platforms"]]
    ids += [lid for s in doc["stations"] for p in s["platforms"] for lid in p["lines"]]
    ids += [pid for line in doc["lines"] for d in line["directions"] for pid in d["platforms"]]
    assert ids and not [i for i in ids if ":" in i]


def test_modes(doc):
    modes = {line["id"]: line["mode"] for line in doc["lines"]}
    assert modes["J"] == "lightRail"
    assert modes["F"] == "streetcar"
    assert modes["PH"] == "cableCar"
    assert modes["1"] == "bus"


def test_line_fields(doc):
    j = by_id(doc["lines"])["J"]
    assert (j["name"], j["badge"], j["style"], j["drawn"], j["replaces"]) == ("J Church", "J", "J", "solid", None)
    assert [d["id"] for d in j["directions"]] == ["0", "1"]
    assert j["directions"][0]["headsign"] == "Balboa Park"
    assert j["listedAtStations"] is True and j["owl"] is False


def test_curated_style_is_the_palette(curation, snapshots):
    curation.lines.root["SF:J"].style = LineStyle.model_validate({
        "fill": {"light": "#FAA633", "dark": "#FAA633"},
        "text": {"light": "#FFFFFF", "dark": "#FFFFFF"},
        "textEdge": {"light": "#C98A00", "dark": "#C98A00"},
    })
    doc, _ = documents(curation, snapshots)
    assert doc["styles"]["J"] == {
        "fill": {"light": "#FAA633", "dark": "#FAA633"},
        "text": {"light": "#FFFFFF", "dark": "#FFFFFF"},
        "textEdge": {"light": "#C98A00", "dark": "#C98A00"},
    }


def test_uncurated_line_gets_its_511_colours(doc):
    assert doc["styles"]["5R"] == {
        "fill": {"light": "#BF2B45", "dark": "#BF2B45"},
        "text": {"light": "#FFFFFF", "dark": "#FFFFFF"},
    }


def test_replacement_is_outlined_in_the_replaced_lines_style(doc):
    lowl = by_id(doc["lines"])["LOWL"]
    assert (lowl["replaces"], lowl["drawn"], lowl["style"], lowl["owl"]) == ("L", "outlined", "L", True)
    # Its own style is never needed, so it is not published.
    assert "LOWL" not in doc["styles"]


def test_replaces_publishes_the_first_entry(curation, snapshots):
    curation.lines.root["SF:LOWL"].replaces = ["SF:N", "SF:L"]
    doc, _ = documents(curation, snapshots)
    assert by_id(doc["lines"])["LOWL"]["style"] == "N"


def test_hidden_line_is_left_out_everywhere(curation, snapshots):
    curation.lines.root["SF:5R"] = LineOverride(hidden=True)
    doc, shapes = documents(curation, snapshots)
    assert "5R" not in by_id(doc["lines"])
    assert "5R" not in doc["styles"]
    assert not [p for s in doc["stations"] for p in s["platforms"] if "5R" in p["lines"]]
    assert check(doc, shapes) == []


def test_line_not_listed_at_stations_keeps_its_directions(curation, snapshots):
    curation.lines.root["SF:J"].listed_at_stations = False
    doc, shapes = documents(curation, snapshots)
    assert not [p for s in doc["stations"] for p in s["platforms"] if "J" in p["lines"]]
    assert by_id(doc["lines"])["J"]["directions"][0]["platforms"]
    assert check(doc, shapes) == []


def test_station_fields(doc):
    stations = by_id(doc["stations"])
    embarcadero = stations["embarcadero"]
    assert embarcadero["kind"] == "underground"
    assert embarcadero["agencies"] == ["bart"]
    assert stations["castroPlaza"]["kind"] == "streetLevel"
    platform = embarcadero["platforms"][0]
    assert list(platform) == [
        "id", "stops", "arrivalStops", "formerIds", "heading", "coordinate", "lines", "label", "terminus",
    ]
    assert platform["stops"][0] == platform["id"]
    assert set(platform["coordinate"]) == {"lat", "lon"}


def test_arrival_stops_are_always_present(curation, snapshots, doc):
    assert all(p["arrivalStops"] == [] for s in doc["stations"] for p in s["platforms"])
    # SF:15418 (Balboa Park BART) is where 511 ends trips; SF:99998 is not in 511.
    curation.stations.stations["powell"].platforms[0].arrival_stops = ["SF:15418", "SF:99998"]
    network, shapes = documents(curation, snapshots)
    powell = by_id(network["stations"])["powell"]
    assert powell["platforms"][0]["arrivalStops"] == ["15418"]
    assert "15418" not in powell["platforms"][0]["stops"]
    assert check(network, shapes) == []


def test_check_finds_an_arrival_stop_that_boards(doc):
    platform = doc["stations"][0]["platforms"][0]
    platform["arrivalStops"] = [platform["id"]]
    assert any("also a platform's stop" in p for p in check(doc, {"format": 1, "lines": {}}))


def test_direction_platforms_are_platform_ids(doc):
    platforms = {p["id"] for s in doc["stations"] for p in s["platforms"]}
    listed = [pid for line in doc["lines"] for d in line["directions"] for pid in d["platforms"]]
    assert listed and set(listed) <= platforms


# MARK: - shapes.json


def test_shapes_are_lat_lon(curation, snapshots):
    _, shapes = documents(curation, snapshots)
    points = [pt for dirs in shapes["lines"].values() for path in dirs.values() for pt in path]
    assert points
    assert all(37 < lat < 38.5 and -123 < lon < -122 for lat, lon in points)
    assert all(round(v, 6) == v for pt in points for v in pt)


# MARK: - check


def test_check_finds_dangling_references(doc):
    doc["lines"][0]["directions"][0]["platforms"].append("99999")
    doc["stations"][0]["platforms"] = []
    problems = check(doc, {"format": 1, "lines": {}})
    assert any("99999" in p for p in problems)
    assert any("has no platforms" in p for p in problems)


def test_check_finds_duplicate_ids(doc):
    doc["stations"].append(dict(doc["stations"][0]))
    assert any("appears 2 times" in p for p in check(doc, {"format": 1, "lines": {}}))


# MARK: - Files


def test_publish_writes_hash_named_files(tmp_path):
    result = publish(FIXTURE, tmp_path)
    directory = tmp_path / "data" / "1"
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest == result.manifest
    for name in ("network", "shapes"):
        entry = manifest["files"][name]
        body = (directory / entry["path"]).read_bytes()
        assert entry["path"] == f"{name}.{digest(body)[:16]}.json"
        assert entry["sha256"] == digest(body) and entry["bytes"] == len(body)
        assert json.loads(body) == getattr(result, name)


def test_publish_is_deterministic(tmp_path):
    publish(FIXTURE, tmp_path / "a", published="2026-01-01T00:00:00Z")
    publish(FIXTURE, tmp_path / "b", published="2026-01-01T00:00:00Z")
    a, b = tmp_path / "a" / "data" / "1", tmp_path / "b" / "data" / "1"
    assert sorted(p.name for p in a.iterdir()) == sorted(p.name for p in b.iterdir())
    for f in a.iterdir():
        assert f.read_bytes() == (b / f.name).read_bytes()


def test_encode_is_compact_utf8():
    assert encode({"b": "é", "a": [1, 2]}) == '{"b":"é","a":[1,2]}'.encode()


def test_publish_prunes_old_files(tmp_path):
    directory = tmp_path / "data" / "1"
    directory.mkdir(parents=True)
    (directory / "network.0123456789abcdef.json").write_text("{}")
    (directory / "README").write_text("kept")
    publish(FIXTURE, tmp_path)
    assert not (directory / "network.0123456789abcdef.json").exists()
    assert (directory / "README").exists()


def test_validation_errors_block_publishing(tmp_path):
    data = tmp_path / "data"
    for f in FIXTURE.rglob("*.json"):
        target = data / f.relative_to(FIXTURE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f.read_bytes())
    stations = json.loads((data / "curation" / "stations.json").read_text())
    # One stop in two stations is a validator error.
    stations["stations"]["castroPlaza"]["platforms"].append(stations["stations"]["embarcadero"]["platforms"][0])
    (data / "curation" / "stations.json").write_text(json.dumps(stations))
    with pytest.raises(PublishError, match="validation errors"):
        build(data)
    assert main(["--data", str(data), "--out", str(tmp_path / "out")]) == 1
    assert not (tmp_path / "out").exists()


def test_cli(tmp_path, capsys):
    assert main(["--data", str(FIXTURE), "--out", str(tmp_path)]) == 0
    assert "8 stations" in capsys.readouterr().out
    assert (tmp_path / "data" / "1" / "manifest.json").exists()
