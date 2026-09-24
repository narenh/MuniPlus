import Foundation
import Observation

/// Arrivals at a set of platforms, kept current for as long as something runs it.
///
/// Run it from the view that shows it, so polling stops when the view goes:
///
/// ```swift
/// .task { await feed.run() }
/// ```
///
/// It polls when the answer says newer data can exist (`refreshAfter`), never on a
/// fixed timer, and once however many views run it. When the Muni+ API can't
/// answer, it asks 511 directly, and nothing here says which one answered. When
/// neither can, it keeps the last arrivals it had. Stop running it in the
/// background and run it again on return (API.md, section 6), for example with
/// `.task(id: scenePhase)`.
@MainActor
@Observable
public final class ArrivalsFeed {
    public let platforms: [Platform]
    public let limit: Int

    /// By platform id. Empty until the first answer.
    public private(set) var arrivals: [StopID: [Arrival]] = [:]
    /// How old the predictions are: 511's own time for them. Show `now - feedAt`
    /// when it grows large, say past five minutes.
    public private(set) var feedAt: Date?
    /// When the last answer came. Nil until the first.
    public private(set) var updatedAt: Date?
    /// Why the last attempt failed, or nil if it didn't. The arrivals are the last
    /// good ones either way.
    public private(set) var lastError: CanopyError?

    @ObservationIgnored private let provider: ArrivalsProvider
    @ObservationIgnored private let runner: FeedRunner
    @ObservationIgnored private let now: @Sendable () -> Date

    /// How long to wait after a failure before asking again.
    static let retryAfterFailure: TimeInterval = 30

    init(platforms: [Platform], limit: Int, provider: ArrivalsProvider,
         now: @escaping @Sendable () -> Date = Date.init,
         sleep: @escaping @Sendable (TimeInterval) async throws -> Void = Feeds.sleep) {
        self.platforms = platforms
        self.limit = limit
        self.provider = provider
        self.now = now
        runner = FeedRunner(now: now, sleep: sleep)
    }

    /// The arrivals due at `platform`, soonest first.
    public func arrivals(at platform: Platform) -> [Arrival] {
        arrivals[platform.id] ?? []
    }

    /// Polls until the task running it is cancelled.
    public func run() async {
        await runner.run { await self.refresh() }
    }

    /// Asks now, whatever `refreshAfter` said: for pull to refresh.
    public func refresh() async {
        guard !platforms.isEmpty else {
            runner.nextDue = .distantFuture
            return
        }
        do {
            let answer = try await provider.arrivals(for: platforms, limit: limit)
            arrivals = answer.platforms
            feedAt = answer.feedAt
            updatedAt = now()
            lastError = nil
            runner.nextDue = now().addingTimeInterval(max(answer.refreshAfter, Polling.minimumInterval))
        } catch {
            if !Task.isCancelled { lastError = error }
            runner.nextDue = now().addingTimeInterval(Self.retryAfterFailure)
        }
    }
}
