import Foundation
import Testing
@testable import CanopyTransitKit

@Suite("Decoding the API's responses")
struct DecodingTests {

    // MARK: - The examples in API.md

    @Test func stations() throws {
        let response = try Fixture.decode(StationsResponse.self, "api-md/stations.json")
        #expect(response.version == "38d2f22ad7a296e47e86dc80679a0a048cf3a81e")
        #expect(response.formerIds == ["mongomery": "montgomery", "castroPlaza": "marketCastro"])
        let embarcadero = try #require(response.stations.first)
        #expect(embarcadero.id == "embarcadero")
        #expect(embarcadero.modes == [.metro])
        #expect(embarcadero.operators == ["SF", "BA"])
        #expect(embarcadero.platforms.map(\.id) == ["SF:16992", "SF:17217"])
        #expect(embarcadero.platforms.map(\.heading) == [.eastbound, .westbound])
        #expect(embarcadero.platforms[0].stops == ["SF:16992"])
        #expect(embarcadero.transfers == [Transfer(to: "marketEmbarcaderoSubway", name: "Embarcadero & Market", mode: .street)])
        #expect(response.subways.map(\.id) == ["marketStreetSubway"])
        #expect(response.subways[0].stations.prefix(2) == ["embarcadero", "montgomery"])
    }

    @Test func stationDetail() throws {
        let response = try Fixture.decode(StationDetailResponse.self, "api-md/station-montgomery.json")
        let station = response.station
        #expect(station.id == "montgomery")
        #expect(station.name == "Montgomery")
        let platform = try #require(station.platforms.first)
        #expect(platform.id == "SF:15731")
        #expect(platform.heading == .eastbound)
        #expect(platform.name == nil)
        #expect(platform.stopName == "Metro Montgomery Station/Downtown")
        #expect(platform.lat == 37.789219)
        // The summary fields ride along, platforms included.
        #expect(station.station.platforms.map(\.id) == ["SF:15731"])
        #expect(station.transfers.map(\.to) == ["market2"])
        #expect(station.subways.map(\.id) == ["marketStreetSubway"])
        let alert = try #require(station.alerts.first)
        #expect(alert.isAgencyWide)
        #expect(alert.activePeriods.first?.end == nil)
        #expect(alert.url == nil)
    }

    @Test func lines() throws {
        let response = try Fixture.decode(LinesResponse.self, "api-md/lines.json")
        let (j, owl) = (response.lines[0], response.lines[1])
        #expect(j.id == "SF:J")
        #expect(j.color == HexColor("#FAA633"))
        #expect(j.mode == .metro)
        #expect(!j.owl)
        #expect(owl.replaces == ["SF:L"])
        #expect(owl.owl)
        #expect(owl.mode == .bus)
    }

    @Test func lineDetail() throws {
        let line = try Fixture.decode(LineDetailResponse.self, "api-md/line-SF_N.json").line
        #expect(line.id == "SF:N")
        #expect(line.name == "N Judah")
        #expect(line.directions.map(\.direction) == [0, 1])
        #expect(line.directions[0].headsign == "Ocean Beach")
        #expect(line.directions[0].stations.first == "fourthKing")
        #expect(line.directions[1].shape == "SF:9766")
    }

    @Test func shapesAreTurnedToLatLon() throws {
        let shapes = try Fixture.decode(ShapesResponse.self, "api-md/shapes.json").shapes
        let first = try #require(shapes["SF:102"]?.first)
        #expect(first == Coordinate(latitude: 37.795436, longitude: -122.396968))
    }

    @Test func arrivals() throws {
        let response = try Fixture.decode(ArrivalsResponse.self, "api-md/arrivals.json")
        #expect(response.fetchedAt == Date(timeIntervalSince1970: 1790197438))
        #expect(response.feedAt == Date(timeIntervalSince1970: 1790113897))
        #expect(response.refreshAfter == 184)
        let ending = try #require(response.platforms["SF:16992"]?.first)
        #expect(ending.kind == .arrival)
        #expect(ending.terminates)
        #expect(ending.trip == "SF:12134484_M11")
        #expect(ending.vehicle == "SF:2019")
        #expect(ending.time == Date(timeIntervalSince1970: 1790113947))
        let starting = try #require(response.platforms["SF:17217"]?.first)
        #expect(starting.kind == .departure)
        #expect(!starting.terminates)
    }

    @Test func vehicles() throws {
        let vehicle = try #require(try Fixture.decode(VehiclesResponse.self, "api-md/vehicles.json").vehicles.first)
        #expect(vehicle.id == "SF:2001")
        #expect(vehicle.status == .stoppedAt)
        #expect(vehicle.bearing == 255)
        #expect(vehicle.stop == "SF:15198")
    }

