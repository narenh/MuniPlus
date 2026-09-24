import Foundation
import Testing
@testable import CanopyTransitKit

@Suite("Reading 511 StopMonitoring as arrivals")
struct StopMonitoringTests {
    /// When 511 answered: 2026-09-24T02:27:37Z.
    static let answeredAt = Date(timeIntervalSince1970: 1790216857)

    static func visits(_ code: String) throws -> StopMonitoring {
        try JSON.decoder().decode(StopMonitoring.self, from: Fixture.data("511/stopmonitoring-SF-\(code).json"))
    }

    @Test func decoding() throws {
        let eastbound = try Self.visits("16992")
        #expect(eastbound.visits.count == 15)
        #expect(eastbound.responseTimestamp == Self.answeredAt)
        // The feed's time, nine seconds before the response.
        #expect(eastbound.feedAt == Date(timeIntervalSince1970: 1790216848))

        // Westbound, most trips haven't started, and say 1970.
        let westbound = try Self.visits("17217")
        #expect(westbound.visits.contains { $0.recordedAtTime == Date(timeIntervalSince1970: 0) })
        #expect(westbound.feedAt == Date(timeIntervalSince1970: 1790216848))
    }

    @Test func eastboundTripsEndHere() throws {
        let arrivals = try Self.visits("16992").arrivals(at: "SF:16992")
        #expect(arrivals.count == 15)
        let m = try #require(arrivals.first { $0.trip == "SF:12135373_M11" })
        #expect(m.line == "SF:M")
        #expect(m.direction == 1)
        #expect(m.kind == .arrival)
        #expect(m.terminates)
        #expect(m.vehicle == "SF:2157")
        // The N carries on to 4th & King.
        let n = try #require(arrivals.first { $0.line == "SF:N" })
        #expect(!n.terminates)
        #expect(n.headsign == "Caltrain/Ballpark")
    }

    @Test func westboundTripsStartHere() throws {
        let arrivals = try Self.visits("17217").arrivals(at: "SF:17217")
        let l = try #require(arrivals.first { $0.trip == "SF:12134415_M11" })
        #expect(l.kind == .departure)
        #expect(!l.terminates)
        #expect(l.headsign == "S.F. Zoo")
        // ExpectedDepartureTime, not the timetable's 02:20.
        #expect(l.time == Date(timeIntervalSince1970: 1790216901))
        // Not yet out of the yard: no vehicle.
        #expect(arrivals.first { $0.trip == "SF:12135226_M11" }?.vehicle == nil)
        // The N comes through from downtown.
        #expect(arrivals.filter { $0.line == "SF:N" }.allSatisfy { $0.kind == .arrival })
    }

    /// The same trips as staging reported them two minutes earlier. The fields must
    /// agree; the times move on, and 511 doesn't know every vehicle staging does.
    @Test func agreesWithTheAPI() throws {
        let staging = try Fixture.decode(ArrivalsResponse.self, "staging/arrivals-16992-17217.json")
        var compared = 0
        for code in ["16992", "17217"] {
            let stop = StopID("SF:\(code)")
            let api = Dictionary(uniqueKeysWithValues: (staging.platforms[stop] ?? []).map { ($0.trip, $0) })
            for arrival in try Self.visits(code).arrivals(at: stop) {
                guard let theirs = api[arrival.trip] else { continue }
                compared += 1
                #expect(arrival.line == theirs.line)
                #expect(arrival.direction == theirs.direction)
                #expect(arrival.headsign == theirs.headsign)
                #expect(arrival.kind == theirs.kind)
                #expect(arrival.terminates == theirs.terminates)
                if let vehicle = arrival.vehicle { #expect(vehicle == theirs.vehicle) }
                #expect(abs(arrival.time.timeIntervalSince(theirs.time)) < 90)
            }
        }
        #expect(compared == 29)
    }

    @Test func mergingAPlatformsStops() {
        let now = Self.answeredAt
        func at(_ offset: TimeInterval, _ trip: TripID, _ line: LineID = "SF:N") -> Arrival {
            Arrival(line: line, direction: 0, headsign: "Ocean Beach", time: now.addingTimeInterval(offset), trip: trip)
        }
        let merged = StopMonitoring.merge([
            [at(300, "SF:a"), at(-30, "SF:gone"), at(600, "SF:c", "SF:NBUS")],
            [at(240, "SF:a"), at(120, "SF:b", "SF:NBUS"), at(600, "SF:d")],
        ], now: now, limit: 3)
        // The same trip at both stops counts once, at its earlier time; nothing past.
        #expect(merged.map(\.trip) == ["SF:b", "SF:a", "SF:d"])
        #expect(merged[1].time == now.addingTimeInterval(240))
    }

    @Test func splittingAndQualifying() {
        #expect(StopMonitoring.split("SF:16992")! == ("SF", "16992"))
        #expect(StopMonitoring.split("SF:12134484_M11")! == ("SF", "12134484_M11"))
        #expect(StopMonitoring.split("embarcadero") == nil)
        #expect(StopMonitoring.qualify("SF", "2019") == "SF:2019")
        #expect(StopMonitoring.qualify("SF", "a:b") == nil)
        #expect(StopMonitoring.qualify("SF", "") == nil)
        #expect(StopMonitoring.qualify("SF", nil) == nil)
    }

    @Test func whenA429SaysToTryAgain() {
        let now = Date(timeIntervalSince1970: 1790216857)  // 02:27:37
        func response(_ headers: [String: String]) -> HTTPURLResponse {
            HTTPURLResponse(url: StopMonitoringClient.endpoint, statusCode: 429, httpVersion: nil, headerFields: headers)!
        }
        #expect(StopMonitoringClient.retryTime(response(["Retry-After": "120"]), now: now) == now.addingTimeInterval(120))
        #expect(StopMonitoringClient.retryTime(response(["RateLimit-Reset": "1790218800"]), now: now)
                == Date(timeIntervalSince1970: 1790218800))
        #expect(StopMonitoringClient.retryTime(response(["Retry-After": "Thu, 24 Sep 2026 03:00:00 GMT"]), now: now)
                == Date(timeIntervalSince1970: 1790218800))
        // Nothing said: the top of the hour, 03:00.
        #expect(StopMonitoringClient.retryTime(response([:]), now: now) == Date(timeIntervalSince1970: 1790218800))
    }
}
