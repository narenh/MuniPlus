"""Every validation code, each from the smallest break of the fixture that causes it."""

from app.data.validate import validate
from app.models.curation import IgnoredStop, LineOverride, Platform, Station, Subway, Transfer


def codes(issues):
    return [i.code for i in issues]


def only(issues, code):
    found = [i for i in issues if i.code == code]
    assert len(found) == 1, issues
    return found[0]


def test_fixture_is_clean_apart_from_the_review_queue(curation, snapshots):
    result = validate(curation, snapshots)
    assert result.errors == []
    assert codes(result.warnings) == ["unassigned-stop"]


def test_unassigned_stop(curation, snapshots):
    # SF:15418 (Balboa Park BART) is in the snapshot and in no station; SF:13510 is
    # in ignored.json, so it is not in the queue.
    issue = only(validate(curation, snapshots).warnings, "unassigned-stop")
    assert issue.stop == "SF:15418"
    assert issue.level == "warning"
    assert "Balboa Park BART/Mezzanine Level" in issue.message


def test_ignored_stop_is_not_in_the_queue(curation, snapshots):
    assert "SF:13510" in curation.ignored.root
    platforms = [i.stop for i in validate(curation, snapshots).warnings]
    assert "SF:13510" not in platforms


# MARK: - Errors


def test_duplicate_station_id(curation, snapshots):
    # A JSON object cannot hold the same key twice once parsed; ids differing only
    # in case are the duplicate that survives.
    stations = curation.stations.stations
    stations["Powell"] = Station(name="Powell again", platforms=[Platform(id="SF:15418", heading="eastbound")])
    issue = only(validate(curation, snapshots).errors, "duplicate-station-id")
    assert issue.station == "powell"
    assert "'Powell'" in issue.message


def test_former_id_equal_to_a_live_id(curation, snapshots):
    curation.stations.stations["montgomery"].former_ids.append("powell")
    issue = only(validate(curation, snapshots).errors, "former-id-collides")
    assert issue.station == "montgomery"
    assert "'powell'" in issue.message


def test_former_id_reused(curation, snapshots):
    curation.stations.stations["powell"].former_ids.append("mongomery")
    issue = only(validate(curation, snapshots).errors, "former-id-collides")
    # Stations are checked in id order, so the second claimant is the one flagged.
    assert issue.station == "powell"
    assert "'montgomery'" in issue.message


def test_platform_in_two_stations(curation, snapshots):
    curation.stations.stations["powell"].platforms.append(Platform(id="SF:15731", heading="eastbound"))
    issue = only(validate(curation, snapshots).errors, "stop-in-two-stations")
    assert (issue.station, issue.stop) == ("powell", "SF:15731")
    assert "Montgomery (montgomery)" in issue.message


def test_platform_listed_twice(curation, snapshots):
    curation.stations.stations["montgomery"].platforms.append(Platform(id="SF:15731", heading="westbound"))
    result = validate(curation, snapshots)
    issue = only(result.errors, "stop-listed-twice")
    assert (issue.station, issue.stop) == ("montgomery", "SF:15731")
    assert "stop-in-two-stations" not in codes(result.errors)


def test_platform_assigned_and_ignored(curation, snapshots):
    curation.ignored.root["SF:14015"] = IgnoredStop(note="test")
    issue = only(validate(curation, snapshots).errors, "stop-assigned-and-ignored")
    assert (issue.station, issue.stop) == ("clayDrumm", "SF:14015")


def test_unknown_station_in_a_transfer(curation, snapshots):
    curation.stations.transfers.append(Transfer(between=["clayDrumm", "nowhere"], mode="street"))
    issue = only(validate(curation, snapshots).errors, "unknown-station")
    assert issue.station == "clayDrumm"
    assert "'nowhere'" in issue.message


