# Road Classification

Before computing stress or routing, every road segment must be understood in two
dimensions: what *type* of road it is, and what *cycling infrastructure* is
present. Classification translates raw OpenStreetMap tags into a standardised
vocabulary that all downstream stages can reason about.

## Functional class

Functional class captures the road's role in the network, from motorways through
to local paths.

| Value | Typical OSM `highway` tag | Network role |
|---|---|---|
| `motorway` | motorway, motorway_link | High-speed limited access highway |
| `trunk` | trunk, trunk_link | Major arterial |
| `primary` | primary, primary_link | City arterial |
| `secondary` | secondary, secondary_link | Collector street |
| `tertiary` | tertiary, tertiary_link | Local collector |
| `residential` | residential | Neighbourhood street |
| `living_street` | living_street, pedestrian+bicycle | Shared pedestrian zone |
| `unclassified` | unclassified, service+bicycle | Minor road with cycling access |
| `service` | *(dropped)* | Private access — excluded from the network |
| `track` | track (grade1 only) | Good-quality unpaved track |
| `path` | cycleway, path, footway+crossing | Shared-use path or crossing |

Roads with `access=private` or `access=no` (without explicit bicycle permission)
are excluded. A `track` is only kept if `tracktype=grade1` (hard surface); lower
grades are dropped.

The functional class determines the baseline stress of a road: motorways are
always high-stress; cycleways and shared paths are always low-stress; residential
and tertiary streets depend on speed, lanes, and bike infrastructure.

## Bike infrastructure

Bike infrastructure captures the cycling facilities present on a road. It is
derived from OSM `cycleway:*` tags and is **set independently for each direction
of travel** — a road can have a bike lane eastbound and nothing westbound.

| Value | Meaning |
|---|---|
| `track` | Physically separated cycle track (barrier, kerb, or elevation) |
| `buffered_lane` | Painted bike lane with an additional painted buffer zone |
| `lane` | Painted bike lane (no buffer) |
| `sharrow` | Shared-lane marking (painted bicycle + chevron in traffic lane) |
| `None` | No specific cycling facility — road is shared with traffic |

The values come directly from OSM `cycleway`, `cycleway:left`, `cycleway:right`,
and `cycleway:both` tags. A `cycleway=track` on a bidirectional road means there
is a protected track on both sides; `cycleway:right=lane` means a painted lane
only in the forward (right-hand) direction.

## Why classification is directional

Roads carry traffic — and cyclists — in two directions, and the cycling
infrastructure is often asymmetric. A one-way street frequently has a bike lane
running *against* traffic (tagged as `cycleway=opposite_lane`). A two-lane
arterial might have a protected track on one side and nothing on the other.

BNA captures this by assigning separate `ft_bike_infra` (forward/from-to
direction) and `tf_bike_infra` (backward/to-from direction) values. Stress is
then computed independently per direction.

For car one-way streets (`oneway=yes` or `oneway=-1`), cyclists can still travel
in both directions if bike-specific infrastructure or an exemption exists.
`oneway:bicycle=no` on a car one-way street keeps the reverse bike direction
open. The output `one_way` column reflects the *bicycle* one-way direction after
accounting for all these tags.

## Parking

Parking is recorded as `ft_park` and `tf_park`, each taking a value of `1`
(parking present), `0` (no parking), or `None` (not tagged). These are derived
from OSM `parking:lane:*` and `parking:*` tags and are used as inputs to the
segment stress rules.

## Class adjustments

At the end of the attributes stage, bikescore-bna applies one further step before finishing:
residential and unclassified roads that have bike infrastructure on both sides,
multiple travel lanes, or a speed limit ≥ 30 mph are promoted to `tertiary`
functional class. This adjustment is stored permanently in `functional_class` so
that the fallback (imputation) passes and the stress stage see the final, promoted class
without any special-casing.

## Implementation

