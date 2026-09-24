import CoreLocation
import Foundation

/// One place a rider stands: a pole, a shelter, a subway platform.
///
/// Usually exactly one 511 stop. Where 511 numbers one place more than once, one
/// platform holds all of those stops, and its lines and arrivals are theirs
/// together. Homes and favourites are platforms: store `id`, and resolve it
/// against the latest station list with `TransitNetwork.resolvePlatform(_:)`.
public struct Platform: Decodable, Hashable, Sendable, Identifiable {
    /// The platform's primary stop.
    public let id: StopID
    public let heading: Heading
    /// Every line stopping at any of its stops.
    public let lines: [LineID]
    /// Its 511 stops, primary first. Never empty.
    public let stops: [StopID]
    /// Ids it has had and lost. Usually empty.
    public let formerIds: [StopID]

    public init(id: StopID, heading: Heading, lines: [LineID] = [], stops: [StopID] = [], formerIds: [StopID] = []) {
        self.id = id
        self.heading = heading
        self.lines = lines
        self.stops = stops.isEmpty ? [id] : stops
        self.formerIds = formerIds
    }

    private enum CodingKeys: String, CodingKey {
        case id, heading, lines, stops, formerIds
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(id: try c.decode(StopID.self, forKey: .id),
                  heading: try c.decode(Heading.self, forKey: .heading),
                  lines: try c.list(forKey: .lines),
                  stops: try c.list(forKey: .stops),
                  formerIds: try c.list(forKey: .formerIds))
    }
}

/// A station reachable on foot from another.
public struct Transfer: Decodable, Hashable, Sendable {
    public let to: StationID
    /// The other station's name.
    public let name: String
    public let mode: TransferMode

    public init(to: StationID, name: String, mode: TransferMode) {
        self.to = to
        self.name = name
        self.mode = mode
    }
}

/// The set of platforms a rider thinks of as one place: Embarcadero, or Church &
/// Market. As `/stations` lists it: enough to draw the map, search, and ask for
/// arrivals with no further request.
public struct Station: Decodable, Hashable, Sendable, Identifiable {
    /// Permanent. Store it.
    public let id: StationID
    public let name: String
    /// The centroid of its platforms.
    public let lat: Double
    public let lon: Double
    /// Every line serving any of its platforms, in `/lines` order.
    public let lines: [LineID]
    /// The modes of those lines, rail first.
    public let modes: [Mode]
    /// Operators with platforms here first, then any with none in the data yet
    /// (BART at Embarcadero).
    public let operators: [OperatorID]
    /// In display order.
    public let platforms: [Platform]
    /// Stations reachable on foot, sorted by the other station's name.
    public let transfers: [Transfer]

    public init(id: StationID, name: String, lat: Double, lon: Double, lines: [LineID] = [], modes: [Mode] = [],
                operators: [OperatorID] = [], platforms: [Platform] = [], transfers: [Transfer] = []) {
        self.id = id
        self.name = name
        self.lat = lat
        self.lon = lon
        self.lines = lines
        self.modes = modes
        self.operators = operators
        self.platforms = platforms
        self.transfers = transfers
    }

    public var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }

    private enum CodingKeys: String, CodingKey {
        case id, name, lat, lon, lines, modes, operators, platforms, transfers
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(id: try c.decode(StationID.self, forKey: .id),
                  name: try c.decode(String.self, forKey: .name),
                  lat: try c.decode(Double.self, forKey: .lat),
                  lon: try c.decode(Double.self, forKey: .lon),
                  lines: try c.list(forKey: .lines),
                  modes: try c.list(forKey: .modes),
                  operators: try c.list(forKey: .operators),
                  platforms: try c.list(forKey: .platforms),
                  transfers: try c.list(forKey: .transfers))
    }
}

/// A named, ordered group of underground stations: the Market Street subway.
public struct Subway: Decodable, Hashable, Sendable, Identifiable {
    public let id: SubwayID
    public let name: String
    /// In order along the subway.
    public let stations: [StationID]

    private enum CodingKeys: String, CodingKey {
        case id, name, stations
    }

    public init(id: SubwayID, name: String, stations: [StationID]) {
        self.id = id
        self.name = name
        self.stations = stations
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(id: try c.decode(SubwayID.self, forKey: .id),
                  name: try c.decode(String.self, forKey: .name),
                  stations: try c.list(forKey: .stations))
    }
}

/// `GET /api/v1/stations`.
public struct StationsResponse: Decodable, Sendable {
    /// The data version. It changes whenever the data does.
    public let version: String
    /// Former station id → current station id. Apply it to saved station ids on
    /// every fetch (`TransitNetwork.currentStationID(_:)` does).
    public let formerIds: [StationID: StationID]
    public let stations: [Station]
    public let subways: [Subway]

    private enum CodingKeys: String, CodingKey {
        case version, formerIds, stations, subways
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = try c.decode(String.self, forKey: .version)
        formerIds = try c.map(forKey: .formerIds)
        stations = try c.list(forKey: .stations)
        subways = try c.list(forKey: .subways)
    }
}

/// A platform as `/stations/{id}` describes it: everything in `Platform`, plus
/// where it is and what it's called.
@dynamicMemberLookup
public struct PlatformDetail: Decodable, Hashable, Sendable, Identifiable {
    public let platform: Platform
    /// Signage ("Platform 1", "To Castro"), where curated. Usually nil. Not 511's name.
    public let name: String?
    /// 511's name for the primary stop.
    public let stopName: String
    /// The centroid of its stops, which are metres apart where there are several.
    public let lat: Double
    public let lon: Double

    public var id: StopID { platform.id }

    public var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }

    public subscript<T>(dynamicMember keyPath: KeyPath<Platform, T>) -> T {
        platform[keyPath: keyPath]
    }

    private enum CodingKeys: String, CodingKey {
        case name, stopName, lat, lon
    }

    public init(from decoder: any Decoder) throws {
        platform = try Platform(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decodeIfPresent(String.self, forKey: .name)
        stopName = try c.decode(String.self, forKey: .stopName)
        lat = try c.decode(Double.self, forKey: .lat)
        lon = try c.decode(Double.self, forKey: .lon)
    }
}

/// A subway a station is part of. Its stations in order are in `/stations`.
public struct SubwayRef: Decodable, Hashable, Sendable, Identifiable {
    public let id: SubwayID
    public let name: String
}

/// One station in full, from `/stations/{id}`.
@dynamicMemberLookup
public struct StationDetail: Decodable, Hashable, Sendable, Identifiable {
    /// The station as `/stations` lists it.
    public let station: Station
    /// The same platforms as `station.platforms`, with their positions and names.
    public let platforms: [PlatformDetail]
    public let subways: [SubwayRef]
    /// Alerts active now that touch this station, including agency-wide ones.
    public let alerts: [Alert]

    public var id: StationID { station.id }

    public subscript<T>(dynamicMember keyPath: KeyPath<Station, T>) -> T {
        station[keyPath: keyPath]
    }

    private enum CodingKeys: String, CodingKey {
        case platforms, subways, alerts
    }

    public init(from decoder: any Decoder) throws {
        station = try Station(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        platforms = try c.list(forKey: .platforms)
        subways = try c.list(forKey: .subways)
        alerts = try c.list(forKey: .alerts)
    }
}

/// `GET /api/v1/stations/{id}`.
public struct StationDetailResponse: Decodable, Sendable {
    public let version: String
    public let station: StationDetail
}
