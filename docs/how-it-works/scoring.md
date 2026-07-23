# Access scoring

The BNA score answers a single question for every census block: **how much of what
you can reach by bicycle can you reach comfortably?**

---

## The fundamental question

Bicycle access is scored in two layers:

- **High-stress access** — how many people / jobs / destinations can you reach by
  any bicycle route, even stressful ones along busy arterials?
- **Low-stress access** — how many of those can you reach via calm, comfortable
  routes (stress level 1 only)?

A block scores well when the low-stress share of what it can reach is high. A
neighbourhood where every school, park, and grocery store requires riding on a
five-lane highway will score poorly even if it has good cycling distance to many
destinations.

---

## Population and employment access

For each block, BNA counts:

- `pop_high_stress` — total population living in blocks reachable at all
- `pop_low_stress` — population reachable via low-stress routes only

The ratio `pop_low_stress / pop_high_stress` is then run through a **piecewise
linear formula** that rewards early gains more than later ones:

```
ratio = pop_low / pop_high

if ratio == 0:   score = 0
if ratio >= 1:   score = max_score (1.0)

Otherwise, interpolate linearly between:
  0         → 0
  step1=3%  → score1=0.1
  step2=20% → score2=0.4
  step3=50% → score3=0.8
  100%      → max_score=1.0
```

The shape is deliberately concave: going from 0% to 3% low-stress connectivity
(the first few comfortable routes appearing) is worth 10% of the maximum score.
Going from 50% to 100% only adds the remaining 20%. This reflects the diminishing
returns of additional comfortable routes once a neighbourhood already has broad
low-stress access.

Employment (`emp_score`) uses the same formula, with LODES WAC job counts replacing
population.

---

## Destination access

Destinations are counted as **clusters** (groups of nearby individual POIs), not
individual buildings. A block's destination score for a type is:

1. Count how many clusters of that type are reachable at all (`high_stress`).
2. Count how many clusters are reachable via low-stress routes (`low_stress`).
3. Apply a stepped formula based on the first few clusters mattering most:

```
if high == 0:     score = NULL  (type not present / reachable)
if low == 0:      score = 0
if high == low:   score = max_score (1.0)

Otherwise (example: first=0.7, second=0.2, third=0):
  low == 1  → 0.7
  low == 2  → 0.9
  low >= 3  → 0.9 + (0.1) × (low - 2) / (high - 2)   [linear to max]
```

The parameters (`first`, `second`, `third`) differ by destination type, reflecting
how much a single accessible hospital matters vs. a single accessible park.
Schools use `first=0.3, second=0.2, third=0.2` because the third school adds
meaningful value; hospitals use `first=0.7` because reaching even one hospital
comfortably is highly significant.

Standard destination parameters by type:

| Type | first | second | third |
|---|---|---|---|
| colleges | 0.7 | 0 | 0 |
| community_centers | 0.4 | 0.2 | 0.1 |
| dentists | 0.4 | 0.2 | 0.1 |
| doctors | 0.4 | 0.2 | 0.1 |
| hospitals | 0.7 | 0 | 0 |
| parks | 0.3 | 0.2 | 0.2 |
| pharmacies | 0.4 | 0.2 | 0.1 |
| retail | 0.4 | 0.2 | 0.1 |
| schools | 0.3 | 0.2 | 0.2 |
| social_services | 0.7 | 0 | 0 |
| supermarkets | 0.6 | 0.2 | 0 |
| transit | 0.6 | 0 | 0 |
| universities | 0.7 | 0 | 0 |

---

## Trail access

Trails are handled differently from destinations because the goal is to count
qualifying recreational paths, not POI clusters. The scoring uses **reverse
Dijkstra** rather than a forward search from each block:

1. Build the transposed (reversed) routing graphs `G_high.T` and `G_low.T`.
2. For each qualifying trail (path_length > 4800 m **and** bbox_length > 3300 m):
   - Run Dijkstra on the reversed low-stress graph from the trail's road vertices.
   - Any source block with a reachable vert within `max_trip_distance` can reach
     this trail via low-stress routes → increment `trails_low_stress`.
   - Repeat on the reversed high-stress graph → increment `trails_high_stress`.
