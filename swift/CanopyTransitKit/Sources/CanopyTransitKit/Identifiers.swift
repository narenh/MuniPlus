import Foundation

/// An id the API hands out.
///
/// Every id is opaque (API.md, section 3): pass back exactly what the API gave you,
/// and never parse or build one. Each kind is its own type so a stop id can't be
/// passed where a station id is wanted. They encode and decode as bare strings,
/// so an id stored by one version of the app reads back in the next.
public protocol OpaqueIdentifier: RawRepresentable<String>, Hashable, Sendable, Codable, CodingKeyRepresentable,
    ExpressibleByStringLiteral, CustomStringConvertible {
    init(rawValue: String)
}

extension OpaqueIdentifier {
    public init(_ rawValue: String) {
        self.init(rawValue: rawValue)
    }

    public init(stringLiteral value: String) {
        self.init(rawValue: value)
    }

    public var description: String { rawValue }
}

/// A station: our own id, permanent (`embarcadero`, `500Parnassus`). Store it.
public struct StationID: OpaqueIdentifier {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
}

/// A 511 stop (`SF:16992`). A platform's id is its primary stop's, so this is also
/// the type of every platform id. Store a platform's id, not a stop's.
public struct StopID: OpaqueIdentifier {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
}

/// A line (`SF:J`, `SF:38R`). Stable in practice; fine to store for filters.
public struct LineID: OpaqueIdentifier {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
}

/// A drawn path (`SF:9717`). Changes with 511's data: never store it.
public struct ShapeID: OpaqueIdentifier {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
}

/// 511's trip id (`SF:12134484_M11`). Lasts minutes: never store it.
public struct TripID: OpaqueIdentifier {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
}

/// A vehicle (`SF:2019`). Lasts minutes: never store it.
public struct VehicleID: OpaqueIdentifier {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
}

/// A subway (`marketStreetSubway`).
public struct SubwayID: OpaqueIdentifier {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
}

/// A transit operator by its 511 code: `SF` is Muni, `BA` is BART. Store it.
public struct OperatorID: OpaqueIdentifier {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
}

/// 511's alert id (`SF_15752`).
public struct AlertID: OpaqueIdentifier {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
}
