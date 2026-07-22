# How it works

`bikescore-bna` runs a fixed, eleven-stage pipeline. Each stage reads files from its upstream
stages' output directories and writes files into its own — a pure
`(inputs, config) → files` function with no shared state.

```
parse → census → jobs → attributes → segment → stress
      → graph → connectivity → destinations → scores → neighborhood
```

| stage | does | page |
|---|---|---|
| `parse` | read the clipped OSM into ways, nodes, and POIs | [OSM parsing](osm-parsing.md) |
| `census` | clip 2020 census blocks to the city | — |
| `jobs` | attach LODES employment to blocks | [Scoring](scoring.md) |
| `attributes` | derive per-way road attributes (lanes, speed, bike infra, …) | [Road attributes](road-features.md) |
| `segment` | split ways into routable road segments; extract trails | [Routing network](routing-network.md) |
| `stress` | assign Level of Traffic Stress to every segment | [Stress](stress.md) |
| `graph` | build the routing network and block↔road links | [Routing network](routing-network.md) |
| `connectivity` | low-stress reachability between census blocks | [Connectivity](connectivity.md) |
| `destinations` | cluster and locate access destinations | [Destinations](destinations.md) |
| `scores` | per-block stress / access / connectivity scores | [Scoring](scoring.md) |
| `neighborhood` | roll blocks up into 0–100 city ratings | [Neighborhood scores](neighborhood-scores.md) |

The exact contents of each output file are catalogued in
[Output files](../reference/output-files.md). Where `bikescore-bna` intentionally diverges
from the brokenspoke-analyzer SQL reference, the difference is documented under
[Known deviations](deviations.md).
