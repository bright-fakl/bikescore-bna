# Routing network

Once the roads have been [split into segments](segmenting.md) and each segment has
a [stress level](stress.md), the **graph** stage assembles them into a routable map
— the structure the connectivity and scoring stages search to find what each block
can reach. It builds two versions of that map: one for every road, and one for only
the comfortable roads.

## What a routing graph is

A routing graph is a mathematical model of a road network. Each intersection (or
road endpoint) becomes a **node**, and each road segment becomes a **directed edge**
connecting two nodes. Every edge has a **cost** — in BNA, cost is travel distance in
metres. When the software asks "can a cyclist reach the grocery store from block X
within 15 minutes?", it runs a shortest-path algorithm across this graph to find the
answer.

BNA uses a directed graph, meaning travel along a road is modelled separately in
each direction. A one-way street has an edge in only one direction; a two-way street
has two opposing edges. Each direction can have a different stress level.

## Two graphs: high-stress and low-stress

BNA builds two parallel graphs from the same road network:

**High-stress graph** — contains every road in the study area, including arterials,
motorways, and roads without any bicycle infrastructure. This graph represents where
a confident, experienced cyclist would ride. It is used to establish that a
destination is physically reachable at all.

**Low-stress graph** — contains only roads where most people would feel comfortable
cycling: protected bike lanes, dedicated cycleways, low-traffic residential streets,
and similar infrastructure. By default, a road qualifies as low-stress if its BNA
stress level is 1. BNA stress=1 covers roughly what the Mineta LTS framework calls
"LTS 1 and LTS 2" — the full range of infrastructure most cyclists find acceptable.
The threshold is configurable: setting it to 2 restricts the comfortable network to
only the most protected infrastructure (requires custom rules that assign stress=2;
see the tutorial [Add a finer stress level](../tutorial/lts-network.md)).

A destination counts toward a block's score only if it is reachable via the low-stress
graph. The high-stress graph is consulted only as a denominator: if the low-stress
path is no more than 25% longer than the high-stress path, the connection is still
considered accessible.

## How census blocks are connected to the network

Every census block is associated with the road segments that run alongside it. The
association uses a 15-metre buffer: any road segment whose geometry either falls
entirely within 15 metres of the block, or overlaps the block boundary by more than
30 metres, is assigned to that block.

These associated road segments are the starting points for routing. When BNA asks
what block A can reach, it starts shortest-path searches from the road segments
adjacent to block A — not from the block's geometric centroid.

## The boundary road filter

The study area in BNA extends slightly beyond the city boundary. A buffer zone of
roughly 2.7 km is included so that routing can pass through neighbouring roads
without hitting a dead end at the city line. These buffer-zone roads are present in
both graphs as traversable intermediate nodes.

However, buffer-zone roads are not used as **starting points** for routing. Only
roads that physically intersect the city boundary polygon are sources. This matches
the intent of the original BNA methodology: we measure access *from* the city, not
from the surrounding hinterland.

Roads excluded from being sources are sometimes called "buffer-zone roads". They
remain in the graph so that a route from a city block can pass through a neighbouring
suburb and return — but no block in that suburb is ever counted as a destination or
a source.

## The configurable stress threshold

The default stress threshold for the low-stress graph is **1**, matching the original
BNA methodology (only the most comfortable roads). This can be changed in
`BNAConfig.graph.low_stress_threshold`:

- `low_stress_threshold = 1` — BNA standard; includes all infrastructure that BNA
  rates as comfortable (roughly equivalent to Mineta LTS 1+2).
- `low_stress_threshold = 2` — stricter analysis; only the most protected
  infrastructure qualifies (requires custom rules that produce stress=2).

Additional graphs at extra thresholds can be built simultaneously using
`BNAConfig.graph.extra_thresholds`. Each extra threshold produces an additional cost
column in the connectivity output for research and comparison purposes.

!!! info "Relationship to brokenspoke-analyzer"
    The SQL scripts this stage replaces — and any points where the output
    intentionally differs — are catalogued in the
    [Differences from brokenspoke-analyzer](../differences/index.md) section.
