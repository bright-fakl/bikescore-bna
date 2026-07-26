# Differences from brokenspoke-analyzer

`bikescore-bna` is a pure-Python reimplementation of the PeopleForBikes
[brokenspoke-analyzer](https://github.com/PeopleForBikes/brokenspoke-analyzer) —
the reference implementation of the Bicycle Network Analysis. This section is for
readers who already know one of the two and want to understand how it relates to
the other: the same scores, computed a different way.

If you just want to understand or run `bikescore-bna`, you don't need this
section at all — start with [What bikescore measures](../what-it-measures.md) or
[How it works](../how-it-works/index.md).

## The same output, computed differently

The two implementations are built to produce the **same output**.
`bikescore-bna` targets value-for-value parity with brokenspoke-analyzer, and
each pipeline stage is validated against the reference output. What differs is
everything *around* the algorithm — the runtime, the deployment, and how the
analysis logic is expressed:

| | brokenspoke-analyzer | bikescore-bna |
|---|---|---|
| Runtime | PostgreSQL + PostGIS, SQL scripts | Pure Python, in-process |
| Deployment | Docker container + database | `pip install bikescore-bna` |
| Interface | Load a DB, run the scripts | A library function and a small CLI |
| Analysis logic | SQL | Decision tables + scenario config (rules are data) |
| State | Database | None — files in, files out |

The motivation for the reimplementation — why database-free, embeddable, and
configurable-as-data were the goals — is covered in
[Why bikescore-bna](../why-bikescore.md).

## Three kinds of difference

When you compare the two implementations, differences fall into three groups:

1. **Architectural differences that don't change the output.** Most of the
   pipeline is like this: brokenspoke runs a query in PostGIS, `bikescore-bna`
   runs the equivalent computation in NumPy / SciPy / pandas, and the results
   match. These are catalogued stage by stage, with the SQL scripts each Python
   stage replaces, in the [Stage-by-stage SQL mapping](sql-mapping.md).

2. **Intentional differences that *do* change the output.** A small, deliberate
   set: cases where `bikescore-bna` fixes a bug in the reference SQL, makes a
   better-justified pipeline-ordering choice, or differs by an irreducible
   floating-point artefact at a threshold. Every one is documented, with its
   reasoning, in [Intentional deviations](deviations.md).

3. **Everything else** is treated as a bug. The SQL reference is the ground
   truth: where the two disagree without an entry on the
   [Intentional deviations](deviations.md) page, `bikescore-bna` is considered
   wrong and the difference is something to fix.

## How parity is checked

Each stage output is compared against a ground-truth reference exported from
brokenspoke-analyzer. Aspen, Colorado, is the maintainer's manual validation
city; Washington, DC, is used for larger-scale checks. The comparison and the
frozen reference data are described under
[Validation & parity](../development/validation.md).

## In this section

- **[Stage-by-stage SQL mapping](sql-mapping.md)** — which SQL scripts each
  Python stage replaces, pipeline stage by pipeline stage.
- **[Intentional deviations](deviations.md)** — the documented cases where the
  output deliberately differs, with reasoning and an assessment of which
  implementation is more correct.
