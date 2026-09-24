import Foundation

/// Polls one feed for as long as anything is running it.
///
/// Any number of views may run a feed at once, as when a platform is on screen
/// twice, and it still polls once. The poll starts with the first runner and stops
/// with the last. A feed run again soon after it stopped waits out the rest of its
/// `refreshAfter` before asking again, rather than asking the moment it reappears.
@MainActor
final class FeedRunner {
    /// When the feed may next ask. Set after every attempt, however it was made.
    var nextDue: Date = .distantPast

    private var runners = 0
    private var poller: Task<Void, Never>?
    private let now: @Sendable () -> Date
    private let sleep: @Sendable (TimeInterval) async throws -> Void

    init(now: @escaping @Sendable () -> Date, sleep: @escaping @Sendable (TimeInterval) async throws -> Void) {
        self.now = now
        self.sleep = sleep
    }

    /// Polls with `refresh` until the calling task is cancelled. `refresh` must set
    /// `nextDue`.
    func run(_ refresh: @escaping @MainActor () async -> Void) async {
        runners += 1
        if runners == 1 {
            poller = Task { await self.poll(refresh) }
        }
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(24 * 3600))
        }
        runners -= 1
        if runners == 0 {
            poller?.cancel()
            poller = nil
        }
    }

    private func poll(_ refresh: @MainActor () async -> Void) async {
        while !Task.isCancelled {
            let wait = nextDue.timeIntervalSince(now())
            if wait > 0 {
                // Checked again on waking: a manual refresh may have moved it.
                do { try await sleep(wait) } catch { return }
                continue
            }
            await refresh()
        }
    }
}

enum Feeds {
    @Sendable static func sleep(_ seconds: TimeInterval) async throws {
        // A wait too long to sleep is one nothing will end but cancellation.
        try await Task.sleep(for: .seconds(min(seconds, 24 * 3600)))
    }
}
