# How destinations work

The destinations stage finds points of interest (POIs) in the parsed OSM data, groups nearby POIs of
the same type into clusters, and links each cluster to the census blocks that
contain it. The scoring stage later uses this to measure how many destinations
each block can reach by bike.

## The 13 standard destination types

BNA measures access to 13 types of essential daily destinations:

| Category | Type | What it captures |
|---|---|---|
| Opportunity | K-12 Schools | Public and private schools, kindergartens |
| Opportunity | Technical/Vocational Colleges | Community and vocational colleges |
| Opportunity | Universities | Four-year universities and campuses |
| Core Services | Doctors / Clinics | Medical clinics and doctors' offices |
| Core Services | Dentists | Dental offices |
| Core Services | Hospitals | Hospital complexes |
| Core Services | Pharmacies | Pharmacies and chemist shops |
| Core Services | Grocery Stores | Supermarkets |
| Core Services | Social Services | Social facilities and service centers |
| Recreation | Parks | Parks, nature reserves, playgrounds |
| Recreation | Community Centers | Community centres |
| Retail | Retail | General shops (excluding grocery) |
| Transit | Transit Stops | Bus stations, rail stations, ferry terminals |

These represent the destinations that most residents need access to for a
functional daily life. Trails — the fourteenth type of "destination" in BNA
scoring — are treated differently because they are scored via reverse routing
rather than cluster counting.

## What "clustering" means and why it matters

Nearby destinations of the same type are grouped into clusters before scoring.
Without clustering, a neighbourhood with three pharmacies in the same block would
score three times as well as a neighbourhood with one pharmacy on each of three
distant blocks — even though the person on the first block can only visit one
pharmacy per trip.

Clustering solves this by treating nearby pharmacies as a single "access point".
A block that can reach a cluster of three pharmacies scores the same as a block
that can reach a single pharmacy at the same distance. The score rewards access
to distinct pharmacy *locations*, not pharmacy *density* at one spot.

## Why clustering tolerances vary

The clustering tolerance (in metres) reflects how people actually seek out each
type of destination:

- **Universities (150m)**: large campuses often span multiple polygon features;
  a tolerance of 150m merges campus buildings that belong together
- **Colleges (100m)**: smaller campuses than universities but still multi-feature
- **Transit, Retail (75m, 50m)**: stations and shops cluster at the neighbourhood
  level — two bus stops on opposite sides of the same intersection are essentially
  one destination
- **Pharmacies, Doctors, Parks, etc. (50m)**: standard neighbourhood-scale tolerance
- **Schools, Social Services (0m)**: each school or social facility is a distinct
  destination regardless of proximity — three schools in adjacent blocks serve
  different communities and should each count

## How destinations are associated with census blocks

For each destination cluster, the stage finds every census block whose polygon
intersects the cluster's geometry. These block IDs are stored in the `blockid20`
column of the destination DataFrame.

During scoring, a block "has access" to a destination cluster if any of the cluster's
associated blocks appear in that source block's connectivity results — i.e., if the
block can reach at least one of the cluster's blocks via the road network.

The intersection check uses the full cluster polygon where available (for polygon-based
destinations) and falls back to the centroid point (for standalone-point destinations).

## The trails exception

Trails are not destinations in the traditional sense — they do not have fixed POI
locations to cluster. Instead, trail access is scored via **reverse Dijkstra**: for
each qualifying trail segment, a reverse shortest-path search finds which census
blocks can reach that trail. Blocks with trail access receive credit proportional to
the trail's length. This is why `trails` does not appear in the `DestinationRegistry`
even though trail scores appear in the output.

## Custom destination types

Custom destinations registered via `DestinationRegistry` are processed identically
to the 13 standard types. No special-casing is needed. See
[Adding a custom destination](../tutorial/add-destination.md) for a worked example.

!!! info "Relationship to brokenspoke-analyzer"
    The SQL scripts this stage replaces — and any points where the output
    intentionally differs — are catalogued in the
    [Differences from brokenspoke-analyzer](../differences/index.md) section.
