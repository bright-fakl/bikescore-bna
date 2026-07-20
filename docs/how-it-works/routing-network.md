# Routing network

The BNA pipeline measures how easily cyclists can reach destinations from any census
block. To answer that question, it needs a map of the road network that a cyclist
might use — and it needs two versions of that map: one for every road, and one for
only the comfortable roads.

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

## Topology splitting: one intersection, one segment

OSM stores roads as continuous linestrings (called *ways*) that may span multiple
intersections. A single way might run for a kilometre and pass through four traffic
lights without any break in the underlying data. Before routing, each way must be
split at every intersection node into individual **segments** — the atomic units of
the routing graph.

This process is called *topology splitting* and mirrors what osm2pgrouting does for
the SQL reference implementation. A node is an intersection if it appears in more
than one way. For each way, bikescore walks the node sequence and splits whenever it
reaches an intersection node, carrying that node forward as the start of the next
segment.

The result: each segment connects exactly two nodes (start and end), has a unique
segment ID, and inherits all road attributes (speed limit, bike infrastructure,
stress level, etc.) from its parent way.

### End nodes as routing identifiers

Every segment is identified in the routing graph by its **end node** (`road_id =
end_node_id`). This matches the SQL reference's `neighborhood_ways_net_vert` table,
where each "vert" (vertex) corresponds to the target end of a road segment.

Using only the end node as the segment identifier is intentional. Using both start
and end nodes would create ~5% extra connectivity pairs, inflating scores.

### Recreational trails

Some path segments form recreational trails — long, spread-out networks of cycling
paths that constitute genuine cycling destinations (not just short connectors).

After topology splitting, bikescore groups connected path segments into clusters using
union-find on shared node IDs. Each cluster that is long enough and geometrically
spread-out enough qualifies as a trail for recreational scoring.

Two thresholds filter the clusters:

- **`min_path_length = 4800 m`** — the total length of all segments in the cluster.
  Very short clusters (parking lot bike racks, short connecting paths) are excluded.
- **`min_bbox_length = 3300 m`** — the diagonal of the bounding box of the cluster
  geometry. This filters paths that loop back on themselves: a circular loop of
  5 km still has a small bounding box if it doesn't go anywhere new, so it fails
  this check.

Together these two thresholds select linear trail networks (rail trails, greenways,
riverside paths) while excluding short connectors and circular loops.

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

## Comparison with brokenspoke-analyzer

brokenspoke builds the routing graph through two tools and one SQL script:

| Step | Tool / file | What it does |
|---|---|---|
| Topology splitting | `osm2pgrouting` | Splits ways at intersections, builds `neighborhood_ways_net_link` and `neighborhood_ways_net_vert` tables |
| Road–block association | `connectivity/census_blocks.sql` | Associates road segments with census blocks using a 15 m buffer |
| Graph construction | `connectivity/build_network.sql` | Creates the pgRouting network table used by reachable-roads queries |

bikescore replaces these with:

| bikescore file | What it does |
|---|---|
| `stages/segment.py` | Topology splitting (pure Python; mirrors osm2pgrouting output) |
| `stages/graph.py` | Builds scipy CSR sparse matrices for high-stress and low-stress routing |

Two deviations arise from differences in how the two implementations handle
topology:

- **[§2a Topology-ordering orphan roads](deviations.md#2a-topology-ordering-orphan-roads)** —
  brokenspoke splits topology *before* dropping unclassified roads, so isolated
  road clusters that connect only to deleted ways can pass the orphan check.
  bikescore classifies first, so these clusters are correctly identified as
  dead ends.
- **[§3a Boundary polygon clip vs. bounding-box truncation](deviations.md#3a-boundary-polygon-clip-vs-bounding-box-truncation)** —
  brokenspoke's vestigial `osmconvert -b=bbox --drop-broken-refs` step truncates
  road geometries at the rectangular census-block bounding box, shortening segments
  that cross the bbox edge. bikescore has no bbox step, so these roads retain their
  full geometry.

### Buffer zone

Both pipelines include a routing buffer zone — road segments outside the city
boundary that cyclists near the edge can use to route through neighbouring areas.
The buffer zone comes from roads that physically cross the city boundary: osmium's
`complete_ways` strategy (used by both brokenspoke and bikescore) preserves the
full geometry of such roads, including nodes outside the polygon.

In brokenspoke, after osm2pgrouting splits those roads into individual topology
segments, `clip_osm.sql` retains outside segments within `max_trip_distance`
(2,680 m) of the city boundary and deletes the rest. Only the city-specific
osmium extract is in the database at this point — not the full regional dataset.

In bikescore, the clip stage keeps whole way objects that intersect the buffered
boundary — nodes are not trimmed at this point. The segment stage then splits ways
at intersection nodes and at the exact city boundary crossing point (inserting
virtual nodes), and removes outside dead-end chains. Roads *entirely* outside the
city (no node inside the polygon) are absent from both graphs, since osmium never
includes them. See
[Differences from brokenspoke-analyzer — Clipping](deviations.md#clipping-approaches)
for a detailed comparison.