def test_unknown_station_in_a_subway(curation, snapshots):
    curation.stations.subways["marketStreetSubway"].stations.append("vanNess")
    issue = only(validate(curation, snapshots).errors, "unknown-station")
    assert "Market Subway" in issue.message and "'vanNess'" in issue.message


def test_unknown_line_in_replaces_warns_but_never_blocks(curation, snapshots):
    curation.lines.root["SF:LOWL"].replaces.append("SF:Q")
    result = validate(curation, snapshots)
    assert "unknown-line" not in codes(result.errors)
    issue = only(result.warnings, "unknown-line")
    assert issue.line == "SF:LOWL"
    assert "SF:Q" in issue.message


def test_transfer_to_itself(curation, snapshots):
    curation.stations.transfers.append(Transfer(between=["powell", "powell"], mode="street"))
    issue = only(validate(curation, snapshots).errors, "transfer-to-itself")
    assert issue.station == "powell"


def test_duplicate_transfer_in_either_order(curation, snapshots):
    # A pair has no direction, so listing it again the other way round is the same pair.
    curation.stations.transfers.append(Transfer(between=["unionSquare", "powell"], mode="street"))
    issue = only(validate(curation, snapshots).errors, "duplicate-transfer")
    assert "'powell' and 'unionSquare'" in issue.message


def test_station_has_no_platforms(curation, snapshots):
    curation.stations.stations["clayDrumm"].platforms.clear()
    result = validate(curation, snapshots)
    issue = only(result.errors, "station-has-no-platforms")
    assert issue.station == "clayDrumm"
    # One problem, one issue: not also "no live platforms".
    assert "station-has-no-live-platforms" not in codes(result.warnings)


# MARK: - Warnings


def test_platform_not_in_snapshot(curation, snapshots):
    curation.stations.stations["clayDrumm"].platforms.append(Platform(id="SF:99999", heading="eastbound"))
    result = validate(curation, snapshots)
    issue = only(result.warnings, "stop-not-in-snapshot")
    assert (issue.station, issue.stop) == ("clayDrumm", "SF:99999")
    assert result.errors == []


def test_station_has_no_live_platforms(curation, snapshots):
    curation.stations.stations["clayDrumm"].platforms = [Platform(id="SF:99999", heading="westbound")]
    result = validate(curation, snapshots)
    assert only(result.warnings, "station-has-no-live-platforms").station == "clayDrumm"
    assert only(result.warnings, "stop-not-in-snapshot").stop == "SF:99999"
    # SF:14015 is free again, so it joins the review queue.
    assert {i.stop for i in result.warnings if i.code == "unassigned-stop"} == {"SF:14015", "SF:15418"}
    assert result.errors == []


def test_unknown_line_override(curation, snapshots):
    curation.lines.root["SF:S"] = LineOverride(hidden=True)
    result = validate(curation, snapshots)
    assert only(result.warnings, "unknown-line-override").line == "SF:S"
    assert result.errors == []


# MARK: - The set of codes


def test_every_code_in_the_plan_is_reachable(curation, snapshots):
    # Everything broken at once, which also shows no check hides another.
    s = curation.stations.stations
    s["Powell"] = Station(name="Powell again", platforms=[Platform(id="SF:15418", heading="eastbound")])
    s["montgomery"].former_ids.append("powell")
    s["powell"].former_ids.append("mongomery")
    s["powell"].platforms.append(Platform(id="SF:15731", heading="eastbound"))
    s["montgomery"].platforms.append(Platform(id="SF:16994", heading="westbound"))
    curation.ignored.root["SF:14015"] = IgnoredStop(note="test")
    curation.stations.transfers.append(Transfer(between=["clayDrumm", "nowhere"], mode="street"))
    curation.lines.root["SF:LOWL"].replaces.append("SF:Q")
    curation.stations.transfers.append(Transfer(between=["powell", "powell"], mode="street"))
    curation.stations.transfers.append(Transfer(between=["unionSquare", "powell"], mode="indoor"))
    s["castroPlaza"].platforms.clear()
    s["churchMarket"].platforms = [Platform(id="SF:99999", heading="eastbound")]
    curation.lines.root["SF:S"] = LineOverride()
    curation.stations.subways["x"] = Subway(name="X", stations=["nowhere"])

    result = validate(curation, snapshots)
    assert set(codes(result.errors)) == {
        "duplicate-station-id",
        "former-id-collides",
        "stop-in-two-stations",
        "stop-listed-twice",
        "stop-assigned-and-ignored",
        "unknown-station",
        "transfer-to-itself",
        "duplicate-transfer",
        "station-has-no-platforms",
    }
    assert set(codes(result.warnings)) == {
        "stop-not-in-snapshot",
        "station-has-no-live-platforms",
        "unassigned-stop",
        "unknown-line-override",
        "unknown-line",
    }
    assert all(i.level == "error" for i in result.errors)
    assert all(i.level == "warning" for i in result.warnings)
    assert all(i.message for i in result.errors + result.warnings)


