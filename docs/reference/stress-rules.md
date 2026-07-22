# Stress rules

Level of Traffic Stress (LTS) is assigned by two **decision tables** — data, not code.
This page is the reference for their structure and the terse authoring form; for the
*model* (what LTS 1–4 mean, how segment and intersection stress combine) see
[How stress works](../how-it-works/stress.md), and for a worked edit see the
[Edit stress rules](../tutorial/adjust-stress-yaml.md) tutorial.

The two built-in rule sets ship under `bikescore-bna/rules/data/` and are carried into every
complete scenario:

| Ruleset | File | Produces | Config field |
|---|---|---|---|
| Segment stress | `segment_stress.yaml` | `ft_seg_stress`, `tf_seg_stress` | `config.stress.segment_rules` |
| Intersection stress | `intersection_stress.yaml` | `ft_int_stress`, `tf_int_stress` | `config.stress.intersection_rules` |

Both run inside the `stress` stage over the segment table (columns like `adj_fc`,
`speed_limit`, `ft_lanes`, `ft_bike_infra`, plus the node attributes). The final
per-segment LTS is the max of its segment and intersection stress in each direction.

## Anatomy of a ruleset

```yaml
name: segment_stress
sets:                     # named value lists reused across rules
  primary_fcs: [primary, primary_link]
passes:
- name: seg_stress
  for:                    # fan a whole pass over directions — <dir> expands to ft, tf
    dir: [ft, tf]
  rules:
  - id: motorway_trunk
    when: { adj_fc: $motorway_trunk_fcs }   # $name references a set
    set:  { <dir>_seg_stress: 3 }
  - id: residential
    when: { adj_fc: residential }
    rules:                # nested rules: only evaluated when the parent `when` holds
    - id: residential_low
      when: { speed_limit: { le: 25 } }     # operators: le, ge, lt, gt, in, ne …
      set:  { <dir>_seg_stress: 1 }
    - id: residential_high                   # no `when` → the fall-through default
      set:  { <dir>_seg_stress: 3 }
```

Key constructs:

- **First match wins.** Within a pass (or a nested rule block), rules are tried top to
  bottom; the first whose `when` matches sets the value. A trailing rule with no `when`
  (or a pass-level `default:`) is the fall-through.
- **`sets`** are named value lists; a condition references one with `$name` instead of
  inlining the list, so the same list is authored once.
- **`for:` fan-out** repeats a pass or rule block over each value of a loop variable,
  substituting `<var>` in ids, conditions, and `set` targets. `dir: [ft, tf]` is how one
  authored rule covers both travel directions; nested `for: { fcg: [...] }` covers
  functional-class groups (see `segment_stress.yaml`).
- **Nested rules** under a rule are scope-gated: they are only reached when the parent's
  `when` holds, which keeps deep tables compact.
- **`predicates`** (intersection ruleset) are named condition bundles referenced by
  `use: name`, e.g. `unprotected_ft`.
- **`after:`** on a pass orders it explicitly after another pass — e.g.
  `int_stress_link_reset` runs after `int_stress` to reset link roads to LTS 1.

## Inspecting and overriding

The rulesets are just fields on the config. A complete scenario carries its own copies, so
the way to change stress assignment is to **edit the ruleset inside a scenario** (or derive
a new scenario from `default`) — see the [tutorial](../tutorial/adjust-stress-yaml.md) and
[Customize stress](../tutorial/customize-stress.md). Rule authoring uses the shared
decision DSL described in [Extensibility](extensibility.md); the same terse form drives the
road-attribute and destination-matcher rules.
