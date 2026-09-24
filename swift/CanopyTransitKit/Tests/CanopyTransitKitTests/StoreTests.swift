import Foundation
import Testing
@testable import CanopyTransitKit

@Suite("Reference data on disk")
struct StoreTests {
    static let stationsTag = #"W/"38d2f22a.b9886267""#
    static let linesTag = #"W/"38d2f22a.lines""#
    static let lineNTag = #"W/"38d2f22a.N""#

    let folder: URL

    init() throws {
        folder = FileManager.default.temporaryDirectory.appending(path: "CanopyTransitKitTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    }

    /// A snapshot made of the staging fixtures, as `make_snapshot.py` writes one.
    func writeSnapshot() throws -> URL {
        func body(_ path: String) throws -> Any { try JSONSerialization.jsonObject(with: Fixture.data(path)) }
        let snapshot: [String: Any] = [
            "format": 1,
            "stations": ["etag": Self.stationsTag, "body": try body("staging/stations.json")],
            "lines": ["etag": Self.linesTag, "body": try body("staging/lines.json")],
            "lineDetails": ["SF:N": ["etag": Self.lineNTag, "body": try body("staging/line-SF_N.json")]],
        ]
        let url = folder.appending(path: "snapshot.json")
        try JSONSerialization.data(withJSONObject: snapshot).write(to: url)
        return url
    }

    var cache: URL { folder.appending(path: "cache") }

    func store(_ transport: FakeTransport, snapshot: URL?) -> ReferenceStore {
        ReferenceStore(contents: ReferenceStore.load(directory: cache, snapshot: snapshot), directory: cache,
                       client: CanopyClient(baseURL: stagingURL, transport: transport))
    }

    /// Answers 304 to any tag it's given, and the API.md examples otherwise.
    static func current(_ request: URLRequest) -> FakeTransport.Reply {
        if let tag = request.value(forHTTPHeaderField: "If-None-Match") { return .init(status: 304, headers: ["ETag": tag]) }
        return switch request.path {
        case "/api/v1/stations": .fixture("api-md/stations.json", headers: ["ETag": #"W/"new.stations""#])
        case "/api/v1/lines": .fixture("api-md/lines.json", headers: ["ETag": #"W/"new.lines""#])
        case "/api/v1/lines/SF:N": .fixture("api-md/line-SF_N.json", headers: ["ETag": #"W/"new.N""#])
        default: .init(status: 404)
        }
    }

    @Test func aFirstLaunchReadsTheSnapshot() throws {
        let contents = ReferenceStore.load(directory: cache, snapshot: try writeSnapshot())
        #expect(contents.stations?.etag == Self.stationsTag)
        #expect(contents.network.stations.count == 6)
        #expect(contents.network.lines.count == 68)
        #expect(contents.network.lineDetail("SF:N")?.directions.count == 2)
    }

    @Test func nothingAtAllIsAnEmptyNetwork() {
        #expect(ReferenceStore.load(directory: cache, snapshot: nil).network.isEmpty)
        #expect(ReferenceStore.load(directory: cache, snapshot: folder.appending(path: "missing.json")).network.isEmpty)
    }

    @Test func aCurrentSnapshotCostsA304() async throws {
        let transport = FakeTransport(Self.current)
        let store = store(transport, snapshot: try writeSnapshot())
        #expect(try await store.refresh(lineDetails: ["SF:N"]) == false)

        let requests = await transport.requests
        #expect(requests.map(\.path) == ["/api/v1/stations", "/api/v1/lines"])
        #expect(requests.map { $0.value(forHTTPHeaderField: "If-None-Match") } == [Self.stationsTag, Self.linesTag])
        // Nothing new, so nothing written.
        #expect(!FileManager.default.fileExists(atPath: cache.path(percentEncoded: false)))
    }

    @Test func aNewBodyIsKeptAndReadNextLaunch() async throws {
        let snapshot = try writeSnapshot()
        let transport = FakeTransport { request in
            // Whatever tag is sent, there's newer data.
            var plain = request
            plain.setValue(nil, forHTTPHeaderField: "If-None-Match")
            return Self.current(plain)
        }
        let store = store(transport, snapshot: snapshot)
        #expect(try await store.refresh(lineDetails: ["SF:N"]))
        #expect(await store.network.version == "38d2f22ad7a296e47e86dc80679a0a048cf3a81e")
        #expect(await store.network.stations.map(\.id) == ["embarcadero"])
        // The lines changed, so the N was asked for again even though it matched.
        #expect(await transport.requests.map(\.path) == ["/api/v1/stations", "/api/v1/lines", "/api/v1/lines/SF:N"])

        let next = ReferenceStore.load(directory: cache, snapshot: snapshot)
        #expect(next.stations?.etag == #"W/"new.stations""#)
        #expect(next.network.stations.map(\.id) == ["embarcadero"])
        #expect(next.network.lines.map(\.id) == ["SF:J", "SF:LOWL"])
        #expect(next.lineDetails["SF:N"]?.etag == #"W/"new.N""#)
    }

    @Test func bodiesAreKeptAsReceived() async throws {
        let body = Fixture.data("edge/stations-future.json")
        let store = store(FakeTransport { _ in .json(body, headers: ["ETag": "t"]) }, snapshot: nil)
        _ = try? await store.refresh(lineDetails: [])
        let file = try Data(contentsOf: cache.appending(path: "v1/stations.json"))
        #expect(file == Data("t\n".utf8) + body)
    }

    @Test func aBodyThatIsNotDataIsNeverKept() async throws {
        let snapshot = try writeSnapshot()
        for reply in [FakeTransport.Reply(status: 200, body: Fixture.data("edge/cloudflare.html")),
                      .json(Data(#"{"version": "x", "formerIds": {}, "stations": []}"#.utf8))] {
            let store = store(FakeTransport { _ in reply }, snapshot: snapshot)
            await #expect(throws: CanopyError.self) { try await store.refresh(lineDetails: []) }
            #expect(await store.network.stations.count == 6)
        }
        #expect(!FileManager.default.fileExists(atPath: cache.appending(path: "v1/stations.json").path(percentEncoded: false)))
    }

    @Test func aDamagedCacheFallsBackToTheSnapshot() throws {
        let folder = cache.appending(path: "v1")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try Data("W/\"x\"\n{\"version\": ".utf8).write(to: folder.appending(path: "stations.json"))
        let contents = ReferenceStore.load(directory: cache, snapshot: try writeSnapshot())
        #expect(contents.stations?.etag == Self.stationsTag)
    }

    @Test func aCurrentLineDetailIsNotAskedFor() async throws {
        let transport = FakeTransport(Self.current)
        let store = store(transport, snapshot: try writeSnapshot())
        #expect(await store.lineDetail("SF:N")?.name == "N Judah")
        #expect(await transport.requests.isEmpty)

        // One the snapshot lacks is fetched, and a 404 is simply nothing.
        #expect(await store.lineDetail("SF:J") == nil)
        #expect(await transport.requests.map(\.path) == ["/api/v1/lines/SF:J"])
    }

    @Test func overlappingRefreshesShareOneRequest() async throws {
        let transport = FakeTransport(Self.current)
        let store = store(transport, snapshot: try writeSnapshot())
        async let first = store.refresh(lineDetails: [])
        async let second = store.refresh(lineDetails: [])
        _ = try await (first, second)
        #expect(await transport.requests.count == 2)
    }
}
