# Contributing

`bikescore` is the scoring **core**: a database-free library that computes stress and
access scores for one city. The orchestration layer (multi-city workspaces,
content-addressed runs, web UI, full CLI) lives separately in `bikescore-app`.

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

**The core must never import from `bikescore_app`.** The dependency direction is
app → core, one-way — content-addressed reuse, the run store, and the web UI all depend on
the core staying free of them. `tests/test_import_guard.py` scans every source and test
file statically and fails on any `bikescore_app` import, so the rule holds even when the
app package isn't installed. A violation is an architecture regression, not a style nit.

Corollaries the core keeps to:

- **No database, workspace, or web dependencies** in core. Stages take file paths in and
  write files out; there is no run store to reach for.
- **Rules are data, not code.** Stress, attribute, and destination logic lives in decision
  tables carried by scenarios (see [Extensibility](../reference/extensibility.md)), not in Python
  branches.
- **The stage metadata stays co-located.** `version` and the config-slice look unused in
  core but are the app's cache-buster and invalidation key — leave them with the stage
  that owns them (an access-audit test keeps this honest).

## Correctness bar

Ground truth is the SQL reference (`brokenspoke-analyzer`); during the Phase 38 split the
algorithm is additionally pinned to the frozen `bna-core` oracle on Aspen. Any behavioural
change must be validated — see [Validation & parity](validation.md). When the spec and the
SQL reference disagree, the SQL reference wins.
