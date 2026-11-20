"""The contract's own guarantees: ids, strictness, and byte-stable files."""

import shutil
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from transit_editor.models import files
from transit_editor.models.curation import Curation, LineOverride, LinesFile, LineStyle, Platform, Station, StationsFile
from transit_editor.models.ids import LineId, StopId, StationId, TripId, VehicleId

FIXTURE = Path(__file__).parent / "fixtures" / "transit"


# MARK: - Ids


REFS = (StopId, LineId, TripId, VehicleId)


@pytest.mark.parametrize("value", [
    "SF:16992", "SF:L", "SF:5R", "SF:LOWL", "BA:12", "BA:EMBR", "CT:70011", "SF:12134484_M11", "SF:2019",
])  # fmt: skip
@pytest.mark.parametrize("kind", REFS)
def test_refs_accept(kind, value):
    TypeAdapter(kind).validate_python(value)


@pytest.mark.parametrize("value", ["16992", "sf:16992", "SF:", ":16992", "SF:16992,16993", "SF:a:b", "SF:a/b", "SF: 1"])
@pytest.mark.parametrize("kind", REFS)
def test_refs_reject(kind, value):
    with pytest.raises(ValidationError):
        TypeAdapter(kind).validate_python(value)


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


def test_duplicate_keys_are_refused(tmp_path):
    # json would keep the second "clayDrumm" and drop the first without a word.
    root = tmp_path / "transit"
    shutil.copytree(FIXTURE, root)
    path = root / files.STATIONS
    text = path.read_text()
    block = text[text.index('    "clayDrumm": {'):text.index('    "embarcadero": {')]
    path.write_text(text.replace(block, block + block))
    with pytest.raises(files.DuplicateKeyError, match="clayDrumm"):
        files.read_curation(root)


def test_editor_lines_carry_directions():
    from transit_editor.models.api import LineDetail
    from transit_editor.models.editor import Derived
    from typing import get_args
    assert get_args(Derived.model_fields["lines"].annotation)[1] is LineDetail


def test_written_files_are_world_readable(tmp_path):
    root = tmp_path / "transit"
    files.write_curation(root, files.read_curation(FIXTURE))
    assert ((root / files.STATIONS).stat().st_mode & 0o777) == 0o644


# MARK: - Terminus


def test_terminus_is_written_only_when_true():
    off = Platform(id="SF:1", heading="northbound")
    on = Platform(id="SF:1", heading="northbound", name="Platform 1", terminus=True)
    assert "terminus" not in files.dumps(Station(name="X", platforms=[off]))
    assert files.dumps(Station(name="X", platforms=[on])).splitlines()[3:9] == [
        '    {',
        '      "id": "SF:1",',
        '      "heading": "northbound",',
        '      "name": "Platform 1",',
        '      "terminus": true',
        '    }',
    ]
    # Never words in the signage.
    assert on.name == "Platform 1"


def test_terminus_round_trips_byte_identically(tmp_path):
    root = tmp_path / "transit"
    shutil.copytree(FIXTURE, root)
    curation = files.read_curation(root)
    curation.stations.stations["powell"].platforms[0].terminus = True
    assert files.write_curation(root, curation) == [files.STATIONS]
    text = (root / files.STATIONS).read_text()
    assert text.count('"terminus"') == 1

    again = files.read_curation(root)
    assert again.stations.stations["powell"].platforms[0].terminus is True
    assert files.write_curation(root, again) == []
    assert (root / files.STATIONS).read_text() == text
    # Through JSON too, as the editor posts it.
    assert files.dumps(Curation.model_validate_json(again.model_dump_json()).stations) == files.dumps(again.stations)


def test_arrival_stops_round_trip_and_default_to_absent(tmp_path):
    root = tmp_path / "transit"
    shutil.copytree(FIXTURE, root)
    curation = files.read_curation(root)
    # Existing data has none, and an unchanged save writes nothing.
    assert all(p.arrival_stops == [] for st in curation.stations.stations.values() for p in st.platforms)
    assert files.write_curation(root, curation) == []
    curation.stations.stations["powell"].platforms[0].arrival_stops = ["SF:15418"]
    assert files.write_curation(root, curation) == [files.STATIONS]
    text = (root / files.STATIONS).read_text()
    assert text.count('"arrivalStops"') == 1

    again = files.read_curation(root)
    assert again.stations.stations["powell"].platforms[0].arrival_stops == ["SF:15418"]
    assert files.write_curation(root, again) == []
    assert (root / files.STATIONS).read_text() == text
    assert files.dumps(Curation.model_validate_json(again.model_dump_json()).stations) == files.dumps(again.stations)