def test_output_is_deterministic(curation, snapshots):
    curation.stations.transfers.append(Transfer(between=["powell", "powell"], mode="street"))
    curation.stations.stations["clayDrumm"].platforms.clear()
    first = validate(curation, snapshots)
    again = validate(curation.model_copy(deep=True), snapshots)
    assert first == again


# MARK: - Platforms of several stops


def test_an_extra_stop_counts_as_the_station_s(curation, snapshots):
    from app.models.curation import Platform

    church = curation.stations.stations["churchMarket"]
    church.platforms = [
        Platform(id="SF:17073", heading="northbound", stops=["SF:18059"]),
        *[p for p in church.platforms if p.id not in ("SF:17073", "SF:18059")],
    ]
    result = validate(curation, snapshots)
    assert result.errors == []
    assert "SF:18059" not in {i.stop for i in result.warnings if i.code == "unassigned-stop"}

    # Its own id repeated, or claimed by another station, is the same error as a
    # platform listed twice or in two stations.
    church.platforms[0].stops = ["SF:18059", "SF:17073"]
    assert only(validate(curation, snapshots).errors, "stop-listed-twice").stop == "SF:17073"
    church.platforms[0].stops = ["SF:18059", "SF:15731"]  # Montgomery's
    issue = only(validate(curation, snapshots).errors, "stop-in-two-stations")
    assert (issue.station, issue.stop) == ("montgomery", "SF:15731")


def test_a_platform_whose_stops_are_far_apart_is_a_warning(curation, snapshots):
    from app.models.curation import Platform

    church = curation.stations.stations["churchMarket"]
    # The fixture's Church St stops are across the street from each other, 27 m
    # apart, and SF:15661 is 25.3 m away on Market: none of these is one shelter.
    church.platforms = [
        Platform(id="SF:17073", heading="northbound", stops=["SF:18059", "SF:15661"]),
        *[p for p in church.platforms if p.id not in ("SF:17073", "SF:18059", "SF:15661")],
    ]
    result = validate(curation, snapshots)
    assert result.errors == []
    assert [i.stop for i in result.warnings if i.code == "platform-stops-far-apart"] == ["SF:18059", "SF:15661"]


def test_a_platform_former_id_must_not_be_a_current_stop(curation, snapshots):
    # It would send a home or favourite to the wrong platform.
    curation.stations.stations["montgomery"].platforms[0].former_ids = ["SF:16992"]  # Embarcadero's
    issue = only(validate(curation, snapshots).errors, "platform-former-id-collides")
    assert "Embarcadero" in issue.message


def test_a_platform_former_id_belongs_to_one_platform(curation, snapshots):
    curation.stations.stations["montgomery"].platforms[0].former_ids = ["SF:99901"]
    curation.stations.stations["embarcadero"].platforms[0].former_ids = ["SF:99901"]
    only(validate(curation, snapshots).errors, "platform-former-id-collides")
