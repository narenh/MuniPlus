"""The derived network, against the real values in tests/fixtures/transit."""

from pathlib import Path

import pytest

from app.data import loader
from app.data.network import Network, title_case
from app.models.curation import LineOverride, Platform, Station, Transfer
from app.models.snapshot import Pattern

FIXTURE = Path(__file__).parent.parent / "fixtures" / "transit"


@pytest.fixture
def net() -> Network:
    return loader.load(FIXTURE, "v1")


# MARK: - Line names


@pytest.mark.parametrize(
    "upper, expected",
    [
        # 511's upper-case long names, and what appdata/bus.json shows for the same line.
        ("CHURCH", "Church"),
        ("OWL TARAVAL", "Owl Taraval"),
        ("MARKET & WHARVES BUS", "Market & Wharves Bus"),
        ("POWELL-HYDE CABLE CAR", "Powell-Hyde Cable Car"),
        ("O'SHAUGHNESSY", "O'Shaughnessy"),
        ("3RD-19TH AVE OWL", "3rd-19th Ave Owl"),
        ("ASHBURY-18TH ST", "Ashbury-18th St"),
        ("BART EARLY BIRD", "BART Early Bird"),
        ("BAYSHORE A EXPRESS", "Bayshore A Express"),
        ("BAYVIEW HUNTERS POINT EXPRESS", "Bayview Hunters Point Express"),
        ("THE EMBARCADERO VIA MARKET", "The Embarcadero via Market"),
        ("MCALLISTER", "McAllister"),
        ("GEARY (OWL)", "Geary (Owl)"),
    ],
)
def test_title_case(upper, expected):
    assert title_case(upper) == expected


def test_default_line_names(net):
    names = {line.id: line.name for line in net.lines().lines}
    assert names["SF:J"] == "J Church"
    assert names["SF:LOWL"] == "LOWL Owl Taraval"
    assert names["SF:F"] == "F Market & Wharves"
    assert names["SF:PH"] == "PH Powell-Hyde Cable Car"


def test_curated_name_wins(curation, snapshots):
    curation.lines.root["SF:LOWL"].name = "L Owl"
    assert Network(curation, snapshots, "v").line("SF:LOWL").name == "L Owl"


# MARK: - Lines


def test_mode_override(net):
    # 511 says metro; curation says streetcar.
    assert net.line("SF:F").mode == "streetcar"
    assert net.line("SF:J").mode == "metro"
    assert net.line("SF:PH").mode == "cableway"


@pytest.mark.parametrize(
    "long_name, owl",
    [
        ("OWL TARAVAL", True),
        ("SAN BRUNO OWL", True),
        ("3RD-19TH AVE OWL", True),
        ("GEARY (OWL)", True),
        ("TARAVAL", False),
        ("INGLESIDE BUS", False),
        ("BOWLING", False),
    ],
)
def test_owl_is_511s_name(curation, snapshots, long_name, owl):
    snapshots["SF"].lines.root["SF:L"].long_name = long_name
    assert Network(curation, snapshots, "v").line("SF:L").owl is owl


def test_a_curated_name_does_not_make_an_owl(net, curation, snapshots):
    assert net.line("SF:LOWL").owl and net.line("SF:NOWL").owl and not net.line("SF:L").owl
    curation.lines.root["SF:LOWL"].name = "L Night"
    assert Network(curation, snapshots, "v").line("SF:LOWL").owl


def test_replaces_and_hidden(net, curation, snapshots):
    assert net.line("SF:LOWL").replaces == ["SF:L"]
    assert net.line("SF:NOWL").replaces == ["SF:N"]
    assert net.line("SF:L").replaces == []
    assert not net.line("SF:L").hidden
    curation.lines.root["SF:5R"] = LineOverride(hidden=True)
    hidden = Network(curation, snapshots, "v").line("SF:5R")
    assert hidden.hidden and hidden.name == "5R Fulton Rapid"


def test_colours(net, curation, snapshots):
    assert net.line("SF:J").color == "#FAA633"
    assert net.line("SF:J").text_color == "#FFFFFF"
    curation.lines.root["SF:5"] = LineOverride(color="#123456")
    line = Network(curation, snapshots, "v").line("SF:5")
    assert (line.color, line.text_color) == ("#123456", "#FFFFFF")


def test_line_order(net):
    # Metro, streetcar, cable car, bus; numbers numerically and before letters.
    assert [line.id for line in net.lines().lines] == [
        "SF:J", "SF:K", "SF:L", "SF:M", "SF:N", "SF:T",
        "SF:F",
        "SF:PH", "SF:PM",
        "SF:1", "SF:5", "SF:5R", "SF:LOWL", "SF:NOWL",
    ]  # fmt: skip