Road attributes are computed by the **`attributes` stage** (`bikescore-bna/stages/attributes.py`).
`functional_class` — including the class-adjustment promotion — is itself a built-in
`DecisionAttribute` declared in `bikescore-bna/data/attributes/standard-bna.yaml`,
as are bike infrastructure, parking, and the one-way flags. The logic lives in the
scenario's attribute YAML, so it can be customised (e.g. to change how service roads with
bicycle access are classified) by editing the scenario rather than Python.

SQL equivalents in brokenspoke-analyzer:

- `features/functional_class.sql` — highway tag → functional_class
- `features/bike_infra.sql` — cycleway tags → ft/tf_bike_infra (built-in attribute: `bike_infra`)
- `features/park.sql` — parking tags → ft/tf_park (built-in attribute: `parking`)
- `features/one_way.sql` — oneway tag → one_way_car (built-in attribute: `one_way_car`)
- `features/class_adjustments.sql` — class promotions (residential/unclassified → tertiary, runs at end of the attributes stage)


---

## Built-in attributes and the Attributes stage

bikescore-bna ships a set of built-in attributes (declared in
`bikescore-bna/data/attributes/standard-bna.yaml`) that bridge OSM tags to the columns the
segment, stress, and scoring stages expect. They are **all applied by the single
`attributes` pipeline stage**.

### Categories

The built-ins fall into a few groups:

- **Primary OSM-tag attributes** (`footway`, `tracktype`, `golf`, `golf_cart`, `width`,
  `access`, `oneway`, `oneway:bicycle`) — each declares a raw `osm_tag` so the parse stage
  loads it as a column, keeping `BASE_WAY_TAGS` minimal.
- **Decision attributes** — `DecisionAttribute` objects that compute columns from those
  tags via the decision DSL: `one_way_car`, `speed_parsed`, `lanes_ft`, `lanes_tf`,
  `width_parsed`, `bike_infra`, `bike_infra_width`, `parking`.
- **`functional_class` and its derived flags** — `functional_class` plus the scratch flag
  attributes it reads (`access_ok`, `footway_wide`, `is_golf_path`, `cls_both_lane`,
  `cls_ft_multi`, `cls_tf_multi`, `cls_speed_high`). The flags carry `persist: false`:
  they are computed so `functional_class` can consume them, then dropped before the stage
  writes its output.

### `BASE_WAY_TAGS`

`BASE_WAY_TAGS` is the minimal, irreducible set of OSM tags the parse stage loads for
every way unconditionally: **`highway`, `bicycle`, `name`**. `highway`/`bicycle` drive
way filtering *before* any attribute runs; `name` is the reserved output-label column.
Everything else a rule or attribute consumes — including `access`, `oneway`, and
`oneway:bicycle` — is loaded through a primary attribute.

### `extra_osm_tags()`

`AttributeRegistry.extra_osm_tags()` returns the union of all `extra_tags` declared
across registered attributes. The parse stage calls this to determine which additional
OSM tags to extract beyond `BASE_WAY_TAGS`.

### Execution order — one global topological sort

The `attributes` stage applies every attribute as a single ordered list produced by
`AttributeRegistry.in_topo_order()`: a global
topological sort where each attribute names in its `after:` field the attributes that must
run before it (independent attributes are ordered by name for determinism). This lets a
later attribute read the columns earlier ones produced — e.g. `functional_class` runs last,
reading the `cls_*` flag columns.

Within the stage (`stages/attributes.py`) the sequence is:

1. **Observed phase** — every attribute's main `compute`, in topo order (`apply_attributes`).
2. Drop rows with NULL `functional_class` (no valid highway type — matches the SQL `DELETE`).
3. Drop the non-persisted scratch flag columns.
4. **Fallback passes** — imputation: each attribute's optional `fallback` Decision fills
   the remaining nulls, keyed on the now-promoted `functional_class`, adding `{col}_imputed`
   flags (`apply_attribute_fallbacks`).

### Selected attribute columns

