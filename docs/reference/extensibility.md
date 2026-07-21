# Extensibility

Almost everything domain-specific in bikescore is **data, not code**: how OSM tags become
road attributes, how attributes become stress, and which POIs count as destinations all
live as decision tables on the `BNAConfig`. They share one small evaluation engine — the
**decision DSL** — so learning it once covers stress rules, road attributes, intersection
attributes, and destination matchers alike.

```
OSM tags ──▶ road attributes ──▶ stress (LTS) ──▶ routing + scoring
            (attribute rules)    (stress rules)
POIs     ──▶ destination catalog ─────────────────▶ access scoring
            (destination matchers)
```

## The decision DSL

A **decision** is a named set of ordered **passes**; each pass is a first-match-wins list
of **rules**. A rule has a `when` condition (a map of `field → matcher`) and a `set` of
output columns. The engine evaluates a whole DataFrame column-wise, so a table of rules
applies to every road (or node, or POI) at once.

Building blocks — all shared across stress, attributes, and destinations:

| Construct | Purpose |
|---|---|
| `sets` | named value lists referenced by `$name` |
| operators | `eq`, `ne`, `in`, `lt`, `le`, `gt`, `ge`, and set membership |
| `for:` fan-out | repeat a pass/rule over a loop variable (`<dir>` → `ft`/`tf`, etc.) |
| nested rules | scope-gated sub-rules reached only when the parent `when` holds |
| `predicates` / `use:` | named reusable condition bundles |
| `after:` | explicit inter-pass ordering |
| `$var:name` | reference a scenario-declared [config variable](config.md#user-defined-variables) |

Value references beyond literals: `FieldRef` (another column), `MapRef` (lookup table),
`LookupRef`, `TransformRef` (`$apply` a named transform), and `CascadeRef` (`$cascade` —
fall through to a prior value). See [Stress rules](stress-rules.md) for a worked table.

## Road attributes

The **attribute layer** turns raw OSM tags into the analysis columns stress depends on —
`functional_class`, speed/lane/width defaults, bike-infra type/width, and derived flags
(`one_way_car`, …). It is an ordered registry of attributes, each one of:

- **`DecisionAttribute`** — a decision table producing one or more columns, with an
  optional **fallback** pass that fills gaps *after* the observed values and class
  adjustments are in (e.g. an FC-keyed default speed when OSM has none). A `{col}_imputed`
  companion column records where a fallback fired.
- **`PrimaryAttribute`** — a thin wrapper mapping a single OSM tag straight through
  (declaring the extra tag to parse).
- **`CustomAttribute`** — an escape hatch for a Python-callable attribute.

Attributes declare their `output_columns`, `referenced_input_columns`, and
`referenced_variables`, which lets the resolver validate producer/consumer wiring before a
run.

## Intersection attributes

Node-level matchers assign the four intersection attributes — `signalized`, `stop`,
`rrfb`, `island` — from OSM node tags. They are mandated (the stress stage requires all
four) and locked in the builder so a rename can't silently drop one. Same matcher form as
everything else.

## Destinations

The [destination catalog](destinations.md) is the fourth consumer of the DSL: each type's
`node_match` / `area_match` / `way_match` / `exclude_match` are matchers over OSM features.

## Where extensions live

There is **no plugin import hook** in `bikescore` — extensions are carried as data inside a
*scenario*. To extend, derive a new scenario from `default` and edit its rulesets,
attribute registry, or destination catalog; `build_config(path)` loads it and
`config.validate()` checks it. (The separate **stage** plugin contract — adding a whole new
compute stage — is `StageSpec`; see the [Python API](api.md#the-stage-contract-stagespec-pipeline-run_stage).)
