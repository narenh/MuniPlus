import CoreLocation
import Foundation

/// A point on a map.
public struct Coordinate: Hashable, Sendable {
    public let latitude: Double
    public let longitude: Double

    public init(latitude: Double, longitude: Double) {
        self.latitude = latitude
        self.longitude = longitude
    }

    public var clCoordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: latitude, longitude: longitude)
    }
}

/// `GET /api/v1/shapes`: the path every line direction draws.
public struct ShapesResponse: Decodable, Sendable {
    public let shapes: [ShapeID: [Coordinate]]

    private enum CodingKeys: String, CodingKey {
        case shapes
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // The one place in the API that is [lon, lat] (GeoJSON order), turned the
        // right way round here so nothing else has to know.
        let raw = try c.map([ShapeID: LossyList<LossyList<Double>>].self, forKey: .shapes)
        shapes = raw.mapValues { points in
            points.elements.compactMap { pair in
                pair.elements.count >= 2 ? Coordinate(latitude: pair.elements[1], longitude: pair.elements[0]) : nil
            }
        }
    }
}
