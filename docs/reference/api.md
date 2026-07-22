# Python API

`bikescore-bna` scores a single city in-process, with no database and no server. The
deliverable surface is small: build a config, hand in the input files, get scores back.

```python
from bikescore_bna import build_config, score_city

config = build_config("default")
result = score_city(inputs, config)          # inputs: {"osm": ..., "boundary": ..., ...}
scores = result.output("scores", "scores.parquet")
```

## Config

```python
BNAConfig.with_defaults() -> BNAConfig
build_config(scenario: str | dict | Path | None = None,
             overrides: dict | None = None) -> BNAConfig
#   None            → with_defaults()
#   "default"/name  → bundled scenario
#   dict / Path     → caller-supplied scenario doc
list_bundled_scenarios() -> list[str]
```

## Scoring

```python
score_city(inputs: dict[str, Path], config: BNAConfig,
           *, workdir: Path | None = None,
           pinned: dict[str, Path] | None = None,
           to_stage: str | None = None) -> ScoreResult
```

Runs every stage in `PIPELINE` order into `workdir` — no SQLite, no content-addressed
hashing, no run store, no `graphlib`. It is the minimal driver: the stages, in order, and
nothing else.

- **`inputs`** — dataset-input name → file path. Must cover every `dataset_inputs` name
  the stages declare, e.g. `{"osm": ..., "boundary": ..., "census": ..., "lodes_main":
  ...}` (see `acquire_city`, 38g).
- **`workdir`** — directory to write stage outputs into (created if missing). Outputs
  **persist** here for reuse; there is no temp dir that gets silently discarded. Defaults
  to a fresh timestamped folder under `./bikescore-bna-runs/`.
- **`pinned`** — `{stage_name: output_dir}` of prebuilt stage outputs. A pinned stage is
  **not** recomputed; its directory is used verbatim as the upstream for later stages
  (e.g. supply a custom network for `parse`).
- **`to_stage`** — stop after this stage (inclusive) for a partial run.

`ScoreResult` carries `stage_dirs: dict[str, Path]` (every stage that ran or was pinned →
its output directory), `workdir: Path` (the run root), and a convenience
`result.output(stage, filename) -> Path`. `ScoreResult.from_dir(workdir)` rebuilds a
result from a folder a prior run wrote to, so export/validate can reuse it without
recomputing.

## The stage contract — `StageSpec`, `PIPELINE`, `run_stage`

This is the **public plugin contract**: a larger orchestration layer can consume these to
build a content-addressed run store, a dynamic DAG, and a UI on top of `bikescore-bna`,
without `bikescore-bna` ever depending on it — the dependency direction is one way, into the
library. `score_city` itself uses only this contract.

```python
@dataclass(frozen=True)
class StageSpec:
    name: str
    depends_on: tuple[str, ...]      # prior stage names; static, resolved before exec
    dataset_inputs: tuple[str, ...]  # named external inputs (opaque to the orchestrator)
    version: str                     # author-declared cache-buster (opaque)
    run: Callable[[dict[str, Path], Path, BNAConfig], None]  # (input_paths, out_dir, config)
    cacheable: bool = True           # False → always recompute (foreign/nondeterministic)

PIPELINE: list[StageSpec]            # the fixed, ordered 11-stage sequence

def run_stage(stage, upstream_dirs, dataset_paths, output_dir, config) -> None: ...
```

- **`PIPELINE`** is an explicit ordered list, **not** a runtime graph. The full pipeline
  is a fixed sequence (no config-driven stage inclusion), so core needs only *ordering*.
  A dynamic DAG system (topological sort, ancestors/descendants, reuse planner,
  `--from/--to` windows) is something a larger tool can layer on, re-deriving its graph
  from the same `depends_on` metadata. One source of truth for deps; a stdlib drift-guard
  test asserts `PIPELINE` is a valid topological order.
- **`run_stage`** is the shared primitive: it assembles `input_paths` (each `depends_on`
  name → its upstream output dir; each `dataset_inputs` name → its path under a
  `"dataset:<name>"` key), then calls `stage.run`. `score_city` loops it; an orchestration
  engine can wrap the *same* primitive with hash/reuse/persist, so the two execution paths
  cannot drift. Dataset-path *resolution* stays out of the primitive (`score_city` passes
  caller paths; an orchestrator resolves its own `file_id → path`).
