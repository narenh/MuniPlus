"""The seed's conversion rules, on a few stations shaped like the real inputs."""

from datetime import date
from pathlib import Path

from app.models import files
from scripts.seed import Inputs, Report, check, convert

FIXTURE = Path(__file__).parent.parent / "fixtures" / "transit"
DAY = date(2026, 9, 22)


def p(code, heading, name=None, **extra):
    return {"id": code, "heading": heading, "name": name, "latitude": 37.0, "longitude": -122.0, "lines": ["J"], **extra}


METRO = {
    "lines": [
        {"id": "J", "name": "J Church", "shortName": "J", "color": "#FAA633", "stationIds": []},
        {"id": "S", "name": "S Shuttle", "shortName": "S", "color": "#FCDB00", "stationIds": [], "hidden": True},
    ],
    "subways": [{"id": "marketStreetSubway", "name": "Market Subway", "stationIds": ["embarcadero", "mongomery"]}],
    "stations": [
        {
            "id": "embarcadero", "name": "Embarcadero", "lines": [], "latitude": None, "longitude": None,
            "levels": [{"id": -1, "name": "Muni Metro", "agency": "muni", "isIsland": None,
                        "platforms": [p("16992", "eastbound", terminates=["J"]), p("17217", "westbound")]}],
            "exits": [], "transfers": [{"to": "mongomery", "mode": "street"}], "transferAgencies": ["bart"],
        },
        {
            "id": "mongomery", "name": "Montgomery", "lines": [], "latitude": None, "longitude": None,
            "levels": [{"id": -1, "name": "Muni Metro", "agency": "muni", "isIsland": None,
                        "platforms": [p("15731", "eastbound"), p("16994", "westbound", "To Castro")]}],
            "exits": [], "transfers": [], "transferAgencies": ["bart", "ferry"],
        },
        {
            "id": "churchMarket", "name": "Church & Market", "lines": [], "latitude": 37.0, "longitude": -122.0,
            "platforms": [p("17073", "northbound", stopName="Church St & Market St"), p("18059", "southbound")],
            "transfers": [], "transferAgencies": [],
        },
    ],
}


def b(code, heading):
    return {"id": code, "heading": heading, "name": None, "latitude": 37.0, "longitude": -122.0}


BUS = {
    "lines": [],
    "subways": [],
    "stations": [
        # Same id as a data.json station: adds 15662 only, and its own name, transfers
        # and the westbound it gives 18059 are all ignored.
        {"id": "churchMarket", "name": "Church St & Market St", "kind": "streetLevel", "lines": [], "latitude": 0, "longitude": 0,
         "transferStations": ["embarcadero"], "transferAgencies": [],
         "platforms": [b("17073", "northbound"), b("18059", "westbound"), b("15662", "eastbound")]},
        {"id": "marketMontgomery", "name": "Market & Montgomery", "kind": "streetLevel", "lines": [], "latitude": 0, "longitude": 0,
         "transferStations": ["mongomery"], "transferAgencies": [], "platforms": [b("15684", "westbound")]},
    ],
}

OVERRIDES = {
    "//": "comment",
    "stations": {"15684": {"station": "marketMontgomery", "why": "Checked on the street."}},
    "headings": {},
    "exclude": {"//": "comment", "13510": "1 of the 48's 869 trips."},
}


def run():
    inputs = Inputs(
        metro=METRO, metro_source="test", bus=BUS, overrides=OVERRIDES,
        snapshot=files.read_snapshot(FIXTURE, "SF"), sources={},
    )
    report = Report()
    seed = convert(inputs, DAY, report)
    return seed, report, check(inputs, seed, report)


def test_merge_and_rename():
    seed, _, errors = run()
    stations = seed.curation.stations.stations
    assert set(stations) == {"embarcadero", "montgomery", "churchMarket", "marketMontgomery"}

    montgomery = stations["montgomery"]
    assert montgomery.former_ids == ["mongomery"]
    assert "typo" in montgomery.note and "migrate" in montgomery.note
    assert [x.name for x in montgomery.platforms] == [None, "To Castro"]
    assert stations["embarcadero"].transfers[0].to == "montgomery"
    assert seed.curation.stations.subways["marketStreetSubway"].stations == ["embarcadero", "montgomery"]
    assert [t.to for t in stations["marketMontgomery"].transfers] == ["montgomery"]

    church = stations["churchMarket"]
    assert church.name == "Church & Market"
    assert [(x.id, x.heading) for x in church.platforms] == [
        ("SF:17073", "northbound"), ("SF:18059", "southbound"), ("SF:15662", "eastbound"),
    ]
    assert church.transfers == []
    assert errors == []


def test_agencies_verified_notes_and_ignored():
    seed, report, _ = run()
    stations = seed.curation.stations.stations
    assert stations["embarcadero"].transfer_agencies == ["BA"]
    # "ferry" has no operator code: dropped and reported, never guessed.
    assert stations["montgomery"].transfer_agencies == ["BA"]
    assert "mongomery: 'ferry'" in report.render()
    assert {k: v.verified for k, v in stations.items()} == {
        "embarcadero": DAY, "montgomery": DAY, "churchMarket": DAY, "marketMontgomery": None,
    }
    assert stations["marketMontgomery"].platforms[0].note == "Checked on the street."
    assert {k: v.note for k, v in seed.curation.ignored.root.items()} == {"SF:13510": "1 of the 48's 869 trips."}
    assert "18059 in churchMarket: data.json southbound, bus.json westbound" in report.render()


def test_lines():
    seed, _, _ = run()
    lines = seed.curation.lines.root
    assert (lines["SF:J"].name, lines["SF:J"].color, lines["SF:J"].hidden) == ("J Church", "#FAA633", False)
    assert lines["SF:S"].hidden is True
    assert lines["SF:F"].mode == "streetcar"
    assert lines["SF:LOWL"].replaces == ["SF:L"]
    assert lines["SF:NOWL"].replaces == lines["SF:NBUS"].replaces == ["SF:N"]


def test_what_is_dropped_is_reported():
    _, report, _ = run()
    text = report.render()
    assert "level id(depth)=-1 name='Muni Metro'" in text
    assert "embarcadero 16992: ['J']" in text  # terminates
    assert "churchMarket 17073: 'Church St & Market St'" in text  # stopName
