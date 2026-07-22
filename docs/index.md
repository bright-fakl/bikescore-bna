# bikescore-bna

**Single-city bicycle network analysis as a Python library.**

`bikescore-bna` computes the [PeopleForBikes Bicycle Network Analysis](https://bikeleague.org)
scores — Level of Traffic Stress, low-stress connectivity, and access to destinations —
for one city, from a plain set of input files. It is a pure-Python port of the
PeopleForBikes [brokenspoke-analyzer](https://github.com/PeopleForBikes/brokenspoke-analyzer)
(the original SQL/PostGIS implementation) that runs without a database — see
[Why bikescore-bna](why-bikescore.md) for the motivation and how the two differ.

```python
from bikescore_bna import acquire_city, build_config, score_city, CityIdentity

city = CityIdentity(name="Aspen", slug="aspen-colorado",
                    region="Colorado", country="united states", fips_code="0803620")

inputs = acquire_city(city, "./data")          # OSM + boundary + census + LODES
config = build_config("default")               # the standard BNA scenario
result = score_city(inputs, config)            # run the 11-stage pipeline

print(result.output("scores", "scores.parquet"))
print(result.output("neighborhood", "neighborhood.parquet"))
```

Or from the command line:

```console
$ bikescore-bna acquire aspen-colorado --out-dir ./data
$ bikescore-bna score   ./aspen-colorado --scenario default --out scores.parquet
```

## What it produces

- **`scores.parquet`** — per-census-block stress, connectivity, and access scores.
- **`neighborhood.parquet`** — the 0–100 city-level ratings (overall + per category).
- Intermediate stage outputs (network, LTS segments, destinations, …) for inspection.

## How it's built

`bikescore-bna` is a pure function: input files in, a config, scores out. It runs the
eleven-stage pipeline in-process with no database, no server, and no persistent state —
each stage reads files from its upstream stages and writes files of its own. The stages
are exposed through a small, generic contract so a larger tool can drive them with
caching, run history, or a UI, without `bikescore-bna` ever depending on that tool. See
[Concepts](concepts.md) and [Extensibility](reference/extensibility.md).

## Next steps

- [Why bikescore-bna](why-bikescore.md) — the motivation and differences from brokenspoke-analyzer.
- [Installation](installation.md) — install the package and the optional `osmium` binary.
- [Score a city](tutorial/run-a-city.md) — the end-to-end tutorial.
- [How it works](how-it-works/index.md) — the pipeline, stage by stage.
- [Python API](reference/api.md) / [CLI](reference/cli.md) — the reference surface.
