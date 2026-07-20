# Customize stress

The stress model reads thresholds and defaults from the `stress` and `imputation`
sections of the config. You can change them without touching the decision rules — either
as one-off overrides or as a saved scenario.

## One-off overrides (`--set`)

```console
$ bikescore-score score ./aspen-colorado \
    --set imputation.city_default_speed=40 \
    --set imputation.default_lanes=2
```

Each `--set key=value` sets a dotted config path; values are coerced to int / float /
bool / string automatically. In Python, pass an overrides dict:

```python
from bikescore import build_config, score_city
config = build_config("default", {"imputation.city_default_speed": 40})
result = score_city(inputs, config)
```

Overrides are applied last, on top of the chosen scenario.

## A saved scenario

For a reusable change, author a scenario YAML that overlays the defaults:

```yaml
# lower-speeds.yaml
type: sparse
version: 1
description: Assume lower default speeds where OSM is silent.
config:
  imputation:
    city_default_speed: 40
```

Run it by name or path:

```console
$ bikescore-score score ./aspen-colorado --scenario lower-speeds.yaml
```

A `sparse` scenario carries only the fields it changes; everything else falls back to the
package defaults. To change the LTS decision logic itself — not just its thresholds —
see [Edit the stress rules](adjust-stress-yaml.md).
