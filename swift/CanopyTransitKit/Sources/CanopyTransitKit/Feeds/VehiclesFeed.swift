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
    @ObservationIgnored private let sleep: @Sendable (TimeInterval) async throws -> Void

    init(lines: [LineID], client: CanopyClient,
         sleep: @escaping @Sendable (TimeInterval) async throws -> Void = Feeds.sleep) {
        self.lines = lines
        self.client = client
        self.sleep = sleep
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
        do {
            let answer = try await client.vehicles(lines: lines)
            vehicles = answer.vehicles
            feedAt = answer.feedAt
            updatedAt = Date()
            lastError = nil
            return max(answer.refreshAfter, Polling.minimumInterval)
        } catch {
            if !Task.isCancelled { lastError = error }
            return ArrivalsFeed.retryAfterFailure
        }
    }
}
