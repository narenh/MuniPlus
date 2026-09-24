import Testing
@testable import CanopyTransitKit

@Suite("Badge colours")
struct HexColorTests {
    @Test func parsing() {
        #expect(HexColor("#FAA633")?.description == "#FAA633")
        #expect(HexColor("faa633")?.description == "#FAA633")
        #expect(HexColor("#FAA63") == nil)
        #expect(HexColor("#GGGGGG") == nil)
    }

    @Test func contrastPicksTheReadableShade() throws {
        // The figures in API.md, section 9.
        let j = try #require(HexColor("#FAA633"))
        let n = try #require(HexColor("#00529C"))
        let white = try #require(HexColor("#FFFFFF"))
        let black = try #require(HexColor("#000000"))
        #expect(j.legibleText == .black)
        #expect(n.legibleText == .white)
        #expect((j.contrast(with: black) * 10).rounded() / 10 == 10.6)
        #expect((j.contrast(with: white) * 10).rounded() / 10 == 2.0)
        #expect((n.contrast(with: white) * 10).rounded() / 10 == 7.8)
    }

    @Test func aLineUsesContrastNotItsTextColor() {
        let j = Line(id: "SF:J", shortName: "J", name: "J Church", color: HexColor("#FAA633"),
                     textColor: HexColor("#FFFFFF"), mode: .metro)
        #expect(j.legibleText == .black)
        #expect(Line(id: "SF:X", shortName: "X", name: "X", mode: .bus).legibleText == .white)
    }
}
