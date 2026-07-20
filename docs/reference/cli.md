# CLI — `bikescore-score`

The core ships one console script, `bikescore-score`, a thin shell over the
[Python API](api.md). Three commands, no workspace or database:

```console
$ bikescore-score --help
```

`<city>` in every command resolves to a directory containing a `city.toml`, given either
as a **path** to that directory or as a **slug** looked up under the global settings
`project_root`.

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
