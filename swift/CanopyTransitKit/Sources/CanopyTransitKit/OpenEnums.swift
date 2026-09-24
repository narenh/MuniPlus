import Foundation

/// A string enum the API may add values to (API.md, section 7, rule 1).
///
/// A value this version of the SDK doesn't know decodes as `unknown(raw)` rather
/// than failing, so a new mode or heading on the server never breaks a response.
public protocol OpenEnum: RawRepresentable<String>, Hashable, Sendable, Codable {
    /// Every value this version of the SDK knows by name.
    static var knownCases: [Self] { get }
    static func unknown(_ rawValue: String) -> Self
    init(rawValue: String)
}

extension OpenEnum {
    public init(rawValue: String) {
        self = Self.knownCases.first { $0.rawValue == rawValue } ?? .unknown(rawValue)
    }
}

/// How a line runs. BART and Caltrain will add values.
public enum Mode: OpenEnum {
    case metro, streetcar, cableway, bus
    case unknown(String)

    public static let knownCases: [Mode] = [.metro, .streetcar, .cableway, .bus]

    public var rawValue: String {
        switch self {
        case .metro: "metro"
        case .streetcar: "streetcar"
        case .cableway: "cableway"
        case .bus: "bus"
        case .unknown(let raw): raw
        }
    }

    /// Rail modes draw as circle badges, buses as pills sized to their label.
    public var isRail: Bool {
        switch self {
        case .metro, .streetcar, .cableway: true
        case .bus, .unknown: false
        }
    }
}

/// The direction a rider on a platform travels, curated as riders name it rather
/// than as a strict compass bearing.
public enum Heading: OpenEnum {
    case northbound, southbound, eastbound, westbound
    case unknown(String)

    public static let knownCases: [Heading] = [.northbound, .southbound, .eastbound, .westbound]

    public var rawValue: String {
        switch self {
        case .northbound: "northbound"
        case .southbound: "southbound"
        case .eastbound: "eastbound"
        case .westbound: "westbound"
        case .unknown(let raw): raw
        }
    }
}

/// Whether a trip reaches a platform or starts there.
public enum ArrivalKind: OpenEnum {
    /// An ordinary arrival, or with `terminates`, a trip ending here.
    case arrival
    /// The trip starts at this platform, and `time` is when it leaves.
    case departure
    case unknown(String)

    public static let knownCases: [ArrivalKind] = [.arrival, .departure]

    public var rawValue: String {
        switch self {
        case .arrival: "arrival"
        case .departure: "departure"
        case .unknown(let raw): raw
        }
    }
}

/// How a vehicle relates to its `stop`.
public enum VehicleStatus: OpenEnum {
    case incomingAt, stoppedAt, inTransitTo
    case unknown(String)

    public static let knownCases: [VehicleStatus] = [.incomingAt, .stoppedAt, .inTransitTo]

    public var rawValue: String {
        switch self {
        case .incomingAt: "incomingAt"
        case .stoppedAt: "stoppedAt"
        case .inTransitTo: "inTransitTo"
        case .unknown(let raw): raw
        }
    }
}

/// How a transfer between two stations is walked.
public enum TransferMode: OpenEnum {
    /// A passage between the two.
    case indoor
    case street
    case unknown(String)

    public static let knownCases: [TransferMode] = [.indoor, .street]

    public var rawValue: String {
        switch self {
        case .indoor: "indoor"
        case .street: "street"
        case .unknown(let raw): raw
        }
    }
}

/// The `error` code of a `Problem`. Handle an unknown one as its status's general case.
public enum ProblemCode: OpenEnum {
    case badRequest, notFound, unavailable
    case unknown(String)

    public static let knownCases: [ProblemCode] = [.badRequest, .notFound, .unavailable]

    public var rawValue: String {
        switch self {
        case .badRequest: "bad-request"
        case .notFound: "not-found"
        case .unavailable: "unavailable"
        case .unknown(let raw): raw
        }
    }
}
