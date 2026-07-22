# CLI — `bikescore-bna`

`bikescore-bna` ships one console script, `bikescore-bna`, a thin shell over the
[Python API](api.md):

```console
$ bikescore-bna --help
```

`<city>` in every command is a **path** to a directory containing a `city.toml`. There is
no slug lookup against a multi-city store — a city is always an explicit path.

## `score`

Run the full pipeline and write the block-level `scores` table.

```console
$ bikescore-bna score <city> [OPTIONS]
```

| option | default | meaning |
|---|---|---|
| `--scenario`, `-s` | `default` | bundled scenario name, or a path to a scenario YAML |
| `--set k=v` | — | config override (repeatable), e.g. `--set city.default_speed=40` |
| `--set-file` | — | YAML file of `key: value` overrides (merged under `--set`) |
| `--out-dir` | `<city>/runs/<timestamp>` | persist **all** stage outputs here (reusable by `export --from`) |
| `--out`, `-o` | — | also copy `scores.parquet` to this file |
| `--datasets` | `<city>/datasets` | directory holding the raw inputs |
| `--to` | — | stop after this stage (partial run) |

Stage outputs are kept under `--out-dir` (never a discarded temp dir), so a later
`export --from <that dir>` reuses them without recomputing.

Raw inputs are discovered in the datasets directory by name (`osm-*.pbf`,
`boundary-*.geojson`, `census-*.parquet`, `lodes_main-*.csv`, `lodes_aux-*.csv`) — the
layout [`acquire`](#acquire) writes. All stage outputs (network, LTS segments,
neighborhood ratings, …) are left in a temp working directory, printed on stderr.

```console
$ bikescore-bna score ./aspen-colorado --scenario default --out scores.parquet
scores → scores.parquet
```

## `acquire`

Download the raw inputs (OSM, boundary, and — for US cities — census + LODES).

```console
$ bikescore-bna acquire <city> [--out-dir DIR] [--pbf-cache-dir DIR] [--force]
```

