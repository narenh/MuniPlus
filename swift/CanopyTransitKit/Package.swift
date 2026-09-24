// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "CanopyTransitKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "CanopyTransitKit", targets: ["CanopyTransitKit"]),
    ],
    targets: [
        .target(name: "CanopyTransitKit"),
        .testTarget(
            name: "CanopyTransitKitTests",
            dependencies: ["CanopyTransitKit"],
            resources: [.copy("Fixtures")]
        ),
    ]
)
