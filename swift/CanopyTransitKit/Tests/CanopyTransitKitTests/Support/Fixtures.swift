import Foundation
@testable import CanopyTransitKit

/// The files under `Fixtures/`:
/// - `api-md/`: the examples in MuniPlus/api/API.md.
/// - `staging/`: muni-staging.canopysf.com, 2026-09-23 19:26 Pacific, before
///   transfers, subways and `owl` were added. The station list is trimmed.
/// - `511/`: 511 StopMonitoring for the same two platforms a few seconds later.
/// - `edge/`: hand-written cases: a newer server, error bodies.
enum Fixture {
    static let root = Bundle.module.url(forResource: "Fixtures", withExtension: nil)!

    static func url(_ path: String) -> URL {
        root.appending(path: path)
    }

    static func data(_ path: String) -> Data {
        try! Data(contentsOf: url(path))
    }

    static func decode<T: Decodable>(_ type: T.Type, _ path: String) throws -> T {
        try JSON.decoder().decode(type, from: data(path))
    }
}