| Attribute | Output columns | Source OSM tags |
|---|---|---|
| `one_way_car` | `one_way_car` | `oneway` (via the `oneway` primary attribute) |
| `speed_parsed` | `speed_limit` | `maxspeed` |
| `lanes_ft` | `ft_lanes` | `lanes`, `lanes:forward`, `turn:lanes`, `turn:lanes:forward` |
| `lanes_tf` | `tf_lanes` | `lanes`, `lanes:backward`, `turn:lanes`, `turn:lanes:backward` |
| `bike_infra` | `ft_bike_infra`, `tf_bike_infra`, `ft_bike_infra_width`, `tf_bike_infra_width` | `cycleway`, `cycleway:left/right/both`, etc. |
| `bike_infra_width` | `ft_bike_infra_width`, `tf_bike_infra_width` | `cycleway:*:width` |
| `parking` | `ft_park`, `tf_park` | `parking:lane:*`, `parking:*` |

The fallback passes fill nulls left by the observed phase, so `speed_limit`, `ft_lanes`,
and `tf_lanes` already carry OSM-derived values before imputation begins; the fallbacks
only fill the remaining gaps.

---

# Attribute Imputation

Before the stress stage runs, every road must have complete values for
`speed_limit`, `ft_lanes`, `tf_lanes`, and `width_ft`. OpenStreetMap is
community-maintained and many roads lack explicit tags for these attributes —
the **fallback passes** of the relevant attributes (run at the tail of the `attributes`
stage) fill in the gaps. "Imputation" is the name for this fallback behaviour.

## Why imputation is necessary

OSM data is rich but inconsistent. Residential streets in the US frequently have
no `maxspeed` tag; local paths rarely carry width measurements; lane counts are
absent on anything that isn't a major arterial. If the stress stage encountered
NULLs in these columns it would have to embed defaults inline for every rule,
making rules harder to read and configure. Imputation separates concerns: by the
time stress runs, all roads have concrete values.

## Resolution hierarchy

Attributes are resolved in priority order — the first non-NULL source wins:

| Priority | Source | Example |
|---|---|---|
| 1 | **OSM tag** | `maxspeed=25 mph`, `lanes=4`, `width=5 m` |
| 2 | `city.default_speed` | Config value for the city being analysed (residential roads) |
| 3 | `city.state_default_speed` | Config value for the state/region (residential roads) |
| 4 | **Per-functional-class default** | primary → 40, secondary → 40, tertiary → 30, unclassified → 25 mph |

Priorities 2–4 are the `fallback` passes of the `speed_parsed` / `lanes_ft` / `lanes_tf`
attributes; they run after the observed OSM values are read and after `functional_class`
is promoted, so the per-class defaults key on the final class.

For `width_ft`, only the OSM `width` tag is used — there are no road-type defaults
because the SQL reference does not apply them and the stress stage uses a fixed 5 ft
fallback for bike infrastructure width checks.

## Speed unit handling

OSM `maxspeed` values appear in several formats. The SQL reference (and bikescore-bna)
converts all values to **mph**:

| OSM value | Interpretation | Result |
|---|---|---|
| `"25 mph"` | Explicit mph | 25 mph (used directly) |
| `"50"` | Bare integer → implicit km/h | 50 ÷ 1.609, rounded to nearest 5 = 30 mph |
| `"40 kmph"` | Explicit km/h | 40 ÷ 1.609, rounded to nearest 5 = 25 mph |
| `"signals"` | Non-numeric | NULL (falls through to next priority level) |

Speeds rounded to the nearest 5 mph match the SQL reference exactly
(`ROUND(... / 1.609 / 5) * 5`).

## Configuring defaults

The residential city/state speed defaults live in the `city` config domain:

```python
from bikescore_bna.config import BNAConfig

config = BNAConfig.with_defaults()
config.city.default_speed = 20        # mph; residential fallback for this city
config.city.state_default_speed = 30  # mph; used if the city default is absent
```

`ImputationConfig` carries `bare_speed_unit` (the unit assumed for unitless OSM
`maxspeed` values) and `default_facility_width_ft` (the bike-infra width fallback).

