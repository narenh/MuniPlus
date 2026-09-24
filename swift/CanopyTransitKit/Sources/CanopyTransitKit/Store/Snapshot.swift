import Foundation

/// The reference data an app bundles, so a first launch with no network has
/// stations to show.
///
/// Written at release time by `scripts/make_snapshot.py`, which asks the API for
/// each resource and records the body with its ETag:
///
/// ```json
/// {
///   "format": 1,
///   "stations": { "etag": "W/\"…\"", "body": { …GET /api/v1/stations… } },
///   "lines": { "etag": "…", "body": { … } },
///   "lineDetails": { "SF:N": { "etag": "…", "body": { …GET /api/v1/lines/SF:N… } } }
/// }
/// ```
///
/// The first revalidation sends the snapshot's ETags, so a snapshot that is still
/// current costs a 304 and no download.
enum Snapshot {
    static let format = 1

    struct File: Decodable {
        let format: Int
        let stations: Item<StationsResponse>
        let lines: Item<LinesResponse>
        let lineDetails: [LineID: Item<LineDetailResponse>]

        private enum CodingKeys: String, CodingKey {
            case format, stations, lines, lineDetails
        }

        init(from decoder: any Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            format = try c.decode(Int.self, forKey: .format)
            stations = try c.decode(Item<StationsResponse>.self, forKey: .stations)
            lines = try c.decode(Item<LinesResponse>.self, forKey: .lines)
            lineDetails = try c.map(forKey: .lineDetails)
        }
    }

    struct Item<Value: Decodable & Sendable>: Decodable {
        let etag: String?
        let body: Value

        var entry: ReferenceStore.Entry<Value> {
            ReferenceStore.Entry(value: body, etag: etag)
        }
    }

    struct Contents {
        let stations: ReferenceStore.Entry<StationsResponse>
        let lines: ReferenceStore.Entry<LinesResponse>
        let lineDetails: [LineID: ReferenceStore.Entry<LineDetailResponse>]
    }

    /// The snapshot at `url`, or nil if it's missing, unreadable or a format this
    /// SDK doesn't know.
    static func read(_ url: URL) -> Contents? {
        guard let data = try? Data(contentsOf: url),
              let file = try? CanopyClient.decode(File.self, from: data),
              file.format == format,
              !file.stations.body.stations.isEmpty else { return nil }
        return Contents(stations: file.stations.entry, lines: file.lines.entry,
                        lineDetails: file.lineDetails.mapValues(\.entry))
    }
}