3. Apply the destination-count formula with `first=0.7, second=0.2`.

The reverse approach is much faster than forward Dijkstra from each block when
there are fewer trails than blocks (typically 50–300 trails vs. thousands of blocks).

Trail scores use `NULL` when `trails_high_stress == 0` (no qualifying trail
reachable at all), `0` when trails are reachable but only via stressful routes,
and the piecewise formula otherwise.

---

## Category scores

Destination scores are grouped into five categories:

| Category | Weight | Members |
|---|---|---|
| People | 15 | population (pop_score) |
| Opportunity | 20 | employment, schools (35%), colleges (10%), universities (20%) |
| Core services | 20 | doctors (20%), dentists (10%), hospitals (20%), pharmacies (10%), supermarkets (25%), social_services (15%) |
| Retail | 15 | retail |
| Recreation | 15 | parks (40%), trails (35%), community_centers (25%) |
| Transit | 15 | transit |

The **category score** is a weighted average of its members, with a conditional
denominator: only members where at least one block has high-stress access in the
city contribute to the denominator. If a city has no hospitals at all, `hospitals`
is excluded from the core_services denominator — it cannot drag the score down.

---

## Overall block score

The per-block `overall_score` (0–100) combines all category contributions:

```
overall_score = 100 × (
    people_weight × pop_score
  + opportunity_weight × opportunity_contribution
  + core_services_weight × cs_contribution
  + retail_weight × retail_score  [if any retail reachable]
  + recreation_weight × rec_contribution
  + transit_weight × transit_score  [if any transit reachable]
) / (
    people_weight
  + opportunity_weight  [if any school/college/university reachable]
  + core_services_weight  [if any core service reachable]
  + retail_weight  [if any retail reachable]
  + recreation_weight  [if any park/trail/cc reachable]
  + transit_weight  [if any transit reachable]
)
```

The conditional denominator is the key design: categories with **zero** high-stress
destinations in the entire city are excluded from both numerator and denominator.
A rural city with no transit stops is not penalised for lacking transit — it is
scored on what it has.

Blocks with no connectivity at all (isolated road network) receive a score of `0.0`
(not NULL) because `people` always contributes to the denominator.

---

## Implementation notes

All scoring uses **vectorised pandas GROUP BY** over `connectivity_df`. The SQL
reference uses correlated subqueries (O(blocks × pairs) per destination type, which
takes minutes for large cities). The Python implementation is O(pairs) total — a
single pass through the connectivity table.

## Comparison with brokenspoke-analyzer

brokenspoke computes per-block scores through a series of SQL scripts in
`connectivity/`:

| SQL file | What it does |
|---|---|
| `access_population.sql` | Population low/high-stress counts and score |
| `access_jobs.sql` | Employment low/high-stress counts and score |
| `access_colleges.sql` | College access score |
| `access_community_centers.sql` | Community centre access score |
| `access_dentists.sql` | Dentist access score |
| `access_doctors.sql` | Doctor access score |
| `access_hospitals.sql` | Hospital access score |
| `access_parks.sql` | Park access score |
| `access_pharmacies.sql` | Pharmacy access score |
| `access_retail.sql` | Retail access score |
| `access_schools.sql` | School access score |
| `access_social_services.sql` | Social-services access score |
| `access_supermarkets.sql` | Supermarket access score |
| `access_trails.sql` | Trail access score (reverse Dijkstra in SQL) |
| `access_transit.sql` | Transit access score |
| `access_universities.sql` | University access score |
| `access_overall.sql` | Per-block overall score combining all categories |

bikescore-bna reimplements this SQL scoring logic in `stages/scores.py`, replacing
the per-destination correlated subqueries with a single vectorised pandas GROUP BY
over the connectivity DataFrame. This is an architectural difference that produces
equivalent results; there are no known deviations in the scores stage.
