# Score a city

Compute BNA scores for a city end to end. We use Aspen, Colorado.

## 1. Describe the city

Create a `city.toml` (or a `CityIdentity` in Python):

```toml
name = "Aspen"
slug = "aspen-colorado"
region = "Colorado"
country = "united states"
fips_code = "0803620"
```

The fields are documented under [`CityIdentity`](../reference/api.md) and
[Concepts → City](../concepts.md).

## 2. Acquire the inputs

```console
$ bikescore-bna acquire ./aspen-colorado --out-dir ./aspen-colorado/datasets
```

This downloads the boundary, the Colorado OSM extract (clipped to Aspen), the 2020
census blocks, and the LODES employment files — see
[Data acquisition](../how-it-works/data-acquisition.md). The files land under
content-addressed names (`osm-*.pbf`, `boundary-*.geojson`, …).

In Python:

```python
from bikescore_bna import acquire_city, load_city
city = load_city("aspen-colorado")
inputs = acquire_city(city, "aspen-colorado/datasets")
```

See [`acquire`](../reference/cli.md#acquire) / [`acquire_city`](../reference/api.md).

## 3. Score

```console
$ bikescore-bna score ./aspen-colorado --scenario default --out scores.parquet
scores → scores.parquet
```

or

```python
from bikescore_bna import build_config, score_city
result = score_city(inputs, build_config("default"))
```

The `default` scenario and how to override it are described under
[Configuration](../reference/config.md); the stage sequence it runs is in
[How it works](../how-it-works/index.md).

## 4. Read the results

```python
import pandas as pd

scores = pd.read_parquet(result.output("scores", "scores.parquet"))
ratings = pd.read_parquet(result.output("neighborhood", "neighborhood.parquet"))

print(ratings[["score_id", "score_normalized"]])   # the 0–100 city ratings
```

- `scores.parquet` — one row per census block, with stress, access, and connectivity
  scores. See [Scoring](../how-it-works/scoring.md).
- `neighborhood.parquet` — the city-level ratings (overall + per category). See
  [Neighborhood scores](../how-it-works/neighborhood-scores.md).

Every column is catalogued in [Output files](../reference/output-files.md).

Every intermediate stage output is also on disk under `result.workdir` for inspection —
the routing network, the LTS segments, the destination clusters, and more. `workdir`
persists (it is the `--out-dir` you passed, or a timestamped folder under
`./bikescore-bna-runs/`), so you can point `export --from` at it later without recomputing.

## 5. Export for GIS

The results are parquet. To open them in QGIS or hand them to the PeopleForBikes
platform, export to GeoJSON / Shapefile / CSV:

```console
$ bikescore-bna export ./aspen-colorado --bundle bna --out ./export
```

The `bna` bundle writes the full deliverable set — `neighborhood_ways.geojson`
(the LTS network), `neighborhood_census_blocks.geojson` (blocks with scores), the
connectivity CSVs, and more — into `./export`, with a `README.md` describing each
file. Export a single layer instead with `--target` / `--format`:

```console
$ bikescore-bna export ./aspen-colorado --target stress --format geojson --out ./export
wrote export/stress.geojson
```

If you kept a prior `score` run's outputs, point `--from` at that run directory to
export without recomputing. In Python:

```python
from bikescore_bna import export_bundle, export_target
config = build_config("default")
export_bundle(result, city, config, "export/", bundle="bna", inputs=inputs)
export_target(result, city, config, "stress", "export/", file_format="geojson", inputs=inputs)
```

See [`export`](../reference/cli.md#export) and
[Output files → Export](../reference/output-files.md#export) for the full target and
bundle catalog.

## Related pages

- **How it works:** [Overview](../how-it-works/index.md) ·
  [Data acquisition](../how-it-works/data-acquisition.md) ·
  [Scoring](../how-it-works/scoring.md) ·
  [Neighborhood scores](../how-it-works/neighborhood-scores.md)
- **Reference:** [CLI](../reference/cli.md) · [Python API](../reference/api.md) ·
  [Configuration](../reference/config.md) · [Output files](../reference/output-files.md)
- **Next:** [Inspect the LTS network](lts-network.md)
