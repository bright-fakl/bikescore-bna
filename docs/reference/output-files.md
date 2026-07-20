# Output files

The core library has **no run store**. [`score_city`](api.md) runs each stage into a
subdirectory of a temporary working directory and returns a [`ScoreResult`](api.md):

```python
result = score_city(inputs, build_config("default"))
result.stage_dirs        # {stage_name: Path} — one output directory per stage that ran
result.workdir           # the temp root; the caller owns cleanup
result.output("stress", "stress.parquet")   # -> Path to a specific output file
```

Each stage owns its directory and writes one or more files into it. The filenames and
schemas below are stable, and columns match brokenspoke-analyzer so the two tools stay
interchangeable for downstream consumers.

Geometry columns are written by GeoParquet where the file is a `GeoDataFrame`; the
destination cluster geometries are stored as EWKB-hex strings (`geom_pt`, `geom_poly`)
so plain `pandas.to_parquet` can round-trip them with their source CRS.

To turn any of these into GeoJSON / Shapefile / CSV for GIS tools or the PeopleForBikes
platform, use the [export framework](#export) below.

## Files by stage

### `parse/`

| File | Contents | Key |
|---|---|---|
| `ways_raw.parquet` | Raw OSM ways with parsed tags (pre-segmentation) | `osm_id` |
| `nodes.parquet` | OSM nodes with intersection attributes (`signalized`, `stop`, `rrfb`, `island`, `lon`, `lat`) | `node_id` |
| `poi_raw.pkl` | Pickled `list[RawPOI]` — raw points/polygons of interest for the destinations stage | — |

### `census/`

| File | Contents | Key |
|---|---|---|
| `census_blocks.parquet` | Census blocks (GeoParquet) with population/housing and the clipped city geometry | `geoid20` |

### `jobs/`

| File | Contents | Key |
|---|---|---|
| `jobs.parquet` | LODES employment counts per census block (US cities only; empty otherwise) | `blockid20` |

### `attributes/`

| File | Contents | Key |
|---|---|---|
| `ways_classified.parquet` | Ways with the resolved attribute layer applied (`functional_class`, speed/lane/width defaults, bike-infra, derived flags) | `osm_id` |

### `segment/`

| File | Contents | Key |
|---|---|---|
| `segments.parquet` | Topology-split road segments (GeoParquet) with `start_node_id` / `end_node_id` | `road_id` |
| `trails.parquet` | Off-network paths (the `network_path` pseudo-destination) with `path_length` / `bbox_length` | — |

### `stress/`

| File | Contents | Key |
|---|---|---|
| `stress.parquet` | Segments with LTS added: `ft_seg_stress`, `tf_seg_stress`, `ft_int_stress`, `tf_int_stress` | `road_id` |

### `graph/`

| File | Contents | Key |
|---|---|---|
| `graph_bundle.pkl` | Pickled `GraphBundle` — the CSR routing graphs (`G_high`, `G_low`) and block↔vertex association tables used by connectivity/scores | — |
| `graph.parquet` | Directed link table: `source_vert`, `target_vert`, `link_cost`, `link_stress` (parallel edges allowed) | — |
| `nodes.parquet` | Vertex↔road map: `vert_id`, `road_id` | `vert_id` |
| `blocks_with_roads.parquet` | Census blocks enriched with the road IDs and projected geometry needed for source-vertex selection | `geoid20` |

### `connectivity/`

| File | Contents | Key |
|---|---|---|
| `connectivity.parquet` | Reachability between every block pair: `source_blockid20`, `target_blockid20`, `low_stress`, `low_stress_cost`, `high_stress`, `high_stress_cost` | (`source`, `target`) |
| `connectivity.csv` | Same table with booleans as PostgreSQL `t`/`f`, matching brokenspoke-analyzer | — |

### `destinations/`

| File | Contents | Key |
|---|---|---|
| `dest_{type}.parquet` | One clustered destination table per type (`schools`, `parks`, `supermarkets`, …); `id`, `blockid20` (array), `geom_pt`/`geom_poly` (EWKB-hex), and `pop_*` placeholder columns filled later by the scoring stage | `id` |
| `destination_summary.parquet` | Cluster count per destination type: `dest_type`, `cluster_count` | `dest_type` |

### `scores/`

| File | Contents | Key |
|---|---|---|
| `scores.parquet` | Per-block access scoring — every `{category}_low_stress` / `{category}_high_stress` / `{category}_score`, plus `opportunity_score`, `core_services_score`, `recreation_score`, `overall_score` | `geoid20` |

### `neighborhood/`

| File | Contents | Key |
|---|---|---|
| `neighborhood.parquet` | The 23 headline scores: `score_id`, `score_original`, `score_normalized`, `human_explanation` | `score_id` |
| `score_inputs.parquet` | The 132 intermediate scores (medians, percentiles, shed scores) with `use_*` contribution flags | `id` |
| `mileage.parquet` | Miles of each bike-infrastructure type within the boundary: `feature_type`, `total_mileage` | `feature_type` |

## Export

The stage outputs above are parquet/pickle — the native format for hashing and reuse. To
hand them to GIS tools or the PeopleForBikes platform, `bikescore.export` writes them to
**GeoJSON, Shapefile, or CSV**, reprojecting geometry to WGS84 (EPSG:4326). It works
directly off a `ScoreResult` — no run store, no database.

```python
from bikescore import score_city, export_target, export_bundle, load_city

city   = load_city(city_dir)
config = build_config("default")
result = score_city(inputs, config)

# one target → one file (road-segment LTS as GeoJSON)
export_target(result, city, config, "stress", "out/", file_format="geojson", inputs=inputs)

# the whole brokenspoke-analyzer deliverable set
export_bundle(result, city, config, "out/", bundle="bna", inputs=inputs)
```

A **target** is one named, exportable output; `export_target` / `export_bundle` build a
[`ExportContext`](api.md) over the result's `stage_dirs` and write the requested formats.
`inputs` is the same dict passed to `score_city` — some targets (e.g. `boundary`) read a
raw dataset directly. `list_export_targets()` / `list_export_bundles()` enumerate what is
available, and the CLI's [`export-list`](cli.md#export-list) prints the table.

### Targets

| Target | Owner stage | Default formats | Notes |
|---|---|---|---|
| `stress` | stress | geojson, shapefile, csv | road segments with LTS — the headline network layer |
| `ways_raw`, `nodes` | parse | geojson, shapefile, csv | raw ways / nodes |
| `intersections` | parse | geojson, shapefile, csv | intersection points with `legs`, `signalized`, `stops`, … |
| `boundary` | parse | geojson, shapefile, csv | the city boundary (reads the raw `boundary` input) |
| `census_blocks` | census | geojson, shapefile, csv | plain census blocks |
| `neighborhood_census_blocks` | census | geojson, shapefile, csv | blocks joined to their `scores` |
| `ways_classified` | attributes | geojson, shapefile, csv | the resolved attribute layer |
| `segments`, `trails` | segment | geojson, shapefile, csv | split segments / off-network paths |
| `blocks_with_roads` | graph | geojson, shapefile, csv | blocks with associated road IDs |
| `destinations` | destinations | geojson, csv | one file **per destination type** (fan-out) |
| `connectivity` | connectivity | csv | block-pair reachability (`t`/`f` booleans) |
| `scores` | scores | csv | per-block access scores |
| `neighborhood_scores`, `score_inputs`, `mileage` | neighborhood | csv | headline / intermediate scores, mileage |
| `speed_limits` | *config* | csv | residential speed limits from city FIPS + config |

### The `bna` bundle

`export_bundle(..., bundle="bna")` writes the full brokenspoke-analyzer deliverable set
into one directory, each target under its platform filename (`neighborhood_ways.geojson`,
`neighborhood_census_blocks.geojson`, `neighborhood_connected_census_blocks.csv`,
`residential_speed_limit.csv`, …) plus a self-describing `README.md`. `neighborhood_census_blocks`
and `stress` are **required** (a missing input raises); optional targets whose inputs are
absent are skipped with a warning.
