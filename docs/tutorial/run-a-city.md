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

## 2. Acquire the inputs

```console
$ bikescore-score acquire ./aspen-colorado --out-dir ./aspen-colorado/datasets
```

This downloads the boundary, the Colorado OSM extract (clipped to Aspen), the 2020
census blocks, and the LODES employment files — see
[Data acquisition](../how-it-works/data-acquisition.md). The files land under
content-addressed names (`osm-*.pbf`, `boundary-*.geojson`, …).

In Python:

```python
from bikescore import acquire_city, load_city
city = load_city("aspen-colorado")
inputs = acquire_city(city, "aspen-colorado/datasets")
```

## 3. Score

```console
$ bikescore-score score ./aspen-colorado --scenario default --out scores.parquet
scores → scores.parquet
```

or

```python
from bikescore import build_config, score_city
result = score_city(inputs, build_config("default"))
```

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

Every intermediate stage output is also on disk under `result.workdir` for inspection —
the routing network, the LTS segments, the destination clusters, and more. `workdir`
persists (it is the `--out-dir` you passed, or a timestamped folder under
`./bikescore-runs/`), so you can point `export --from` at it later without recomputing
(see [Output files](../reference/output-files.md)).
