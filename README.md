# bikescore

**Single-city bicycle network analysis as a database-free Python library.**

`bikescore` computes bicycle safety (Level of Traffic Stress) and connectivity/access
scores for one city from OpenStreetMap + US Census/LODES inputs — with **no database, no
workspace, and no web dependencies**. It is the scoring *core* carved out of
[`bna-core`](../bna-core); multi-city workspaces, content-addressed runs, dataset
versioning, the web UI, and the full CLI live in the separate orchestration layer
[`bikescore-app`](../bikescore-app).

> **Status:** early port (Phase 38). The public API below is landing sub-phase by
> sub-phase (38a scaffold → 38g release). Until 38f, `score_city` is not yet wired.

## Public API (target)

```python
from bikescore import build_config, score_city, acquire_city, list_bundled_scenarios

config = build_config("default")
inputs = acquire_city(city)                 # {"osm": …, "boundary": …, "census": …, …}
result = score_city(inputs, config)         # DB-free, ~11-stage pipeline into a temp dir
```

## CLI

```
bikescore-score score    <city> [--scenario default|path.yaml] [--set k=v …] [--out scores.parquet]
bikescore-score acquire  <city> [--out-dir ./data]
bikescore-score scenarios
```

## Install (dev)

```
uv sync
uv run pytest
```

`osmium` (the CLI binary) is recommended for fast PBF clipping; a pure-Python pyosmium
fallback exists (~8× slower).

## Parity

During the Phase 38 split the algorithm is validated against the **frozen `bna-core`
oracle** on Aspen, Colorado, via `compare_dataframes` — no workspace or run store required.
That oracle baseline is **split-time scaffolding**: it lives locally under `tests/oracle/`
(gitignored, regenerable via `tests/oracle/regenerate_aspen.py`) and is removed at cutover.
The durable parity mechanism carried into the core suite is the brokenspoke-analyzer
reference-parquet comparison (`/export-reference` + `/validate-stage`, landing in 38d/38g).

## License

MIT
