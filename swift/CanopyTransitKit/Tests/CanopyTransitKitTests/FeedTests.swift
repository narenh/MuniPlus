import Foundation
import Testing
@testable import CanopyTransitKit

/// Records each wait a feed asks for, and ends the run after `limit` of them.
final class Waits: @unchecked Sendable {
    private let lock = NSLock()
    private var waits: [TimeInterval] = []
    private let limit: Int
    private let between: @Sendable (Int) async -> Void

    init(limit: Int, between: @escaping @Sendable (Int) async -> Void = { _ in }) {
        self.limit = limit
        self.between = between
    }

    var recorded: [TimeInterval] { lock.withLock { waits } }

    func sleep(_ seconds: TimeInterval) async throws {
        let count = lock.withLock { waits.append(seconds); return waits.count }
        if count >= limit { throw CancellationError() }
        await between(count)
    }
}

@Suite("Feeds")
@MainActor
struct FeedTests {
    let clock = TestClock(StopMonitoringTests.answeredAt)

    @Test func pollsOnRefreshAfterAndKeepsTheLastGoodAnswer() async throws {
        let transport = FakeTransport()
        let now = clock.now
        await transport.answer { _ in ArrivalsProviderTests.api(age: 20, at: now) }
        let provider = ArrivalsProvider(client: CanopyClient(baseURL: stagingURL, transport: transport),
                                        fallback: nil, now: { [clock] in clock.now })
        // After the first answer the API goes away.
        let waits = Waits(limit: 2) { _ in await transport.answer { _ in .failure(.timedOut) } }
        let feed = ArrivalsFeed(platforms: [ArrivalsProviderTests.eastbound], limit: 6, provider: provider,
                                sleep: waits.sleep)

        await feed.run()
        #expect(waits.recorded == [25, ArrivalsFeed.retryAfterFailure])
        #expect(feed.arrivals(at: ArrivalsProviderTests.eastbound).map(\.trip) == ["SF:api"])
        #expect(feed.feedAt == now.addingTimeInterval(-20))
        #expect(feed.lastError != nil)
    }

    @Test func neverPollsFasterThanTheFloor() async {
        let transport = FakeTransport { _ in
            .json(Data(#"{"fetchedAt": 1, "feedAt": 1, "refreshAfter": 1, "vehicles": []}"#.utf8))
        }
        let waits = Waits(limit: 1)
        let feed = VehiclesFeed(lines: ["SF:N"], client: CanopyClient(baseURL: stagingURL, transport: transport),
                                sleep: waits.sleep)
        await feed.run()
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
    }
}
