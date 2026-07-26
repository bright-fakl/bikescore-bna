# bikescore-bna

**How bikeable is a city — and where does it fall short?**

`bikescore-bna` answers that question with numbers. It computes the
[PeopleForBikes Bicycle Network Analysis](https://bna.peopleforbikes.org/) for a single
city: how comfortable each road is to cycle on, which places residents can reach
on comfortable routes, and a single 0–100 rating for the city as a whole. It
works from ordinary open data — OpenStreetMap plus US Census figures — and needs
no database, server, or account.

New to the BNA? **[What bikescore measures](what-it-measures.md)** explains the
scores without code.

## Who it's for

- **Planners, advocates, and researchers** who want to understand or reproduce a
  city's bike score, and see *where* the network fails people. Start with
  [What it measures](what-it-measures.md) and [How it works](how-it-works/index.md).
- **Developers** who want to run the analysis, embed it, or customise the rules.
  Jump to [Score a city](tutorial/run-a-city.md) or the [Python API](reference/api.md).

## What it produces

- **`scores.parquet`** — per-neighbourhood (census block) stress, connectivity, and access scores.
- **`neighborhood.parquet`** — the 0–100 city-level ratings (overall + per category).
- Intermediate outputs (the road network, stress-labelled segments, destinations, …) for inspection.

## Run it

`bikescore-bna` is a plain function — input files in, a config, scores out — that
runs the whole analysis in-process with no database and no server:

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

It runs an eleven-stage pipeline in-process — each stage reads
files from the stages before it and writes files of its own, with no shared
state. The stages are exposed through a small, generic contract so a larger tool
can drive them with caching, run history, or a UI, without `bikescore-bna` ever
depending on that tool. See [Concepts](concepts.md) and
[Extensibility](reference/extensibility.md).

## Next steps

- [What bikescore measures](what-it-measures.md) — the scores, in plain language.
- [Installation](installation.md) — install the package and the optional `osmium` binary.
- [Score a city](tutorial/run-a-city.md) — the end-to-end tutorial.
- [How it works](how-it-works/index.md) — the pipeline, stage by stage.
- [Python API](reference/api.md) / [CLI](reference/cli.md) — the reference surface.
- [Differences from brokenspoke-analyzer](differences/index.md) — how this relates to the original PeopleForBikes implementation.