The per-functional-class speed and lane defaults are the `fallback` passes of the
`speed_parsed` / `lanes_ft` / `lanes_tf` attributes in
`bikescore-bna/data/attributes/standard-bna.yaml`. To change them, edit those attributes'
fallback decisions in the scenario.

## Implementation

Imputation is the `fallback` passes of the `speed_parsed`,
`lanes_ft`, `lanes_tf`, and bike-infra-width attributes, applied by the `attributes`
stage (`bikescore-bna/stages/attributes.py`) via `apply_attribute_fallbacks` *after* the
observed phase and the `functional_class` promotion. So `functional_class` is already set
(and class-adjusted) when the per-class speed and lane defaults are applied.

SQL equivalents in brokenspoke-analyzer:

- `features/speed_limit.sql` — OSM maxspeed → speed_limit (mph)
- `features/lanes.sql` — lane count tags → ft_lanes, tf_lanes
- `features/width_ft.sql` — OSM width → width_ft (feet)

---

## Comparison with brokenspoke-analyzer

### Classification

brokenspoke computes road attributes as a series of in-database SQL updates
inside `compute.attributes()`. The execution order matters because later scripts
read columns written by earlier ones:

| SQL file | bikescore-bna equivalent |
|---|---|
| `prepare_tables.sql` | `stages/parse.py` (column naming during parse) |
| `features/one_way.sql` | `one_way_car` attribute (`data/attributes/standard-bna.yaml`) |
| `features/functional_class.sql` | `functional_class` attribute, observed pass (`data/attributes/standard-bna.yaml`) |
| `features/paths.sql` | `functional_class` attribute + its `footway_wide`/`is_golf_path` flags |
| `features/bike_infra.sql` | `bike_infra` attribute (`data/attributes/standard-bna.yaml`) |
| `features/park.sql` | `parking` attribute (`data/attributes/standard-bna.yaml`) |
| `features/class_adjustments.sql` | `functional_class` attribute, `class_promotion` pass (`data/attributes/standard-bna.yaml`) |
| `features/legs.sql` | *(deferred to stress stage — requires topology)* |
| `features/signalized.sql` | intersection attributes (`intersection_attributes.py`, applied by the parse stage) |
| `features/stops.sql` | intersection attributes (`intersection_attributes.py`, applied by the parse stage) |
| `features/rrfb.sql` | intersection attributes (`intersection_attributes.py`, applied by the parse stage) |
| `features/island.sql` | intersection attributes (`intersection_attributes.py`, applied by the parse stage) |

All of these are applied by the single `attributes` stage (`stages/attributes.py`), except
the intersection attributes, which the parse stage attaches. Two SQL bugs are fixed in
bikescore-bna's attributes output:

- **[§1a Parking tag overwrite](deviations.md#1a-parking-tag-overwrite)** —
  `park.sql` silently clears valid parking data for roads tagged with
  `parking:lane:both`. bikescore-bna evaluates all three cases independently.
- **[§1b Opposite-direction track dead code](deviations.md#1b-opposite-direction-bike-track-dead-code)** —
  a copy-paste error in `bike_infra.sql` makes the `tf_bike_infra=track`
  assignment for `ft` one-way roads unreachable. bikescore-bna corrects the
  condition.

### Imputation

| SQL file | bikescore-bna equivalent |
|---|---|
| `features/speed_limit.sql` | `speed_parsed` attribute — fallback passes (`data/attributes/standard-bna.yaml`) |
| `features/lanes.sql` | `lanes_ft` / `lanes_tf` attributes — fallback passes |
| `features/width_ft.sql` | `width_parsed` attribute |
| `speed_tables.sql` | `bikescore-bna/data/city_fips_speed.csv` + `state_fips_speed.csv` |

In brokenspoke, speed and lane imputation runs as part of the same `attributes()`
call that does classification (before topology splitting). In bikescore-bna the same work
happens as the `fallback` passes at the tail of the `attributes` stage (after the observed
phase and `functional_class` promotion), so the effect is identical — `functional_class`
is already set when the road-type defaults are applied.

