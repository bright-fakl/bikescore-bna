---
name: export-reference
description: Export intermediate stage outputs from brokenspoke-analyzer as validation reference parquets
---

# Skill: export-reference

Export intermediate stage outputs from brokenspoke-analyzer for use as
bna-core validation reference data.

## Usage

```bash
cd ../brokenspoke-analyzer
./bna-city washington-dc start

# Export a specific stage
uv run python tools/export_reference.py \
    --city washington-dc \
    --stage stress \
    --out ../bna-core/tests/reference/washington-dc/stages/

# Export all stages at once
uv run python tools/export_reference.py \
    --city washington-dc \
    --all \
    --out ../bna-core/tests/reference/washington-dc/stages/
```

## What gets exported

Each stage produces one parquet file with columns matching the bna-core schema.
The export script is at `../brokenspoke-analyzer/tools/export_reference.py`.
Reference parquet column names and available stages are documented in `spec/checklist.md` §14.

The script queries these PostGIS tables for each stage:

| Stage | Source table(s) |
|---|---|
| parse | `received.neighborhood_ways`, `received.neighborhood_osm_full_point` |
| impute | `received.neighborhood_ways` (after speed/lane SQL) |
| classify | `received.neighborhood_ways` (after features SQL) |
| segment | `received.neighborhood_ways_net_vert`, `received.neighborhood_ways_net_link` |
| stress | `received.neighborhood_ways` (ft/tf seg/int stress columns) |
| graph | derived from `received.neighborhood_ways_net_vert/link` |
| connectivity | `generated.neighborhood_connected_census_blocks` |
| destinations | `generated.neighborhood_{type}` (13 tables) |
| scores | `generated.neighborhood_census_blocks` (score columns) |
| neighborhood | `generated.neighborhood_overall_scores`, `generated.neighborhood_score_inputs` |

## Column name mapping

The export script translates PostGIS column names to bna-core column names where
they differ. The mapping is documented in `tools/export_reference.py`.

## Notes

- Reference data is NOT checked into git (too large for DC: ~500MB total)
- Store in `tests/reference/` which is gitignored
- Regenerate if brokenspoke-analyzer's SQL pipeline changes
- Always regenerate before validating a new stage implementation
- The export requires a fully-completed brokenspoke-analyzer run for the city
