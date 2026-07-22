# Why bikescore-bna

`bikescore-bna` is a pure-Python reimplementation of the PeopleForBikes
[brokenspoke-analyzer](https://github.com/PeopleForBikes/brokenspoke-analyzer) — the
Bicycle Network Analysis (BNA). It produces the same Level of Traffic Stress,
connectivity, and destination-access scores from the same OpenStreetMap and US Census /
LODES inputs.

## Motivation

brokenspoke-analyzer implements the analysis as SQL running inside a PostgreSQL / PostGIS
database, orchestrated through Docker. Scoring a city means standing up a database,
loading data into it, and running the SQL scripts in order. That is a capable setup for a
hosted service, but heavyweight when all you want is the score for one city.

`bikescore-bna` reimplements the same algorithm as an ordinary Python library:

- **No database, no server, no container.** The pipeline runs in-process on
  GeoPandas / Shapely / SciPy. Inputs are files; outputs are files.
- **Embeddable.** `score_city(inputs, config)` is a plain function you can call from a
  script, a notebook, or another application, and get results back as DataFrames /
  parquet.
- **Configurable as data.** Stress thresholds, imputation defaults, scoring weights,
  destination catalogs, and the traffic-stress rules themselves live in scenario
  documents and decision tables, not in code — so you can adjust the analysis without
  editing the pipeline. See [Extensibility](reference/extensibility.md).

## How it differs from brokenspoke-analyzer

At the level of the **algorithm and the numbers**, it does not: `bikescore-bna` targets
value-for-value parity with brokenspoke-analyzer, and each stage is validated against the
reference output (Aspen, Colorado, is the manual validation city). What differs is
everything *around* the algorithm:

| | brokenspoke-analyzer | bikescore-bna |
|---|---|---|
| Runtime | PostgreSQL + PostGIS, SQL scripts | Pure Python, in-process |
| Deployment | Docker container + database | `pip install bikescore-bna` |
| Interface | Load a DB, run the scripts | A library function and a small CLI |
| Analysis logic | SQL | Decision tables + scenario config (rules are data) |
| State | Database | None — files in, files out |

There are also a **small number of intentional differences in the output**, where
`bikescore-bna` corrects a bug in the reference SQL, makes a different (and better-justified)
pipeline-ordering choice, or differs by an irreducible floating-point artefact at a
threshold. Each one is documented, with its reasoning, under
[Differences from brokenspoke-analyzer](how-it-works/deviations.md). The SQL reference is
the ground truth: where the two ever disagree without an entry on that page, `bikescore-bna`
is treating it as a bug to fix.
