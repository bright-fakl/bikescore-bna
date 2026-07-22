# Reading OSM data

OpenStreetMap (OSM) is the data source for the road network, cycling infrastructure,
and destination points of interest. This page explains how bikescore-bna reads
and prepares OSM data before the analysis begins.

## What OpenStreetMap is

OpenStreetMap is a free, community-maintained geographic database. Millions of
contributors around the world map roads, buildings, amenities, parks, and
hundreds of other features. bikescore-bna uses OSM for:

- **Road geometry** — the actual line shapes of every street, path, and cycleway
- **Road attributes** — speed limits, lane counts, one-way status, cycling infra tags
- **Points of interest** — schools, hospitals, grocery stores, transit stops, parks

OSM data is global and free, which means bikescore-bna can run on any city in the world
without requiring proprietary data sources.

## PBF files and where they come from

OSM publishes raw data as **PBF** (Protocol Buffer Format) files — compact binary
files that can contain all features in a region. State and national extracts are
available from [Geofabrik](https://download.geofabrik.de/). For example, the
entire US state of Virginia is a single PBF file of about 500 MB.

bikescore-bna reads PBF files directly using [osmium](https://osmcode.org/pyosmium/),
a Python binding to the fast osmium C++ library. The parse stage makes a single
pass through the file, collecting all relevant features in one sweep.

## The study area and buffer

bikescore-bna analyses a *city*, not an entire region. Before the parse stage even
runs, the input PBF has already been clipped to the city boundary by the
`acquire` step: osmium extracts the city from the regional PBF using
`--strategy=complete_ways`, which keeps any way that has at least one node inside
the exact boundary polygon — including the complete geometry of cross-boundary
roads with their outside nodes.

The parse stage reads this already city-clipped PBF. Every way it produces
therefore has at least one node inside the boundary by construction.

Because the city PBF is already clipped to the exact boundary by `acquire`,
a separate clip stage is not needed. The only operation that was previously in
the clip stage — removing ways where `bicycle=no AND highway=path` — has been
moved into `parse`. There is no clip stage in the pipeline.

The **buffer zone** itself is established by the osmium `complete_ways` strategy:
cross-boundary roads are kept in full, providing road geometry beyond the city
edge. This gives the routing graph the surrounding context it needs — a cyclist
near the boundary can route through nearby roads outside the city. The segment
stage later splits these cross-boundary roads at the exact boundary crossing
point and removes outside dead-end chains, leaving clean in/out sub-segments.

## What bikescore-bna reads from OSM

### Roads (highway ways)

Any OSM way with a `highway=*` tag is extracted. This includes motorways, residential
streets, cycleways, footways, paths, and tracks. Ways with a `bicycle=*` tag are
also extracted even if they lack a `highway` tag.

For each way, bikescore-bna stores:

- **Geometry** — the LineString in WGS84 (EPSG:4326)
- **Node IDs** — the ordered list of OSM node IDs forming the way
- **Tag columns** — speed, lanes, width, cycling infrastructure, parking, one-way,
  and all tags needed for the classify stage

Ways where `bicycle=no AND highway=path` are removed by `parse` — footpaths
that explicitly prohibit cycling are excluded from the routing network.

### Intersection nodes

Every node referenced by a highway way is stored in `nodes_df` for topology building
in the segment stage. Additionally, nodes that carry traffic-control attributes are
flagged directly from their OSM tags:

| Attribute | OSM condition |
|---|---|
| `signalized` | `highway=traffic_signals` |
| `stop` | `highway=stop AND stop=all` |
| `rrfb` | `highway=crossing AND flashing_lights ∈ {yes,button,always,sensor}` |
| `island` | `highway=crossing AND (crossing=island OR crossing:island=yes)` |

These flags are used later by the stress stage to compute intersection stress
(signalised intersections are lower stress to cross).

### Destination POIs

bikescore-bna scores cycling access to 13 categories of destination — schools,
hospitals, grocery stores, parks, and others. During the same osmium parse pass,
every node and closed way (polygon) is checked against the destination type matchers.

A single OSM feature can match multiple destination types simultaneously. For
example, a chemist (`shop=chemist`) can match both **pharmacies** and **retail**.
The parse stage collects all matches without breaking — exclusions and deduplication
are applied later in the destinations stage.

## Why a single parse pass?

OSM PBF files can be large (500 MB for a US state). bikescore-bna makes exactly one
pass through the file, collecting roads, intersection nodes, and POIs in the same
sweep. This is efficient and avoids loading the entire file into memory.

## Configuration

The parse and clip stages are configured through `BNAConfig`:

```python
from bikescore_bna.config import BNAConfig

config = BNAConfig.with_defaults()
# Override buffer distance (default 2680m)
config.max_trip_distance = 3000

# Add a custom destination type to the parser
from bikescore_bna.destinations import DestinationType, OsmMatcher
config.destinations.register(DestinationType(
    name="libraries",
    display_name="Public Libraries",
    node_matchers=[OsmMatcher({"amenity": "library"})],
    area_matchers=[OsmMatcher({"amenity": "library"})],
    clustering_tolerance_m=100,
    scoring_category="recreation",
))
```

## Implementation

OSM parsing is implemented in `bikescore-bna/stages/parse.py` using a single
`osmium.SimpleHandler` subclass.

SQL equivalents in brokenspoke-analyzer:

- `osm2pgsql` import — reads OSM into PostgreSQL tables
- `prepare_tables.sql` — sets up columns, merges cycleway data
- `clip_osm.sql` — clips to `ST_Buffer(boundary, :nb_boundary_buffer)`

## Comparison with brokenspoke-analyzer

brokenspoke-analyzer processes OSM data in four steps before any feature
computation begins:

1. **osmium extract** (`runner.run_osmium_extract`) — clips the regional PBF to the
   city boundary polygon: `osmium extract -p boundary.geojson region.pbf -o city.osm`.
   The default strategy is `complete_ways`: any way with at least one node inside the
   polygon is kept in full, including nodes that lie outside the polygon. The city OSM
   file therefore contains cross-boundary roads with their complete geometry.

2. **osmconvert** (`runner.run_osm_convert`) — applies a secondary clip to the bounding
   box of the census block dataset:
   ```
   osmconvert city.osm --drop-broken-refs -b=west,south,east,north -o=city.clipped.osm
   ```
   The bbox is `ST_Extent(census_blocks)` in WGS84 — approximately the rectangular
   envelope of the city. Any node outside this box is removed; `--drop-broken-refs`
   then removes those node references from any way that pointed to them. The way is
   kept, but with a shorter node list — its geometry is truncated to the inside-bbox
   portion. For cross-boundary roads whose outside nodes lie within the census-block
   bbox, nothing changes — they survive intact. Roads whose outside nodes extend
   beyond the bbox are geometrically shortened at the bbox edge. The brokenspoke
   source itself notes this step as "normally useless now since we clip the data
   during the prepare phase," meaning it rarely affects anything for typical cities
   where the census-block bbox closely matches the city extent.

3. **osm2pgsql / osm2pgrouting** — bulk-imports the osmconvert-clipped city file (not
   the regional PBF) into a PostGIS database. osm2pgrouting builds the routing
   topology, splitting ways into individual segments at intersections. `prepare_tables.sql`
   then renames columns and merges cycleway attributes.

4. **clip_osm.sql** — after topology splitting, removes from the database any road
   segment (or POI) outside `ST_DWithin(boundary, :nb_boundary_buffer)`:
   ```sql
   DELETE FROM neighborhood_ways WHERE NOT ST_DWithin(geom, boundary, :nb_boundary_buffer);
   DELETE FROM neighborhood_osm_full_point WHERE NOT ST_DWithin(way, boundary, :nb_boundary_buffer);
   -- (same for lines and polygons)
   ```
   The buffer equals `max_trip_distance` (2,680 m). Cross-boundary roads were split by
   osm2pgrouting at intersection nodes (nodes shared by multiple ways). Resulting
   segments may lie wholly inside the city, wholly outside, or straddle the boundary.
   clip_osm.sql keeps any segment within 2,680 m of the boundary polygon, creating a
   buffer zone from the cross-boundary road portions.

bikescore-bna replaces steps 1–4 with three stages:

- **acquire** — `pre_clip_pbf(region_pbf, boundary, buffer_m=0.0)` runs
  `osmium extract --strategy=complete_ways -p boundary.geojson`. Both pipelines
  produce the same city PBF with complete cross-boundary road geometry.
- **parse** — a single `osmium.SimpleHandler` pass collects roads, intersection
  nodes, and destination POIs in one sweep. Removes `bicycle=no AND highway=path`
  ways (footpaths that prohibit cycling).
- **segment** — topology splitting at intersection nodes, followed by
  `split_at_boundary()`, which splits any segment that straddles the city boundary
  polygon at the exact crossing point (inserting a virtual node) and then removes
  out-of-city dead-end chains. This is the step that produces clean inside/outside
  sub-segments from cross-boundary roads.

| brokenspoke file | bikescore-bna equivalent |
|---|---|
| `runner.run_osmium_extract` | `acquire.py: pre_clip_pbf()` |
| `runner.run_osm_convert` (bbox, vestigial) | *(absent — no bbox step)* |
| `prepare_tables.sql` | `stages/parse.py: _ParseHandler` |
| `clip_osm.sql` | *(removed — no-op given osmium pre-clip; path filter moved to parse)* |
| *(topology split only at intersections)* | `stages/segment.py: split_at_boundary()` |

**Buffer zone**: both pipelines create a routing buffer zone from cross-boundary
roads. osmium's `complete_ways` strategy gives both the full geometry of roads that
cross the city boundary. In brokenspoke, osm2pgrouting splits ways at intersection
nodes, then `clip_osm.sql` retains any resulting segment within 2,680 m of the
boundary polygon. In bikescore-bna there is no clip stage — `parse` keeps all ways
from the city PBF (every way already intersects the boundary by construction);
the `segment` stage then splits them at intersection nodes and at the exact
boundary crossing point, and removes outside dead-end chains. See
[Differences from brokenspoke-analyzer — Clipping](deviations.md#clipping-approaches)
for a full comparison.

**osmconvert bbox step**: bikescore-bna has no equivalent. When a cross-boundary road
has outside nodes that extend beyond the census-block bounding box, osmconvert
truncates the road at the bbox edge — the outside portion is removed from the
geometry. This is the root cause of the segment-length differences noted in
[deviation §3a](deviations.md#3a-boundary-polygon-clip-vs-bounding-box-truncation).
