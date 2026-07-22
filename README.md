# bikescore-bna

**Single-city bicycle network analysis as a pure-Python library.**

`bikescore-bna` computes bicycle safety (Level of Traffic Stress) and connectivity/access
scores for one city from OpenStreetMap + US Census/LODES inputs. It is a pure-Python port
of the PeopleForBikes [brokenspoke-analyzer](https://github.com/PeopleForBikes/brokenspoke-analyzer)
(the original SQL/PostGIS implementation) that runs entirely in-process — **no database,
no server, no container**. `score_city(inputs, config)` is a plain function: files in,
scores out.

## Public API

```python
from bikescore_bna import build_config, score_city, acquire_city, list_bundled_scenarios

config = build_config("default")
inputs = acquire_city(city)                 # {"osm": …, "boundary": …, "census": …, …}
result = score_city(inputs, config)         # DB-free, ~11-stage pipeline into a temp dir
```

## CLI

```
bikescore-bna score    <city> [--scenario default|path.yaml] [--set k=v …] [--out scores.parquet]
bikescore-bna acquire  <city> [--out-dir ./data]
bikescore-bna scenarios
```

## Install (dev)

```
uv sync
uv run pytest
```

`osmium` (the CLI binary) is recommended for fast PBF clipping; a pure-Python pyosmium
fallback exists (~8× slower).

## Parity

`bikescore-bna` targets value-for-value parity with brokenspoke-analyzer. Each stage output is
compared against a ground-truth reference (via `compare_dataframes`) on Aspen, Colorado —
the maintainer's manual validation city. A small set of intentional, documented
divergences (SQL bug fixes, pipeline-ordering choices, floating-point artefacts) lives in
`bikescore_bna.deviations`; see the docs under **Differences from brokenspoke-analyzer**. Where
the two disagree without a documented deviation, the SQL reference is ground truth.

## License

MIT
