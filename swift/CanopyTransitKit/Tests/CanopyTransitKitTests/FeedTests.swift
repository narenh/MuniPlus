import Foundation
import Testing
@testable import CanopyTransitKit

/// Stands in for sleeping: records each wait, moves the clock on by it, and after
/// `limit` waits holds the feed there until its run is cancelled.
final class Waits: @unchecked Sendable {
    private let lock = NSLock()
    private var waits: [TimeInterval] = []
    private let limit: Int
    private let clock: TestClock
    private let between: @Sendable (Int) async -> Void

    init(limit: Int, clock: TestClock, between: @escaping @Sendable (Int) async -> Void = { _ in }) {
        self.limit = limit
        self.clock = clock
        self.between = between
    }

    var recorded: [TimeInterval] { lock.withLock { waits } }

    func sleep(_ seconds: TimeInterval) async throws {
        let count = lock.withLock { waits.append(seconds); return waits.count }
        if count >= limit {
            while true { try await Task.sleep(for: .seconds(3600)) }
        }
        await between(count)
        clock.advance(seconds)
    }

    /// Until the feed has asked for `limit` waits.
    func reached() async {
        while recorded.count < limit { await Task.yield() }
    }
}

@Suite("Feeds")
@MainActor
struct FeedTests {
    let clock = TestClock(StopMonitoringTests.answeredAt)

    func provider(_ transport: FakeTransport) -> ArrivalsProvider {
        ArrivalsProvider(client: CanopyClient(baseURL: stagingURL, transport: transport),
                         fallback: nil, now: { [clock] in clock.now })
    }

    @Test func pollsOnRefreshAfterAndKeepsTheLastGoodAnswer() async throws {
        let transport = FakeTransport()
        let now = clock.now
        await transport.answer { _ in ArrivalsProviderTests.api(age: 20, at: now) }
        // After the first answer the API goes away.
        let waits = Waits(limit: 2, clock: clock) { _ in await transport.answer { _ in .failure(.timedOut) } }
        let feed = ArrivalsFeed(platforms: [ArrivalsProviderTests.eastbound], limit: 6, provider: provider(transport),
                                now: { [clock] in clock.now }, sleep: waits.sleep)

        let run = Task { await feed.run() }
        await waits.reached()
        run.cancel()
        await run.value

        #expect(waits.recorded == [25, ArrivalsFeed.retryAfterFailure])
        #expect(await transport.requests.count == 2)
        #expect(feed.arrivals(at: ArrivalsProviderTests.eastbound).map(\.trip) == ["SF:api"])
        #expect(feed.feedAt == now.addingTimeInterval(-20))
        #expect(feed.lastError != nil)
    }

    @Test func aPlatformLeftOutKeepsItsArrivalsAndIsStale() async throws {
        let transport = FakeTransport()
        let provider = ArrivalsProvider(client: CanopyClient(baseURL: stagingURL, transport: transport),
                                        fallback: StopMonitoringClient(endpoint: ArrivalsProviderTests.relay, key: nil,
                                                                       transport: transport),
                                        stopMonitoringOnly: true, now: { [clock] in clock.now })
        let east = ArrivalsProviderTests.eastbound, west = ArrivalsProviderTests.westbound
        let feed = ArrivalsFeed(platforms: [east, west], limit: 6, provider: provider, now: { [clock] in clock.now })

        await transport.answer(with: ArrivalsProviderTests.from511)
        await feed.refresh()
        let westBefore = feed.arrivals(at: west)
        #expect(!westBefore.isEmpty)
        #expect(feed.stalePlatforms.isEmpty)

        // Westbound's stop fails: eastbound is answered afresh, westbound keeps what it had.
        clock.advance(40)
        await transport.answer { request in
            request.query("stopcode") == "17217" ? ArrivalsProviderTests.noData : ArrivalsProviderTests.from511(request)
        }
        await feed.refresh()
        #expect(feed.arrivals(at: west) == westBefore)
        #expect(feed.arrivals(at: east).allSatisfy { $0.time >= clock.now })
        #expect(feed.stalePlatforms == ["SF:17217"])
        #expect(feed.isStale(west) && !feed.isStale(east))
        #expect(feed.lastError == nil)
        #expect(feed.updatedAt == clock.now)

        // Every stop fails: everything is kept, and everything is stale.
        await transport.answer { _ in .failure(.timedOut) }
        let eastBefore = feed.arrivals(at: east)
        await feed.refresh()
        #expect(feed.arrivals(at: east) == eastBefore && feed.arrivals(at: west) == westBefore)
        #expect(feed.stalePlatforms == ["SF:16992", "SF:17217"])
        #expect(feed.lastError != nil)

        // Back: nothing stale.
        await transport.answer(with: ArrivalsProviderTests.from511)
        await feed.refresh()
        #expect(feed.stalePlatforms.isEmpty)
        #expect(feed.lastError == nil)
    }

