import Foundation
import Testing
@testable import CanopyTransitKit

@Suite("Resolving stored ids")
struct NetworkTests {

    /// Two stations. Duboce & Church has a platform that absorbed another's stop
    /// (SF:18061) and one that took a new id when 511 renumbered its stop.
    let network = TransitNetwork(
        version: "v",
        stations: [
            Station(id: "duboceChurch", name: "Duboce & Church", lat: 37.769, lon: -122.429, platforms: [
                Platform(id: "SF:14448", heading: .eastbound, stops: ["SF:14448", "SF:18061"]),
                Platform(id: "SF:19000", heading: .westbound, formerIds: ["SF:14006"]),
            ]),
            Station(id: "montgomery", name: "Montgomery", lat: 37.789, lon: -122.401, platforms: [
                Platform(id: "SF:15731", heading: .eastbound),
                // Bad data: this platform lists Duboce's primary as a former id. The id
                // rule comes first, so Duboce keeps it.
                Platform(id: "SF:16994", heading: .westbound, formerIds: ["SF:14448"]),
            ]),
        ],
        lines: [
            Line(id: "SF:L", shortName: "L", name: "L Taraval", mode: .metro),
            Line(id: "SF:LOWL", shortName: "LOWL", name: "LOWL Owl Taraval", mode: .bus, replaces: ["SF:L"], owl: true),
        ],
        formerStationIDs: ["mongomery": "montgomery", "churchDuboceJ": "duboceChurch", "loopA": "loopB", "loopB": "loopA"]
    )

    @Test func byIDThenStopsThenFormerIDs() {
        #expect(network.resolvePlatform("SF:14448")?.platform.id == "SF:14448")
        #expect(network.resolvePlatform("SF:18061")?.platform.id == "SF:14448")
        #expect(network.resolvePlatform("SF:14006")?.platform.id == "SF:19000")
        #expect(network.resolvePlatform("SF:14006")?.station.id == "duboceChurch")
        #expect(network.resolvePlatform("SF:99999") == nil)
    }

    @Test func theIDRuleWinsOverAFormerID() {
        #expect(network.resolvePlatform("SF:14448")?.station.id == "duboceChurch")
    }

    @Test func formerStationIDs() {
        #expect(network.currentStationID("montgomery") == "montgomery")
        #expect(network.currentStationID("mongomery") == "montgomery")
        #expect(network.resolveStation("churchDuboceJ")?.name == "Duboce & Church")
        #expect(network.currentStationID("nowhere") == nil)
        #expect(network.currentStationID("loopA") == nil)
    }

    @Test func replacements() {
        #expect(network.replacements(of: "SF:L").map(\.id) == ["SF:LOWL"])
        #expect(network.replacements(of: "SF:LOWL").isEmpty)
    }

    @Test func fromStagingData() throws {
        let stations = try Fixture.decode(StationsResponse.self, "staging/stations.json")
        let lines = try Fixture.decode(LinesResponse.self, "staging/lines.json")
        let network = TransitNetwork(stations: stations, lines: lines)
        // The old app's favourites at the stations merged into 4th & King.
        #expect(network.resolveStation("fourthKingN")?.id == "fourthKing")
        #expect(network.resolveStation("fourthKingT")?.id == "fourthKing")
        #expect(network.resolvePlatform("SF:16992")?.station.id == "embarcadero")
        #expect(network.line("SF:J")?.color == HexColor("#FAA633"))
    }
}