`--out-dir` is where the content-addressed input files land — it **defaults to
`<city>/datasets/`**, the same place `score`/`export` read from, so `acquire <city>`
then `score <city>` works with no flags. Point it elsewhere to keep several input sets
side by side (see [Working with multiple datasets](#working-with-multiple-datasets));
`--pbf-cache-dir` relocates the shared regional-PBF cache (default `$BIKESCORE_PBF_CACHE`
or `~/.bikescore-bna/pbf`); `--force` re-downloads the shared regional PBF even on a cache
hit. See [Data acquisition](../how-it-works/data-acquisition.md).

## `scenarios`

List the bundled scenario names available to `--scenario`.

```console
$ bikescore-bna scenarios
default
```

## `scenario show`

Dump a bundled scenario's YAML so you can copy, edit, and feed it back via `--scenario FILE`.
Prints to stdout (redirect or pipe it), or use `--out` to write a file.

```console
$ bikescore-bna scenario show default > my-scenario.yaml
# …edit my-scenario.yaml…
$ bikescore-bna score ./aspen-colorado --scenario my-scenario.yaml
```

| option | default | meaning |
|---|---|---|
| `--out`, `-o` | — | write the YAML to this file instead of stdout |

`<name>` may pin a version (e.g. `default@1`). A reusable scenario is one self-contained
YAML file; keep policy in the scenario and put per-run scalar tweaks in a separate
`--set-file` (or `--set`) so the scenario stays reusable.

## `export`

Export a city's pipeline outputs to GeoJSON / Shapefile / CSV. Pass `--from <run dir>` to
reuse a prior `score` run's outputs without recomputing; otherwise the pipeline runs first,
persisting stage outputs under `--workdir` (default `<city>/runs/<timestamp>`). The
requested outputs are written under `--out`. See
[Output files → Export](output-files.md#export) for the target and bundle catalog.

```console
$ bikescore-bna export <city> [OPTIONS]
```

| option | default | meaning |
|---|---|---|
| `--target`, `-t` | — | a single [export target](output-files.md#targets); requires `--format` |
| `--bundle`, `-b` | `bna` (if no `--target`) | export a named bundle of targets |
| `--format`, `-f` | — | `geojson` \| `shapefile` \| `csv` (with `--target`) |
| `--out`, `-o` | `./export` | destination directory |
| `--from` | — | reuse stage outputs from a prior `score` run dir (no recompute) |
| `--workdir` | `<city>/runs/<timestamp>` | where to persist stage outputs when computing |
| `--scenario`, `-s` | `default` | bundled scenario name or a scenario YAML path |
| `--set k=v` | — | config override (repeatable) |
| `--set-file` | — | YAML file of `key: value` overrides (merged under `--set`) |
| `--datasets` | `<city>/datasets` | directory holding the raw inputs |

Pass either `--target` or `--bundle`, not both. With `--from`, the pipeline is not run —
outputs are read from the given run directory. Export the road-segment stress network as
GeoJSON:

```console
$ bikescore-bna export ./aspen-colorado --target stress --format geojson --out ./gis
wrote gis/stress.geojson
1 file(s) → ./gis
```

…or the whole brokenspoke-analyzer deliverable set:

```console
$ bikescore-bna export ./aspen-colorado --bundle bna --out ./results
```

## `export-list`

List the exportable targets, their owner stage, supported formats, and the bundles that
include each.

```console
$ bikescore-bna export-list
```

## `validate`

Score a city and compare each stage output against a reference directory (the
`<stage>/<file>.parquet` layout of `tests/oracle/aspen` or a brokenspoke-analyzer
export). Prints a per-stage pass/fail table and exits non-zero if any stage differs.

```console
$ bikescore-bna validate <city> --reference tests/oracle/aspen [--stage stress]
```

| option | default | meaning |
|---|---|---|
| `--reference`, `-r` | *(required)* | reference dir with `<stage>/<file>.parquet` |
| `--stage` | all | validate only this stage (faster partial run) |
| `--datasets` | `<city>/datasets` | directory holding the raw inputs |
| `--scenario`, `-s` / `--set` / `--set-file` | `default` | config, as for `score` |
| `--strict` | off | treat known SQL deviations as differences |

See [Validation](../development/validation.md) for the full workflow.

## Working with multiple datasets

`bikescore-bna` is a **stateless function of explicit inputs** — it remembers nothing
between calls. A "dataset" is just a **directory of the five role-named files** (`osm-*`,
`boundary-*`, `census-*`, `lodes_main-*`, `lodes_aux-*`); there is no registry. So you
handle many datasets — or many cities — simply by **looping the same commands**, one
input directory at a time:

```console
# same city, two input sets (e.g. this year's OSM vs a fresh re-pull)
$ bikescore-bna acquire ./aspen-colorado --out-dir ./inputs/2024
$ bikescore-bna acquire ./aspen-colorado --out-dir ./inputs/2025 --force
$ bikescore-bna score ./aspen-colorado --datasets ./inputs/2024 --out scores-2024.parquet
$ bikescore-bna score ./aspen-colorado --datasets ./inputs/2025 --out scores-2025.parquet
```

None of this needs any extra tooling: files are content-addressed (re-acquiring identical
bytes is a no-op) and the regional-PBF cache is shared across directories (only the clip
differs).

From Python the loop is just as direct. `discover_inputs(dir)` turns a directory into the
`{role: Path}` mapping `score_city` wants, so batching over cities is a plain `for`:

```python
from bikescore_bna import build_config, discover_inputs, score_city

config = build_config("default")
for city_dir in ("./aspen-colorado", "./boulder-colorado", "./denver-colorado"):
    result = score_city(discover_inputs(f"{city_dir}/datasets"), config)
    ...
```

Because the input is an explicit dict, you can also reuse most of one dataset and swap a
single role — e.g. score against an alternate OSM extract without re-acquiring the rest:

```python
from pathlib import Path

inputs = discover_inputs("./inputs/2024")
inputs["osm"] = Path("./inputs/2025/osm-abc123.pbf")
score_city(inputs, config)
```

What `bikescore-bna` deliberately does *not* do is **track** any of this: giving datasets
names and IDs, versioning them, deduping them as entities, recording provenance, or
comparing runs across them. That bookkeeping is a system-of-record concern, left to
whatever tool wraps the library; `bikescore-bna` itself stays a stateless function of explicit
inputs.
