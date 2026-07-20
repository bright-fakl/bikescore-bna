# bikescore

**Single-city bicycle network analysis as a database-free Python library.**

`bikescore` computes the [PeopleForBikes Bicycle Network Analysis](https://bikeleague.org)
scores — Level of Traffic Stress, low-stress connectivity, and access to destinations —
for one city, from a plain set of input files. No database, no workspace, no web server:
just `inputs → config → scores`.

```python
from bikescore import acquire_city, build_config, score_city, CityIdentity

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
$ bikescore-score acquire aspen-colorado --out-dir ./data
$ bikescore-score score   ./aspen-colorado --scenario default --out scores.parquet
```

## What it produces

- **`scores.parquet`** — per-census-block stress, connectivity, and access scores.
- **`neighborhood.parquet`** — the 0–100 city-level ratings (overall + per category).
- Intermediate stage outputs (network, LTS segments, destinations, …) for inspection.

## Where it fits

`bikescore` is the **scoring core**. Multi-city workspaces, content-addressed run
caching, dataset versioning, and the web UI live in the separate **bikescore-app**
orchestration layer, which drives this library through a small stage contract. The
core never depends on the app — see [Concepts](concepts.md) and
[Extensibility](reference/extensibility.md).

## Next steps

- [Installation](installation.md) — install the package and the optional `osmium` binary.
- [Score a city](tutorial/run-a-city.md) — the end-to-end tutorial.
- [How it works](how-it-works/index.md) — the pipeline, stage by stage.
- [Python API](reference/api.md) / [CLI](reference/cli.md) — the reference surface.
