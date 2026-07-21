# CLI — `bikescore-score`

The core ships one console script, `bikescore-score`, a thin shell over the
[Python API](api.md). No workspace or database:

```console
$ bikescore-score --help
```

`<city>` in every command is a **path** to a directory containing a `city.toml`. Slug
lookup against a multi-city project store lives in bikescore-app, not the core CLI.

## `score`

Run the full pipeline and write the block-level `scores` table.

```console
$ bikescore-score score <city> [OPTIONS]
```

| option | default | meaning |
|---|---|---|
| `--scenario`, `-s` | `default` | bundled scenario name, or a path to a scenario YAML |
| `--set k=v` | — | config override (repeatable), e.g. `--set imputation.city_default_speed=40` |
| `--out`, `-o` | `scores.parquet` | where to write the scores table |
| `--datasets` | `<city>/datasets` | directory holding the raw inputs |
| `--to` | — | stop after this stage (partial run) |

Raw inputs are discovered in the datasets directory by name (`osm-*.pbf`,
`boundary-*.geojson`, `census-*.parquet`, `lodes_main-*.csv`, `lodes_aux-*.csv`) — the
layout [`acquire`](#acquire) writes. All stage outputs (network, LTS segments,
neighborhood ratings, …) are left in a temp working directory, printed on stderr.

```console
$ bikescore-score score ./aspen-colorado --scenario default --out scores.parquet
scores → scores.parquet
```

## `acquire`

Download the raw inputs (OSM, boundary, and — for US cities — census + LODES).

```console
$ bikescore-score acquire <city> [--out-dir ./data] [--force]
```

`--out-dir` is where the content-addressed input files land (default `./data`);
`--force` re-downloads the shared regional PBF even on a cache hit. See
[Data acquisition](../how-it-works/data-acquisition.md).

## `scenarios`

List the bundled scenario names available to `--scenario`.

```console
$ bikescore-score scenarios
default
```

## `export`

Run the pipeline for a city and export outputs to GeoJSON / Shapefile / CSV. The full
pipeline runs first — the core keeps no run store to reuse — then the requested outputs are
written under `--out`. See [Output files → Export](output-files.md#export) for the target
and bundle catalog.

```console
$ bikescore-score export <city> [OPTIONS]
```

| option | default | meaning |
|---|---|---|
| `--target`, `-t` | — | a single [export target](output-files.md#targets); requires `--format` |
| `--bundle`, `-b` | `bna` (if no `--target`) | export a named bundle of targets |
| `--format`, `-f` | — | `geojson` \| `shapefile` \| `csv` (with `--target`) |
| `--out`, `-o` | `./export` | destination directory |
| `--scenario`, `-s` | `default` | bundled scenario name or a scenario YAML path |
| `--set k=v` | — | config override (repeatable) |
| `--datasets` | `<city>/datasets` | directory holding the raw inputs |

Pass either `--target` or `--bundle`, not both. Export the road-segment stress network as
GeoJSON:

```console
$ bikescore-score export ./aspen-colorado --target stress --format geojson --out ./gis
wrote gis/stress.geojson
1 file(s) → ./gis
```

…or the whole brokenspoke-analyzer deliverable set:

```console
$ bikescore-score export ./aspen-colorado --bundle bna --out ./results
```

## `export-list`

List the exportable targets, their owner stage, supported formats, and the bundles that
include each.

```console
$ bikescore-score export-list
```
