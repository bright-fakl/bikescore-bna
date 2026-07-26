# Segmenting the road network

OpenStreetMap stores roads as long, continuous lines that can run through many
intersections without a break. Before any routing can happen, `bikescore-bna` has
to cut those lines into the individual pieces a route is built from. That is the
job of the **segment** stage.

It turns the ways read from OSM into **segments** — each a single stretch of road
between two intersections — and, along the way, identifies the recreational trails
that scoring later rewards. The stress and routing stages that follow all operate
on these segments.

## From ways to segments

OSM stores roads as continuous linestrings (called *ways*) that may span multiple
intersections. A single way might run for a kilometre and pass through four traffic
lights without any break in the underlying data. Each way is split at every
intersection node into individual **segments** — the atomic units of the routing
network.

This process is called *topology splitting*. A node is an intersection if it
appears in more than one way. For each way, `bikescore-bna` walks the node sequence
and splits whenever it reaches an intersection node, carrying that node forward as
the start of the next segment.

The result: each segment connects exactly two nodes (start and end), has a unique
segment ID, and inherits the road attributes of its parent way — speed limit, lane
counts, bike infrastructure, and so on. Level of Traffic Stress is assigned to each
segment afterwards, by the [stress stage](stress.md).

## Segment identifiers

Every segment is identified by its **end node** (`road_id = end_node_id`); each
vertex corresponds to the target end of a road segment. This identifier is what the
[routing network](routing-network.md) later uses to wire segments together.

Using only the end node as the identifier is intentional. Using both start and end
nodes would create ~5% extra connectivity pairs, inflating scores.

## Recreational trails

Some path segments form recreational trails — long, spread-out networks of cycling
paths that are cycling destinations in their own right, not just short connectors.
The segment stage identifies them so the [scoring stage](scoring.md) can reward
access to them.

After splitting, `bikescore-bna` groups connected path segments into clusters using
union-find on shared node IDs. Each cluster that is long enough and geometrically
spread-out enough qualifies as a trail for recreational scoring.

Two thresholds filter the clusters:

- **`min_path_length = 4800 m`** — the total length of all segments in the cluster.
  Very short clusters (parking-lot bike racks, short connecting paths) are excluded.
- **`min_bbox_length = 3300 m`** — the diagonal of the bounding box of the cluster
  geometry. This filters paths that loop back on themselves: a circular loop of
  5 km still has a small bounding box if it doesn't go anywhere new, so it fails
  this check.

Together these two thresholds select linear trail networks (rail trails, greenways,
riverside paths) while excluding short connectors and circular loops.

!!! info "Relationship to brokenspoke-analyzer"
    The SQL scripts this stage replaces — and any points where the output
    intentionally differs — are catalogued in the
    [Differences from brokenspoke-analyzer](../differences/index.md) section.
