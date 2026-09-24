import Foundation

/// The Muni+ v1 API, one method per endpoint.
///
/// Reference data (stations, lines, shapes) comes back as a `Conditional`: pass the
/// ETag you stored as `ifNoneMatch`, and an unchanged resource answers `.unchanged`
/// with no body. `CanopyTransit` does all of that for you; use the client directly
/// for what it doesn't cover.
public actor CanopyClient {
    /// A reference resource, or word that the copy you have is current.
    public enum Conditional<Value: Sendable>: Sendable {
        /// A new body, and the ETag to send back next time, exactly as received.
        case changed(Value, etag: String?)
        /// The `ifNoneMatch` sent is current.
        case unchanged
    }

    public nonisolated let baseURL: URL
    private let transport: any HTTPTransport

    /// Realtime requests give up quickly: past this, arrivals are better asked of 511.
    static let realtimeTimeout: TimeInterval = 5
    static let referenceTimeout: TimeInterval = 20

    public init(baseURL: URL, transport: any HTTPTransport = URLSessionTransport()) {
        self.baseURL = baseURL
        self.transport = transport
    }

    // MARK: - Reference data

    public func stations(ifNoneMatch etag: String? = nil) async throws(CanopyError) -> Conditional<StationsResponse> {
        try await conditional(StationsResponse.self, path: ["stations"], ifNoneMatch: etag)
    }

    /// One station in full, with its current alerts. A former id answers with the
    /// current station.
    public func station(_ id: StationID) async throws(CanopyError) -> StationDetailResponse {
        let raw = try await get(["stations", id.rawValue], timeout: Self.referenceTimeout)
        return try Self.decode(StationDetailResponse.self, from: raw.data)
    }

    public func lines(ifNoneMatch etag: String? = nil) async throws(CanopyError) -> Conditional<LinesResponse> {
        try await conditional(LinesResponse.self, path: ["lines"], ifNoneMatch: etag)
    }

    public func line(_ id: LineID, ifNoneMatch etag: String? = nil) async throws(CanopyError) -> Conditional<LineDetailResponse> {
        try await conditional(LineDetailResponse.self, path: ["lines", id.rawValue], ifNoneMatch: etag)
    }

    public func shapes(ifNoneMatch etag: String? = nil) async throws(CanopyError) -> Conditional<ShapesResponse> {
        try await conditional(ShapesResponse.self, path: ["shapes"], ifNoneMatch: etag)
    }

    // MARK: - Realtime

    /// The next arrivals at up to 50 platforms, keyed by platform id. Any stop or
    /// former id of a platform works, and is answered under the platform's `id`.
    ///
    /// This asks the API alone. `CanopyTransit`'s feeds fall back to 511 when it
    /// can't answer.
    public func arrivals(platforms: [StopID], limit: Int? = nil) async throws(CanopyError) -> ArrivalsResponse {
        var query = [URLQueryItem(name: "platforms", value: platforms.map(\.rawValue).joined(separator: ","))]
        if let limit { query.append(URLQueryItem(name: "limit", value: String(limit))) }
        let raw = try await get(["arrivals"], query: query, timeout: Self.realtimeTimeout)
        return try Self.decode(ArrivalsResponse.self, from: raw.data)
    }

    /// Vehicles in service, on `lines` or, with none, everywhere.
    public func vehicles(lines: [LineID] = []) async throws(CanopyError) -> VehiclesResponse {
        let query = lines.isEmpty ? [] : [URLQueryItem(name: "line", value: lines.map(\.rawValue).joined(separator: ","))]
        let raw = try await get(["vehicles"], query: query, timeout: Self.realtimeTimeout)
        return try Self.decode(VehiclesResponse.self, from: raw.data)
    }

    /// Alerts active now touching any of the lines, stations or platforms named,
    /// plus every agency-wide one. With none named, every alert.
    public func alerts(lines: [LineID] = [], stations: [StationID] = [],
                       platforms: [StopID] = []) async throws(CanopyError) -> AlertsResponse {
        var query: [URLQueryItem] = []
        if !lines.isEmpty { query.append(URLQueryItem(name: "line", value: lines.map(\.rawValue).joined(separator: ","))) }
        if !stations.isEmpty { query.append(URLQueryItem(name: "station", value: stations.map(\.rawValue).joined(separator: ","))) }
        if !platforms.isEmpty { query.append(URLQueryItem(name: "platforms", value: platforms.map(\.rawValue).joined(separator: ","))) }
        let raw = try await get(["alerts"], query: query, timeout: Self.referenceTimeout)
        return try Self.decode(AlertsResponse.self, from: raw.data)
    }

    // MARK: - Requests

    struct Raw: Sendable {
        let data: Data
        let etag: String?
        let notModified: Bool
    }

    private func conditional<Value: Decodable & Sendable>(_ type: Value.Type, path: [String],
                                                         ifNoneMatch etag: String?) async throws(CanopyError) -> Conditional<Value> {
        let raw = try await get(path, ifNoneMatch: etag, timeout: Self.referenceTimeout)
        if raw.notModified { return .unchanged }
        return .changed(try Self.decode(type, from: raw.data), etag: raw.etag)
    }

    /// A GET under `/api/v1/`. A 304 comes back as `notModified`; any other status
    /// outside 2xx throws.
    func get(_ path: [String], query: [URLQueryItem] = [], ifNoneMatch etag: String? = nil,
             timeout: TimeInterval) async throws(CanopyError) -> Raw {
        var request = URLRequest(url: url(path, query: query), cachePolicy: .reloadIgnoringLocalCacheData,
                                 timeoutInterval: timeout)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        // Sent back exactly as received: Cloudflare weakens every tag to W/"…".
        if let etag { request.setValue(etag, forHTTPHeaderField: "If-None-Match") }

        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await transport.send(request)
        } catch {
            throw .transport(error)
        }
        let tag = response.value(forHTTPHeaderField: "ETag")
        switch response.statusCode {
        case 304 where etag != nil:
            return Raw(data: Data(), etag: tag ?? etag, notModified: true)
        case 200..<300:
            return Raw(data: data, etag: tag, notModified: false)
        default:
            throw .status(response.statusCode, Problem(status: response.statusCode, body: data))
        }
    }

    private func url(_ path: [String], query: [URLQueryItem]) -> URL {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        let base = components.percentEncodedPath.hasSuffix("/")
            ? String(components.percentEncodedPath.dropLast())
            : components.percentEncodedPath
        let segments = (["api", "v1"] + path).map {
            $0.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? $0
        }
        components.percentEncodedPath = base + "/" + segments.joined(separator: "/")
        components.queryItems = query.isEmpty ? nil : query
        return components.url!
    }

    static func decode<Value: Decodable>(_ type: Value.Type, from data: Data) throws(CanopyError) -> Value {
        do {
            return try JSON.decoder().decode(type, from: JSON.withoutBOM(data))
        } catch {
            throw .unreadable(String(describing: error))
        }
    }
}
