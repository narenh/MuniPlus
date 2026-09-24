import Foundation

/// 511's SIRI StopMonitoring answer for one stop, as much of it as arrivals need.
///
/// Every field is optional and read leniently: this is someone else's API, and a
/// visit that can't be made into an arrival is skipped rather than failing the rest.
struct StopMonitoring: Decodable, Sendable {
    let responseTimestamp: Date?
    let visits: [Visit]

    struct Visit: Decodable, Sendable {
        let recordedAtTime: Date?
        let lineRef: String?
        let directionRef: String?
        let datedVehicleJourneyRef: String?
        let originRef: String?
        let destinationRef: String?
        let destinationName: String?
        let vehicleRef: String?
        let destinationDisplay: String?
        let aimedArrivalTime: Date?
        let expectedArrivalTime: Date?
        let aimedDepartureTime: Date?
        let expectedDepartureTime: Date?

        private enum VisitKeys: String, CodingKey {
            case recordedAtTime = "RecordedAtTime", journey = "MonitoredVehicleJourney"
        }

        private enum JourneyKeys: String, CodingKey {
            case lineRef = "LineRef", directionRef = "DirectionRef", framed = "FramedVehicleJourneyRef"
            case originRef = "OriginRef", destinationRef = "DestinationRef", destinationName = "DestinationName"
            case vehicleRef = "VehicleRef", call = "MonitoredCall"
        }

        private enum FramedKeys: String, CodingKey {
            case datedVehicleJourneyRef = "DatedVehicleJourneyRef"
        }

        private enum CallKeys: String, CodingKey {
            case destinationDisplay = "DestinationDisplay"
            case aimedArrivalTime = "AimedArrivalTime", expectedArrivalTime = "ExpectedArrivalTime"
            case aimedDepartureTime = "AimedDepartureTime", expectedDepartureTime = "ExpectedDepartureTime"
        }

        init(from decoder: any Decoder) throws {
            let visit = try decoder.container(keyedBy: VisitKeys.self)
            recordedAtTime = visit.siriTime(forKey: .recordedAtTime)
            let journey = try visit.nestedContainer(keyedBy: JourneyKeys.self, forKey: .journey)
            lineRef = journey.text(forKey: .lineRef)
            directionRef = journey.text(forKey: .directionRef)
            datedVehicleJourneyRef = (try? journey.nestedContainer(keyedBy: FramedKeys.self, forKey: .framed))?
                .text(forKey: .datedVehicleJourneyRef)
            originRef = journey.text(forKey: .originRef)
            destinationRef = journey.text(forKey: .destinationRef)
            destinationName = journey.text(forKey: .destinationName)
            vehicleRef = journey.text(forKey: .vehicleRef)
            let call = try? journey.nestedContainer(keyedBy: CallKeys.self, forKey: .call)
            destinationDisplay = call?.text(forKey: .destinationDisplay)
            aimedArrivalTime = call?.siriTime(forKey: .aimedArrivalTime)
            expectedArrivalTime = call?.siriTime(forKey: .expectedArrivalTime)
            aimedDepartureTime = call?.siriTime(forKey: .aimedDepartureTime)
            expectedDepartureTime = call?.siriTime(forKey: .expectedDepartureTime)
        }
    }

    private enum RootKeys: String, CodingKey {
        case serviceDelivery = "ServiceDelivery"
    }

    private enum DeliveryKeys: String, CodingKey {
        case responseTimestamp = "ResponseTimestamp", stopMonitoringDelivery = "StopMonitoringDelivery"
    }

    private enum StopKeys: String, CodingKey {
        case visits = "MonitoredStopVisit"
    }

    init(from decoder: any Decoder) throws {
        let root = try decoder.container(keyedBy: RootKeys.self)
        let delivery = try root.nestedContainer(keyedBy: DeliveryKeys.self, forKey: .serviceDelivery)
        responseTimestamp = delivery.siriTime(forKey: .responseTimestamp)
        // One delivery for one stop code, though SIRI allows a list of them.
        if let one = try? delivery.nestedContainer(keyedBy: StopKeys.self, forKey: .stopMonitoringDelivery) {
            visits = try one.list(forKey: .visits)
        } else if var many = try? delivery.nestedUnkeyedContainer(forKey: .stopMonitoringDelivery) {
            var visits: [Visit] = []
            while !many.isAtEnd {
                guard let one = try? many.nestedContainer(keyedBy: StopKeys.self) else { break }
                visits += try one.list(forKey: .visits)
            }
            self.visits = visits
        } else {
            visits = []
        }
    }

