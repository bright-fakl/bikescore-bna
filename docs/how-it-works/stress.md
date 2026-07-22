# Level of Traffic Stress

Every road in the BNA network gets a **stress level** — a number that captures how comfortable it is to cycle on. Stress levels drive everything downstream: low-stress connectivity, scores, and ultimately the overall BNA grade for a city.

## What is LTS?

Level of Traffic Stress (LTS) is a framework developed by the Mineta Transportation Institute for rating how comfortable roads are to cycle on. It was designed to match how different types of cyclists actually behave:

- **LTS 1**: Comfortable for virtually everyone, including children. Protected lanes, very slow streets.
- **LTS 2**: Comfortable for most adults. Conventional bike lanes on moderate-speed streets.
- **LTS 3**: Tolerable for experienced cyclists only. Sharrows on busy roads, unprotected lanes at speed.
- **LTS 4**: Uncomfortable for almost everyone. High-speed arterials, no facilities.

### How BNA maps to LTS

BNA uses a **simplified binary** rather than the full four-level scale: road segments receive either **stress 1** (comfortable) or **stress 3** (uncomfortable). This two-value system is not a direct copy of the Mineta LTS levels — the mapping is:

- **BNA stress 1** ≈ **Mineta LTS 1 + LTS 2**: roads that most cyclists find acceptable, including protected tracks, conventional bike lanes at moderate speeds, and very quiet residential streets.
- **BNA stress 3** ≈ **Mineta LTS 3 + LTS 4**: roads that most cyclists avoid — high-speed streets without infrastructure, sharrows on busy roads, arterials.

BNA stress 2 is never produced by the default rules. It exists as a configurable intermediate level for researchers who want to distinguish between "very comfortable" (strict LTS 1 equivalent) and "moderately comfortable" (LTS 2 equivalent) within what BNA would otherwise treat as a single comfortable category. See the tutorial [Add a finer stress level](../tutorial/lts-network.md) for a worked example.

## Factors that determine stress

### Segment stress

Stress on a road segment depends on three things:

1. **Vehicle speed** — Higher speeds make cycling more dangerous and uncomfortable.
2. **Number of lanes** — More lanes to cross or share with traffic increases stress.
3. **Bicycle infrastructure** — The type of bike facility dramatically reduces stress.

The infrastructure hierarchy, from most to least protective:

| Infrastructure type | BNA name | Effect |
|---|---|---|
| Separated bike path | `track` | Always stress 1, regardless of speed |
| Buffered bike lane | `buffered_lane` | Stress 1 at ≤25 mph with single lane |
| Conventional bike lane | `lane` | Stress 1 at ≤20 mph with single lane (≥4 ft wide) |
| Sharrow / shared lane | `sharrow` | Stress 1 only at ≤15 mph with single lane |
| No infrastructure | — | Stress 1 only at very low speeds (quiet residential) |

**Example**: A protected lane on a 45 mph road is stress 1. The same road without infrastructure is stress 3.

### Road type defaults

The BNA doesn't need an explicit speed tag to assign stress. Each road type has a default speed used when the OSM `maxspeed` tag is missing:

| Road type | Default speed | Default lanes |
|---|---|---|
| Primary, secondary | 40 mph | 2 |
| Tertiary | 30 mph | 1 |
| Residential | State/city default (e.g. 20 mph in DC) | — |
| Unclassified | 25 mph | 1 |

### Class adjustments

Before stress is computed, the BNA **promotes** residential and unclassified roads to tertiary if they have:
- Bike infrastructure in both directions (lane/buffered_lane/track on both sides), or
- More than one lane in either direction, or
- An explicit speed tag ≥ 30 mph

This ensures that a residential road with a painted bike lane is evaluated with the more demanding tertiary rules, not the lenient residential speed-only rule.

## Intersection stress

Segment stress tells you how comfortable a road is to *ride on*. Intersection stress tells you how comfortable it is to *cross* a road.

`ft_int_stress` is the stress of crossing the intersection at the **end** of a road. `tf_int_stress` is the stress at the **start**. When a cyclist reaches an intersection, they may need to cross a busier road — that crossing contributes to overall route stress.

