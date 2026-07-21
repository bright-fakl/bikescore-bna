# Contributing

`bikescore` is a database-free library that computes stress and access scores for one
city. It stays a pure `(inputs, config) → files` function; anything stateful built on top
of it — multi-city stores, run caching, a web UI — lives outside the library and is not
part of this repo.

## Dev setup

```console
$ uv sync              # create the venv and install core + dev extras
$ uv run pytest        # fast suite (slow + integration deselected by default)
```

`osmium` (the CLI binary) is recommended for fast PBF clipping; a pure-Python pyosmium
fallback works but is ~8× slower. See [Installation](../installation.md).

## Tooling

| Tool | Command | Notes |
|---|---|---|
| Tests | `uv run pytest` | markers `slow`, `integration`, `oracle`, `unit`; the default `addopts` deselects `slow` + `integration` |
| Lint | `uv run ruff check` | line length 100; **ruff is the gate** — CI fails on findings |
| Format | `uv run ruff format` | not a gate, but keep the diff clean |
| Types | `uv run pyright` | advisory, not a merge gate |

Run the heavier checks explicitly when touching the pipeline:

```console
$ uv run pytest -m slow            # per-stage + end-to-end parity on Aspen
$ uv run pytest -m integration     # needs local cached input data
```

## The one load-bearing rule: import direction

**`bikescore` must never depend on anything built on top of it.** The dependency direction
is one-way, into the library — any caching layer, run store, or web UI is a consumer of
`bikescore`, never the reverse. `tests/test_import_guard.py` scans every source and test
file statically and fails on any import of such an orchestration layer, so the rule holds
even when that layer isn't installed. A violation is an architecture regression, not a
style nit.

Corollaries `bikescore` keeps to:

- **No database, workspace, or web dependencies.** Stages take file paths in and write
  files out; there is no run store to reach for.
- **Rules are data, not code.** Stress, attribute, and destination logic lives in decision
  tables carried by scenarios (see [Extensibility](../reference/extensibility.md)), not in Python
  branches.
- **The stage metadata stays co-located.** `version` and the config-slice look unused
  here but are the cache-buster and invalidation key a caching layer relies on — leave them
  with the stage that owns them (an access-audit test keeps this honest).

## Correctness bar

Ground truth is the SQL reference (`brokenspoke-analyzer`); the algorithm is pinned to a
frozen reference output on Aspen. Any behavioural change must be validated — see
[Validation & parity](validation.md). When the spec and the SQL reference disagree, the
SQL reference wins.
