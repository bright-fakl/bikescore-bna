# Configuration

Every run is driven by one `BNAConfig` — a plain dataclass tree the
[stages](output-files.md) read. You never mutate it in place; you *build* one with
[`build_config`](#build_config), optionally layering a scenario and/or scalar overrides
over the package defaults.

```python
from bikescore import build_config

config = build_config("default")                       # bundled scenario
config = build_config("default", {"graph.low_stress_threshold": 2})   # + overrides
config = build_config(Path("my-scenario.yaml"))        # a scenario file
```

## `build_config`

```python
build_config(scenario: str | dict | Path | None = None,
             overrides: dict | None = None) -> BNAConfig
```

| `scenario` | resolves to |
|---|---|
| `None` | `BNAConfig.with_defaults()` — the compiled-in defaults, nothing layered |
| `"default"` / a name | a **bundled** scenario under `bikescore/scenarios/data/` (see [`list_bundled_scenarios`](api.md)) |
| a `Path` to a `.yaml` | a caller-supplied scenario document on disk |
| a `dict` | an in-memory scenario document |

`overrides` is a flat `{"section.field": value}` map applied **last**, over whatever the
scenario produced — the same key space the CLI's `--set` uses (e.g.
`--set imputation.city_default_speed=40`). Overrides are scalar-only; structural changes
(rules, catalogs, attributes) belong in a scenario.

!!! info "Scenarios, not a stack"
    A **complete** scenario is a self-contained snapshot — all config options *plus* the
    rule sets, attributes, and destination catalogs — and is the structural source of
    truth the resolver block-replaces from. A **sparse** scenario is just a dict of
    config-namespace deltas read over the defaults. There is no scenario *stack*: to
    customise, derive a new scenario from a base (copy + edit) rather than layering. The
    bundled `default` scenario is complete and is what new cities are seeded from.

## The config tree

`BNAConfig` holds a handful of top-level scalars plus eight typed sub-configs. Each stage
reads only its own slice.

| Sub-config | Type | Feeds | Selected fields |
|---|---|---|---|
| `city` | `CityIdentityConfig` | attributes, speed limits | `default_speed`, `state_default_speed`, `country` |
| `imputation` | `ImputationConfig` | attributes | `bare_speed_unit`, `default_facility_width_ft` |
| `stress` | `StressConfig` | stress | `segment_rules`, `intersection_rules`, `crossing_speed_defaults`, `level_names` |
| `graph` | `GraphConfig` | graph | `low_stress_threshold`, `extra_thresholds`, `link_stress_model` |
| `connectivity` | `ConnectivityConfig` | connectivity | `include_self_pairs`, `low_stress_ratio`, `use_turn_restrictions` |
| `scoring` | `ScoringConfig` | scores | `people`, `category_weights`, `population` |
| `export` | `ExportConfig` | export | `base_dir` |
| `cache` | `CacheConfig` | parse | `cache_dir` — where the clipped-PBF cache lives |

Top-level scalars include `max_trip_distance` (2680 m — the reachability horizon),
`block_road_buffer` / `block_road_min_length` / `block_boundary_overlap` (block↔road
association), `exclude_water_blocks`, and the trail thresholds `min_path_length` /
`min_bbox_length`.

Three fields hold the **structural** layers a complete scenario supplies:

| Field | Holds | Reference |
|---|---|---|
| `attributes` | the road-attribute registry (functional class, speed/lane/width defaults, bike-infra, derived flags) | [Extensibility](extensibility.md) |
| `destinations` | the destination catalog (the 13 standard types + any custom) | [Destination catalogs](destinations.md) |
| `intersection_attributes` | the node-attribute matchers (`signalized`, `stop`, `rrfb`, `island`) | [Extensibility](extensibility.md) |

## User-defined variables

A scenario may declare `variables` — named values the rule sets reference by `$var:name`
— plus `required_variables`, names a run *must* supply. This lets a ruleset stay generic
while a scenario (or `--set variables.x=…`) pins the numbers. A missing required variable
fails `config.validate()` before any stage runs.

## Validation

`config.validate()` raises `ConfigValidationError` on an inconsistent config — an unknown
scoring-category weight, a rule referencing an undeclared variable, or a stage producing a
column no downstream consumer's schema expects. `build_config` validates for you; call it
directly only when assembling a config by hand.
