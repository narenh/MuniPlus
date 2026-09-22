"""The contract's own guarantees: ids, strictness, and byte-stable files."""

import shutil
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.models import files
from app.models.curation import Curation, Platform, Station, StationsFile
from app.models.ids import LineId, PlatformId, StationId

FIXTURE = Path(__file__).parent / "fixtures" / "transit"


# MARK: - Ids


@pytest.mark.parametrize("value", ["SF:16992", "SF:L", "SF:5R", "SF:LOWL", "BA:12", "BA:EMBR", "CT:70011"])
def test_refs_accept(value):
    TypeAdapter(PlatformId).validate_python(value)
    TypeAdapter(LineId).validate_python(value)


@pytest.mark.parametrize("value", ["16992", "sf:16992", "SF:", ":16992", "SF:16992,16993", "SF:a:b", "SF:a/b", "SF: 1"])
def test_refs_reject(value):
    with pytest.raises(ValidationError):
        TypeAdapter(PlatformId).validate_python(value)


@pytest.mark.parametrize("value", ["embarcadero", "churchMarket", "500Parnassus", "3801SanBruno"])
def test_station_ids_accept(value):
    TypeAdapter(StationId).validate_python(value)


@pytest.mark.parametrize("value", ["", "church-market", "church market", "SF:embarcadero"])
def test_station_ids_reject(value):
    with pytest.raises(ValidationError):
        TypeAdapter(StationId).validate_python(value)


# MARK: - Strictness


def test_unknown_keys_are_refused():
    # A misspelt key in a hand-edited file must fail, not vanish on the next save.
    with pytest.raises(ValidationError):
        Station.model_validate({"name": "X", "platforms": [], "transferAgency": ["BA"]})


def test_blank_names_are_refused():
    with pytest.raises(ValidationError):
        Station.model_validate({"name": " ", "platforms": []})


def test_terminates_is_not_curated():
    with pytest.raises(ValidationError):
        Platform.model_validate({"id": "SF:16992", "heading": "eastbound", "terminates": ["SF:J"]})


# MARK: - Files


def test_fixture_loads():
    curation = files.read_curation(FIXTURE)
    assert "montgomery" in curation.stations.stations
    assert curation.stations.stations["montgomery"].former_ids == ["mongomery"]
    assert curation.lines.root["SF:LOWL"].replaces == ["SF:L"]
    assert files.snapshot_operators(FIXTURE) == ["SF"]
    snapshot = files.read_snapshot(FIXTURE, "SF")
    assert snapshot.lines.root["SF:F"].mode == "metro"


def test_round_trip_is_byte_identical(tmp_path):
    root = tmp_path / "transit"
    shutil.copytree(FIXTURE, root)
    assert files.write_curation(root, files.read_curation(root)) == []
    assert files.write_snapshot(root, files.read_snapshot(root, "SF")) == []
    for path in FIXTURE.rglob("*.json"):
        assert (root / path.relative_to(FIXTURE)).read_bytes() == path.read_bytes()


def test_one_field_edit_is_one_line_diff(tmp_path):
    root = tmp_path / "transit"
    shutil.copytree(FIXTURE, root)
    curation = files.read_curation(root)
    curation.stations.stations["clayDrumm"].name = "Clay St & Drumm St"
    assert files.write_curation(root, curation) == [files.STATIONS]

    before = (FIXTURE / files.STATIONS).read_text().splitlines()
    after = (root / files.STATIONS).read_text().splitlines()
    assert len(before) == len(after)
    assert [(a, b) for a, b in zip(before, after) if a != b] == [
        ('      "name": "Clay & Drumm",', '      "name": "Clay St & Drumm St",')
    ]


def test_maps_are_written_sorted_and_unset_fields_omitted():
    doc = StationsFile(stations={
        "zoo": Station(name="Z", platforms=[]),
        "aardvark": Station(name="A", platforms=[Platform(id="SF:1", heading="northbound")]),
    })
    text = files.dumps(doc)
    assert text.index('"aardvark"') < text.index('"zoo"')
    for unset in ("transfers", "formerIds", "verified", "note", "subways", "null"):
        assert unset not in text


def test_platform_order_is_preserved():
    # Platforms are a display order, not a map: never sorted.
    doc = Station(name="X", platforms=[Platform(id="SF:2", heading="northbound"), Platform(id="SF:1", heading="southbound")])
    assert [p["id"] for p in doc.model_dump()["platforms"]] == ["SF:2", "SF:1"]


def test_curation_round_trips_through_json():
    # What the editor posts is what it was sent.
    curation = files.read_curation(FIXTURE)
    again = Curation.model_validate_json(curation.model_dump_json())
    assert files.dumps(again.stations) == files.dumps(curation.stations)
