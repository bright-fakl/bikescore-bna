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

At the end of the classify stage, bikescore applies one further step before finishing:
residential and unclassified roads that have bike infrastructure on both sides,
multiple travel lanes, or a speed limit ≥ 30 mph are promoted to 
functional class. This adjustment is stored permanently in  so
that the imputation and stress stages see the final, promoted class without any
special-casing.

## Implementation

Classification is implemented in `bikescore/stages/classification.py`. Functional class
rules are stored in `bikescore/rules/classification.py` as a `RuleSet` so they can be
customised (e.g., to change how service roads with bicycle access are classified
in a particular city). Bike infrastructure, parking, and one-way flags are computed
by built-in YAML `DecisionAttribute` objects registered via `load_builtin_attributes()`.

SQL equivalents in brokenspoke-analyzer:

- `features/functional_class.sql` — highway tag → functional_class
- `features/bike_infra.sql` — cycleway tags → ft/tf_bike_infra (built-in attribute: `bike_infra`)
- `features/park.sql` — parking tags → ft/tf_park (built-in attribute: `parking`)
- `features/one_way.sql` — oneway tag → one_way_car (built-in attribute: `one_way_car`)
- `features/class_adjustments.sql` — class promotions (residential/unclassified → tertiary, runs at end of classify)


---

## Built-in attributes and gate pipeline

bikescore ships 7 built-in `DecisionAttribute` objects (loaded from
`bikescore/data/attributes/standard-bna.yaml`) that bridge OSM tags to the columns
expected by classify, impute, stress, and segment stages.

### `BASE_WAY_TAGS`

`BASE_WAY_TAGS` is the minimal set of OSM tags parsed for every way unconditionally.
It includes `highway`, `oneway`, `access`, `bicycle`, `name`, and other core tags.

### `extra_osm_tags()`

`AttributeRegistry.extra_osm_tags()` returns the union of all `extra_tags` declared
across registered attributes. The parse stage calls this to determine which additional
OSM tags to extract beyond `BASE_WAY_TAGS`.

### Gate execution order

Attributes run in four gate passes, each after the named pipeline stage:

| Gate | Pipeline stage | Built-in attributes |
|---|---|---|
| `clip` | parse / clip | `one_way_car`, `speed_parsed`, `lanes_ft`, `lanes_tf` |
| `impute` | impute | *(none built-in)* |
| `classify` | classify | `bike_infra`, `bike_infra_width`, `parking` |
| `segment` | segment | *(none built-in)* |

### Built-in attribute columns

| Attribute | Gate | Output columns | Source OSM tags |
|---|---|---|---|
| `one_way_car` | clip | `one_way_car` | `oneway` (already in BASE_WAY_TAGS) |
| `speed_parsed` | clip | `speed_limit` | `maxspeed` |
| `lanes_ft` | clip | `ft_lanes` | `lanes`, `lanes:forward`, `turn:lanes`, `turn:lanes:forward` |
| `lanes_tf` | clip | `tf_lanes` | `lanes`, `lanes:backward`, `turn:lanes`, `turn:lanes:backward` |
| `bike_infra` | classify | `ft_bike_infra`, `tf_bike_infra`, `ft_bike_infra_width`, `tf_bike_infra_width` | `cycleway`, `cycleway:left/right/both`, etc. |
| `bike_infra_width` | classify | `ft_bike_infra_width`, `tf_bike_infra_width` | `cycleway:*:width` |
| `parking` | classify | `ft_park`, `tf_park` | `parking:lane:*`, `parking:*` |

The imputation stage (`impute.py`) runs its `fillna` / road-type-default logic
*after* clip-gate attributes, so `speed_limit`, `ft_lanes`, and `tf_lanes` already
contain OSM-derived values when imputation begins; impute only fills the remaining
nulls.

---

# Attribute Imputation

Before the stress stage runs, every road must have complete values for
`speed_limit`, `ft_lanes`, `tf_lanes`, and `width_ft`. OpenStreetMap is
community-maintained and many roads lack explicit tags for these attributes —
the imputation stage fills in the gaps.

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
| 2 | `city_default_speed` | Set in `ImputationConfig` for the city being analysed |
| 3 | `state_default_speed` | Set in `ImputationConfig` for the state/region |
| 4 | **Road-type rule** | Primary → 40 mph, residential → 25 mph, path → 15 mph |

