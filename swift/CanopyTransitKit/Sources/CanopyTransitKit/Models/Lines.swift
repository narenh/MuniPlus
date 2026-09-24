import Foundation

/// A route as 511 lists it: the J, the 38R, the LOWL.
public struct Line: Decodable, Hashable, Sendable, Identifiable {
    public let id: LineID
    /// What goes in a badge: "J", "38R", "NOWL". It can be four characters.
    public let shortName: String
    /// The full name: "J Church".
    public let name: String
    public let color: HexColor?
    /// 511's text colour for the badge. White almost everywhere, even where it barely
    /// reads, so use `legibleText` instead.
    public let textColor: HexColor?
    public let mode: Mode
    /// Curated as not for display. Leave it out of pickers.
    public let hidden: Bool
    /// Lines this one substitutes for: the LOWL replaces the L overnight, the KBUS the
    /// K during an outage. A view of some lines should include the lines replacing them.
    public let replaces: [LineID]
    /// Overnight service (LOWL, NOWL, 90, 91). With `replaces`, it tells the owl
    /// covering a line's corridor at night from a bus standing in for it by day.
    public let owl: Bool

    public init(id: LineID, shortName: String, name: String, color: HexColor? = nil, textColor: HexColor? = nil,
                mode: Mode, hidden: Bool = false, replaces: [LineID] = [], owl: Bool = false) {
        self.id = id
        self.shortName = shortName
        self.name = name
        self.color = color
        self.textColor = textColor
        self.mode = mode
        self.hidden = hidden
        self.replaces = replaces
        self.owl = owl
    }

    /// Black or white, whichever reads better on `color` (API.md, section 7, rule 11).
    public var legibleText: TextShade {
        color?.legibleText ?? .white
    }

    private enum CodingKeys: String, CodingKey {
        case id, shortName, name, color, textColor, mode, hidden, replaces, owl
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(id: try c.decode(LineID.self, forKey: .id),
                  shortName: try c.decode(String.self, forKey: .shortName),
                  name: try c.decode(String.self, forKey: .name),
                  color: c.lenient(forKey: .color),
                  textColor: c.lenient(forKey: .textColor),
                  mode: try c.decode(Mode.self, forKey: .mode),
                  hidden: try c.decodeIfPresent(Bool.self, forKey: .hidden) ?? false,
                  replaces: try c.list(forKey: .replaces),
                  owl: try c.decodeIfPresent(Bool.self, forKey: .owl) ?? false)
    }
}

extension Line {
    /// One way a line runs: the line's most-run stop sequence that way. A line may
    /// have one direction, not two.
    public struct Direction: Decodable, Hashable, Sendable {
        /// 511's direction id, 0 or 1. It only means something within one line;
        /// `headsign` is what to show.
        public let direction: Int
        public let headsign: String
        /// The stations served, in order. The first and last are the terminals.
        public let stations: [StationID]
        /// The same trip, stop by stop, leaving out stops no station claims.
        public let stops: [StopID]
        /// A key into `/shapes`. When nil, draw straight through the stops.
        public let shape: ShapeID?

        private enum CodingKeys: String, CodingKey {
            case direction, headsign, stations, stops, shape
        }

        public init(from decoder: any Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            direction = try c.decode(Int.self, forKey: .direction)
            headsign = try c.decode(String.self, forKey: .headsign)
            stations = try c.list(forKey: .stations)
            stops = try c.list(forKey: .stops)
            shape = try c.decodeIfPresent(ShapeID.self, forKey: .shape)
        }
    }
}

/// One line with its directions, from `/lines/{id}`.
@dynamicMemberLookup
public struct LineDetail: Decodable, Hashable, Sendable, Identifiable {
    public let line: Line
    public let directions: [Line.Direction]

    public var id: LineID { line.id }

    public subscript<T>(dynamicMember keyPath: KeyPath<Line, T>) -> T {
        line[keyPath: keyPath]
    }

    private enum CodingKeys: String, CodingKey {
        case directions
    }

    public init(from decoder: any Decoder) throws {
        line = try Line(from: decoder)
        directions = try decoder.container(keyedBy: CodingKeys.self).list(forKey: .directions)
    }
}

/// `GET /api/v1/lines`.
public struct LinesResponse: Decodable, Sendable {
    public let version: String
    /// In display order: metro, streetcar, cableway, bus; numbered before lettered.
    public let lines: [Line]

    private enum CodingKeys: String, CodingKey {
        case version, lines
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = try c.decode(String.self, forKey: .version)
        lines = try c.list(forKey: .lines)
    }
}

/// `GET /api/v1/lines/{id}`.
public struct LineDetailResponse: Decodable, Sendable {
    public let version: String
    public let line: LineDetail
}
