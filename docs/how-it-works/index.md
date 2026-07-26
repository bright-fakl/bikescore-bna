# How it works

This section explains how `bikescore-bna` turns raw map and census data into a
bike score. For *what* the scores mean, see
[What bikescore measures](../what-it-measures.md); this section covers *how*
they are computed.

Before any scoring happens, a separate **[data acquisition](data-acquisition.md)**
step gathers the raw inputs — the OpenStreetMap extract, the city boundary, and
US census and employment data. It is not part of the pipeline; it produces the
files the pipeline consumes.

Given those inputs, the scoring itself is a fixed, eleven-stage **pipeline**. Each
stage reads files produced by the stages before it and writes files of its own —
a pure `(inputs, config) → files` function with no shared state and no database.

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
| `segment` | split ways into routable road segments; extract trails | [Segmenting](segmenting.md) |
| `stress` | assign Level of Traffic Stress to every segment | [Stress](stress.md) |
| `graph` | build the routing network and block↔road links | [Routing network](routing-network.md) |
| `connectivity` | low-stress reachability between census blocks | [Connectivity](connectivity.md) |
| `destinations` | cluster and locate access destinations | [Destinations](destinations.md) |
| `scores` | per-block stress / access / connectivity scores | [Scoring](scoring.md) |
| `neighborhood` | roll blocks up into 0–100 city ratings | [Neighborhood scores](neighborhood-scores.md) |

The exact contents of each output file are catalogued in
[Output files](../reference/output-files.md). How each stage relates to the original brokenspoke-analyzer implementation — and
the few places the output intentionally differs — is covered in
[Differences from brokenspoke-analyzer](../differences/index.md).
