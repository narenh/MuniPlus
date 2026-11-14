import Foundation

/// Asks a StopMonitoring endpoint for one stop's arrivals: 511 itself with the
/// app's own key, or something answering in 511's format that needs none, such as
/// a relay holding the key for it.
///
/// 511 takes one stop code per call (it refuses a comma list), so a platform of
/// several stops costs several calls.
struct StopMonitoringClient: Sendable {
    enum Failure: Error, Sendable {
        /// 511 said 429. Ask again no sooner than `until`.
        case rateLimited(until: Date)
        case unusable(CanopyError)
    }

    static let endpoint = URL(string: "https://api.511.org/transit/StopMonitoring")!
    /// Per stop, when a StopMonitoring endpoint is the only source of arrivals: a
    /// slow stop shouldn't hold up every board for longer than this.
    static let onlySourceTimeout: TimeInterval = 8

    let endpoint: URL
    /// Sent as `api_key`. Nil sends none.
    let key: String?
    let transport: any HTTPTransport
    /// How long one stop's request may take.
    let timeout: TimeInterval

    init(endpoint: URL = StopMonitoringClient.endpoint, key: String?, transport: any HTTPTransport,
         timeout: TimeInterval = CanopyClient.referenceTimeout) {
        self.endpoint = endpoint
        self.key = key
        self.transport = transport
        self.timeout = timeout
    }

    func stopMonitoring(_ stop: StopID, now: Date) async throws(Failure) -> StopMonitoring {
        guard let (op, code) = StopMonitoring.split(stop) else { return StopMonitoring.none }
        var components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false)!
        components.queryItems = (key.map { [URLQueryItem(name: "api_key", value: $0)] } ?? []) + [
            URLQueryItem(name: "agency", value: op),
            URLQueryItem(name: "stopcode", value: code),
            URLQueryItem(name: "format", value: "json"),
        ]
        let request = URLRequest(url: components.url!, cachePolicy: .reloadIgnoringLocalCacheData,
                                 timeoutInterval: timeout)
        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await transport.send(request)
        } catch {
            throw .unusable(.transport(error))
        }
        if response.statusCode == 429 {
            throw .rateLimited(until: Self.retryTime(response, now: now))
        }
        guard (200..<300).contains(response.statusCode) else {
            throw .unusable(.status(response.statusCode, Problem(status: response.statusCode, body: data)))
        }
        do {
            return try JSON.decoder().decode(StopMonitoring.self, from: JSON.withoutBOM(data))
        } catch {
            throw .unusable(.unreadable(String(describing: error)))
        }
    }

    /// When a 429 says to ask again: `Retry-After`, else `RateLimit-Reset`, else the
    /// top of the next hour, when 511's hourly allowance starts over.
    static func retryTime(_ response: HTTPURLResponse, now: Date) -> Date {
        for header in ["Retry-After", "RateLimit-Reset"] {
            guard let value = response.value(forHTTPHeaderField: header)?.trimmingCharacters(in: .whitespaces) else { continue }
            if let seconds = Double(value) {
                // Seconds to wait, or, if it's that large, an epoch time.
                return seconds > 1_000_000_000 ? Date(timeIntervalSince1970: seconds) : now.addingTimeInterval(seconds)
            }
            let http = DateFormatter()
            http.locale = Locale(identifier: "en_US_POSIX")
            http.timeZone = TimeZone(identifier: "GMT")
            http.dateFormat = "EEE, dd MMM yyyy HH:mm:ss zzz"
            if let date = http.date(from: value) { return date }
        }
        let hour = 3600.0
        return Date(timeIntervalSince1970: (now.timeIntervalSince1970 / hour).rounded(.down) * hour + hour)
    }
}

extension StopMonitoring {
    /// Nothing to report: what a stop id that can't be asked of 511 answers.
    static let none = StopMonitoring(responseTimestamp: nil, visits: [])

    init(responseTimestamp: Date?, visits: [Visit]) {
        self.responseTimestamp = responseTimestamp
        self.visits = visits
    }
}
