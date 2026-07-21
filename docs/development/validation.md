# Validation

bikescore validates its output **stage by stage** against a *reference directory* — a
ground-truth set of per-stage parquets. The repo ships one: `tests/oracle/aspen`, the
frozen reference output for Aspen, Colorado (the maintainer's manual validation city).
References can also be exported from [brokenspoke-analyzer](https://github.com/PeopleForBikes/brokenspoke-analyzer),
the original SQL/PostGIS implementation.

A reference directory holds one parquet per stage output:

```
<reference>/
  parse/ways_raw.parquet   parse/nodes.parquet
  stress/stress.parquet
  scores/scores.parquet
  neighborhood/neighborhood.parquet  …
  destinations/dest_<type>.parquet   …
```

## Run validation (CLI)

Score a city and compare every stage output against the reference:

```bash
bikescore-score validate <city> --reference tests/oracle/aspen
```

`<city>` is a path to a city directory (its inputs are read from `<city>/datasets`, or
pass `--datasets DIR`). Add `--stage stress` to validate a single stage (a faster partial
run). Other options mirror `score`: `--scenario`, `--set`, `--set-file`.

The command prints a per-stage table and **exits non-zero if any stage differs**:

```
┏━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┳━━━━━━┳━━━━━━━━┓
┃ stage / case ┃ matched ┃ differing ┃ explained ┃ +comp ┃ +ref ┃ result ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━╇━━━━━━╇━━━━━━━━┩
│ stress       │ 831     │ 0         │ 0         │ 0     │ 0    │ PASS   │
└──────────────┴─────────┴───────────┴───────────┴───────┴──────┴────────┘
```

Known SQL divergences are annotated as *expected* (the `explained` column) and do not
fail the run; pass `--strict` to treat them as differences.

## Run validation (Python)

The same harness is a library function — score a city, then compare the result:

```python
from bikescore import build_config, discover_inputs, score_city
from bikescore.parity import validate_result
from bikescore.deviations import KNOWN_DEVIATIONS

result = score_city(discover_inputs("aspen-colorado/datasets"), build_config("default"))

for sp in validate_result(
    result, "tests/oracle/aspen", city="aspen-colorado", deviations=KNOWN_DEVIATIONS
):
    if sp.report is None:
        continue  # stage skipped (reference/computed file absent)
    print(sp.case, "PASS" if sp.passed else "FAIL")
    if not sp.passed:
        sp.report.print()   # detailed column/row diff
```

`validate_result` returns one [`StageParity`](#) per stage output (in pipeline order);
`stages=[...]` restricts the comparison to named stages.

## Validation report fields

Each `StageParity.report` is a `ValidationReport`:

| Field | Description |
|---|---|
| `passed` | `True` when all diffs are within tolerance or explained by known deviations |
| `rows_total` | Rows matched (inner join) and compared |
| `n_computed` / `n_reference` | Row counts on each side before the join |
| `rows_only_computed` | Keys present in output but not in reference |
| `rows_only_reference` | Keys present in reference but missing from output |
| `rows_differing` | Matched rows with a column-level difference |
| `deviation_explained_rows` | Differing rows fully accounted for by known deviations |

## Tolerance and deviations

Numeric columns compare exactly by default (`compare_dataframes(tolerance=...)` loosens
it). Known permanent divergences from the SQL reference live in `bikescore.deviations`
(`KNOWN_DEVIATIONS`) and are excluded from the pass/fail decision when passed in.

See [Differences from brokenspoke-analyzer](../how-it-works/deviations.md) for the full
list of accepted deviations and the reasoning behind each one.
