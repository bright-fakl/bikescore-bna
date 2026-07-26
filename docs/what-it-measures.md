# What bikescore measures

`bikescore-bna` produces the
[PeopleForBikes Bicycle Network Analysis (BNA)](https://bna.peopleforbikes.org/):
a measure of how well a city's streets let people reach everyday destinations by
bike, on routes most people would find comfortable. This page describes what the
scores mean, independent of the code that produces them.

The analysis turns on one distinction — not how many roads a cyclist *could* use,
but how many a cautious rider *would* use — and measures it in four layers, each
building on the previous:

1. **Traffic stress** — how comfortable each road is to ride on.
2. **Connectivity** — which places you can reach on comfortable roads.
3. **Access** — how much of what you need is within comfortable reach.
4. **The city rating** — a single 0–100 score for the whole city.

---

## 1. Traffic stress: how comfortable is each road?

Roads differ widely in how comfortable they are to cycle on. `bikescore-bna`
captures this with **Level of Traffic Stress (LTS)**, an established framework
(from the Mineta Transportation Institute) that rates each road by comfort, based
on how cyclists behave rather than on distance alone.

The full LTS scale runs from 1 to 4:

| Level | Who is comfortable here | Typical road |
|---|---|---|
| **LTS 1** | Almost everyone, including children | Protected bike lanes, very quiet streets |
| **LTS 2** | Most adults | Ordinary bike lanes on moderate-speed streets |
| **LTS 3** | Confident cyclists only | Painted markings on busy roads |
| **LTS 4** | Almost no one | High-speed arterials with no bike facilities |

Bikescore simplifies this into two buckets — **comfortable** (LTS 1–2) and
**stressful** (LTS 3–4) — and labels every road segment accordingly, from its
speed limit, lane count, bike infrastructure, and whether cars park alongside.
The result is two networks: the roads most people will ride, and the roads most
people avoid.

!!! info "Going deeper"
    How each road gets its stress label is covered in
    [Level of Traffic Stress](how-it-works/stress.md) and
    [Road classification](how-it-works/road-features.md).

---

## 2. Connectivity: where can you get?

A comfortable road is only useful if it connects to other comfortable roads. For
every neighbourhood, `bikescore-bna` finds which other places are reachable within
a reasonable trip — about **2.7 km**, roughly a 15-minute ride — and does so
twice: once using **any** road (the *high-stress* network), and once using **only
comfortable** roads (the *low-stress* network). The gap between the two shows
where the comfortable network falls short.

A comfortable route only counts if it is not a large detour: it must be **no more
than 25% longer** than the most direct route.

!!! info "Going deeper"
    The routing model, distance threshold, and detour rule are described in
    [Connectivity](how-it-works/connectivity.md).

---

## 3. Access: what can you reach?

Reaching other neighbourhoods matters because of what is in them. This layer
scores, for each neighbourhood, how much of what people need is within
comfortable reach, across a fixed set of destinations:

- **People and jobs** — the population and employment you can reach.
- **Core services** — grocery stores, doctors, dentists, pharmacies, hospitals, social services.
- **Opportunity** — schools, colleges, universities.
- **Retail** — shops.
- **Recreation** — parks, trails, community centres.
- **Transit** — stops and stations.

For each, it compares what is reachable on *any* road with what is reachable on
*comfortable* roads. Two properties of the scoring are worth noting:

- **Each additional destination counts for less.** Being able to reach one
  grocery store rather than none is a large gain; a second or third reachable
  store adds progressively less. The score reflects this diminishing value.
- **A city is scored on what it has.** A city with no transit is not penalised
  for lacking transit — that category drops out of its score.

!!! info "Going deeper"
    The per-destination formulas and category weights are in
    [Access scoring](how-it-works/scoring.md); where destinations come from is in
    [Destinations](how-it-works/destinations.md).

---

## 4. The city rating: one number, 0–100

The neighbourhood scores roll up into a single **0–100 BNA score**, with higher
meaning more bikeable. It is a **population-weighted average**, so neighbourhoods
with more residents count for more; neighbourhoods with no bike connectivity at
all are excluded rather than counted as zero. The same roll-up produces
per-category ratings, showing where a city does well or poorly.

!!! info "Going deeper"
    The weighting, missing-destination handling, and unconnected-block rule are in
    [Neighborhood scores](how-it-works/neighborhood-scores.md).

---

## In summary

```
Traffic stress   →  Is this road comfortable?
Connectivity     →  Which places can I reach on comfortable roads?
Access           →  How much of what I need is comfortably reachable?
City rating      →  How bikeable is the city overall?
```

## Where to go next

- **Score a city:** [Score a city](tutorial/run-a-city.md).
- **The mechanics, stage by stage:** [How it works](how-it-works/index.md).
- **Why it is a database-free Python library:** [Why bikescore-bna](why-bikescore.md).
