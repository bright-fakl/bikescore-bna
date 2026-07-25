# Configuration

Every run is driven by one `BNAConfig` — a plain dataclass tree the
[stages](output-files.md) read. You never mutate it in place; you *build* one with
[`build_config`](#build_config), optionally layering a scenario and/or scalar overrides
over the package defaults.

```python
from bikescore_bna import build_config

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
| `"default"` / a name | a **bundled** scenario under `bikescore-bna/scenarios/data/` (see [`list_bundled_scenarios`](api.md)) |
| a `Path` to a `.yaml` | a caller-supplied scenario document on disk |
| a `dict` | an in-memory scenario document |

`overrides` is a flat `{"section.field": value}` map applied **last**, over whatever the
scenario produced — the same key space the CLI's `--set` uses (e.g.
`--set city.default_speed=40`). Overrides are scalar-only; structural changes
(rules, catalogs, attributes) belong in a scenario.

!!! info "Scenarios, not a stack"
    A **complete** scenario is a self-contained snapshot — all config options *plus* the
    rule sets, attributes, and destination catalogs — and is the structural source of
    truth the resolver block-replaces from. A **sparse** scenario is just a dict of
    config-namespace deltas read over the defaults. There is no scenario *stack*: to
    customise, derive a new scenario from a base (copy + edit) rather than layering. The
    bundled `default` scenario is complete and is what new cities are seeded from.

## The config tree

`BNAConfig` holds a handful of top-level scalars plus nine typed sub-configs. Each stage
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
| `boundary` | `BoundaryConfig` | acquire → parse, census, segment | `fill_holes`, `keep_largest_part`, `clip_shape`, `clip_size_m`, `convex_hull`, `override_geometry`, `network_buffer_m` (see [Boundary transforms](#boundary-transforms)) |

Top-level scalars include `max_trip_distance` (2680 m — the reachability horizon),
`block_road_buffer` / `block_road_min_length` / `block_boundary_overlap` (block↔road
association), `exclude_water_blocks`, the trail thresholds `min_path_length` /
`min_bbox_length`, and `extra_regions` (extra Geofabrik/state extracts to acquire when a
boundary transform expands the extent — see [Boundary transforms](#boundary-transforms)).

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

## Boundary transforms

The `boundary` sub-config reshapes the **analysis boundary** — the polygon `parse`,
`census`, and `segment` actually consume — before scoring. Transforms are applied once, at
[acquire](../how-it-works/data-acquisition.md#boundary-manipulation) time, by
`prepare_boundary`, in the fixed order `keep_largest_part → fill_holes → clip` (with
`make_valid` run unconditionally first to repair self-intersections). With no field set the
transform is identity and the analysis boundary is byte-for-byte the fetched one, so default
output — and [oracle parity](../development/validation.md) — is unchanged.

| Field | Type | Effect |
|---|---|---|
| `fill_holes` | `bool` | Rebuild each polygon from its exterior ring, absorbing interior enclave holes completely (predictable, not radius-dependent). |
| `keep_largest_part` | `bool` | Drop detached exclaves/islands, keeping only the largest polygon of a MultiPolygon boundary. |
| `clip_shape` | `"box" \| "circle" \| None` | Region-restrict to a shape centered on the boundary centroid, **intersected with** the real boundary (keeps realistic edges). Requires `clip_size_m`. |
| `clip_size_m` | `float \| None` | Box side length / circle diameter, in metres. Required when `clip_shape` is set; must be positive. |
| `convex_hull` | `bool` | Replace the boundary with its convex hull. **Extent-expanding.** |
| `override_geometry` | path \| bbox \| `None` | Replace the fetched city boundary at the **source** — a GeoJSON/vector path or an inline WGS84 bbox `[minx, miny, maxx, maxy]`. Not a transform: the transforms above run on top of it. **Extent-expanding.** |
| `network_buffer_m` | `float` | Buffer (m) applied to the analysis boundary when clipping the OSM road network **only** — never the scoring boundary. `0.0` = exact clip (oracle parity); `>0` is opt-in and **extent-expanding**. |

**Subsetting vs. expanding.** `fill_holes`, `keep_largest_part`, and the box/circle clip
either shrink the extent or fill interior holes, so they stay inside the already-downloaded
regional data — no re-acquire. The three expanding knobs (`convex_hull`,
`override_geometry`, a non-zero `network_buffer_m`) can push the analysis + clip extent past
the fetched region(s). When they do, acquire's coverage guard requires the missing regions
in the **top-level** `extra_regions` list (e.g. `extra_regions: ["maryland", "virginia"]`)
and otherwise raises an actionable error naming them — no surprise downloads. Preview the
plan with [`acquire --dry-run`](cli.md#acquire); the mechanics are covered under
[Data acquisition → Boundary manipulation](../how-it-works/data-acquisition.md#boundary-manipulation).

The original fetched boundary is always kept alongside the derived one for provenance, and
the census stage additionally emits an inert [excluded-block layer](output-files.md#targets)
so dropped water/out-of-boundary blocks are distinguishable from missing data.

## Validation

`config.validate()` raises `ConfigValidationError` on an inconsistent config — an unknown
scoring-category weight, a rule referencing an undeclared variable, or a stage producing a
column no downstream consumer's schema expects. `build_config` validates for you; call it
directly only when assembling a config by hand.
