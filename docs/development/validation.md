# Validation

!!! note "Internal use"
    Validation requires a running [brokenspoke-analyzer](https://github.com/PeopleForBikes/brokenspoke-analyzer)
    instance and a reference export tool that is not yet publicly released.
    This page is intended for bikescore contributors verifying correctness
    against the SQL reference implementation.

bikescore can validate its output stage-by-stage against reference parquets
exported from brokenspoke-analyzer (the original SQL/PostGIS implementation).

## Export reference data

Export reference parquets from a running brokenspoke-analyzer instance:

```bash
uv run python tools/export_reference.py washington-district-of-columbia --all
```

Reference parquets land in `tests/reference/{slug}/stages/`.

## Run validation (CLI)

```bash
bikescore validate washington-district-of-columbia --stage stress
```

Output is a Markdown report showing row coverage, differing values, and any
declared deviations.

## Run validation (Python)

```python
from bikescore import Pipeline, BNAConfig
from bikescore.city import City
from bikescore.validation import Reference

city = City(name="washington", country="united states", region="district of columbia")
pipeline = Pipeline(city, BNAConfig.with_defaults(), cache_dir=".")
pipeline.load_from_cache("stress")

ref = Reference("tests/reference/washington-district-of-columbia")
reports = pipeline.validate("stress", ref)
for r in reports:
    r.print_summary()
```

## Validation report fields

| Field | Description |
|---|---|
| `passed` | `True` when all diffs are within tolerance or declared as known deviations |
| `n_computed` / `n_reference` | Row counts |
| `rows_only_computed` | Rows present in output but not in reference |
| `rows_only_reference` | Rows present in reference but missing from output |
| `rows_differing` | Rows present in both but with column-level differences |
| `deviation_explained_rows` | Rows accounted for by known deviations |

## Tolerance and deviations

Numeric columns are compared with a configurable tolerance (default `1e-4`).
Known permanent divergences from the reference implementation are declared in
`ValidationReport.deviations` and excluded from the pass/fail decision.

See [Differences from brokenspoke-analyzer](../how-it-works/deviations.md) for
the full list of accepted deviations and the reasoning behind each one.
