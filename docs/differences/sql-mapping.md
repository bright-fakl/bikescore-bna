# Stage-by-stage SQL mapping

This page traces each `bikescore-bna` pipeline stage back to the
brokenspoke-analyzer SQL scripts (and osmium/osm2pgrouting steps) it replaces.
It is a reference for readers porting knowledge between the two implementations,
or verifying that nothing in the reference was dropped.

The mappings below are **architectural** — the Python stage computes the same
result as the SQL it replaces. Where the *output* deliberately differs, the row
links to the relevant entry on the [Intentional deviations](deviations.md) page.

For what each Python stage actually does, follow the "how it works" link in each
section heading.

---

## acquire & parse — [OSM parsing](../how-it-works/osm-parsing.md)

brokenspoke prepares OSM data in four steps before any feature computation:
`osmium extract` (polygon clip) → `osmconvert` (bounding-box clip) → `osm2pgsql` /
`osm2pgrouting` (import + topology) → `clip_osm.sql` (service-buffer clip).
`bikescore-bna` collapses this into an acquire-time pre-clip and a single osmium
parse pass.

| brokenspoke step | bikescore-bna equivalent |
|---|---|
| `runner.run_osmium_extract` (polygon clip, `complete_ways`) | `acquire.py: pre_clip_pbf()` |
| `runner.run_osm_convert` (census-block bbox clip) | *(absent — no bbox step; see [§3a](deviations.md#3a-boundary-polygon-clip-vs-bounding-box-truncation))* |
| `osm2pgsql` import | `stages/parse.py` (single `osmium.SimpleHandler` pass) |
| `prepare_tables.sql` (columns, cycleway merge) | `stages/parse.py: _ParseHandler` |
| `clip_osm.sql` (service-buffer clip) | *(removed — no-op given the osmium pre-clip; the `bicycle=no AND highway=path` filter moved into parse)* |

See [Clipping approaches](deviations.md#clipping-approaches) for the full
treatment of how the two build the study-area dataset and buffer zone.

---

## attributes — [Road classification](../how-it-works/road-features.md)

brokenspoke computes road attributes as a sequence of in-database SQL updates
inside `compute.attributes()`, where later scripts read columns written by
earlier ones. `bikescore-bna` applies all of them in the single `attributes`
stage, driven by decision tables in `data/attributes/standard-bna.yaml`.

### Classification

| brokenspoke SQL file | bikescore-bna equivalent |
|---|---|
| `prepare_tables.sql` | `stages/parse.py` (column naming during parse) |
| `features/one_way.sql` | `one_way_car` attribute |
| `features/functional_class.sql` | `functional_class` attribute, observed pass |
| `features/paths.sql` | `functional_class` attribute + `footway_wide`/`is_golf_path` flags |
| `features/bike_infra.sql` | `bike_infra` attribute |
| `features/park.sql` | `parking` attribute |
| `features/class_adjustments.sql` | `functional_class` attribute, `class_promotion` pass |
| `features/legs.sql` | *(deferred to stress stage — requires topology)* |
| `features/signalized.sql` | intersection attributes (`intersection_attributes.py`, applied by parse) |
| `features/stops.sql` | intersection attributes (applied by parse) |
| `features/rrfb.sql` | intersection attributes (applied by parse) |
| `features/island.sql` | intersection attributes (applied by parse) |

Two SQL bugs are fixed in this stage:
[§1a Parking tag overwrite](deviations.md#1a-parking-tag-overwrite) and
[§1b Opposite-direction track dead code](deviations.md#1b-opposite-direction-bike-track-dead-code).

### Imputation (attribute fallbacks)

| brokenspoke SQL file | bikescore-bna equivalent |
|---|---|
| `features/speed_limit.sql` | `speed_parsed` attribute — fallback passes |
| `features/lanes.sql` | `lanes_ft` / `lanes_tf` attributes — fallback passes |
| `features/width_ft.sql` | `width_parsed` attribute |
| `speed_tables.sql` | `data/city_fips_speed.csv` + `data/state_fips_speed.csv` |

In brokenspoke, imputation runs inside the same `attributes()` call as
classification, before topology splitting. In `bikescore-bna` it is the
`fallback` passes at the tail of the `attributes` stage, after the observed phase
and `functional_class` promotion — so the effect is identical.

---

## stress — [Level of Traffic Stress](../how-it-works/stress.md)

brokenspoke computes stress as a sequence of SQL `UPDATE` statements in
`compute.stress()`, most taking speed/lane defaults via `psql -v` parameters.
`bikescore-bna` replaces the sequence with a rules engine (`stages/stress.py`)
driven by `rules/data/segment_stress.yaml` and `intersection_stress.yaml`.

| brokenspoke SQL file | What it does |
|---|---|
| `stress/stress_motorway-trunk.sql` | Motorways and trunks — always high stress |
| `stress/stress_segments_higher_order.sql` | Primary/secondary/tertiary segment stress |
| `stress/stress_segments_lower_order.sql` | Residential, unclassified segment stress |
| `stress/stress_segments_lower_order_res.sql` | Residential segment stress (low-speed variant) |
| `stress/stress_living_street.sql` | Living streets — always low stress |
| `stress/stress_path.sql` | Off-street paths — always low stress |
| `stress/stress_track.sql` | Tracks (grade1) — always low stress |
| `stress/stress_one_way_reset.sql` | Reset stress for one-way roads without reverse infrastructure |
| `stress/stress_motorway-trunk_ints.sql` | Intersection stress for motorway/trunk crossings |
| `stress/stress_primary_ints.sql` | Intersection stress for primary crossings |
| `stress/stress_secondary_ints.sql` | Intersection stress for secondary crossings |
| `stress/stress_tertiary_ints.sql` | Intersection stress for tertiary crossings |
| `stress/stress_lesser_ints.sql` | Intersection stress for lower-order crossings |
| `stress/stress_link_ints.sql` | Reset `_link` roads to low intersection stress |

A structural difference: brokenspoke embeds speed/lane defaults as SQL
substitution parameters, whereas in `bikescore-bna` imputation has already filled
those values before stress runs, so the rules operate on concrete columns.
**No known deviations** in this stage.

---

## segment — [Segmenting the road network](../how-it-works/segmenting.md)

brokenspoke splits ways into routable segments with osm2pgrouting;
`bikescore-bna` does the topology split (and trail extraction) in pure Python.

| brokenspoke tool / file | What it does | bikescore-bna equivalent |
|---|---|---|
| `osm2pgrouting` | Split ways at intersections; build `neighborhood_ways_net_link` / `_vert` | `stages/segment.py` (pure-Python topology split; trail extraction) |

[§2a Topology-ordering orphan roads](deviations.md#2a-topology-ordering-orphan-roads)
arises here.

---

## graph — [Routing network](../how-it-works/routing-network.md)

brokenspoke builds the routing graph with two SQL scripts; `bikescore-bna` builds
SciPy sparse matrices and the block↔road links.

| brokenspoke SQL file | What it does | bikescore-bna equivalent |
|---|---|---|
| `connectivity/census_blocks.sql` | Associate road segments with census blocks (15 m buffer) | `stages/graph.py` (block↔road links) |
| `connectivity/build_network.sql` | Create the pgRouting network table | `stages/graph.py` (high/low-stress CSR matrices) |

[§3a Boundary polygon clip vs. bbox truncation](deviations.md#3a-boundary-polygon-clip-vs-bounding-box-truncation)
arises here.

---

## connectivity — [Connectivity](../how-it-works/connectivity.md)

brokenspoke computes block-to-block connectivity with pgRouting plus a chain of
SQL scripts. `bikescore-bna` replaces the whole chain with one vectorised SciPy
Dijkstra traversal per source block.

| brokenspoke SQL file | What it does |
|---|---|
| `connectivity/census_blocks.sql` | Associate roads with census blocks |
| `connectivity/reachable_roads_high_stress_prep.sql` | Prepare high-stress reachability run |
| `connectivity/reachable_roads_high_stress_calc.sql` | `pgr_drivingDistance` (high stress) |
| `connectivity/reachable_roads_high_stress_cleanup.sql` | Post-process high-stress results |
| `connectivity/reachable_roads_low_stress_prep.sql` | Prepare low-stress reachability run |
| `connectivity/reachable_roads_low_stress_calc.sql` | `pgr_drivingDistance` (low stress) |
| `connectivity/reachable_roads_low_stress_cleanup.sql` | Post-process low-stress results |
| `connectivity/connected_census_blocks.sql` | Join reachable roads into block pairs; apply the 1.25× ratio and adjacent-block flags |

All replaced by `stages/connectivity.py`. This is an architectural difference
motivated by performance — brokenspoke's `connected_census_blocks.sql` runs
correlated subqueries per candidate block pair, whereas `bikescore-bna` runs one
graph traversal per source block. **No known deviations** in this stage.

---

## destinations — [Destinations](../how-it-works/destinations.md)

brokenspoke locates destinations with one SQL script per type, each using
`ST_DWithin` to restrict to the service area and `ST_ClusterDBSCAN` (or a custom
approach for point types) to cluster. `bikescore-bna` collects all POIs in the
parse pass and clusters them with `scipy.cluster.hierarchy` in
`stages/destinations.py`; the `OsmMatcher` conditions encode the same tag logic.

| brokenspoke SQL file | Destination type |
|---|---|
| `destinations/colleges.sql` | Technical/vocational colleges |
| `destinations/community_centers.sql` | Community centers |
| `destinations/dentists.sql` | Dentists |
| `destinations/doctors.sql` | Doctors / clinics |
| `destinations/hospitals.sql` | Hospitals |
| `destinations/parks.sql` | Parks |
| `destinations/pharmacies.sql` | Pharmacies |
| `destinations/retail.sql` | Retail |
| `destinations/schools.sql` | K-12 schools |
| `destinations/social_services.sql` | Social services |
| `destinations/supermarkets.sql` | Grocery stores |
| `destinations/transit.sql` | Transit stops |
| `destinations/universities.sql` | Universities |

Two deviations affect destination counts:
[§5 Clipping differences](deviations.md#clipping-approaches) (transit stops) and
[§6a Retail cluster floating-point sensitivity](deviations.md#6a-floating-point-sensitivity-at-cluster-threshold).

---

## scores — [Access scoring](../how-it-works/scoring.md)

brokenspoke computes per-block access scores through one SQL script per
destination category plus an overall combiner. `bikescore-bna` reimplements all
of them as a single vectorised pandas GROUP BY over the connectivity DataFrame in
`stages/scores.py`.

| brokenspoke SQL file | What it does |
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

Architectural difference, equivalent results. **No known deviations** in this stage.

---

## neighborhood — [Neighborhood scores](../how-it-works/neighborhood-scores.md)

brokenspoke computes city-level aggregates with four SQL scripts; `bikescore-bna`
implements the same aggregation in `stages/neighborhood.py`. The score-inputs
table structure (132 rows, `use_*` flag columns) matches the brokenspoke schema
exactly.

| brokenspoke SQL file | What it does |
|---|---|
| `connectivity/category_scores.sql` | Population-weighted category and overall scores |
| `connectivity/score_inputs.sql` | 132-row score-inputs table (percentiles, averages) |
| `connectivity/overall_scores.sql` | City-level headline scores, mileage statistics |
| `features/calculate_mileage.sql` | Total miles of each bike infrastructure type |

**No known deviations** in this stage — all column values match the reference to
four decimal places on the Washington, DC validation city.
