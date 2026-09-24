# CanopyTransitKit

A Swift client for the Muni+ v1 API ([`api/API.md`](../../api/API.md)), for iOS 17
and macOS 14.

- **Models** for every response. Enums are open (`.unknown("…")`), unknown fields
  are ignored, and a bad element is dropped without failing the rest of the response.
- **`CanopyClient`**, an actor with one async method per endpoint.
- **`CanopyTransit`**, the `@Observable` entry point:
  - Keeps `/stations`, `/lines` and chosen line diagrams on disk, revalidating
    them with their ETags. Starts from a bundled snapshot on a first launch.
  - Builds a `TransitNetwork` that resolves stored station and platform ids.
- **`ArrivalsFeed` and `VehiclesFeed`**, which poll on `refreshAfter`.
  - Arrivals fall back to 511 directly when the API can't answer, using a 511 key
    the app passes in.

```swift
let transit = CanopyTransit(configuration: .init(
    baseURL: URL(string: "https://muni-staging.canopysf.com")!,
    fallbackKey: my511Key,
    snapshot: Bundle.main.url(forResource: "canopy-snapshot", withExtension: "json"),
    lineDetails: ["SF:J", "SF:N"]))

try? await transit.refresh()        // on launch, and on return to the foreground

if let home = transit.network.resolvePlatform(savedPlatformID) {
    let feed = transit.arrivalsFeed(platforms: [home.platform], limit: 12)
    // In the view: .task { await feed.run() }, then feed.arrivals(at: home.platform)
}
```

## The 511 fallback

The Muni+ API is asked first. 511's StopMonitoring is asked instead when any of
these happen:

- The request fails, or takes longer than 5 s.
- The API answers with a 5xx or 429.
- The body isn't JSON.
- The predictions are more than 5 minutes old.

511 then stays the source for 2 minutes, polled every 40 s, before the API gets
another try.

- A 429 from 511 stops all calls to it until `Retry-After`, `RateLimit-Reset` or
  the top of the hour.
- Meanwhile, old predictions from the API are still shown, and failing that, the
  last arrivals the feed had.
- The answer has the same shape either way, so nothing tells the app which source
  answered.

511 takes one stop per call, so a platform with N stops costs N calls. Its answers
are read as the API would give them: ids qualified `SF:…`, with the headsign taken
from `DestinationDisplay`. `StopMonitoringTests.agreesWithTheAPI` checks this
against recorded answers from both.

The package holds no key. Pass one in `Configuration.fallbackKey`, or pass nil to
turn the fallback off.

## The bundled snapshot

```sh
python3 scripts/make_snapshot.py --base-url https://muni-staging.canopysf.com --out path/to/canopy-snapshot.json
```

Run it at release time and bundle the output. It holds `/stations`, `/lines` and
every line's `/lines/{id}`, each with the ETag it came with. If the snapshot is
still current, the first revalidation costs a 304.

## Tests

```sh
swift test
CANOPY_LIVE=1 swift test --filter liveRevalidation   # checks that staging's 304s reach the SDK
```

The fixtures (`Tests/CanopyTransitKitTests/Fixtures`) are:

- The examples from API.md.
- Captures from staging and from 511, taken on 2026-09-23.
- Hand-written edge cases.
