# Connectivity

At the heart of every BNA score is a single question: **can a cyclist get from
block A to block B within a reasonable trip distance, on a comfortable route?**

The connectivity stage answers that question for every pair of census blocks in
the city, producing the table that all downstream scoring draws on.

---

## What "reachable" means

The analysis models the city's road network as a directed graph — every
intersection is a node, every road segment is a directed edge. The edge weight
is the length of that segment in metres.

For each source block, the algorithm runs a shortest-path search (Dijkstra)
simultaneously from all of the block's road vertices, finding every other vertex
reachable within **2.68 km** of network travel. That distance corresponds to
roughly a 15-minute bicycle ride at a comfortable pace, and is the threshold the
PeopleForBikes methodology uses to define a "reasonable" trip.

Any census block that contains a reachable vertex becomes a connected target
block. The recorded cost is the minimum distance to any of that block's vertices.

---

## Two networks, one table

The search runs twice: once on the **high-stress network** (all roads) and once
on the **low-stress network** (comfortable roads only — those meeting a stress
threshold the analyst chooses, typically quiet residential streets and dedicated
paths).

Both costs end up in the same row of the connectivity table:

| Column | Meaning |
|---|---|
| `source_blockid20` | Census block the trip starts in |
| `target_blockid20` | Census block the trip ends in |
| `high_stress_cost` | Shortest route using any road, in metres |
| `low_stress_cost` | Shortest route using only comfortable roads, in metres |
| `high_stress` | Always True — every included pair is reachable by some route |
| `low_stress` | True if the comfortable route is not much longer (see below) |

A pair only appears in the table if it is reachable within 2.68 km on at least
one of the two networks.

---

## The low-stress flag and the 1.25× ratio rule

Having a low-stress path exist is not enough on its own. If the only comfortable
route requires a major detour — say, cycling 5 km to avoid a high-stress
intersection that a direct route crosses in 2 km — that is not really
"comfortable access" in any practical sense.

The analysis uses a **1.25× ratio rule**: a pair is flagged `low_stress = True`
if the comfortable route is at most 25% longer than the direct route:

```
low_stress_cost / high_stress_cost ≤ 1.25
```

A 10-minute ride that becomes a 12.5-minute ride on comfortable streets is
acceptable. A 10-minute ride that becomes a 20-minute detour is not.

Two additional cases are always flagged as low-stress:

- **Adjacent blocks** — blocks that share a road segment are neighbours by
  definition and are always considered low-stress regardless of routing.
- **Self-pairs** — every block can reach itself at zero cost (relevant for
  population and destination scoring).

---

## Why this produces millions of rows

Washington DC has approximately 5,800 census blocks with associated road
network. Each can be the source of a trip to hundreds of nearby blocks within
2.68 km. The full connectivity table for DC contains roughly **3.8 million
pairs** — one row for every (source, target) combination where at least one
network route exists within the distance limit.

For a city official reviewing scores, this table is an intermediate product:
it is never published directly but is joined against destination locations to
produce the per-block access scores that appear in the BNA report.

---

## Connection to the scores

Every destination score — "can residents reach a park?", "can they reach a
doctor?" — is computed by looking up the connectivity table. A block scores well
for a destination category if many destinations of that type appear as reachable
targets, especially on the low-stress network. Blocks with many reachable
destinations on comfortable roads receive higher scores.

The separation between high-stress and low-stress reachability is what makes
BNA meaningful as a bicycle-specific analysis: it distinguishes cities where
cycling is technically possible from those where cycling is genuinely
comfortable.

!!! info "Relationship to brokenspoke-analyzer"
    The SQL scripts this stage replaces — and any points where the output
    intentionally differs — are catalogued in the
    [Differences from brokenspoke-analyzer](../differences/index.md) section.
