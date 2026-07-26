# Inspect the LTS network

Level of Traffic Stress (LTS) is assigned per road segment in the `stress` stage. This
walk-through reads that output directly. For the model behind it, see
[Level of Traffic Stress](../how-it-works/stress.md); for how roads become segments in
the first place, see [Segmenting the road network](../how-it-works/segmenting.md).

## Run to the stress stage

You only need the pipeline up to `stress`:

```console
$ bikescore-bna score ./aspen-colorado --to stress
```

or in Python:

```python
from bikescore_bna import build_config, score_city
result = score_city(inputs, build_config("default"), to_stage="stress")
```

`--to`/`to_stage` stops after the named stage — a fast, partial run. See
[`score`](../reference/cli.md#score).

## Read the segments

```python
import pandas as pd
seg = pd.read_parquet(result.output("stress", "stress.parquet"))
print(seg[["road_id", "ft_seg_stress", "tf_seg_stress"]].head())
```

Each segment carries a directional stress level (`ft` = from→to, `tf` = to→from) on a
1–4 scale, where **1–2 is low-stress** (comfortable for most riders) and **3–4 is
high-stress**. The columns and their meaning are catalogued in
[Output files](../reference/output-files.md); the model itself is described under
[Level of Traffic Stress](../how-it-works/stress.md).

## View it in a GIS tool

Instead of joining tables in code, export the LTS network to GeoJSON and open it in
QGIS, geojson.io, or any GIS tool. The `stress` export target is the road segments
with their LTS values:

```python
from bikescore_bna import export_target
export_target(result, city, build_config("default"), "stress", "export/",
              file_format="geojson", inputs=inputs)
# wrote export/stress.geojson
```

or from the command line:

```console
$ bikescore-bna export ./aspen-colorado --target stress --format geojson --out ./export
wrote export/stress.geojson
```

Open `export/stress.geojson` and style it by `ft_seg_stress` / `tf_seg_stress` to see
the low-stress network at a glance. The full [`bna` bundle](../reference/output-files.md#the-bna-bundle)
additionally writes `neighborhood_ways.geojson` — the same network under its
PeopleForBikes platform filename — alongside the scored census blocks.

## Map it in code

Alternatively, join the segments to the routing network geometry produced by the
`graph` stage (`graph/graph.parquet`, `graph/nodes.parquet`), load both with GeoPandas,
and colour by `*_seg_stress`. The connected 1–2 subgraph is what the `connectivity`
stage routes over — see [Routing network](../how-it-works/routing-network.md) and
[Connectivity](../how-it-works/connectivity.md).

## Related pages

- **How it works:** [Segmenting the road network](../how-it-works/segmenting.md) ·
  [Level of Traffic Stress](../how-it-works/stress.md) ·
  [Routing network](../how-it-works/routing-network.md) ·
  [Connectivity](../how-it-works/connectivity.md)
- **Reference:** [CLI](../reference/cli.md) · [Output files](../reference/output-files.md) ·
  [Python API](../reference/api.md)
- **Related:** [Customize stress](customize-stress.md) ·
  [Edit the stress rules](adjust-stress-yaml.md)