For `width_ft`, only the OSM `width` tag is used — there are no road-type defaults
because the SQL reference does not apply them and the stress stage uses a fixed 5 ft
fallback for bike infrastructure width checks.

## Speed unit handling

OSM `maxspeed` values appear in several formats. The SQL reference (and bikescore)
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

Default speeds and lane counts can be overridden in `BNAConfig`:

```python
from bikescore.config import BNAConfig, ImputationConfig

config = BNAConfig()
config.imputation = ImputationConfig(
    city_default_speed=20,   # mph; overrides road-type rules for this city
    state_default_speed=30,  # mph; used if city default is absent
)
```

Road-type speed and lane rules live in `bikescore/rules/data/speed_imputation.yaml` and
`bikescore/rules/data/lane_imputation.yaml`. To override rules for a specific road type:

```python
from bikescore.rules import Condition, Rule
config.imputation.speed_rules.replace("speed_residential", Rule(
    name="speed_residential",
    priority=60,
    conditions=[Condition("functional_class", "residential"), Condition("speed_limit", None)],
    action={"speed_limit": 20.0},
    notes="custom residential speed override",
))
```

## Implementation

Imputation is implemented in `bikescore/stages/impute.py`. Speed and lane rules are
stored as `RuleSet` objects in `bikescore/rules/imputation.py` and serialised to YAML
in `bikescore/rules/data/`. The imputation stage runs after classification, so
`functional_class` is already set (and class-adjusted) when road-type speed and lane
defaults are applied.

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

| SQL file | bikescore equivalent |
|---|---|
| `prepare_tables.sql` | `stages/parse.py` (column naming during parse) |
| `features/one_way.sql` | `stages/classification.py: _compute_one_way()` |
| `features/functional_class.sql` | `functional_class` attribute, pass 1 (`data/attributes/standard-bna.yaml`) — Phase 35n |
| `features/paths.sql` | `stages/classification.py` (path/cycleway handling) |
| `features/bike_infra.sql` | `stages/classification.py: _compute_bike_infra()` |
| `features/park.sql` | `stages/classification.py: _compute_park()` |
| `features/class_adjustments.sql` | `functional_class` attribute, `class_promotion` pass (`data/attributes/standard-bna.yaml`) — Phase 35n |
| `features/legs.sql` | *(deferred to stress stage — requires topology)* |
| `features/signalized.sql` | `stages/classification.py: _propagate_intersection_attrs()` |
| `features/stops.sql` | `stages/classification.py: _propagate_intersection_attrs()` |
| `features/rrfb.sql` | `stages/classification.py: _propagate_intersection_attrs()` |
| `features/island.sql` | `stages/classification.py: _propagate_intersection_attrs()` |

Two SQL bugs are fixed in bikescore's classify output:

- **[§1a Parking tag overwrite](deviations.md#1a-parking-tag-overwrite)** —
  `park.sql` silently clears valid parking data for roads tagged with
  `parking:lane:both`. bikescore evaluates all three cases independently.
- **[§1b Opposite-direction track dead code](deviations.md#1b-opposite-direction-bike-track-dead-code)** —
  a copy-paste error in `bike_infra.sql` makes the `tf_bike_infra=track`
  assignment for `ft` one-way roads unreachable. bikescore corrects the
  condition.

### Imputation

| SQL file | bikescore equivalent |
|---|---|
| `features/speed_limit.sql` | `stages/impute.py: _impute_speed()` |
| `features/lanes.sql` | `stages/impute.py: _impute_lanes()` |
| `features/width_ft.sql` | `stages/impute.py: _impute_width()` |
| `speed_tables.sql` | `bikescore/data/city_fips_speed.csv` + `state_fips_speed.csv` |

In brokenspoke, speed and lane imputation runs as part of the same `attributes()`
call that does classification (before topology splitting). In bikescore, impute
is a separate pipeline stage that runs after classify and before segment, but
the effect is identical — `functional_class` is already set when road-type
defaults are applied.

