# Neighborhood scores

The neighborhood stage aggregates per-block access scores into city-level summary statistics.
These are the numbers that appear in PeopleForBikes city rankings.

## The headline BNA score

The overall BNA score is a **population-weighted average** of every block's overall score,
restricted to blocks that have at least one reachable block (connected to the network):

```
overall_score = Σ(block.overall_score / 100 × block.pop20) / Σ(pop20 for reachable blocks)
```

**Why population-weighted?** Blocks with more residents represent more people's daily experience.
A 70-score block with 1,000 residents matters more than a 90-score block with 50 residents.

**Why exclude unconnected blocks?** Blocks with `reachable_blocks == 0` have no bicycle
connectivity at all — no roads connect them to any destination. Including them would penalise
cities for having a few isolated parcels at the fringe, which isn't a useful signal.

The score is on a 0–100 scale. Higher is better.

## Category and sub-category scores

The pipeline computes scores at three levels:

| Level | Examples |
|---|---|
| Sub-category | `opportunity_k12_education`, `core_services_grocery` |
| Category | `opportunity`, `core_services`, `recreation` |
| Overall | `overall_score` |

Each sub-category score is the **population-weighted average of per-block scores** for that
destination type, using only the population of blocks that can reach at least one such destination
by high-stress cycling (i.e. blocks where at least one destination is theoretically reachable):

```
sub_score = Σ(pop20 × dest_score) / Σ(pop20 for blocks where dest_high_stress > 0)
```

Category scores combine sub-category scores with fixed weights — but only for sub-categories
where destinations actually exist in the city. Missing destinations are dropped from the
denominator, so a city without hospitals still gets a valid `core_services` score:

| Category | Sub-categories and weights |
|---|---|
| Opportunity | Employment 35%, K12 schools 35%, Tech colleges 10%, Universities 20% |
| Core services | Doctors 20%, Dentists 10%, Hospitals 20%, Pharmacies 10%, Grocery 25%, Social services 15% |
| Recreation | Parks 40%, Trails 35%, Community centers 25% |

## What the score_inputs table contains

`neighborhood_score_inputs` has 132 rows, one per statistical measure across all destination
types. For each destination there are typically:

- **Percentile ratios** (median, 70th, 30th): distribution of the per-block low/high ratio
- **Average ratio**: city-wide `sum(low_stress) / sum(high_stress)` (integer division for destination counts)
- **Population-weighted average**: the sub-category score (has a `use_*` flag set to True)
- **Bike-shed statistics**: destination-level measures using `pop_low_stress / pop_high_stress`
  at each destination point (not available for trails)

Only the rows with `use_*` flags set feed into `neighborhood_overall_scores`.

## Mileage statistics

The mileage table reports total miles of bike infrastructure by type:

| Type | Description |
|---|---|
| `lane` | Dedicated bike lane |
| `buffered_lane` | Buffered bike lane |
| `track` | Protected cycle track |
| `sharrow` | Shared-lane marking |
| `path` | Off-street path (excludes crosswalks when xwalk data is available) |

Mileage measures infrastructure quantity — not quality or connectivity. A city can have many
miles of sharrows but a low overall score if those sharrows don't connect people to destinations.

`total_miles_low_stress` and `total_miles_high_stress` in `neighborhood_overall_scores` measure
the total road network miles that are low-stress or high-stress cycling, clipped to the city
boundary.

## Score normalization

`score_normalized = score_original × 100` for all sub-category and category scores.

Exceptions:
- `population_total`: no normalization (raw count)
- `total_miles_*`: rounded to 1 decimal place

## Comparison with brokenspoke-analyzer

brokenspoke computes neighborhood-level aggregates with:

| SQL file | What it does |
|---|---|
| `connectivity/category_scores.sql` | Population-weighted category and overall scores |
| `connectivity/score_inputs.sql` | 132-row score-inputs table (percentiles, averages) |
| `connectivity/overall_scores.sql` | City-level headline scores, mileage statistics |
| `features/calculate_mileage.sql` | Total miles of each bike infrastructure type |

bikescore-bna implements the same aggregation logic in `stages/neighborhood.py`.
The mileage computation mirrors `calculate_mileage.sql`. The score-inputs table
structure (132 rows, `use_*` flag columns) matches the brokenspoke schema
exactly.

There are no known deviations in the neighborhood stage. All column values
match the brokenspoke reference to four decimal places on the Washington DC
validation city.
