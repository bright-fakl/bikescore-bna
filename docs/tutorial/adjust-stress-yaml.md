# Edit the stress rules

LTS assignment is **data, not code**: a decision table evaluated by the rule engine.
The segment table lives in `bikescore/rules/data/segment_stress.yaml` (and the
intersection table alongside it). To change the logic, overlay it from a scenario.

## Anatomy of a rule set

```yaml
name: segment_stress
sets:
  motorway_trunk_fcs: [motorway, trunk, motorway_link, trunk_link]
passes:
  - name: seg_stress
    for:
      dir: [ft, tf]          # evaluated once per direction
    rules:
      - id: motorway_trunk
        when:
          adj_fc: $motorway_trunk_fcs   # condition on a named set
        set:
          <dir>_seg_stress: 3            # <dir> expands to ft / tf
```

Rules are tried in order; the first match sets the output column. `$name` references a
named `set`; `<dir>` expands to the current direction. See
[Stress rules](../reference/stress-rules.md) for the full construct reference.

## Overlay a rule change in a scenario

A scenario's `rule_sets` block replaces the matching rule set. For example, to treat a
new bike-infrastructure tag as low-stress, add a rule ahead of the default:

```yaml
type: sparse
version: 1
description: Treat protected two-way tracks as low-stress.
rule_sets:
  segment_stress:
    # … your amended passes/rules here …
```

```console
$ bikescore-score score ./aspen-colorado --scenario my-stress.yaml
```

Because `brokenspoke-analyzer` is the parity ground truth, validate any rule change
against a known city before relying on it — the default rule set is tuned to match the
reference exactly (see [Validation & parity](../development/validation.md)).
