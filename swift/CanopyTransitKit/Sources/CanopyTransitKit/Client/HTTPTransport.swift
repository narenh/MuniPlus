import Foundation

/// Sends a request and hands back the response, whatever its status.
///
/// The seam tests use to answer requests without a network. The default is
/// `URLSessionTransport`.
public protocol HTTPTransport: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

/// `HTTPTransport` over a `URLSession` that caches nothing.
///
/// The SDK keeps reference data on disk itself and revalidates it with the ETags
/// it stored, so `URLCache` stays out of the way: it would otherwise answer a
/// conditional request from its own copy, or hold a second copy of the stations.
public struct URLSessionTransport: HTTPTransport {
    public let session: URLSession

    public init(session: URLSession = URLSessionTransport.makeSession()) {
        self.session = session
    }

    public static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: configuration)
    }

    public func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        return (data, http)
    }
}
