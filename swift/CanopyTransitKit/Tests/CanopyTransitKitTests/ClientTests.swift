import Foundation
import Testing
@testable import CanopyTransitKit

@Suite("The API client")
struct ClientTests {

    @Test func requestsAreBuiltUnderAPIV1() async throws {
        let transport = FakeTransport { request in
            switch request.path {
            case "/api/v1/lines/SF:N": .fixture("api-md/line-SF_N.json")
            case "/api/v1/arrivals": .fixture("api-md/arrivals.json")
            case "/api/v1/stations/montgomery": .fixture("api-md/station-montgomery.json")
            default: .init(status: 404)
            }
        }
        let client = CanopyClient(baseURL: URL(string: "https://example.com/")!, transport: transport)

        guard case .changed(let line, _) = try await client.line("SF:N") else { Issue.record("no body"); return }
        #expect(line.line.id == "SF:N")
        _ = try await client.arrivals(platforms: ["SF:16992", "SF:17217"], limit: 12)
        _ = try await client.station("montgomery")

        let requests = await transport.requests
        #expect(requests.map { $0.url!.absoluteString } == [
            "https://example.com/api/v1/lines/SF:N",
            "https://example.com/api/v1/arrivals?platforms=SF:16992,SF:17217&limit=12",
            "https://example.com/api/v1/stations/montgomery",
        ])
        #expect(requests.allSatisfy { $0.value(forHTTPHeaderField: "If-None-Match") == nil })
        #expect(requests[1].timeoutInterval == 5)
    }

    @Test func revalidationSendsTheTagBackAsReceived() async throws {
        let tag = #"W/"38d2f22a.b9886267""#
        let transport = FakeTransport { request in
            request.value(forHTTPHeaderField: "If-None-Match") == tag
                ? .init(status: 304, headers: ["ETag": tag])
                : .fixture("api-md/stations.json", headers: ["ETag": tag])
        }
        let client = CanopyClient(baseURL: stagingURL, transport: transport)

        guard case .changed(let stations, let etag) = try await client.stations() else { Issue.record("no body"); return }
        #expect(stations.stations.count == 1)
        #expect(etag == tag)
        guard case .unchanged = try await client.stations(ifNoneMatch: etag) else { Issue.record("not a 304"); return }
    }

    @Test func errorsAreReadLeniently() async {
        let transport = FakeTransport { request in
            switch request.path {
            case "/api/v1/stations/nowhere": .fixture("edge/detail.json", status: 404)
            case "/api/v1/arrivals": .init(status: 502, body: Fixture.data("edge/cloudflare.html"))
            case "/api/v1/vehicles": .init(status: 200, body: Fixture.data("edge/cloudflare.html"))
            default: .failure(.notConnectedToInternet)
            }
        }
        let client = CanopyClient(baseURL: stagingURL, transport: transport)

        await #expect {
            try await client.station("nowhere")
        } throws: { error in
            guard case CanopyError.status(404, let problem) = error else { return false }
            return problem.code == .notFound && problem.message == "Not Found"
        }
        await #expect {
            try await client.arrivals(platforms: ["SF:16992"])
        } throws: { error in
            guard case CanopyError.status(502, let problem) = error else { return false }
            return (error as? CanopyError)?.isOutage == true && problem.code == .unknown("502")
        }
        await #expect {
            try await client.vehicles()
        } throws: { error in
            guard case CanopyError.unreadable = error else { return false }
            return (error as? CanopyError)?.isOutage == true
        }
        await #expect {
            try await client.alerts()
        } throws: { error in
            guard case CanopyError.transport = error else { return false }
            return (error as? CanopyError)?.isOutage == true
        }
    }

    @Test func aClientErrorIsNotAnOutage() {
        #expect(!CanopyError.status(400, Problem(code: .badRequest, message: "")).isOutage)
        #expect(!CanopyError.status(404, Problem(code: .notFound, message: "")).isOutage)
        #expect(CanopyError.status(503, Problem(code: .unavailable, message: "")).isOutage)
        #expect(CanopyError.status(429, Problem(code: .unknown("429"), message: "")).isOutage)
    }

    /// Against staging itself: that a 304 reaches the SDK through `URLSession` with
    /// the cache switched off, rather than being turned back into a 200.
    @Test(.enabled(if: ProcessInfo.processInfo.environment["CANOPY_LIVE"] != nil))
    func liveRevalidation() async throws {
        let client = CanopyClient(baseURL: stagingURL)
        guard case .changed(_, let etag?) = try await client.lines() else { Issue.record("no ETag"); return }
        guard case .unchanged = try await client.lines(ifNoneMatch: etag) else { Issue.record("not a 304"); return }
    }
}
