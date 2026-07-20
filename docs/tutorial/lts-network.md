# Inspect the LTS network

Level of Traffic Stress (LTS) is assigned per road segment in the `stress` stage. This
walk-through reads that output directly.

## Run to the stress stage

You only need the pipeline up to `stress`:

```console
$ bikescore-score score ./aspen-colorado --to stress
```

or in Python:

```python
from bikescore import build_config, score_city
result = score_city(inputs, build_config("default"), to_stage="stress")
```

`--to`/`to_stage` stops after the named stage — a fast, partial run.

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

## Map it

The segments join back to the routing network geometry produced by the `graph` stage
(`graph/graph.parquet`, `graph/nodes.parquet`). Load both with GeoPandas and colour by
`*_seg_stress` to see the low-stress network — the connected 1–2 subgraph is what the
`connectivity` stage routes over.