def test_numbers_sort_numerically(curation, snapshots):
    snap = snapshots["SF"]
    for short in ("38", "38R", "14"):
        snap.lines.root[f"SF:{short}"] = snap.lines.root["SF:1"].model_copy(update={"short_name": short})
    ids = [line.id for line in Network(curation, snapshots, "v").lines().lines if line.mode == "bus"]
    assert ids == ["SF:1", "SF:5", "SF:5R", "SF:14", "SF:38", "SF:38R", "SF:LOWL", "SF:NOWL"]


def test_override_for_a_line_511_lacks_is_ignored(curation, snapshots):
    curation.lines.root["SF:S"] = LineOverride(name="S Shuttle")
    net = Network(curation, snapshots, "v")
    assert net.line("SF:S") is None
    assert "SF:S" not in net.derived().lines


# MARK: - Directions


def test_directions_follow_the_most_run_pattern(net):
    n = net.line("SF:N")
    # Direction 1 has "Caltrain" x175 and "Embarcadero" x12.
    assert [(d.direction, d.headsign) for d in n.directions] == [(0, "Ocean Beach"), (1, "Caltrain")]
    assert n.directions[0].stations == ["embarcadero", "montgomery", "powell"]
    assert n.directions[0].stops == ["SF:17217", "SF:16994", "SF:16995"]


def test_one_direction_lines(net):
    assert [d.direction for d in net.line("SF:1").directions] == [0]
    assert net.line("SF:1").directions[0].stations == ["clayDrumm"]


def test_unclaimed_stops_are_dropped_and_repeats_collapse(curation, snapshots):
    # Add Balboa Park (unassigned) and Church & Market's second pole to the J.
    j = snapshots["SF"].patterns.root["SF:J"]
    j[0] = Pattern(
        direction=0,
        headsign="Balboa Park",
        trips=120,
        stops=["SF:17217", "SF:16994", "SF:16995", "SF:18059", "SF:17073", "SF:15418", "SF:13510"],
    )
    d = Network(curation, snapshots, "v").line("SF:J").directions[0]
    assert d.stations == ["embarcadero", "montgomery", "powell", "churchMarket"]
    assert d.stops == ["SF:17217", "SF:16994", "SF:16995", "SF:18059", "SF:17073"]


def test_headsign(net):
    assert net.headsign("SF:N", 1) == "Caltrain"
    assert net.headsign("SF:J", 0) == "Balboa Park"
    assert net.headsign("SF:1", 1) is None
    assert net.headsign("SF:Q", 0) is None


# MARK: - Platforms and stations


def test_station_of_and_is_live(net, curation, snapshots):
    assert net.version == "v1"
    assert net.station_of("SF:15731") == "montgomery"
    assert net.is_live("SF:15731")
    # In the snapshot but ignored / unassigned: no station, not live.
    assert net.station_of("SF:13510") is None and not net.is_live("SF:13510")
    assert net.station_of("SF:15418") is None and not net.is_live("SF:15418")
    # Assigned but gone from 511: owned, not live.
    curation.stations.stations["clayDrumm"].platforms.append(Platform(id="SF:99999", heading="eastbound"))
    gone = Network(curation, snapshots, "v")
    assert gone.station_of("SF:99999") == "clayDrumm"
    assert not gone.is_live("SF:99999")
    assert [p.id for p in gone.station("clayDrumm").platforms] == ["SF:14015"]


def test_platform_lines_are_the_union_over_patterns(net):
    platforms = {p.id: p for s in net.stations().stations for p in s.platforms}
    assert platforms["SF:15688"].lines == ["SF:5", "SF:5R"]
    assert platforms["SF:16063"].lines == ["SF:PH", "SF:PM"]
    # Both N patterns in direction 1 stop here; the line is listed once.
    assert platforms["SF:15417"].lines == ["SF:J", "SF:K", "SF:L", "SF:M", "SF:N"]


def test_station_lines_and_modes(net):
    church = net.station("churchMarket")
    assert church.lines == ["SF:J", "SF:F"]
    assert church.modes == ["metro", "streetcar"]
    assert net.station("powellMarket").modes == ["cableway", "bus"]


def test_station_coordinate_is_the_centroid(net):
    emb = net.station("embarcadero")
    assert emb.lat == pytest.approx((37.793134 + 37.79271) / 2, abs=1e-6)
    assert emb.lon == pytest.approx((-122.396431 + -122.39715) / 2, abs=1e-6)
    assert (net.station("clayDrumm").lat, net.station("clayDrumm").lon) == (37.79532, -122.397473)


def test_centroid_ignores_platforms_511_dropped(curation, snapshots):
    curation.stations.stations["clayDrumm"].platforms.append(Platform(id="SF:99999", heading="eastbound"))
    clay = Network(curation, snapshots, "v").station("clayDrumm")
    assert (clay.lat, clay.lon) == (37.79532, -122.397473)


