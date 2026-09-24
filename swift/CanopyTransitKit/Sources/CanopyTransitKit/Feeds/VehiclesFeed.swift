import Foundation
import Observation

/// Live vehicle positions, kept current for as long as something runs it, the same
/// way as `ArrivalsFeed`.
///
/// Positions only move when the server fetches from 511, so animate a marker to its
/// new place rather than jumping it. There is no 511 fallback for vehicles.
@MainActor
@Observable
public final class VehiclesFeed {
    /// The lines followed, or empty for every vehicle in service.
    public let lines: [LineID]

    public private(set) var vehicles: [Vehicle] = []
    /// The age of every position: 511 stamps a whole feed with one time.
    public private(set) var feedAt: Date?
    public private(set) var updatedAt: Date?
    public private(set) var lastError: CanopyError?

    @ObservationIgnored private let client: CanopyClient
    @ObservationIgnored private let runner: FeedRunner
    @ObservationIgnored private let now: @Sendable () -> Date

    init(lines: [LineID], client: CanopyClient,
         now: @escaping @Sendable () -> Date = Date.init,
         sleep: @escaping @Sendable (TimeInterval) async throws -> Void = Feeds.sleep) {
        self.lines = lines
        self.client = client
        self.now = now
        runner = FeedRunner(now: now, sleep: sleep)
    }

    /// Polls until the task running it is cancelled.
    public func run() async {
        await runner.run { await self.refresh() }
    }

    /// Asks now, whatever `refreshAfter` said.
    public func refresh() async {
        do {
            let answer = try await client.vehicles(lines: lines)
            vehicles = answer.vehicles
            feedAt = answer.feedAt
            updatedAt = now()
            lastError = nil
            runner.nextDue = now().addingTimeInterval(max(answer.refreshAfter, Polling.minimumInterval))
        } catch {
            if !Task.isCancelled { lastError = error }
            runner.nextDue = now().addingTimeInterval(ArrivalsFeed.retryAfterFailure)
        }
    }
}
