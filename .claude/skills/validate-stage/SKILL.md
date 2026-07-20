---
name: validate-stage
description: Validate a bna-core stage output against brokenspoke-analyzer reference parquets
---

# Skill: validate-stage

Validate a bna-core stage output against the brokenspoke-analyzer SQL reference.

## Quick validation

```bash
uv run bna validate washington-dc --stage {stage}
```

## Full manual validation workflow

### Step 1: Export reference data from brokenspoke-analyzer (or use /export-reference skill)

```bash
cd ../brokenspoke-analyzer
./bna-city washington-dc start
uv run python tools/export_reference.py --city washington-dc --stage {stage} \
    --out ../bna-core/tests/reference/washington-dc/stages/
```

### Step 2: Run the Python stage

```python
import bna
pipeline = bna.Pipeline(city, config, cache_dir="./cache")
pipeline.run(only=["{stage}"])
```

### Step 3: Load and compare

```python
from bna.validation import validate_stage, Reference

ref = Reference("./tests/reference/washington-dc/")
report = validate_stage("{stage}", pipeline.get("{stage}"), ref)
report.print()
```

## Interpreting results

**`rows_differing == 0`**: stage passes. Proceed to next stage.

**`rows_differing > 0`**: investigate:
1. `report.column_diffs` — which columns differ?
2. `report.column_diffs[col].examples` — show 5 example mismatches
3. For stress rules: `ruleset.explain(row)` — which rule fired?
4. For numeric differences: check if it's a unit issue (mph vs km/h, feet vs metres)
5. Read the exact SQL CASE branch for that column

## What "passing" means per stage

| Stage | Pass condition |
|---|---|
| parse | same osm_id set, same highway values, length within 0.1m |
| impute | all speed/lane/width non-NULL, values match SQL defaults |
| classify | exact match on functional_class, bike_infra, one_way, park |
| segment | same road_id set, same start/end node IDs |
| stress | exact match on ft/tf seg/int stress for every segment |
| graph | same vert count, same edge counts, same boundary_road_ids |
| connectivity | same pair count, same low_stress flags, costs within 1m |
| destinations | same cluster count per type, same block assignments |
| scores | exact match on all score columns (within 1e-6) |
| neighborhood | exact match on all 23 neighborhood_overall_scores rows |

## Escalation

If a stage passes on DC but you suspect edge cases:
- Run on a city with complex bike infrastructure (Portland OR)
- Run on a city with many motorways (Houston TX)
- Run on a non-US city (different default speeds)
