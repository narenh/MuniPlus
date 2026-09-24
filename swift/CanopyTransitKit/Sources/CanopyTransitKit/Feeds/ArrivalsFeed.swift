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
/// fixed timer. When the Muni+ API can't answer, it asks 511 directly, and nothing
/// here says which one answered. When neither can, it keeps showing the last
/// arrivals it had. Stop running it in the background, and run it again on return
/// (API.md, section 6), for example with `.task(id: scenePhase)`.
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
    @ObservationIgnored private let sleep: @Sendable (TimeInterval) async throws -> Void

    /// How long to wait after a failure before asking again.
    static let retryAfterFailure: TimeInterval = 30

    init(platforms: [Platform], limit: Int, provider: ArrivalsProvider,
         sleep: @escaping @Sendable (TimeInterval) async throws -> Void = Feeds.sleep) {
        self.platforms = platforms
        self.limit = limit
        self.provider = provider
        self.sleep = sleep
    }

    /// The arrivals due at `platform`, soonest first.
    public func arrivals(at platform: Platform) -> [Arrival] {
        arrivals[platform.id] ?? []
    }

    /// Polls until the task running it is cancelled.
    public func run() async {
        while !Task.isCancelled {
            let wait = await refresh()
            do { try await sleep(wait) } catch { return }
        }
    }

    /// Asks once, now. Returns the seconds to wait before asking again.
    @discardableResult
    public func refresh() async -> TimeInterval {
        guard !platforms.isEmpty else { return .infinity }
        do {
            let answer = try await provider.arrivals(for: platforms, limit: limit)
            arrivals = answer.platforms
            feedAt = answer.feedAt
            updatedAt = Date()
            lastError = nil
            return max(answer.refreshAfter, Polling.minimumInterval)
        } catch {
            if !Task.isCancelled { lastError = error }
            return Self.retryAfterFailure
        }
    }
}

enum Feeds {
    @Sendable static func sleep(_ seconds: TimeInterval) async throws {
        guard seconds.isFinite else {
            // Nothing will ever be due: wait to be cancelled.
            while true { try await Task.sleep(for: .seconds(3600)) }
        }
        try await Task.sleep(for: .seconds(seconds))
    }
}