def test_platform_order_is_the_curated_order(net):
    assert [p.id for p in net.station("churchMarket").platforms] == ["SF:17073", "SF:18059", "SF:15662", "SF:15661"]


def test_station_detail(net):
    powell = net.station("powell")
    # Ordered by the other station's name.
    assert [(t.to, t.name, t.mode) for t in powell.transfers] == [
        ("powellMarket", "Powell & Market", "street"),
        ("unionSquare", "Union Square", "indoor"),
    ]
    # One pair, both ends.
    assert [(t.to, t.mode) for t in net.station("unionSquare").transfers] == [("powell", "indoor")]
    assert [(t.to, t.mode) for t in net.station("powellMarket").transfers] == [("powell", "street")]
    assert [(s.id, s.name) for s in powell.subways] == [("marketStreetSubway", "Market Subway")]
    # Operators with platforms here first, then those with none in the data yet.
    assert net.station("embarcadero").operators == ["SF", "BA"]
    assert net.station("powell").operators == ["SF"]
    assert net.station("clayDrumm").subways == []
    p = powell.platforms[0]
    assert (p.id, p.heading, p.stop_name, p.name) == ("SF:15417", "eastbound", "Metro Powell Station/Downtown", None)
    assert (p.lat, p.lon) == (37.784653, -122.407086)
    assert powell.alerts == []


def test_station_order_is_by_id(net):
    ids = [s.id for s in net.stations().stations]
    assert ids == sorted(ids)
    assert len(ids) == 8


def test_former_ids(net):
    assert net.stations().former_ids == {"mongomery": "montgomery"}
    assert net.current_id("mongomery") == "montgomery"
    assert net.current_id("montgomery") is None


def test_former_id_never_shadows_a_live_id(curation, snapshots):
    curation.stations.stations["montgomery"].former_ids.append("powell")
    net = Network(curation, snapshots, "v")
    assert net.current_id("powell") is None
    assert net.station("powell").name == "Powell"


def test_the_list_carries_transfers_and_subways(net):
    # Every row of a station list can show its transfers, and the subways can be
    # browsed in order, with no request per station.
    summaries = {s.id: s for s in net.stations().stations}
    for sid, summary in summaries.items():
        assert summary.transfers == net.station(sid).transfers
    assert [(t.to, t.mode) for t in summaries["powell"].transfers] == [("powellMarket", "street"), ("unionSquare", "indoor")]
    assert summaries["embarcadero"].transfers == []
    assert [(s.id, s.name, s.stations) for s in net.stations().subways] == [
        ("marketStreetSubway", "Market Subway", ["embarcadero", "montgomery", "powell"]),
    ]


# MARK: - Stations with no live platforms


def test_station_without_live_platforms_is_public_nowhere_but_derived(curation, snapshots):
    curation.stations.stations["clayDrumm"].platforms = [Platform(id="SF:99999", heading="westbound")]
    curation.stations.stations["clayDrumm"].former_ids = ["drumm"]
    curation.stations.transfers.append(Transfer(between=["powell", "clayDrumm"], mode="street"))
    net = Network(curation, snapshots, "v")
    assert "clayDrumm" not in [s.id for s in net.stations().stations]
    assert net.station("clayDrumm") is None
    assert "drumm" not in net.stations().former_ids
    assert "clayDrumm" not in [t.to for t in net.station("powell").transfers]
    powell = next(s for s in net.stations().stations if s.id == "powell")
    assert "clayDrumm" not in [t.to for t in powell.transfers]
    derived = net.derived().stations["clayDrumm"]
    assert (derived.lat, derived.lon, derived.lines, derived.modes) == (None, None, [], [])


# MARK: - derived()


def test_derived(net, curation):
    d = net.derived()
    assert set(d.stations) == set(curation.stations.stations)
    assert d.stations["powell"].lat == net.station("powell").lat
    assert d.lines["SF:F"].mode == "streetcar"
    assert list(d.lines) == [line.id for line in net.lines().lines]
    # Every stop 511 lists is there, assigned or not, so the review queue can show
    # what serves an unassigned one.
    assert d.stops["SF:15418"].live
    assert d.stops["SF:15418"].name == "Balboa Park BART/Mezzanine Level"
    assert d.stops["SF:13510"].lines == []
    assert d.stops["SF:16992"].lines == ["SF:J", "SF:K", "SF:L", "SF:M", "SF:N"]


def test_derived_platform_gone_from_511(curation, snapshots):
    curation.stations.stations["clayDrumm"].platforms.append(Platform(id="SF:99999", heading="eastbound"))
    p = Network(curation, snapshots, "v").derived().stops["SF:99999"]
    assert (p.live, p.lines, p.lat, p.lon, p.name) == (False, [], None, None, None)


