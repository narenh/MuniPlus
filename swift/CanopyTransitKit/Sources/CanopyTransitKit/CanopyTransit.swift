import Foundation
import Observation

/// Where an app starts: the reference data, kept on disk and current, and feeds of
/// realtime data.
///
/// ```swift
/// let transit = CanopyTransit(configuration: .init(
///     baseURL: URL(string: "https://muni-staging.canopysf.com")!,
///     fallbackKey: my511Key,
///     snapshot: Bundle.main.url(forResource: "canopy-snapshot", withExtension: "json")))
///
/// await transit.refresh()                     // on launch and on return to the foreground
/// transit.network.resolvePlatform(savedHome)  // stored ids → what they mean now
/// let feed = transit.arrivalsFeed(platforms: [home.platform])
/// ```
@MainActor
@Observable
public final class CanopyTransit {
    public struct Configuration: Sendable {
        /// The Muni+ API, without `/api/v1`.
        public var baseURL: URL
        /// A 511 key, for asking 511 for arrivals itself when the Muni+ API can't
        /// answer. Nil turns that off.
        public var fallbackKey: String?
        /// Where to keep reference data between launches. Nil keeps nothing.
        public var directory: URL?
        /// The snapshot bundled with the app, written by `scripts/make_snapshot.py`:
        /// what a first launch with no network shows.
        public var snapshot: URL?
        /// Lines whose diagrams (`/lines/{id}`) every refresh keeps current.
        public var lineDetails: Set<LineID>
        public var transport: any HTTPTransport

        public init(baseURL: URL, fallbackKey: String? = nil, directory: URL? = Configuration.defaultDirectory,
                    snapshot: URL? = nil, lineDetails: Set<LineID> = [],
                    transport: any HTTPTransport = URLSessionTransport()) {
            self.baseURL = baseURL
            self.fallbackKey = fallbackKey
            self.directory = directory
            self.snapshot = snapshot
            self.lineDetails = lineDetails
            self.transport = transport
        }

        /// Application Support/CanopyTransitKit: the app's own copy of its data, which
        /// the system mustn't reclaim the way it may Caches.
        public static var defaultDirectory: URL? {
            try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                         appropriateFor: nil, create: true)
                .appending(path: "CanopyTransitKit", directoryHint: .isDirectory)
        }
    }

    /// The stations and lines. Read from disk at launch, so it's never empty once an
    /// app has launched online or bundles a snapshot, and replaced whole when a
    /// refresh brings new data.
    public private(set) var network: TransitNetwork
    /// When the reference data was last confirmed current. Nil until then.
    public private(set) var refreshedAt: Date?

    public let client: CanopyClient

    @ObservationIgnored private let store: ReferenceStore
    @ObservationIgnored private let arrivals: ArrivalsProvider
    @ObservationIgnored private let lineDetailIDs: Set<LineID>

    public init(configuration: Configuration) {
        client = CanopyClient(baseURL: configuration.baseURL, transport: configuration.transport)
        let contents = ReferenceStore.load(directory: configuration.directory, snapshot: configuration.snapshot)
        network = contents.network
        store = ReferenceStore(contents: contents, directory: configuration.directory, client: client)
        arrivals = ArrivalsProvider(
            client: client,
            fallback: configuration.fallbackKey.map { StopMonitoringClient(key: $0, transport: configuration.transport) })
        lineDetailIDs = configuration.lineDetails
    }

    /// Revalidates the stations, lines and the configured line diagrams, swapping in
    /// a new `network` if any changed. Call it on launch and whenever the app comes
    /// back to the foreground. Returns whether anything changed.
    @discardableResult
    public func refresh() async throws(CanopyError) -> Bool {
        let changed = try await store.refresh(lineDetails: lineDetailIDs)
        refreshedAt = Date()
        if changed { network = await store.network }
        return changed
    }

    /// One line's directions, from disk if current, else from the API. It joins
    /// `network.lineDetails` too.
    public func lineDetail(_ id: LineID) async -> LineDetail? {
        let detail = await store.lineDetail(id)
        if let detail, network.lineDetail(id) != detail {
            network = await store.network
        }
        return detail
    }

    /// A feed of the arrivals at `platforms` (at most 50). Resolve stored ids first
    /// (`network.resolvePlatform`), and read the answer by each platform's `id`.
    public func arrivalsFeed(platforms: [Platform], limit: Int = 6) -> ArrivalsFeed {
        ArrivalsFeed(platforms: platforms, limit: limit, provider: arrivals)
    }

    /// A feed of the vehicles on `lines`, or with none, every vehicle in service.
    public func vehiclesFeed(lines: [LineID] = []) -> VehiclesFeed {
        VehiclesFeed(lines: lines, client: client)
    }
}
