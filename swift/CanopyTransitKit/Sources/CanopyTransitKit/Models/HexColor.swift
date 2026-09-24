import Foundation

/// A colour as the API sends it, `#RRGGBB`.
public struct HexColor: Hashable, Sendable, Decodable, CustomStringConvertible {
    /// Each channel, 0 to 1.
    public let red: Double
    public let green: Double
    public let blue: Double

    public init?(_ hex: String) {
        let digits = hex.hasPrefix("#") ? hex.dropFirst() : Substring(hex)
        guard digits.count == 6, let value = UInt32(digits, radix: 16) else { return nil }
        red = Double((value >> 16) & 0xFF) / 255
        green = Double((value >> 8) & 0xFF) / 255
        blue = Double(value & 0xFF) / 255
    }

    public init(from decoder: any Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        guard let color = HexColor(raw) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath,
                                                    debugDescription: "Not a #RRGGBB colour: \(raw)"))
        }
        self = color
    }

    public var description: String {
        String(format: "#%02X%02X%02X", Int((red * 255).rounded()), Int((green * 255).rounded()), Int((blue * 255).rounded()))
    }

    /// WCAG 2 relative luminance.
    public var relativeLuminance: Double {
        func linear(_ c: Double) -> Double {
            c <= 0.03928 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)
    }

    /// The contrast ratio between two colours, 1 to 21.
    public func contrast(with other: HexColor) -> Double {
        let (a, b) = (relativeLuminance, other.relativeLuminance)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)
    }

    /// Black or white, whichever has the higher contrast on this colour. Black on the
    /// J's orange (10.6:1, where white is 2.0:1), white on the N's blue (7.8:1).
    public var legibleText: TextShade {
        let l = relativeLuminance
        return 1.05 / (l + 0.05) >= (l + 0.05) / 0.05 ? .white : .black
    }
}

/// Text on a coloured badge.
public enum TextShade: Hashable, Sendable {
    case black, white
}

#if canImport(SwiftUI)
import SwiftUI

extension HexColor {
    public var color: Color {
        Color(.sRGB, red: red, green: green, blue: blue)
    }
}

extension TextShade {
    public var color: Color {
        switch self {
        case .black: .black
        case .white: .white
        }
    }
}
#endif