    @Test func alerts() throws {
        let alert = try #require(try Fixture.decode(AlertsResponse.self, "api-md/alerts.json").alerts.first)
        #expect(alert.id == "SF_15752")
        #expect(alert.lines == ["SF:38", "SF:38R"])
        #expect(alert.stations == ["powellOfarrell"])
        #expect(!alert.isAgencyWide)
        #expect(alert.activePeriods.first?.start == Date(timeIntervalSince1970: 1789369200))
    }

    // MARK: - A server that has moved on

    @Test func unknownEnumValuesAndFieldsDecode() throws {
        let response = try Fixture.decode(StationsResponse.self, "edge/stations-future.json")
        let embarcadero = try #require(response.stations.first)
        #expect(embarcadero.modes == [.metro, .unknown("heavyRail")])
        #expect(embarcadero.platforms[1].heading == .unknown("towardsAirport"))
        #expect(embarcadero.transfers.first?.mode == .unknown("elevator"))
    }

    @Test func aBadElementIsDroppedNotTheResponse() throws {
        let response = try Fixture.decode(StationsResponse.self, "edge/stations-future.json")
        #expect(response.stations.map(\.id) == ["embarcadero", "montgomery"])

        let arrivals = try Fixture.decode(ArrivalsResponse.self, "edge/arrivals-future.json")
        // The one with a time that isn't a number goes; the unknown kind stays.
        #expect(arrivals.platforms["SF:16992"]?.map(\.trip) == ["SF:1"])
        #expect(arrivals.platforms["SF:16992"]?.first?.kind == .unknown("passThrough"))
        #expect(arrivals.platforms["SF:17217"] == [])
    }

    @Test func aServerOlderThanThisSDKDecodes() throws {
        // Staging before transfers, subways and owl.
        let stations = try Fixture.decode(StationsResponse.self, "staging/stations.json")
        #expect(stations.stations.count == 6)
        #expect(stations.subways.isEmpty)
        #expect(stations.stations.allSatisfy { $0.transfers.isEmpty })
        #expect(stations.formerIds["fourthKingN"] == "fourthKing")

        let lines = try Fixture.decode(LinesResponse.self, "staging/lines.json")
        #expect(lines.lines.count == 68)
        #expect(lines.lines.allSatisfy { !$0.owl })
        #expect(lines.lines.first { $0.id == "SF:KBUS" }?.replaces == ["SF:K"])

        let detail = try Fixture.decode(StationDetailResponse.self, "staging/station-embarcadero.json")
        #expect(detail.station.transfers.map(\.to) == ["marketEmbarcaderoSubway"])

        let arrivals = try Fixture.decode(ArrivalsResponse.self, "staging/arrivals-16992-17217.json")
        #expect(arrivals.platforms["SF:16992"]?.count == 30)
        _ = try Fixture.decode(LineDetailResponse.self, "staging/line-SF_N.json")
    }

    @Test func aByteOrderMarkIsTolerated() throws {
        let body = Data([0xEF, 0xBB, 0xBF]) + Fixture.data("api-md/lines.json")
        #expect(JSON.withoutBOM(body) == Fixture.data("api-md/lines.json"))
        let lines = try CanopyClient.decode(LinesResponse.self, from: body)
        #expect(lines.lines.count == 2)
    }

    // MARK: - Error bodies

    @Test func problemBodies() {
        let problem = Problem(status: 400, body: Fixture.data("edge/problem.json"))
        #expect(problem.code == .badRequest)
        #expect(problem.message.hasPrefix("platforms:"))

        let detail = Problem(status: 404, body: Fixture.data("edge/detail.json"))
        #expect(detail == Problem(code: .notFound, message: "Not Found"))

        let validation = Problem(status: 422, body: Fixture.data("edge/validation.json"))
        #expect(validation == Problem(code: .unknown("422"), message: "Field required"))

        let html = Problem(status: 502, body: Fixture.data("edge/cloudflare.html"))
        #expect(html.code == .unknown("502"))

        #expect(Problem(status: 503, body: Data()).code == .unavailable)
        #expect(ProblemCode(rawValue: "teapot") == .unknown("teapot"))
    }

    // MARK: - Ids

    @Test func idsAreBareStrings() throws {
        let encoded = try JSONEncoder().encode(["home": StationID("embarcadero")])
        #expect(String(decoding: encoded, as: UTF8.self) == #"{"home":"embarcadero"}"#)
        let platforms = try JSONDecoder().decode([StopID].self, from: Data(#"["SF:16992"]"#.utf8))
        #expect(platforms == ["SF:16992"])
    }
}
