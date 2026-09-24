import Foundation

// The API grows by adding (API.md, section 6), and one bad value must not fail a
// whole response (section 7). So lists decode element by element, dropping any
// element that fails, and a list that is missing reads as empty: a server older
// than this SDK simply hasn't sent it yet.

/// Decodes as many elements of an array as decode, skipping the rest.
struct LossyList<Element: Decodable>: Decodable {
    let elements: [Element]

    init(from decoder: any Decoder) throws {
        var container = try decoder.unkeyedContainer()
        var elements: [Element] = []
        if let count = container.count { elements.reserveCapacity(count) }
        while !container.isAtEnd {
            // Through `Failable`, which never throws, because a failed decode doesn't
            // move the container on to the next element.
            if let element = try container.decode(Failable<Element>.self).value {
                elements.append(element)
            }
        }
        self.elements = elements
    }
}

extension KeyedDecodingContainer {
    /// A list that may be missing or null (read as empty), whose bad elements are dropped.
    func list<Element: Decodable>(_ type: Element.Type = Element.self, forKey key: Key) throws -> [Element] {
        try decodeIfPresent(LossyList<Element>.self, forKey: key)?.elements ?? []
    }

    /// A map of id to value that may be missing or null (read as empty), whose bad
    /// values are dropped.
    func map<ID: OpaqueIdentifier, Value: Decodable>(_ type: [ID: Value].Type = [ID: Value].self,
                                                    forKey key: Key) throws -> [ID: Value] {
        guard let raw = try decodeIfPresent([String: Failable<Value>].self, forKey: key) else { return [:] }
        var out: [ID: Value] = [:]
        out.reserveCapacity(raw.count)
        for (key, value) in raw {
            if let value = value.value { out[ID(rawValue: key)] = value }
        }
        return out
    }

    /// A nullable value that decodes as nil rather than failing when it is the wrong
    /// shape: a colour that isn't `#RRGGBB`, a URL that isn't one.
    func lenient<Value: Decodable>(_ type: Value.Type = Value.self, forKey key: Key) -> Value? {
        (try? decodeIfPresent(Failable<Value>.self, forKey: key))??.value
    }

    /// A time in epoch seconds, or nil.
    func epoch(forKey key: Key) throws -> Date? {
        try decodeIfPresent(Double.self, forKey: key).map(Date.init(timeIntervalSince1970:))
    }
}

/// One value that may fail to decode, captured as nil instead of an error.
struct Failable<Value: Decodable>: Decodable {
    let value: Value?

    init(from decoder: any Decoder) throws {
        value = try? decoder.singleValueContainer().decode(Value.self)
    }
}

enum JSON {
    /// The decoder for every API response. Times are epoch seconds.
    static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .secondsSince1970
        return decoder
    }

    /// Drops a UTF-8 byte order mark. 511 has sent one in the past, though not on
    /// 2026-09-23, and not every Foundation's JSONDecoder has accepted one.
    static func withoutBOM(_ data: Data) -> Data {
        data.starts(with: [0xEF, 0xBB, 0xBF]) ? data.dropFirst(3) : data
    }
}