    @Test func aPlatformNeverAnsweredHasNoArrivalsAndIsStale() async throws {
        let transport = FakeTransport()
        let provider = ArrivalsProvider(client: CanopyClient(baseURL: stagingURL, transport: transport),
                                        fallback: StopMonitoringClient(endpoint: ArrivalsProviderTests.relay, key: nil,
                                                                       transport: transport),
                                        stopMonitoringOnly: true, now: { [clock] in clock.now })
        let east = ArrivalsProviderTests.eastbound, quiet = ArrivalsProviderTests.quiet
        let feed = ArrivalsFeed(platforms: [east, quiet], limit: 6, provider: provider, now: { [clock] in clock.now })
        await transport.answer(with: ArrivalsProviderTests.relay(failingQuietWith: ArrivalsProviderTests.noData))
        await feed.refresh()
        #expect(!feed.arrivals(at: east).isEmpty)
        #expect(feed.arrivals["SF:15731"] == nil)
        #expect(feed.stalePlatforms == ["SF:15731"])
        #expect(feed.lastError == nil)
    }

    @Test func twoRunnersPollOnce() async throws {
        let transport = FakeTransport()
        let now = clock.now
        await transport.answer { _ in ArrivalsProviderTests.api(age: 20, at: now) }
        let waits = Waits(limit: 3, clock: clock)
        let feed = ArrivalsFeed(platforms: [ArrivalsProviderTests.eastbound], limit: 6, provider: provider(transport),
                                now: { [clock] in clock.now }, sleep: waits.sleep)

        let first = Task { await feed.run() }
        let second = Task { await feed.run() }
        await waits.reached()
        // Three waits between them, and the three answers that led to them: one poll.
        #expect(await transport.requests.count == 3)

        // The poll outlives the first runner, and ends with the second.
        first.cancel()
        await first.value
        second.cancel()
        await second.value
    }

    @Test func aFeedRunAgainWaitsOutItsRefreshAfter() async throws {
        let transport = FakeTransport()
        let now = clock.now
        await transport.answer { _ in ArrivalsProviderTests.api(age: 20, at: now) }
        let waits = Waits(limit: 1, clock: clock)
        let feed = ArrivalsFeed(platforms: [ArrivalsProviderTests.eastbound], limit: 6, provider: provider(transport),
                                now: { [clock] in clock.now }, sleep: waits.sleep)
        await feed.refresh()

        // Shown again ten seconds later: it waits the other fifteen, and asks nothing.
        clock.advance(10)
        let run = Task { await feed.run() }
        await waits.reached()
        run.cancel()
        await run.value
        #expect(waits.recorded == [15])
        #expect(await transport.requests.count == 1)
    }

    @Test func neverPollsFasterThanTheFloor() async {
        let transport = FakeTransport { _ in
            .json(Data(#"{"fetchedAt": 1, "feedAt": 1, "refreshAfter": 1, "vehicles": []}"#.utf8))
        }
        let waits = Waits(limit: 1, clock: clock)
        let feed = VehiclesFeed(lines: ["SF:N"], client: CanopyClient(baseURL: stagingURL, transport: transport),
                                now: { [clock] in clock.now }, sleep: waits.sleep)
        let run = Task { await feed.run() }
        await waits.reached()
        run.cancel()
        await run.value
        #expect(waits.recorded == [10])
        #expect(await transport.requests.first?.query("line") == "SF:N")
    }

    @Test func theEntryPointStartsFromTheSnapshotAndRefreshes() async throws {
        let folder = FileManager.default.temporaryDirectory.appending(path: "CanopyTransitKitTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let snapshot = try StoreTests().writeSnapshot()
        let transport = FakeTransport(StoreTests.current)
        let transit = CanopyTransit(configuration: .init(baseURL: stagingURL, fallbackKey: "k", directory: folder,
                                                         snapshot: snapshot, lineDetails: ["SF:N"],
                                                         transport: transport))
        #expect(transit.network.stations.count == 6)
        #expect(transit.refreshedAt == nil)

        #expect(try await transit.refresh() == false)
        #expect(transit.refreshedAt != nil)
        #expect(transit.network.stations.count == 6)

        let home = try #require(transit.network.resolvePlatform("SF:16992"))
        let feed = transit.arrivalsFeed(platforms: [home.platform])
        #expect(feed.platforms.map(\.id) == ["SF:16992"])
        #expect(feed.limit == 6)
        // The same platforms, the same feed.
        #expect(transit.arrivalsFeed(platforms: [home.platform]) === feed)
        #expect(transit.arrivalsFeed(platforms: [home.platform], limit: 12) !== feed)
    }
}
