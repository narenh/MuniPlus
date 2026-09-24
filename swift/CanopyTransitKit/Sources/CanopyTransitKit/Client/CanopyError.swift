import Foundation

/// An error body, as far as one could be read.
///
/// The API's own errors are `{"error", "message"}`. A path that doesn't exist gets
/// the framework's `{"detail": …}` instead, and a proxy in front may answer with an
/// HTML page, so this is decoded leniently and falls back to the status code.
public struct Problem: Hashable, Sendable {
    public let code: ProblemCode
    /// For a person to read.
    public let message: String

    public init(code: ProblemCode, message: String) {
        self.code = code
        self.message = message
    }

    /// Whatever can be made of an error response's body.
    init(status: Int, body: Data) {
        let json = try? JSONSerialization.jsonObject(with: JSON.withoutBOM(body)) as? [String: Any]
        let fallback = Problem.code(forStatus: status)
        if let error = json?["error"] as? String {
            code = ProblemCode(rawValue: error)
            message = json?["message"] as? String ?? ""
        } else if let detail = json?["detail"] {
            code = fallback
            // A string, or for a validation error, a list of objects with a `msg`.
            message = (detail as? String)
                ?? (detail as? [[String: Any]])?.compactMap { $0["msg"] as? String }.joined(separator: "; ")
                ?? ""
        } else {
            code = fallback
            message = HTTPURLResponse.localizedString(forStatusCode: status)
        }
    }

    private static func code(forStatus status: Int) -> ProblemCode {
        switch status {
        case 400: .badRequest
        case 404: .notFound
        case 503: .unavailable
        default: .unknown(String(status))
        }
    }
}

/// Why a request to the API failed.
public enum CanopyError: Error, Sendable {
    /// No response: offline, a timeout, a TLS failure.
    case transport(any Error)
    /// A non-2xx response.
    case status(Int, Problem)
    /// A 2xx response whose body isn't the JSON it should be: a captive portal's
    /// login page, a proxy's HTML, a truncated body.
    case unreadable(String)

    /// Whether this says the server couldn't answer rather than that the request
    /// was wrong: what makes arrivals fall back to 511.
    var isOutage: Bool {
        switch self {
        case .transport, .unreadable: true
        case .status(let status, _): status >= 500 || status == 429
        }
    }
}
