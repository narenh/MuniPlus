import CoreLocation
import Foundation

// MARK: - Arrivals

/// One trip due at a platform.
///
/// How to show it (API.md, section 5): an `arrival` is an ordinary arrival; a
/// `departure` starts here, and `time` is when it leaves; an arrival that
/// `terminates` ends here, so nobody boards it. Decide "ends here" from
/// `terminates`, never from `headsign`.
public struct Arrival: Decodable, Hashable, Sendable, Identifiable {
    public let line: LineID
    /// 0 or 1, meaningful only within `line`.
    public let direction: Int
    /// Where the vehicle is going.
    public let headsign: String
    /// The predicted time. Count down against the device clock.
    public let time: Date
    public let kind: ArrivalKind
    /// The trip ends here: nobody boards it.
    public let terminates: Bool
    /// Ties the same trip across platforms within one answer. Never store it.
    public let trip: TripID
    /// The vehicle serving the trip, when known. It matches `Vehicle.id`.
    public let vehicle: VehicleID?

    public var id: TripID { trip }

    public init(line: LineID, direction: Int, headsign: String, time: Date, kind: ArrivalKind = .arrival,
                terminates: Bool = false, trip: TripID, vehicle: VehicleID? = nil) {
        self.line = line
        self.direction = direction
        self.headsign = headsign
        self.time = time
        self.kind = kind
        self.terminates = terminates
        self.trip = trip
        self.vehicle = vehicle
    }

    private enum CodingKeys: String, CodingKey {
        case line, direction, headsign, time, kind, terminates, trip, vehicle
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(line: try c.decode(LineID.self, forKey: .line),
                  direction: try c.decode(Int.self, forKey: .direction),
                  headsign: try c.decode(String.self, forKey: .headsign),
                  time: Date(timeIntervalSince1970: try c.decode(Double.self, forKey: .time)),
                  kind: try c.decode(ArrivalKind.self, forKey: .kind),
                  terminates: try c.decodeIfPresent(Bool.self, forKey: .terminates) ?? false,
                  trip: try c.decode(TripID.self, forKey: .trip),
                  vehicle: try c.decodeIfPresent(VehicleID.self, forKey: .vehicle))
    }
}

/// `GET /api/v1/arrivals`.
public struct ArrivalsResponse: Decodable, Sendable {
    /// When the predictions were downloaded. Nil before the first download.
    public let fetchedAt: Date?
    /// 511's own time for the predictions: how old they are. Show `now - feedAt`
    /// when it grows large.
    public let feedAt: Date?
    /// Seconds until newer data can exist. Poll no sooner.
    public let refreshAfter: TimeInterval
    /// Keyed by platform id. Every platform asked for is here, as an empty list when
    /// nothing is due, soonest first.
    public let platforms: [StopID: [Arrival]]

    init(fetchedAt: Date?, feedAt: Date?, refreshAfter: TimeInterval, platforms: [StopID: [Arrival]]) {
        self.fetchedAt = fetchedAt
        self.feedAt = feedAt
        self.refreshAfter = refreshAfter
        self.platforms = platforms
    }

    private enum CodingKeys: String, CodingKey {
        case fetchedAt, feedAt, refreshAfter, platforms
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        fetchedAt = try c.epoch(forKey: .fetchedAt)
        feedAt = try c.epoch(forKey: .feedAt)
        refreshAfter = try c.decodeIfPresent(Double.self, forKey: .refreshAfter) ?? Polling.minimumInterval
        platforms = try c.map([StopID: LossyList<Arrival>].self, forKey: .platforms).mapValues(\.elements)
    }
}

// MARK: - Vehicles

/// A vehicle in service, where the server last saw it.
public struct Vehicle: Decodable, Hashable, Sendable, Identifiable {
    public let id: VehicleID
    public let line: LineID
    public let direction: Int?
    public let trip: TripID
    public let lat: Double
    public let lon: Double
    /// Degrees clockwise from north. Nil for about one vehicle in five: draw a dot,
    /// not an arrow.
    public let bearing: Double?
    /// Metres per second.
    public let speed: Double?
    /// The stop it is at or heading to.
    public let stop: StopID?
    public let status: VehicleStatus?

    public var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }
}

/// `GET /api/v1/vehicles`.
public struct VehiclesResponse: Decodable, Sendable {
    public let fetchedAt: Date?
    /// The age of every position in the answer: 511 stamps a whole feed with one time.
    public let feedAt: Date?
    public let refreshAfter: TimeInterval
    public let vehicles: [Vehicle]

    private enum CodingKeys: String, CodingKey {
        case fetchedAt, feedAt, refreshAfter, vehicles
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        fetchedAt = try c.epoch(forKey: .fetchedAt)
        feedAt = try c.epoch(forKey: .feedAt)
        refreshAfter = try c.decodeIfPresent(Double.self, forKey: .refreshAfter) ?? Polling.minimumInterval
        vehicles = try c.list(forKey: .vehicles)
    }
}

// MARK: - Alerts

/// When an alert applies. Either bound may be nil, meaning open-ended.
public struct ActivePeriod: Decodable, Hashable, Sendable {
    public let start: Date?
    public let end: Date?

    private enum CodingKeys: String, CodingKey {
        case start, end
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        start = try c.epoch(forKey: .start)
        end = try c.epoch(forKey: .end)
    }
}

/// A service alert active now.
public struct Alert: Decodable, Hashable, Sendable, Identifiable {
    public let id: AlertID
    /// A short summary. 511 often repeats it word for word as `description`.
    public let header: String
    public let description: String
    public let activePeriods: [ActivePeriod]
    /// What 511 says it affects. Both empty means agency-wide.
    public let lines: [LineID]
    public let stops: [StopID]
    /// The stations those stops belong to.
    public let stations: [StationID]
    public let url: URL?

    /// Touches everything: no lines and no stops.
    public var isAgencyWide: Bool { lines.isEmpty && stops.isEmpty }

    private enum CodingKeys: String, CodingKey {
        case id, header, description, activePeriods, lines, stops, stations, url
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(AlertID.self, forKey: .id)
        header = try c.decodeIfPresent(String.self, forKey: .header) ?? ""
        description = try c.decodeIfPresent(String.self, forKey: .description) ?? ""
        activePeriods = try c.list(forKey: .activePeriods)
        lines = try c.list(forKey: .lines)
        stops = try c.list(forKey: .stops)
        stations = try c.list(forKey: .stations)
        url = c.lenient(String.self, forKey: .url).flatMap(URL.init(string:))
    }
}

/// `GET /api/v1/alerts`.
public struct AlertsResponse: Decodable, Sendable {
    public let fetchedAt: Date?
    public let feedAt: Date?
    public let refreshAfter: TimeInterval
    public let alerts: [Alert]

    private enum CodingKeys: String, CodingKey {
        case fetchedAt, feedAt, refreshAfter, alerts
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        fetchedAt = try c.epoch(forKey: .fetchedAt)
        feedAt = try c.epoch(forKey: .feedAt)
        refreshAfter = try c.decodeIfPresent(Double.self, forKey: .refreshAfter) ?? Polling.minimumInterval
        alerts = try c.list(forKey: .alerts)
    }
}

// MARK: - Polling

enum Polling {
    /// The API never sends a `refreshAfter` below this.
    static let minimumInterval: TimeInterval = 10
}
