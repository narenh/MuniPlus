import Foundation

/// Asks 511 for one stop's arrivals, with the app's own key.
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

    let key: String
    let transport: any HTTPTransport

    func stopMonitoring(_ stop: StopID, now: Date) async throws(Failure) -> StopMonitoring {
        guard let (op, code) = StopMonitoring.split(stop) else { return StopMonitoring.none }
        var components = URLComponents(url: Self.endpoint, resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "api_key", value: key),
            URLQueryItem(name: "agency", value: op),
            URLQueryItem(name: "stopcode", value: code),
            URLQueryItem(name: "format", value: "json"),
        ]
        let request = URLRequest(url: components.url!, cachePolicy: .reloadIgnoringLocalCacheData,
                                 timeoutInterval: CanopyClient.referenceTimeout)
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
