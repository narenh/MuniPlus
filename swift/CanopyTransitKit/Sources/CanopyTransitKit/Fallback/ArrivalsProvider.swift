import Foundation

/// Answers arrivals from the API, and from 511 directly when the API can't.
///
/// The API is asked first. When it can't answer (no response, a timeout, a 5xx, a
/// body that isn't JSON) or its predictions are more than five minutes old, and
/// there is a 511 key, 511 is asked instead, and keeps being asked for two
/// minutes before the API gets another try. Either way the answer has the same
/// shape: nothing tells a caller which one it came from.
///
/// 511 is polled every 40 seconds while it's the source. A 429 stops all calls to
/// it until the time it gives, or the top of the hour; meanwhile a stale answer
/// from the API is still returned, and failing that the caller keeps what it had.
actor ArrivalsProvider {
    /// Predictions older than this are worth asking 511 about.
    static let staleAfter: TimeInterval = 5 * 60
    /// How long 511 stays the source once the API has failed.
    static let fallbackWindow: TimeInterval = 2 * 60
    /// How often to poll while 511 is the source.
    static let fallbackRefresh: TimeInterval = 40

    private let client: CanopyClient
    private let fallback: StopMonitoringClient?
    private let now: @Sendable () -> Date

    private var fallingBackUntil: Date?
    private var rateLimitedUntil: Date?

    init(client: CanopyClient, fallback: StopMonitoringClient?, now: @escaping @Sendable () -> Date = Date.init) {
        self.client = client
        self.fallback = fallback
        self.now = now
    }

    /// The next `limit` arrivals at each platform (at most 50 of them), keyed by
    /// platform id. Throws only when neither source has an answer.
    func arrivals(for platforms: [Platform], limit: Int) async throws(CanopyError) -> ArrivalsResponse {
        var asked511 = false
        if let until = fallingBackUntil, now() < until, canAsk511 {
            asked511 = true
            if let answer = await ask511(platforms, limit: limit) { return answer }
        }

        let answer: ArrivalsResponse
        do {
            answer = try await client.arrivals(platforms: platforms.map(\.id), limit: limit)
        } catch {
            // Cancelled is the caller going away, not the API failing.
            guard error.isOutage, !Task.isCancelled else { throw error }
            if !asked511, let fallen = await fallBack(platforms, limit: limit) { return fallen }
            throw error
        }
        if let feedAt = answer.feedAt, now().timeIntervalSince(feedAt) <= Self.staleAfter {
            fallingBackUntil = nil
            return answer
        }
        if !asked511, let fallen = await fallBack(platforms, limit: limit) { return fallen }
        return answer
    }

    private var canAsk511: Bool {
        guard fallback != nil else { return false }
        return rateLimitedUntil.map { now() >= $0 } ?? true
    }

    /// Makes 511 the source for the next two minutes, and asks it.
    private func fallBack(_ platforms: [Platform], limit: Int) async -> ArrivalsResponse? {
        guard canAsk511 else { return nil }
        fallingBackUntil = now().addingTimeInterval(Self.fallbackWindow)
        return await ask511(platforms, limit: limit)
    }

    private func ask511(_ platforms: [Platform], limit: Int) async -> ArrivalsResponse? {
        guard let fallback else { return nil }
        let stops = Array(Set(platforms.flatMap(\.stops)))
        let asked = now()
        let answers = await withTaskGroup(of: (StopID, Result<StopMonitoring, StopMonitoringClient.Failure>).self) { group in
            for stop in stops {
                group.addTask {
                    do throws(StopMonitoringClient.Failure) {
                        return (stop, .success(try await fallback.stopMonitoring(stop, now: asked)))
                    } catch {
                        return (stop, .failure(error))
                    }
                }
            }
            var answers: [(StopID, Result<StopMonitoring, StopMonitoringClient.Failure>)] = []
            for await answer in group { answers.append(answer) }
            return answers
        }

        var byStop: [StopID: StopMonitoring] = [:]
        var failed = false
        for (stop, result) in answers {
            switch result {
            case .success(let visits):
                byStop[stop] = visits
            case .failure(.rateLimited(let until)):
                rateLimitedUntil = max(rateLimitedUntil ?? until, until)
                failed = true
            case .failure(.unusable):
                failed = true
            }
        }
        // A board missing one of its stops would look quieter than it is.
        guard !failed else { return nil }

        let at = now()
        var boards: [StopID: [Arrival]] = [:]
        for platform in platforms {
            boards[platform.id] = StopMonitoring.merge(platform.stops.map { byStop[$0]?.arrivals(at: $0) ?? [] },
                                                       now: at, limit: limit)
        }
        // With several stops, the oldest of their times, as the API does.
        return ArrivalsResponse(fetchedAt: at, feedAt: byStop.values.compactMap(\.feedAt).min(),
                                refreshAfter: Self.fallbackRefresh, platforms: boards)
    }
}