**Signalized crossings are always low stress** — a traffic signal gives cyclists a protected moment to cross regardless of the speed or number of lanes on the crossing road.

For unsignalized crossings, stress depends on the road being crossed:
- **Motorway or trunk**: always high stress (crossing is essentially impossible)
- **Primary, secondary, tertiary**: depends on speed, lanes, RRFB (Rectangular Rapid Flash Beacon), and median refuge islands

### Crossing stress thresholds

For a **two-way primary** road:
- Total lanes > 4 → high stress
- Total lanes = 4, speed > 30 mph → high stress (or ≥30 with no median island)
- Total lanes < 4, speed > 30 mph, no island → high stress

For a **one-way primary** road:
- Lanes > 2 → high stress
- Lanes ≤ 2, speed > 30 mph → high stress

An RRFB (flashing beacon) raises the threshold slightly, allowing more lanes at the same stress level.

## The configurable threshold

By default, the BNA uses `low_stress_threshold = 1`: only roads with stress=1 are included in the comfortable routing network. This is the standard BNA setting and covers the full range of infrastructure that Mineta LTS would classify as LTS 1 or LTS 2.

You can introduce a finer distinction by adding custom rules that produce stress=2 and then setting the threshold to 2:

```toml
[graph]
low_stress_threshold = 2
```

With custom stress=2 rules in place, this setting restricts the comfortable network to only the most protected infrastructure (strict Mineta LTS 1 equivalent), excluding painted bike lanes on faster streets that default rules would have classed as stress=1.

## Why only values 1 and 3?

The BNA SQL reference assigns only values 1 and 3 — value 2 is never produced by any default rule. This is intentional: BNA treats comfort as a binary distinction for most analyses. The intermediate value 2 is available for custom extensions when a finer split is needed.

When you add a custom rule that produces stress=2, `StressConfig.n_levels` automatically adjusts to accommodate the new value.

## Comparison with brokenspoke-analyzer

brokenspoke computes stress through a sequence of SQL UPDATE statements in
`compute.stress()`. Each file handles one road class or intersection type; most
take parameterised speed and lane defaults via `psql -v` variables:

| SQL file | What it does |
|---|---|
| `stress/stress_motorway-trunk.sql` | Motorways and trunks — always high stress |
| `stress/stress_segments_higher_order.sql` | Primary, secondary, tertiary segment stress (called 3×) |
| `stress/stress_segments_lower_order.sql` | Residential, unclassified segment stress |
| `stress/stress_segments_lower_order_res.sql` | Residential segment stress (low-speed variant) |
| `stress/stress_living_street.sql` | Living streets — always low stress |
| `stress/stress_path.sql` | Off-street paths — always low stress |
| `stress/stress_track.sql` | Tracks (grade1) — always low stress |
| `stress/stress_one_way_reset.sql` | Reset stress for one-way roads without reverse infrastructure |
| `stress/stress_motorway-trunk_ints.sql` | Intersection stress for motorway/trunk crossings |
| `stress/stress_primary_ints.sql` | Intersection stress for primary road crossings |
| `stress/stress_secondary_ints.sql` | Intersection stress for secondary road crossings |
| `stress/stress_tertiary_ints.sql` | Intersection stress for tertiary road crossings |
| `stress/stress_lesser_ints.sql` | Intersection stress for lower-order road crossings |
| `stress/stress_link_ints.sql` | Reset `_link` roads to low intersection stress |

bikescore-bna replaces this sequence with a rules engine (`stages/stress.py`) that
applies the same logic from YAML rule sets (`bikescore-bna/rules/data/segment_stress.yaml`
and `intersection_stress.yaml`). The default rules encode the same conditions as
the SQL files above, producing identical results on the Washington DC validation
city. There are no known deviations in the stress stage.

The key structural difference is that brokenspoke embeds speed/lane defaults
directly as SQL substitution parameters, while bikescore-bna's imputation stage
already fills these values before stress runs — so stress rules operate on
concrete column values rather than SQL-level defaults.
