import Foundation
import Testing
@testable import CanopyTransitKit

@Suite("Falling back to 511")
struct ArrivalsProviderTests {
    static let eastbound = Platform(id: "SF:16992", heading: .eastbound)
    static let westbound = Platform(id: "SF:17217", heading: .westbound)
    static let apiHost = "muni-staging.canopysf.com"
    static let host511 = "api.511.org"

    let clock = TestClock(StopMonitoringTests.answeredAt)
    let transport = FakeTransport()

    func provider(key: String? = "test-key") -> ArrivalsProvider {
        ArrivalsProvider(client: CanopyClient(baseURL: stagingURL, transport: transport),
                         fallback: key.map { StopMonitoringClient(key: $0, transport: transport) },
                         now: { [clock] in clock.now })
    }

    /// An API answer whose predictions are `age` seconds old.
    static func api(age: TimeInterval, at now: Date) -> FakeTransport.Reply {
        let feedAt = Int(now.timeIntervalSince1970 - age)
        let body = """
        {"fetchedAt": \(feedAt), "feedAt": \(feedAt), "refreshAfter": 25, "platforms": {
          "SF:16992": [{"line": "SF:J", "direction": 1, "headsign": "Embarcadero Station", "time": \(feedAt + 600),
                        "kind": "arrival", "terminates": true, "trip": "SF:api", "vehicle": null}],
          "SF:17217": []}}
        """
        return .json(Data(body.utf8))
    }

    static func from511(_ request: URLRequest) -> FakeTransport.Reply {
        guard let code = request.query("stopcode") else { return .init(status: 400) }
        return .fixture("511/stopmonitoring-SF-\(code).json")
    }

    /// The API answering with `api`, and 511 with its recordings.
    func answer(api: @escaping @Sendable (URLRequest) -> FakeTransport.Reply,
                on511: @escaping @Sendable (URLRequest) -> FakeTransport.Reply = ArrivalsProviderTests.from511) async {
        await transport.answer { request in
            request.url?.host == Self.host511 ? on511(request) : api(request)
        }
    }

    func requests(to host: String) async -> Int {
        await transport.requests(to: host).count
    }

    // MARK: -

    @Test func aFreshAnswerComesFromTheAPI() async throws {
        let now = clock.now
        await answer(api: { _ in Self.api(age: 30, at: now) })
        let answer = try await provider().arrivals(for: [Self.eastbound, Self.westbound], limit: 6)
        #expect(answer.platforms["SF:16992"]?.map(\.trip) == ["SF:api"])
        #expect(answer.refreshAfter == 25)
        #expect(await requests(to: Self.host511) == 0)
    }

    @Test(arguments: [
        FakeTransport.Reply.failure(.timedOut),
        .failure(.notConnectedToInternet),
        .init(status: 503, body: Data(#"{"error": "unavailable", "message": "realtime is off"}"#.utf8)),
        .init(status: 502, body: Fixture.data("edge/cloudflare.html")),
        .init(status: 200, body: Fixture.data("edge/cloudflare.html")),
    ])
    func anOutageAsks511(_ outage: FakeTransport.Reply) async throws {
        await answer(api: { _ in outage })
        let answer = try await provider().arrivals(for: [Self.eastbound, Self.westbound], limit: 6)
        // 511's answer, shaped like the API's.
        let east = try #require(answer.platforms["SF:16992"])
        #expect(east.count == 6)
        #expect(east.first?.trip == "SF:12135937_M11")
        #expect(answer.platforms["SF:17217"]?.first?.kind == .departure)
        #expect(answer.refreshAfter == 40)
        #expect(answer.fetchedAt == clock.now)
        #expect(answer.feedAt == Date(timeIntervalSince1970: 1790216848))
        // One call per stop, with the app's key.
        let calls = await transport.requests(to: Self.host511)
        #expect(calls.compactMap { $0.query("stopcode") }.sorted() == ["16992", "17217"])
        #expect(calls.allSatisfy { $0.query("api_key") == "test-key" && $0.query("agency") == "SF" })
    }

    @Test func oldPredictionsAsk511() async throws {
        let now = clock.now
        await answer(api: { _ in Self.api(age: 5 * 60 + 1, at: now) })
        let answer = try await provider().arrivals(for: [Self.eastbound], limit: 6)
        #expect(answer.refreshAfter == 40)
        #expect(await requests(to: Self.host511) == 1)
    }

    @Test func aRequestTheAPIRefusesDoesNotFallBack() async {
        await answer(api: { _ in .fixture("edge/problem.json", status: 400) })
        await #expect(throws: CanopyError.self) {
            try await provider().arrivals(for: [Self.eastbound], limit: 6)
        }
        #expect(await requests(to: Self.host511) == 0)
    }

    @Test func withoutAKeyAnOutageIsAnError() async {
        await answer(api: { _ in .failure(.timedOut) })
        await #expect(throws: CanopyError.self) {
            try await provider(key: nil).arrivals(for: [Self.eastbound], limit: 6)
        }
        #expect(await requests(to: Self.host511) == 0)
    }