- **The metadata is the orchestration contract.** `version` and the co-located
  `config_slice_for_stage` / `_STAGE_CONFIG_FIELDS` (in `bikescore_bna.config`) look "dead"
  in the library — `score_city` reads only `depends_on` / `dataset_inputs` — but they are
  the cache-buster and the fine-grained-invalidation key any caching layer relies on. They
  stay co-located with the stage that owns them (the author is the only party who knows the
  right value); moving them out of the library would force external edits on every
  algorithm change.

### Determinism invariant (the reuse contract)

Content-addressed reuse in any caching layer silently assumes each stage's output is a
**deterministic function of `(config-slice, upstream output hashes, dataset hashes,
version)`** and nothing else — no unseeded RNG, wall-clock timestamps, environment reads,
or unordered output. A violation is a silent parity bug, not a crash. A stage that cannot
honour it sets `cacheable=False` (always recompute); downstream still stays
content-addressed on that stage's *actual* output hash.

### Execution-model assumptions (A–F)

The stage contract is designed so a **generic "cached DAG-of-file-stages runner"** can
drive `bikescore-bna` as plugin #1. Such a runner makes **zero domain assumptions** (nothing
about bikes / OSM / scoring) but a small fixed set of execution-model assumptions. Any
compute library that ships `list[StageSpec]` + a config factory + (optionally) an
`InputProvider` is orchestratable the same way, whatever its domain.

| | Assumption | What it means |
|---|---|---|
| **A** | File I/O through directories | Stages exchange state only as files: read upstream from `input_paths` dirs, write all outputs into `output_dir`. No in-memory handoff, shared DB, or hidden globals — content-addressed reuse hashes *files*. |
| **B** | Determinism | Output is a deterministic function of inputs + config-slice + version (the invariant above). |
| **C** | Static, declared DAG | `depends_on` is fixed metadata resolved *before* execution — no data-dependent graph shape. |
| **D** | Named external inputs | `dataset_inputs` names files the orchestrator supplies; names are opaque to it, but *something* (an `InputProvider`) must produce them. |
| **E** | Hashable, opaque config | A caching layer hashes config per stage and passes it to `run`; a `slice_config(cfg, stage)` hook is an optimization for fine-grained invalidation (whole-config hashing is coarser but still correct). |
| **F** | Author-declared version | `version` is an opaque cache-buster the library author bumps on behavioural change. |

**Foreign libraries (the adapter pattern).** A–F cannot be *imposed* on a third-party
library; it is integrated by writing an **adapter** that implements `StageSpec` over its
native API. An adapter can always provide the *interface* assumptions (A, C, D, F) but can
only partially guarantee the *semantic* ones (B determinism, complete E). Hence
`cacheable`: execution needs only A + C + D + a run callable (always suppliable); reuse
additionally needs B + complete-E + F. **Safe default for anything uncertain:
`cacheable=False` — correctness over reuse.** You lose reuse *at that node*, never the
ability to orchestrate.

## Rule & config analysis — `bikescore_bna.decision.analysis`

Static analysis over the decision DSL (`Decision` objects + field catalogs) — no
orchestration, database, or run store. It powers `BNAConfig.validate()` and the
orchestration app's `bikescore-bna rules` commands and rule-builder endpoints.

```python
from bikescore_bna.decision.analysis import (
    validate_decision,   # static checks: unknown-field, op-type, enum-domain, duplicate-id
    find_unreachable,    # rules shadowed by an earlier, broader rule
    check_exhaustive,    # passes that can fall through with no default
    coverage,            # winning-rule tallies + never-fired detection over a frame
    trace,               # per-row clause/rule/pass explanation
    unique_contexts,     # exact, finite threshold-partitioned decision contexts
    simulate,            # before/after diff of two rule versions over those contexts
    cross_engine_diff,   # equivalence harness vs a reference decider
)
```

Resolve-time producer/consumer and `$var:` validation — what `BNAConfig.validate()`
calls to reject a config at save/resolve:

```python
from bikescore_bna.decision.analysis.producer_consumer import unproduced_references
from bikescore_bna.decision.analysis.variables import (
    undeclared_variables, null_variables, orphan_overrides,
)
```

- **`unproduced_references(config)`** — columns an active rule/stage references that no
  active attribute, ruleset output, base OSM tag, or stage produces.
- **`undeclared_variables(config)`** — referenced `$var:` names neither declared in the
  scenario's `variables` nor supplied by a stage's fixed `_RULE_VARIABLES` vocabulary
  (discovered by walking `PIPELINE`).
- **`null_variables`** / **`orphan_overrides`** — advisory: declared-but-null variables,
  and city overrides no active rule references.
