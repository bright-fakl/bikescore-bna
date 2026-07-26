# bikescore-bna

**How bikeable is a city — and where does it fall short?**

`bikescore-bna` computes the PeopleForBikes
[Bicycle Network Analysis](https://bna.peopleforbikes.org/) for a single city:
how comfortable each road is to cycle on, which places residents can reach on
comfortable routes, and a single 0–100 rating for the city as a whole. It works
from ordinary open data — OpenStreetMap plus US Census / LODES figures — and runs
entirely in-process: **no database, no server, no container.**
`score_city(inputs, config)` is a plain function: files in, scores out.

## Quick start

```python
from bikescore_bna import acquire_city, build_config, score_city, CityIdentity

city = CityIdentity(name="Aspen", slug="aspen-colorado",
                    region="Colorado", country="united states", fips_code="0803620")

inputs = acquire_city(city, "./data")   # OSM + boundary + census + LODES
config = build_config("default")        # the standard BNA scenario
result = score_city(inputs, config)     # run the 11-stage pipeline, DB-free

print(result.output("scores", "scores.parquet"))
print(result.output("neighborhood", "neighborhood.parquet"))
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

`osmium` (the CLI binary) is recommended for fast PBF clipping; a pure-Python
pyosmium fallback exists (~8× slower).

## Documentation

Full documentation: <https://bright-fakl.github.io/bikescore-bna/>

- **What bikescore measures** — the scores, in plain language.
- **How it works** — the eleven-stage pipeline, stage by stage.
- **Differences from brokenspoke-analyzer** — how this relates to the original.

## Relationship to brokenspoke-analyzer

`bikescore-bna` is a pure-Python port of the PeopleForBikes
[brokenspoke-analyzer](https://github.com/PeopleForBikes/brokenspoke-analyzer)
(the original SQL/PostGIS implementation). It targets value-for-value parity:
each stage output is compared against a ground-truth reference (via
`compare_dataframes`) on Aspen, Colorado — the maintainer's manual validation
city. A small set of intentional, documented divergences (SQL bug fixes,
pipeline-ordering choices, floating-point artefacts) lives in
`bikescore_bna.deviations`; see the docs under **Differences from
brokenspoke-analyzer**. Where the two disagree without a documented deviation,
the SQL reference is ground truth.

## License

MIT