# MARK: - Line styles, hand-entered directions, listedAtStations


S_SHUTTLE = """  "SF:S": {
    "name": "S Shuttle",
    "color": "#FCDB00",
    "mode": "metro",
    "style": {
      "fill": {
        "light": "#FCDB00",
        "dark": "#FCDB00"
      },
      "text": {
        "light": "#FFFFFF",
        "dark": "#FFFFFF"
      },
      "textEdge": {
        "light": "#A88400",
        "dark": "#A88400"
      }
    },
    "directions": [
      {
        "headsign": "West Portal",
        "platforms": [
          "SF:17217",
          "SF:16994"
        ]
      },
      {
        "headsign": "Embarcadero",
        "platforms": [
          "SF:15731",
          "SF:16992"
        ]
      }
    ],
    "listedAtStations": false
  }
"""


def test_style_directions_and_listing_round_trip_byte_identically(tmp_path):
    root = tmp_path / "transit"
    shutil.copytree(FIXTURE, root)
    path = root / files.LINES
    text = path.read_text()
    # After the last entry, SF:NOWL, which sorts before SF:S.
    text = text[: text.rindex("}")].rstrip() + ",\n" + S_SHUTTLE + "}\n"
    path.write_text(text)

    curation = files.read_curation(root)
    s = curation.lines.root["SF:S"]
    assert s.style.text_edge.dark == "#A88400" and s.listed_at_stations is False
    assert [d.platforms for d in s.directions] == [["SF:17217", "SF:16994"], ["SF:15731", "SF:16992"]]
    assert files.write_curation(root, curation) == []
    assert path.read_text() == text
    # Through JSON too, as the editor posts it (with every unset field spelt out).
    again = Curation.model_validate_json(curation.model_dump_json())
    assert files.dumps(again.lines) == text


def test_lines_without_the_new_fields_omit_them(tmp_path):
    text = files.dumps(files.read_curation(FIXTURE).lines)
    for unset in ("style", "directions", "listedAtStations", "null"):
        assert unset not in text
    assert files.dumps(LinesFile({"SF:K": LineOverride(listed_at_stations=True)})) == '{\n  "SF:K": {}\n}\n'


@pytest.mark.parametrize("bad", ["#fcdb00", "FCDB00", "#FCDB0", "#FCDB00 ", "yellow"])
def test_style_colours_are_upper_case_hex(bad):
    with pytest.raises(ValidationError):
        LineStyle.model_validate({"fill": {"light": bad, "dark": "#FCDB00"}, "text": {"light": "#FFFFFF", "dark": "#FFFFFF"}})


@pytest.mark.parametrize("style", [
    {"fill": {"light": "#FCDB00", "dark": "#FCDB00"}},
    {"fill": {"light": "#FCDB00"}, "text": {"light": "#FFFFFF", "dark": "#FFFFFF"}},
    {"fill": {"light": "#FCDB00", "dark": "#FCDB00"}, "text": {"light": "#FFFFFF", "dark": "#FFFFFF"}, "edge": None},
])  # fmt: skip
def test_style_needs_fill_and_text_in_both_appearances(style):
    with pytest.raises(ValidationError):
        LineStyle.model_validate(style)


@pytest.mark.parametrize("directions", [
    [{"headsign": "X", "platforms": ["SF:1"]}],
    [{"headsign": " ", "platforms": ["SF:1", "SF:2"]}],
    [{"headsign": "X", "platforms": ["SF:1", "2"]}],
    [{"headsign": "X", "platforms": ["SF:1", "SF:2"]}] * 3,
])  # fmt: skip
def test_hand_entered_directions_are_checked(directions):
    with pytest.raises(ValidationError):
        LineOverride.model_validate({"directions": directions})
