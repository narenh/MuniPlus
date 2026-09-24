import Foundation

/// Keeps `/stations`, `/lines` and line details on disk, revalidating them with the
/// ETags they came with.
///
/// Each resource is read from the first of these that decodes: the copy last
/// downloaded, then the snapshot bundled with the app. A download replaces the
/// copy on disk only once it has decoded, so a truncated write or a captive
/// portal's page can never be what the next launch reads. Bodies are kept exactly
/// as the server sent them, never re-encoded, so a field this version of the SDK
/// doesn't know survives for the one that does.
actor ReferenceStore {
    struct Entry<Value: Sendable>: Sendable {
        let value: Value
        let etag: String?
    }

    /// Whatever could be read at launch, before any request.
    struct Contents: Sendable {
        var stations: Entry<StationsResponse>?
        var lines: Entry<LinesResponse>?
        var lineDetails: [LineID: Entry<LineDetailResponse>] = [:]

        var network: TransitNetwork {
            guard let stations, let lines else { return .empty }
            return TransitNetwork(stations: stations.value, lines: lines.value,
                                  lineDetails: lineDetails.values.map(\.value.line))
        }
    }

    /// Bumped when what's on disk can no longer be read the old way; a new number
    /// is a new folder, and the old one is ignored.
    static let diskFormat = 1

    private let directory: URL?
    private let client: CanopyClient
    private var contents: Contents
    private var refreshing: Task<Bool, any Error>?

    init(contents: Contents, directory: URL?, client: CanopyClient) {
        self.contents = contents
        self.directory = directory.map(Self.folder(in:))
        self.client = client
    }

    var network: TransitNetwork { contents.network }

    // MARK: - Loading

    /// Reads what's on disk, synchronously, for the first screen to draw from.
    static func load(directory: URL?, snapshot: URL?) -> Contents {
        let folder = directory.map(folder(in:))
        let bundled = snapshot.flatMap(Snapshot.read)
        var contents = Contents()
        contents.stations = folder.flatMap { read(StationsResponse.self, .stations, in: $0) } ?? bundled?.stations
        contents.lines = folder.flatMap { read(LinesResponse.self, .lines, in: $0) } ?? bundled?.lines
        var details = bundled?.lineDetails ?? [:]
        if let folder {
            for id in cachedLineIDs(in: folder) {
                if let entry = read(LineDetailResponse.self, .line(id), in: folder) { details[id] = entry }
            }
        }
        contents.lineDetails = details
        return contents
    }

    private static func folder(in directory: URL) -> URL {
        directory.appending(path: "v\(diskFormat)", directoryHint: .isDirectory)
    }

    // MARK: - Refreshing

    /// Revalidates the stations and lines, and the line details in `lineDetails`
    /// that may be out of date. Returns whether anything changed.
    ///
    /// A call while one is running waits for that one rather than asking twice.
    func refresh(lineDetails ids: Set<LineID>) async throws(CanopyError) -> Bool {
        if let refreshing {
            return try await Self.value(of: refreshing)
        }
        let task = Task { try await self.revalidate(lineDetails: ids) }
        refreshing = task
        defer { refreshing = nil }
        return try await Self.value(of: task)
    }

    /// One line's directions: the copy on hand if it matches the lines' version,
    /// else a fresh one. Nil when there's neither.
    func lineDetail(_ id: LineID) async -> LineDetail? {
        let entry = contents.lineDetails[id]
        if let entry, entry.value.version == contents.lines?.value.version {
            return entry.value.line
        }
        _ = try? await revalidateLine(id)
        return contents.lineDetails[id]?.value.line
    }

    private func revalidate(lineDetails ids: Set<LineID>) async throws(CanopyError) -> Bool {
        let stationsChanged = try await revalidate(.stations, current: contents.stations,
                                                   accept: { !$0.stations.isEmpty }) { self.contents.stations = $0 }
        let linesChanged = try await revalidate(.lines, current: contents.lines,
                                                accept: { !$0.lines.isEmpty }) { self.contents.lines = $0 }
        var changed = stationsChanged || linesChanged
        for id in ids.sorted(by: { $0.rawValue < $1.rawValue }) {
            // A detail is as current as the lines when it carries their version and
            // the lines themselves came back unchanged (a new response shape moves
            // the ETags without moving the version).
            let entry = contents.lineDetails[id]
            if let entry, !linesChanged, entry.value.version == contents.lines?.value.version { continue }
            // One line failing (a line 511 has dropped answers 404) mustn't stop the rest.
            if (try? await revalidateLine(id)) == true { changed = true }
        }
        return changed
    }

    private func revalidateLine(_ id: LineID) async throws(CanopyError) -> Bool {
        try await revalidate(.line(id), current: contents.lineDetails[id],
                             accept: { _ in true }) { self.contents.lineDetails[id] = $0 }
    }

    /// Asks for `resource` with the tag on hand. A new body is kept, and written to
    /// disk, only if it decodes and `accept` agrees it's real.
    private func revalidate<Value: Decodable & Sendable>(
        _ resource: Resource,
        current: Entry<Value>?,
        accept: (Value) -> Bool,
        keep: (Entry<Value>) -> Void
    ) async throws(CanopyError) -> Bool {
        let raw = try await client.get(resource.path, ifNoneMatch: current?.etag, timeout: CanopyClient.referenceTimeout)
        if raw.notModified { return false }
        let value = try CanopyClient.decode(Value.self, from: raw.data)
        guard accept(value) else { throw .unreadable("\(resource.path.joined(separator: "/")) came back empty") }
        keep(Entry(value: value, etag: raw.etag))
        if let directory { Self.write(raw.data, etag: raw.etag, resource, in: directory) }
        return true
    }

    private static func value(of task: Task<Bool, any Error>) async throws(CanopyError) -> Bool {
        do {
            return try await task.value
        } catch let error as CanopyError {
            throw error
        } catch {
            throw .transport(error)
        }
    }

    // MARK: - Files

    enum Resource: Sendable {
        case stations, lines, line(LineID)

        var path: [String] {
            switch self {
            case .stations: ["stations"]
            case .lines: ["lines"]
            case .line(let id): ["lines", id.rawValue]
            }
        }

        var fileName: String {
            switch self {
            case .stations: "stations.json"
            case .lines: "lines.json"
            case .line(let id): Self.linePrefix + (id.rawValue.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? id.rawValue) + ".json"
            }
        }

        static let linePrefix = "line-"
    }

    // Each file is the ETag on its first line and the body exactly as received after
    // it, so the two are always written together, in one atomic write.

    private static func read<Value: Decodable & Sendable>(_ type: Value.Type, _ resource: Resource, in folder: URL) -> Entry<Value>? {
        guard let data = try? Data(contentsOf: folder.appending(path: resource.fileName)),
              let newline = data.firstIndex(of: UInt8(ascii: "\n")) else { return nil }
        let tag = String(decoding: data[data.startIndex..<newline], as: UTF8.self)
        guard let value = try? CanopyClient.decode(type, from: Data(data[data.index(after: newline)...])) else { return nil }
        return Entry(value: value, etag: tag.isEmpty ? nil : tag)
    }

    private static func write(_ body: Data, etag: String?, _ resource: Resource, in folder: URL) {
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            var file = Data((etag ?? "").utf8)
            file.append(UInt8(ascii: "\n"))
            file.append(body)
            try file.write(to: folder.appending(path: resource.fileName), options: .atomic)
        } catch {
            // The copy in memory still stands; the next launch reads the older file.
        }
    }

    private static func cachedLineIDs(in folder: URL) -> [LineID] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: folder.path(percentEncoded: false))) ?? []
        return names.compactMap { name in
            guard name.hasPrefix(Resource.linePrefix), name.hasSuffix(".json") else { return nil }
            let encoded = name.dropFirst(Resource.linePrefix.count).dropLast(".json".count)
            return String(encoded).removingPercentEncoding.map(LineID.init(rawValue:))
        }
    }
}