    /// How old these predictions are: the latest `RecordedAtTime`, which is the
    /// feed's time rather than the moment of the response. 511 writes 1970 on
    /// trips that aren't running yet, so those don't count. With none, the
    /// response's own time.
    var feedAt: Date? {
        visits.compactMap(\.recordedAtTime).filter { $0.timeIntervalSince1970 > 0 }.max() ?? responseTimestamp
    }
}

private let siriFormats = [Date.ISO8601FormatStyle(), Date.ISO8601FormatStyle(includingFractionalSeconds: true)]

extension KeyedDecodingContainer {
    /// A non-empty string, or nil.
    fileprivate func text(forKey key: Key) -> String? {
        guard let value = try? decodeIfPresent(String.self, forKey: key), !value.isEmpty else { return nil }
        return value
    }

    /// An ISO 8601 time (`2026-09-24T02:27:37Z`), or nil.
    fileprivate func siriTime(forKey key: Key) -> Date? {
        guard let text = text(forKey: key) else { return nil }
        return siriFormats.lazy.compactMap { try? $0.parse(text) }.first
    }
}

// MARK: - Reading 511 as this API would have answered

extension StopMonitoring {
    /// The visits at `stop` as arrivals, qualified the way the API qualifies ids
    /// (API.md, section 3). A visit with no line, trip, direction or time is skipped.
    func arrivals(at stop: StopID) -> [Arrival] {
        guard let (op, code) = StopMonitoring.split(stop) else { return [] }
        return visits.compactMap { visit in
            guard let line = StopMonitoring.qualify(op, visit.lineRef),
                  let trip = StopMonitoring.qualify(op, visit.datedVehicleJourneyRef),
                  let direction = StopMonitoring.direction(visit.directionRef) else { return nil }

            // A trip with nothing expected to arrive here, only to leave, starts here:
            // Embarcadero westbound, Balboa Park. Its time is when it leaves.
            let startsHere = visit.originRef == code && visit.expectedArrivalTime == nil
            let kind: ArrivalKind
            let time: Date
            if startsHere, let leaves = visit.expectedDepartureTime ?? visit.aimedDepartureTime {
                (kind, time) = (.departure, leaves)
            } else if let at = visit.expectedArrivalTime ?? visit.expectedDepartureTime
                        ?? visit.aimedArrivalTime ?? visit.aimedDepartureTime {
                (kind, time) = (.arrival, at)
            } else {
                return nil
            }

            return Arrival(
                line: LineID(line),
                direction: direction,
                // DestinationDisplay is the headsign the API sends ("Caltrain/Ballpark");
                // DestinationName is the last stop's name ("King St & 4th St").
                headsign: visit.destinationDisplay ?? visit.destinationName ?? (direction == 0 ? "Outbound" : "Inbound"),
                time: time,
                kind: kind,
                terminates: kind == .arrival && visit.destinationRef == code,
                trip: TripID(trip),
                vehicle: StopMonitoring.qualify(op, visit.vehicleRef).map(VehicleID.init(rawValue:))
            )
        }
    }

    /// `SF:16992` → (`SF`, `16992`): the one place an id may be taken apart, to ask
    /// 511 for it.
    static func split(_ stop: StopID) -> (operator: String, code: String)? {
        guard let colon = stop.rawValue.firstIndex(of: ":") else { return nil }
        let op = String(stop.rawValue[..<colon])
        let code = String(stop.rawValue[stop.rawValue.index(after: colon)...])
        guard !op.isEmpty, !code.isEmpty else { return nil }
        return (op, code)
    }

    /// `<operator>:<id>`, or nil where 511's id couldn't be one: the same check the
    /// server makes.
    static func qualify(_ op: String, _ upstream: String?) -> String? {
        guard let upstream, !upstream.isEmpty,
              !upstream.contains(where: { $0 == "," || $0 == ":" || $0 == "/" || $0.isWhitespace }) else { return nil }
        return "\(op):\(upstream)"
    }

    /// SFMTA's SIRI directions onto its GTFS ones: outbound is 0, inbound 1.
    static func direction(_ ref: String?) -> Int? {
        switch ref {
        case "OB": 0
        case "IB": 1
        default: nil
        }
    }

    /// One platform's arrivals from its stops' answers, as the server merges them: a
    /// trip seen at two stops counts once, at its earlier time; nothing before `now`;
    /// soonest first; at most `limit`.
    static func merge(_ lists: [[Arrival]], now: Date, limit: Int) -> [Arrival] {
        var first: [TripID: Arrival] = [:]
        for arrival in lists.joined() where arrival.time >= now {
            if let seen = first[arrival.trip], seen.time <= arrival.time { continue }
            first[arrival.trip] = arrival
        }
        return first.values
            .sorted { ($0.time, $0.line.rawValue, $0.trip.rawValue) < ($1.time, $1.line.rawValue, $1.trip.rawValue) }
            .prefix(limit)
            .map { $0 }
    }
}
