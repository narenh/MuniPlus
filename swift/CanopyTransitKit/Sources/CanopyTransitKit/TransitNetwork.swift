import Foundation

/// A platform found from a stored id, and the station it now belongs to.
public struct ResolvedPlatform: Hashable, Sendable {
    public let station: Station
    public let platform: Platform
}

/// One version of the reference data, from `/stations` and `/lines`, with the
/// indexes to look things up in it. Immutable: a refresh builds a new one.
public struct TransitNetwork: Sendable {
    /// The data version (the same on stations and lines).
    public let version: String
    public let stations: [Station]
    /// In display order.
    public let lines: [Line]
    public let subways: [Subway]
    /// Former station id → current station id.
    public let formerStationIDs: [StationID: StationID]
    /// The line diagrams this network has, by line. Not every line need have one.
    public let lineDetails: [LineID: LineDetail]

    private let stationsByID: [StationID: Station]
    private let linesByID: [LineID: Line]
    private let subwaysByID: [SubwayID: Subway]
    /// Stored platform id → (station, platform index), one map per rule in API.md
    /// section 3, tried in that order.
    private let platformsByID: [StopID: PlatformIndex]
    private let platformsByStop: [StopID: PlatformIndex]
    private let platformsByFormerID: [StopID: PlatformIndex]

    private struct PlatformIndex: Sendable {
        let station: Int
        let platform: Int
    }

    public init(stations: StationsResponse, lines: LinesResponse, lineDetails: [LineDetail] = []) {
        self.init(version: stations.version, stations: stations.stations, lines: lines.lines, subways: stations.subways,
                  formerStationIDs: stations.formerIds, lineDetails: lineDetails)
    }

    public init(version: String, stations: [Station], lines: [Line], subways: [Subway] = [],
                formerStationIDs: [StationID: StationID] = [:], lineDetails: [LineDetail] = []) {
        self.version = version
        self.stations = stations
        self.lines = lines
        self.subways = subways
        self.formerStationIDs = formerStationIDs
        self.lineDetails = Dictionary(lineDetails.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        stationsByID = Dictionary(stations.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        linesByID = Dictionary(lines.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        subwaysByID = Dictionary(subways.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })

        // The first claim on an id wins within each map, stations in list order, so a
        // conflict in the data resolves the same way on every build.
        var byID: [StopID: PlatformIndex] = [:]
        var byStop: [StopID: PlatformIndex] = [:]
        var byFormer: [StopID: PlatformIndex] = [:]
        for (s, station) in stations.enumerated() {
            for (p, platform) in station.platforms.enumerated() {
                let index = PlatformIndex(station: s, platform: p)
                if byID[platform.id] == nil { byID[platform.id] = index }
                for stop in platform.stops where byStop[stop] == nil { byStop[stop] = index }
                for former in platform.formerIds where byFormer[former] == nil { byFormer[former] = index }
            }
        }
        platformsByID = byID
        platformsByStop = byStop
        platformsByFormerID = byFormer
    }

    /// Nothing at all: before any data has loaded.
    public static let empty = TransitNetwork(version: "", stations: [], lines: [])

    public var isEmpty: Bool { stations.isEmpty }

    // MARK: - Stations

    /// The station with exactly this id.
    public func station(_ id: StationID) -> Station? {
        stationsByID[id]
    }

    /// The id a stored station id means now: itself, or where `formerIds` sends it.
    /// Nil when the station no longer exists. Save the result (API.md, section 7,
    /// rule 6).
    public func currentStationID(_ stored: StationID) -> StationID? {
        var id = stored
        // A former id normally points straight at a live one; the bound only guards
        // against a cycle in bad data.
        for _ in 0..<8 {
            if stationsByID[id] != nil { return id }
            guard let next = formerStationIDs[id] else { return nil }
            id = next
        }
        return nil
    }

    /// The station a stored station id means now.
    public func resolveStation(_ stored: StationID) -> Station? {
        currentStationID(stored).flatMap { stationsByID[$0] }
    }

    // MARK: - Platforms

    /// The platform a stored platform id means now, and its station, which may not
    /// be the station it used to belong to.
    ///
    /// By `id`, then by `stops` (the platform was merged into another), then by
    /// `formerIds` (511 renumbered its stop). Nil when none match: the platform is
    /// gone, so say so rather than dropping it (API.md, section 7, rule 5).
    public func resolvePlatform(_ stored: StopID) -> ResolvedPlatform? {
        guard let index = platformsByID[stored] ?? platformsByStop[stored] ?? platformsByFormerID[stored] else {
            return nil
        }
        let station = stations[index.station]
        return ResolvedPlatform(station: station, platform: station.platforms[index.platform])
    }

    // MARK: - Lines

    public func line(_ id: LineID) -> Line? {
        linesByID[id]
    }

    /// `line`'s directions, if this network has them.
    public func lineDetail(_ id: LineID) -> LineDetail? {
        lineDetails[id]
    }

    /// The lines standing in for `id`: its owl, its substitute bus.
    public func replacements(of id: LineID) -> [Line] {
        lines.filter { $0.replaces.contains(id) }
    }

    // MARK: - Subways

    public func subway(_ id: SubwayID) -> Subway? {
        subwaysByID[id]
    }

    /// Replaces the line diagrams, keeping everything else.
    func with(lineDetails: [LineDetail]) -> TransitNetwork {
        TransitNetwork(version: version, stations: stations, lines: lines, subways: subways,
                       formerStationIDs: formerStationIDs, lineDetails: lineDetails)
    }
}
