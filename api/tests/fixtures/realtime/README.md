# Recorded 511 realtime feeds (agency SF)

Raw GTFS-Realtime protobuf, gzipped, captured on 2026-09-22 around 14:50 Pacific.
`FIXTURES=1` replays these instead of calling 511, so no local run or test ever
spends the API key.

| file | feed | notes |
|---|---|---|
| `tripupdates-1.pb.gz`, `-2` | `/transit/tripupdates` | ~1.08 MB each raw, 18 s apart. Arrival-only updates mark a trip's last stop; departure-only mark its first. |
| `vehiclepositions-1.pb.gz`, `-2` | `/transit/vehiclepositions` | same two moments. Bearing and speed are `0.0` when unknown. ~20% of vehicles have no trip (out of service). |
| `servicealerts.pb.gz` | `/transit/servicealerts` | 41 alerts; `informed_entity` is (agency, route, stop) triples; `cause`/`effect` unset; English only. |

The feed timestamps are from that afternoon. Anything that filters by "now" has to
take its clock from the feed header in tests, not from the wall clock.
