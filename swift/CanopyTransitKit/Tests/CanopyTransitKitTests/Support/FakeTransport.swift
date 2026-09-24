import Foundation
@testable import CanopyTransitKit

/// Answers requests from a handler, and remembers every request it was sent.
actor FakeTransport: HTTPTransport {
    struct Reply: Sendable {
        var status: Int = 200
        var body: Data = Data()
        var headers: [String: String] = [:]
        var error: URLError?

        static func json(_ body: Data, status: Int = 200, headers: [String: String] = [:]) -> Reply {
            Reply(status: status, body: body, headers: headers.merging(["Content-Type": "application/json"]) { a, _ in a })
        }

        static func fixture(_ path: String, status: Int = 200, headers: [String: String] = [:]) -> Reply {
            .json(Fixture.data(path), status: status, headers: headers)
        }

        static func failure(_ code: URLError.Code) -> Reply {
            Reply(error: URLError(code))
        }
    }

    typealias Handler = @Sendable (URLRequest) -> Reply

    private var handler: Handler
    private(set) var requests: [URLRequest] = []

    init(_ handler: @escaping Handler = { _ in Reply(status: 404) }) {
        self.handler = handler
    }

    func answer(with handler: @escaping Handler) {
        self.handler = handler
    }

    /// Requests to one host, in order.
    func requests(to host: String) -> [URLRequest] {
        requests.filter { $0.url?.host == host }
    }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        requests.append(request)
        let reply = handler(request)
        if let error = reply.error { throw error }
        let response = HTTPURLResponse(url: request.url!, statusCode: reply.status, httpVersion: "HTTP/1.1",
                                       headerFields: reply.headers)!
        return (reply.body, response)
    }
}

extension URLRequest {
    var path: String { url?.path(percentEncoded: false) ?? "" }

    func query(_ name: String) -> String? {
        URLComponents(url: url!, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == name }?.value
    }
}

let stagingURL = URL(string: "https://muni-staging.canopysf.com")!
