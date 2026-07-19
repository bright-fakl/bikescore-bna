# Differences from brokenspoke-analyzer

bikescore reimplements the [PeopleForBikes brokenspoke-analyzer](https://github.com/PeopleForBikes/brokenspoke-analyzer)
pipeline in pure Python. The two implementations produce closely matching output across all stages, with exceptions documented here.

Every deviation is one of three types:

- **Bug fix** — bikescore corrects a defect in the SQL reference
- **Architectural difference** — different approach, justified and more correct
- **Numerical artefact** — floating-point sensitivity at a threshold; irreducible

---

## 1. SQL bug fixes (classify stage)

### 1a. Parking tag overwrite

**Affected columns:** `ft_park`, `tf_park`  
**Stage:** classify

`park.sql` runs three sequential UPDATEs for `both`, `right`, and `left`
parking lanes. The right and left passes overwrite the `both` result
unconditionally. A road tagged `parking:lane:both=parallel` with no separate
`:right` or `:left` tags has its parking columns cleared by the later passes.

bikescore evaluates the three cases independently and preserves the `both`
result whenever `right`/`left` would produce NULL.

**Assessment:** bikescore is correct; the SQL reference silently drops valid
parking data for roads using the `both` convention.

---

### 1b. Opposite-direction bike track dead code

**Affected column:** `tf_bike_infra`  
**Stage:** classify

A copy-paste error in `bike_infra.sql`: inside the `WHEN one_way_car='ft'`
outer CASE branch, two inner conditions test `one_way_car='tf'` — a condition
that can never be true inside that branch. This makes the `tf` opposite-track
assignment for `ft` one-way roads unreachable.

bikescore adds the correct assignment for those two conditions.

**Assessment:** bikescore is correct; the dead-code branch never fires in the
SQL reference so the column is systematically wrong for affected roads.

---

## 2. Pipeline ordering (segment stage)

### 2a. Topology-ordering orphan roads

**Affected rows:** a small number of isolated road ways absent from bikescore
output but present in the reference  
**Stage:** classify → segment (cascades downstream)

brokenspoke-analyzer builds road topology (via osm2pgrouting) **before**
`functional_class.sql` removes ways with no functional class. A way that
connects only to a to-be-deleted way gets split at that shared node first.
The two resulting segments each reference the other, so both pass the SQL
one-hop orphan check — even though the whole cluster is disconnected from the
main network.

bikescore runs `classify` (dropping unclassified ways) **before** `segment`
(building topology). Without the deleted connecting way, the remaining ways
are correctly identified as orphans and removed.

**Assessment:** bikescore is more correct. The reference retains isolated
dead-end clusters as a pipeline-ordering artefact; they are unreachable from
any census block and contribute nothing to scores.

---

## 3. Road geometry at the city boundary

### 3a. Boundary polygon clip vs. bounding-box truncation

**Affected outputs:** road segment lengths, routing graph, connectivity, and
all downstream scores  
**Stage:** segment

After osmium extract, brokenspoke passes the city PBF through `osmconvert
-b=bbox --drop-broken-refs` before database import. The bbox is the axis-aligned
extent of the census block dataset. osmium's `complete_ways` strategy already gave
both pipelines the full geometry of cross-boundary roads (all nodes present). osmconvert
removes nodes that lie outside the census-block bbox; `--drop-broken-refs` then
removes those node references from any way that pointed to them. The way is kept but
with a shorter node list — its geometry is truncated to the inside-bbox portion. Roads
whose outside nodes extend beyond the census-block bbox are therefore geometrically
shortened at the bbox edge. The `osmconvert` step is explicitly acknowledged as
vestigial in the brokenspoke source and rarely affects anything for typical cities
where the census-block bbox closely matches the city extent.

bikescore uses `osmium extract --strategy=complete_ways`, which is identical to
the brokenspoke default. bikescore skips the osmconvert bbox step entirely, so
cross-boundary roads retain their full geometry regardless of the census-block bbox.

**Assessment:** bikescore's approach is more precise. The osmconvert bbox clip
has no consistent geometric relationship to the city boundary; the brokenspoke
developer noted it was intended to be removed. Segment-length differences in
affected roads are all attributable to this bbox truncation, not to routing or
cost logic.

---

## 4. Parse way count

### 4a. Raw parse output vs. classified network

**Affected output:** row count of the parse stage  
**Stage:** parse

bikescore's parse stage produces more ways than the brokenspoke-analyzer
reference parquet. The reference was exported after the classify stage, which
drops unclassified roads and ways with no functional class. bikescore's parse
output includes all highway-tagged ways within the service buffer before any
classification.

After classify, way counts align (minus any topology-ordering orphans from
deviation §2a).

**Assessment:** Not a real deviation — the reference parquet does not
represent raw parse output. The apparent difference disappears after classify.

---

## 5. Clipping approaches {#clipping-approaches}

The two implementations take fundamentally different approaches to building
the study-area dataset from a regional PBF.

### brokenspoke-analyzer clipping pipeline

brokenspoke clips data in three steps, in sequence:

**Step 1 — osmium extract (polygon)**
`runner.run_osmium_extract` clips the regional PBF to the city boundary
polygon using `osmium extract -p boundary.geojson`. This is a polygon clip
with no buffer; only ways that have at least one node inside the polygon are
kept (with their complete geometry).

**Step 2 — osmconvert (bounding box)**
`runner.run_osm_convert` then clips the osmium output further:
```
osmconvert city.pbf --drop-broken-refs -b=west,south,east,north -o=city.clipped.osm
```
The bounding box is the axis-aligned bounding box of the census block dataset
(`ST_Extent(census_blocks)` in WGS84). The brokenspoke source code comments note
this step as "normally useless now since we clip the data during the prepare phase"
and questions whether it should be removed — but it still executes. Because osmium's
`complete_ways` strategy already produced complete way geometries (all nodes present),
osmconvert finds no pre-existing broken references. Its effect is to remove nodes
outside the census-block bbox and then, via `--drop-broken-refs`, to remove those
node references from any way that pointed to them. The way is retained but with a
shorter node list — its geometry is truncated to the portion within the bbox.
For typical cities where the census-block bbox closely matches the city extent,
this step truncates nothing.

**Step 3 — clip_osm.sql (service buffer)**
After osm2pgsql bulk-imports the osmconvert output into PostGIS, `clip_osm.sql`
runs:
```sql
DELETE FROM neighborhood_ways WHERE NOT ST_DWithin(geom, boundary, :nb_boundary_buffer);
DELETE FROM neighborhood_osm_full_point WHERE NOT ST_DWithin(way, boundary, :nb_boundary_buffer);
-- (same for lines and polygons)
```
`nb_boundary_buffer` equals `max_trip_distance` (2,680 m). Cross-boundary
roads were split by osm2pgrouting at intersection nodes (nodes shared by
multiple ways). Resulting segments may lie wholly inside the city, wholly outside,
or straddle the boundary. clip_osm.sql retains any segment within 2,680 m of the
boundary polygon, creating a buffer zone from the cross-boundary road portions.
Roads entirely outside the city (no node inside the polygon) were never included
by osmium and are absent from both graphs.

The net effect: brokenspoke's routing graph contains outside segments of
cross-boundary roads within 2,680 m of the city boundary (from step 4). Outside
segments of cross-boundary roads whose outside nodes extended beyond the census-block
bounding box are absent (dropped by step 2).

### bikescore clipping pipeline

bikescore uses two stages:

**acquire — exact boundary pre-clip**
`acquire.py` calls `pre_clip_pbf(region_pbf, boundary, buffer_m=0.0)`, which
runs:
```
osmium extract --strategy=complete_ways -p boundary.geojson region.pbf -o city.pbf
```
The `complete_ways` strategy keeps the full geometry of any way with at least
one node inside the polygon. No buffer; the output PBF contains only data
within (or crossing) the exact city boundary.

**clip stage — keep whole ways that touch the buffer zone**
`stages/clip.py` keeps any way whose geometry intersects the boundary polygon
expanded by `max_trip_distance` (2,680 m). Ways are kept or discarded as whole
objects — nodes are not trimmed. A cross-boundary way from osmium's `complete_ways`
output is retained in its entirety, including its outside nodes, because it
intersects the buffered boundary.

**segment stage — split at boundary, remove dead ends**
After topology splitting at intersection nodes, `split_at_boundary()` finds segments
that straddle the city boundary polygon, splits each at the exact crossing point
(inserting a virtual node), and then `remove_out_of_city_deadends()` removes outside
dead-end chains. Roads wholly outside the city (no node inside the polygon) are
absent from both graphs, since osmium never includes them.

### Practical consequences

| Scenario | brokenspoke | bikescore |
|---|---|---|
| Road inside city boundary | ✓ in graph | ✓ in graph |
| Road crossing boundary, outside nodes within bbox | ✓ outside segments within 2,680 m | ✓ outside portions within 2,680 m |
| Road crossing boundary, outside nodes beyond bbox | ✓ truncated at bbox edge; segments within 2,680 m kept | ✓ full geometry; portions within 2,680 m kept |
| Road entirely outside boundary | ✗ absent (osmium never includes) | ✗ absent (osmium never includes) |
| POI outside boundary, within 2,680 m, inside bbox | ✓ included | ✓ included |
| POI outside boundary, within 2,680 m, outside bbox | ✗ absent (osmconvert removed) | ✗ absent |

**Assessment for routing**: both pipelines include buffer-zone roads (outside
segments of cross-boundary roads within 2,680 m of the boundary). bikescore
additionally retains cross-boundary roads that osmconvert would have dropped due to
the census-block bbox restriction. For most cities this has minimal practical effect,
since the census-block bbox closely matches the city extent.

**Assessment for transit points** (formerly §5a): transit stops that are
outside the city boundary but within 2,680 m are present in brokenspoke's
database *only if* they also fall inside the census-block bounding box. This
is an accidental restriction, not the stated intent. bikescore does not apply
this bbox restriction and includes any transit stop within 2,680 m of the boundary.

**Assessment overall**: both implementations provide a routing buffer zone from
cross-boundary road segments. brokenspoke's osmconvert bbox step may drop some
cross-boundary roads entirely (if their outside nodes extend beyond the census-block
bbox); bikescore avoids this by skipping the bbox step. For most cities the
difference is minimal.

### 5a. Transit point count difference

**Affected output:** transit destination cluster count  
**Stage:** destinations

As a consequence of the clipping differences above, brokenspoke may exclude
transit stops that are within the service radius but outside the census-block
bounding box (e.g. ferry terminals beyond a rectangular bbox edge). bikescore
clips transit stops only by `DWithin(max_trip_distance)` from the city
boundary — the same criterion that brokenspoke's `clip_osm.sql` intends to
apply, but without the bbox pre-filter.

**Assessment:** bikescore implements the stated intent of `clip_osm.sql`.
Count differences in transit are attributable to the bbox relic in
brokenspoke, not to a logic error in bikescore.

---

## 6. Destinations retail clustering

### 6a. Floating-point sensitivity at cluster threshold

**Affected output:** retail destination cluster count  
**Stage:** destinations

For a small number of retail POI pairs whose distance is within floating-point
rounding of the 50 m cluster threshold, bikescore (scipy) and
brokenspoke-analyzer (PostGIS) assign them to different clusters. The
difference is typically one or two points on a dataset of hundreds.

**Assessment:** Irreducible. Replicating PostGIS-specific rounding behaviour
would defeat the purpose of a pure-Python reimplementation. The difference is
sub-1% and caused by numerical precision, not a logic error.

---

## Summary

| # | Deviation | Stage | Type | Assessment |
|---|---|---|---|---|
| §1a | Parking tag overwrite | classify | SQL bug fix | bikescore correct |
| §1b | Opposite-direction track dead code | classify | SQL bug fix | bikescore correct |
| §2a | Topology-ordering orphan roads | segment | Architecture | bikescore more correct |
| §3a | Boundary polygon clip vs. bbox truncation | segment / graph | Architecture | bikescore more principled |
| §5 | Clipping approaches (buffer zone, bbox relic) | parse / clip / destinations | Architecture | bikescore implements stated intent without bbox relic |
| §5a | Transit point count | destinations | Architecture | bikescore implements intent |
| §6a | Retail cluster floating-point | destinations | Numerical artefact | Irreducible, sub-1% |
| §4a | Parse way count | parse | Comparison artefact | Not a real deviation |