    @Test func withoutAKeyOldPredictionsAreStillShown() async throws {
        let now = clock.now
        await answer(api: { _ in Self.api(age: 600, at: now) })
        let answer = try await provider(key: nil).arrivals(for: [Self.eastbound], limit: 6)
        #expect(answer.platforms["SF:16992"]?.map(\.trip) == ["SF:api"])
    }

    @Test func fiveElevenStaysTheSourceForTwoMinutes() async throws {
        let provider = provider()
        await answer(api: { _ in .failure(.timedOut) })
        _ = try await provider.arrivals(for: [Self.eastbound], limit: 6)
        #expect(await requests(to: Self.apiHost) == 1)

        // The API is back, but for two minutes 511 is still asked, and the API isn't.
        let now = clock.now
        await answer(api: { _ in Self.api(age: 10, at: now.addingTimeInterval(120)) })
        for _ in 0..<2 {
            clock.advance(40)
            #expect(try await provider.arrivals(for: [Self.eastbound], limit: 6).refreshAfter == 40)
        }
        #expect(await requests(to: Self.apiHost) == 1)
        #expect(await requests(to: Self.host511) == 3)

        // Then the API gets another try, and answers.
        clock.advance(40)
        let back = try await provider.arrivals(for: [Self.eastbound], limit: 6)
        #expect(back.platforms["SF:16992"]?.map(\.trip) == ["SF:api"])
        #expect(await requests(to: Self.apiHost) == 2)
        #expect(await requests(to: Self.host511) == 3)
    }

    @Test func anAPIStillDownStartsAnotherTwoMinutes() async throws {
        let provider = provider()
        await answer(api: { _ in .init(status: 503) })
        _ = try await provider.arrivals(for: [Self.eastbound], limit: 6)
        clock.advance(121)
        _ = try await provider.arrivals(for: [Self.eastbound], limit: 6)
        #expect(await requests(to: Self.apiHost) == 2)
        clock.advance(60)
        _ = try await provider.arrivals(for: [Self.eastbound], limit: 6)
        #expect(await requests(to: Self.apiHost) == 2)
        #expect(await requests(to: Self.host511) == 3)
    }

    @Test func a429StopsCallsTo511UntilItsReset() async throws {
        let provider = provider()
        let reset = clock.now.addingTimeInterval(600)
        await answer(api: { _ in .failure(.timedOut) },
                     on511: { _ in .init(status: 429, headers: ["RateLimit-Reset": "600"]) })
        await #expect(throws: CanopyError.self) { try await provider.arrivals(for: [Self.eastbound], limit: 6) }
        #expect(await requests(to: Self.host511) == 1)

        // 511 is back, but not to be asked until the reset. The API is, each time.
        await answer(api: { _ in .failure(.timedOut) })
        clock.advance(300)
        await #expect(throws: CanopyError.self) { try await provider.arrivals(for: [Self.eastbound], limit: 6) }
        #expect(await requests(to: Self.host511) == 1)
        #expect(await requests(to: Self.apiHost) == 2)

        clock.advance(reset.timeIntervalSince(clock.now))
        #expect(try await provider.arrivals(for: [Self.eastbound], limit: 6).refreshAfter == 40)
        #expect(await requests(to: Self.host511) == 2)
    }

    @Test func whileLimitedOldPredictionsAreStillShown() async throws {
        let provider = provider()
        let now = clock.now
        await answer(api: { _ in Self.api(age: 900, at: now) }, on511: { _ in .init(status: 429) })
        // Stale, 511 refuses: the stale answer rather than nothing.
        let first = try await provider.arrivals(for: [Self.eastbound], limit: 6)
        #expect(first.platforms["SF:16992"]?.map(\.trip) == ["SF:api"])
        // No reset given: nothing more to 511 until 03:00.
        clock.advance(30 * 60)
        _ = try await provider.arrivals(for: [Self.eastbound], limit: 6)
        #expect(await requests(to: Self.host511) == 1)
    }

    @Test func aPlatformOfSeveralStopsAsksForEach() async throws {
        // Both Embarcadero stops as one platform: every trip at either, merged.
        let both = Platform(id: "SF:16992", heading: .eastbound, stops: ["SF:16992", "SF:17217"])
        await answer(api: { _ in .init(status: 503) })
        let answer = try await provider().arrivals(for: [both], limit: 30)
        #expect(await requests(to: Self.host511) == 2)
        let board = try #require(answer.platforms["SF:16992"])
        #expect(board.count == 30)
        #expect(zip(board, board.dropFirst()).allSatisfy { $0.time <= $1.time })
        #expect(board.allSatisfy { $0.time >= clock.now })
    }

    @Test func oneStopFailingFailsTheBoard() async {
        let both = Platform(id: "SF:16992", heading: .eastbound, stops: ["SF:16992", "SF:17217"])
        await answer(api: { _ in .init(status: 503) }, on511: { request in
            request.query("stopcode") == "17217" ? .failure(.timedOut) : Self.from511(request)
        })
        await #expect(throws: CanopyError.self) { try await provider().arrivals(for: [both], limit: 6) }
    }
}