# MARK: - Invalid curation


def test_builds_from_invalid_curation(curation, snapshots):
    # The editor derives from unsaved curation, which may fail validation.
    s = curation.stations.stations
    s["powell"].platforms.append(Platform(id="SF:15731", heading="eastbound"))  # also Montgomery's
    s["montgomery"].platforms.append(Platform(id="SF:16994", heading="westbound"))  # listed twice
    curation.stations.transfers.append(Transfer(between=["clayDrumm", "nowhere"], mode="indoor"))
    curation.stations.transfers.append(Transfer(between=["powell", "unionSquare"], mode="street"))  # a duplicate
    s["castroPlaza"].platforms.clear()
    s["empty"] = Station(name="Empty", platforms=[])
    curation.stations.subways["marketStreetSubway"].stations.append("nowhere")
    curation.lines.root["SF:LOWL"].replaces.append("SF:Q")

    net = Network(curation, snapshots, "v")
    # First claim wins, stations in id order: Montgomery keeps SF:15731.
    assert net.station_of("SF:15731") == "montgomery"
    assert [p.id for p in net.station("powell").platforms] == ["SF:15417", "SF:16995"]
    assert [p.id for p in net.station("montgomery").platforms] == ["SF:15731", "SF:16994"]
    assert net.station("clayDrumm").transfers == []
    # The first listing of a duplicated pair wins, and it shows once.
    assert [(t.to, t.mode) for t in net.station("unionSquare").transfers] == [("powell", "indoor")]
    assert net.station("castroPlaza") is None
    # A subway lists only stations the API serves.
    assert net.stations().subways[0].stations == ["embarcadero", "montgomery", "powell"]
    assert net.derived().stations["empty"].lat is None
    assert net.line("SF:LOWL").replaces == ["SF:L", "SF:Q"]


def test_operators_filter():
    net = loader.load(FIXTURE, "v", operators=["BA"])
    assert net.stations().stations == []
    assert net.lines().lines == []
    assert loader.load(FIXTURE, "v", operators=["SF"]).line("SF:J") is not None


# MARK: - Platforms of several stops


def two_stop_platform(curation):
    """Church & Market's southbound stop made an extra stop of its northbound
    platform: one place with two ids, for these tests' purposes."""
    church = curation.stations.stations["churchMarket"]
    church.platforms = [
        Platform(id="SF:17073", heading="northbound", stops=["SF:18059"]),
        *[p for p in church.platforms if p.id not in ("SF:17073", "SF:18059")],
    ]
    return curation


def test_a_platform_is_the_union_of_its_stops(curation, snapshots):
    net = Network(two_stop_platform(curation), snapshots, "v")
    summary = next(s for s in net.stations().stations if s.id == "churchMarket")
    p = summary.platforms[0]
    assert (p.id, p.stops) == ("SF:17073", ["SF:17073", "SF:18059"])
    assert p.lines == sorted({*net.derived().stops["SF:17073"].lines, *net.derived().stops["SF:18059"].lines},
                             key=[line.id for line in net.lines().lines].index)
    detail = net.station("churchMarket").platforms[0]
    # Between the two stops, named for the primary.
    assert detail.lat == round((37.767509 + 37.767286) / 2, 6)
    assert detail.stop_name == "Church St & Market St"
    assert [q.id for q in net.station("churchMarket").platforms] == ["SF:17073", "SF:15662", "SF:15661"]


def test_every_stop_of_a_platform_resolves_to_it(curation, snapshots):
    net = Network(two_stop_platform(curation), snapshots, "v")
    for stop in ("SF:17073", "SF:18059"):
        assert net.station_of(stop) == "churchMarket"
        assert net.platform_of(stop) == "SF:17073"
        assert net.stops_of(stop) == ["SF:17073", "SF:18059"]
        assert net.is_live(stop)
    assert net.stops_of("SF:15418") == ["SF:15418"]  # in no platform: just itself
    assert net.platform_of("SF:15418") is None


def test_a_platforms_former_id_still_finds_it(curation, snapshots):
    # 511 retired Montgomery's SF:15731 and the platform now answers to another id:
    # a home or favourite that kept SF:15731 must still reach it.
    p = curation.stations.stations["montgomery"].platforms[0]
    p.id, p.former_ids = "SF:16994", ["SF:15731"]
    curation.stations.stations["montgomery"].platforms = [p]
    net = Network(curation, snapshots, "v")
    assert net.platform_of("SF:15731") == "SF:16994"
    assert net.stops_of("SF:15731") == ["SF:16994"]
    assert net.station_of("SF:15731") is None  # an alias for platforms, not a stop
    summary = next(s for s in net.stations().stations if s.id == "montgomery")
    assert summary.platforms[0].former_ids == ["SF:15731"]
