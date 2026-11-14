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
/// answer, it asks 511 directly, and nothing here says which one answered; with
/// `ArrivalsSource.stopMonitoring`, it asks only that endpoint, stop by stop, and a
/// stop that can't be asked leaves only its own platforms out of date. Whatever
/// can't be asked, it keeps the last arrivals it had, and lists the platforms whose
/// arrivals those are in `stalePlatforms`. Stop running it in the
/// background and run it again on return (API.md, section 6), for example with
/// `.task(id: scenePhase)`.
@MainActor
@Observable
public final class ArrivalsFeed {
    public let platforms: [Platform]
    public let limit: Int

    /// By platform id. A platform is missing until its first answer.
    public private(set) var arrivals: [StopID: [Arrival]] = [:]
    /// The ids of the platforms the last attempt brought no new arrivals for. Their
    /// `arrivals` are the last they had, from an earlier answer (or none, if they've
    /// never had one), so they may be out of date: show them as such.
    ///
    /// With `ArrivalsSource.stopMonitoring`, that is each platform with a stop that
    /// couldn't be asked this time while others could. When the whole attempt fails
    /// (`lastError`), it is every platform. Empty after a full answer.
    public private(set) var stalePlatforms: Set<StopID> = []
    /// How old the predictions are: 511's own time for them, for the platforms the
    /// last answer covered. Show `now - feedAt` when it grows large, say past five
    /// minutes. Stale platforms' arrivals are older than this.
    public private(set) var feedAt: Date?
    /// When the last answer came, full or partial. Nil until the first.
    public private(set) var updatedAt: Date?
    /// Why the last attempt failed, or nil if it didn't. The arrivals are the last
    /// good ones either way. An answer that left out some platforms isn't a failure:
    /// see `stalePlatforms`.
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

    /// Whether `platform`'s arrivals are out of date: see `stalePlatforms`.
    public func isStale(_ platform: Platform) -> Bool {
        stalePlatforms.contains(platform.id)
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
            // New boards replace old ones; a platform left out keeps its old board.
            arrivals = answer.platforms.merging(arrivals.filter { answer.unanswered.contains($0.key) }) { new, _ in new }
            stalePlatforms = answer.unanswered
            feedAt = answer.feedAt
            updatedAt = now()
            lastError = nil
            runner.nextDue = now().addingTimeInterval(max(answer.refreshAfter, Polling.minimumInterval))
        } catch {
            if !Task.isCancelled {
                lastError = error
                stalePlatforms = Set(platforms.map(\.id))
            }
            runner.nextDue = now().addingTimeInterval(Self.retryAfterFailure)
        }
    }
}
