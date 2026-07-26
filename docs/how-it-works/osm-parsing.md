# Reading OSM data

OpenStreetMap (OSM) is the data source for the road network, cycling
infrastructure, and destination points of interest. This page explains how
`bikescore-bna` reads and prepares OSM data before the analysis begins.

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

## Configuration

The parse stage is configured through `BNAConfig`:

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

!!! info "Relationship to brokenspoke-analyzer"
    The SQL scripts this stage replaces — and any points where the output
    intentionally differs — are catalogued in the
    [Differences from brokenspoke-analyzer](../differences/index.md) section.
