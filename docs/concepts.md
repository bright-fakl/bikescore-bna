# Concepts

A `bikescore-bna` run is a pure function of three things: **inputs**, a **config**, and the
**pipeline**. Understanding these five nouns is enough to use the whole library.

## City

A [`CityIdentity`](reference/api.md) is static regional metadata — name, slug, region,
country, and (for US cities) a Census FIPS code. It drives *acquisition* (which boundary,
OSM extract, census, and employment data to fetch) but is not itself a scoring input.
It is typically stored as a `city.toml` file and loaded with `load_city(dir)`.

## Inputs

The raw files the pipeline consumes, as a `dict[str, Path]`:

| name | what | source |
|---|---|---|
| `osm` | OSM PBF clipped to the city boundary | Geofabrik |
| `boundary` | city boundary polygon (GeoJSON) | US Census / Nominatim |
| `census` | 2020 census blocks (population) | US Census (pygris) |
| `lodes_main`, `lodes_aux` | LODES employment (jobs) | US Census LODES |

[`acquire_city`](how-it-works/data-acquisition.md) produces this dict; you can also
assemble it by hand from files you already have.

## Config

A [`BNAConfig`](reference/config.md) is the effective, fully-resolved set of knobs the
stages read — stress thresholds, imputation defaults, scoring weights, destination
catalogs, and the LTS decision rules. You never construct it directly; you call
[`build_config`](reference/api.md):

```python
build_config()                 # library defaults
build_config("default")        # the bundled standard-BNA scenario
build_config("my.yaml")        # a scenario file you authored
build_config("default", {"city.default_speed": 40})  # with overrides
```

A **scenario** is a YAML document describing a config *layer* — the rules, catalogs, and
overrides that make a run mean something. `build_config` resolves a scenario into a
`BNAConfig`. See [Extensibility](reference/extensibility.md).

## Pipeline

The fixed, ordered list of eleven [stages](how-it-works/index.md):

```
parse → census → jobs → attributes → segment → stress
      → graph → connectivity → destinations → scores → neighborhood
```

Each stage is a `StageSpec`: a name, its upstream dependencies, the dataset inputs it
needs, a version, and a compute callable that reads files from upstream directories and
writes files into its own output directory. `bikescore-bna` ships `PIPELINE` as a static list;
the same `depends_on` metadata is enough for a larger tool to re-derive a DAG from it.

## `score_city`

The database-free driver: it runs every stage in `PIPELINE` order into a `workdir` you
choose (default a timestamped folder under `./bikescore-bna-runs/`), wiring each stage's
inputs from prior outputs, and returns a [`ScoreResult`](reference/api.md) mapping each
stage to its output directory. There is no SQLite, no hashing, and no run store —
`score_city` simply runs the stages in order and leaves their outputs on disk for reuse
(`ScoreResult.from_dir` rebuilds a result from such a folder).

```python
result = score_city(inputs, config)
scores = result.output("scores", "scores.parquet")
```
