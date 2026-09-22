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
    assert issue.platform == "SF:15418"
    assert issue.level == "warning"
    assert "Balboa Park BART/Mezzanine Level" in issue.message


def test_ignored_stop_is_not_in_the_queue(curation, snapshots):
    assert "SF:13510" in curation.ignored.root
    platforms = [i.platform for i in validate(curation, snapshots).warnings]
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
    issue = only(validate(curation, snapshots).errors, "platform-in-two-stations")
    assert (issue.station, issue.platform) == ("powell", "SF:15731")
    assert "Montgomery (montgomery)" in issue.message


def test_platform_listed_twice(curation, snapshots):
    curation.stations.stations["montgomery"].platforms.append(Platform(id="SF:15731", heading="westbound"))
    result = validate(curation, snapshots)
    issue = only(result.errors, "platform-listed-twice")
    assert (issue.station, issue.platform) == ("montgomery", "SF:15731")
    assert "platform-in-two-stations" not in codes(result.errors)


def test_platform_assigned_and_ignored(curation, snapshots):
    curation.ignored.root["SF:14015"] = IgnoredStop(note="test")
    issue = only(validate(curation, snapshots).errors, "platform-assigned-and-ignored")
    assert (issue.station, issue.platform) == ("clayDrumm", "SF:14015")


def test_unknown_station_in_a_transfer(curation, snapshots):
    curation.stations.stations["clayDrumm"].transfers.append(Transfer(to="nowhere", mode="street"))
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


def test_indoor_transfer_not_reciprocated(curation, snapshots):
    curation.stations.stations["unionSquare"].transfers.clear()
    issue = only(validate(curation, snapshots).errors, "indoor-transfer-not-reciprocated")
    assert issue.station == "powell"
    assert "'unionSquare'" in issue.message


def test_indoor_answered_by_street_is_not_reciprocated(curation, snapshots):
    curation.stations.stations["unionSquare"].transfers[0].mode = "street"
    issue = only(validate(curation, snapshots).errors, "indoor-transfer-not-reciprocated")
    assert issue.station == "powell"


def test_street_transfers_may_be_one_way(curation, snapshots):
    curation.stations.stations["powellMarket"].transfers.clear()
    assert validate(curation, snapshots).errors == []


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
    issue = only(result.warnings, "platform-not-in-snapshot")
    assert (issue.station, issue.platform) == ("clayDrumm", "SF:99999")
    assert result.errors == []


def test_station_has_no_live_platforms(curation, snapshots):
    curation.stations.stations["clayDrumm"].platforms = [Platform(id="SF:99999", heading="westbound")]
    result = validate(curation, snapshots)
    assert only(result.warnings, "station-has-no-live-platforms").station == "clayDrumm"
    assert only(result.warnings, "platform-not-in-snapshot").platform == "SF:99999"
    # SF:14015 is free again, so it joins the review queue.
    assert {i.platform for i in result.warnings if i.code == "unassigned-stop"} == {"SF:14015", "SF:15418"}
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
    s["clayDrumm"].transfers.append(Transfer(to="nowhere", mode="street"))
    curation.lines.root["SF:LOWL"].replaces.append("SF:Q")
    s["unionSquare"].transfers.clear()
    s["castroPlaza"].platforms.clear()
    s["churchMarket"].platforms = [Platform(id="SF:99999", heading="eastbound")]
    curation.lines.root["SF:S"] = LineOverride()
    curation.stations.subways["x"] = Subway(name="X", stations=["nowhere"])

    result = validate(curation, snapshots)
    assert set(codes(result.errors)) == {
        "duplicate-station-id",
        "former-id-collides",
        "platform-in-two-stations",
        "platform-listed-twice",
        "platform-assigned-and-ignored",
        "unknown-station",
        "indoor-transfer-not-reciprocated",
        "station-has-no-platforms",
    }
    assert set(codes(result.warnings)) == {
        "platform-not-in-snapshot",
        "station-has-no-live-platforms",
        "unassigned-stop",
        "unknown-line-override",
        "unknown-line",
    }
    assert all(i.level == "error" for i in result.errors)
    assert all(i.level == "warning" for i in result.warnings)
    assert all(i.message for i in result.errors + result.warnings)


def test_output_is_deterministic(curation, snapshots):
    curation.stations.stations["unionSquare"].transfers.clear()
    curation.stations.stations["clayDrumm"].platforms.clear()
    first = validate(curation, snapshots)
    again = validate(curation.model_copy(deep=True), snapshots)
    assert first == again
