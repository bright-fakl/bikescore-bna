# How destinations work

The destinations stage finds points of interest (POIs) in the parsed OSM data,
groups nearby POIs of the same type into clusters, and then links each cluster to
the census blocks that contain it. This association is later used by the scoring
stage to measure how many destination clusters each block can reach by bike.

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

## Comparison with brokenspoke-analyzer

brokenspoke locates destinations through SQL scripts that query the PostGIS
tables populated during OSM import. One script per destination type, all under
`connectivity/destinations/`:

| SQL file | Destination type |
|---|---|
| `destinations/colleges.sql` | Technical/vocational colleges |
| `destinations/community_centers.sql` | Community centers |
| `destinations/dentists.sql` | Dentists |
| `destinations/doctors.sql` | Doctors / clinics |
| `destinations/hospitals.sql` | Hospitals |
| `destinations/parks.sql` | Parks |
| `destinations/pharmacies.sql` | Pharmacies |
| `destinations/retail.sql` | Retail |
| `destinations/schools.sql` | K-12 schools |
| `destinations/social_services.sql` | Social services |
| `destinations/supermarkets.sql` | Grocery stores |
| `destinations/transit.sql` | Transit stops |
| `destinations/universities.sql` | Universities |

Each script uses `ST_DWithin(boundary, max_trip_distance)` to restrict POIs to
the service area, then applies PostGIS `ST_ClusterDBSCAN` for polygon-based types
and a custom clustering approach for point types.

bikescore collects all destination POIs during the parse stage (a single osmium
pass), then clusters and filters them in `stages/destinations.py` using
`scipy.cluster.hierarchy`. The `OsmMatcher` conditions defined in
`DestinationRegistry` encode the same tag logic as the per-type SQL scripts.

Two known deviations affect destination counts:

- **[§5 Clipping differences](deviations.md#clipping-approaches)** — brokenspoke's
  vestigial `osmconvert -b=bbox` step can exclude transit stops that are within
  the service radius but outside the census-block bounding box (e.g. ferry
  terminals). bikescore clips only by `DWithin(max_trip_distance)` and
  includes these stops correctly.
- **[§6a Retail cluster floating-point sensitivity](deviations.md#6a-floating-point-sensitivity-at-cluster-threshold)** —
  for a small number of retail POI pairs at the 50 m cluster threshold,
  scipy and PostGIS assign different cluster memberships due to floating-point
  differences. The discrepancy is sub-1% and irreducible.
