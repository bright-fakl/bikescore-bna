# Why bikescore-bna

The [Bicycle Network Analysis](what-it-measures.md) measures how bikeable a city
is. Running it, historically, has meant standing up a database. `bikescore-bna`
reimplements it as an ordinary Python library, so computing a city's bike score
is a function call.

## The problem it solves

The reference implementation of the analysis — PeopleForBikes'
[brokenspoke-analyzer](https://github.com/PeopleForBikes/brokenspoke-analyzer) —
runs the whole thing as SQL inside a PostgreSQL / PostGIS database, orchestrated
through Docker. Scoring a city means provisioning a database, loading data into
it, and running the SQL scripts in order.

That is a capable, production-grade setup for a hosted service. It is also
heavyweight when all you want is the score for one city — to explore a "what if we
added a protected lane here?" scenario, to run the analysis inside a notebook, or
to embed scoring in a larger application.

## The design goals

`bikescore-bna` reimplements the exact same analysis as an ordinary Python
library, built around three goals:

- **No database, no server, no container.** The pipeline runs in-process on
  GeoPandas / Shapely / SciPy. Inputs are files; outputs are files. Installation
  is `pip install bikescore-bna`.

- **Embeddable.** `score_city(inputs, config)` is a plain function you can call
  from a script, a notebook, or another application, and get results back as
  DataFrames / parquet. There is no shared state to manage between runs.

- **Configurable as data.** Stress thresholds, imputation defaults, scoring
  weights, destination catalogs, and the traffic-stress rules themselves live in
  scenario documents and decision tables, not in code. You can adjust the analysis
  — or model a policy scenario — without editing the pipeline. See
  [Extensibility](reference/extensibility.md).

## What it deliberately keeps identical

The one thing `bikescore-bna` does *not* change is the analysis itself. It targets
value-for-value parity with brokenspoke-analyzer, and every pipeline stage is
validated against the reference output. A score from `bikescore-bna` is meant to
be the score you would have gotten from the original SQL implementation — just
computed without the database.

Where the two implementations differ — the SQL scripts each Python stage replaces,
and the small set of places the output *intentionally* differs (bug fixes,
better-justified ordering choices, irreducible floating-point artefacts) — is
documented in full in
[Differences from brokenspoke-analyzer](differences/index.md).

## Where to go next

- [What bikescore measures](what-it-measures.md) — the scores, in plain language.
- [How it works](how-it-works/index.md) — the pipeline, stage by stage.
- [Differences from brokenspoke-analyzer](differences/index.md) — how this relates to the reference implementation.
