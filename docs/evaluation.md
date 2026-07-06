# Evaluation And Benchmarks

Telchines ships with an offline benchmark suite. In a source checkout the suite lives under `benchmarks/`; installed packages use the bundled benchmark assets.

Covered task types:

- repair
- agent
- triage
- retrieval
- SVA generation
- cocotb generation
- coverage planning
- CI/regression import
- coverage import
- provider-response handling

## Run The Suite

```bash
tel eval run
tel eval report
```

Inside an initialized Telchines project, `tel eval run` runs with that project's policy/configuration and persists `latest_eval` for `tel eval report`. Outside a project, `tel eval run` creates a temporary scratch project, uses local `benchmarks/` when present or bundled benchmarks otherwise, prints the report, and marks it with `project_context: "scratch"` and `report_persisted: false`. Scratch reports are intentionally not available through `tel eval report`.

## What The Suite Validates

- end-to-end workflow execution
- retrieval grounding quality
- artifact generation and validation
- coverage recommendation quality
- UVM-like and vendor build log parsing
- CI import normalization for JUnit, GitHub Actions, and Jenkins fixtures
- coverage import normalization for UCIS JSON and vendor-style text fixtures
- malformed or partial local provider responses fail with explicit provider-error rows
- replayable release-over-release checks

The report includes `metrics.readiness` plus per-case `benchmark_scope` and
`execution_backing` fields. Treat these as calibration labels: most default
cases are deterministic fixtures, and generated-code pass rates should be read
with `validation_status`, `validation_mode`, `formal_status`, and
`executable_status`. A passing fixture row is not evidence that a production
simulator, formal engine, or vendor build flow ran unless the backing field says
so.

## Release Gate Expectations

Before cutting a public release:

- the full test suite should pass
- the benchmark suite should pass
- the generated evaluation report should be inspectable and reproducible

## Current Default Suite Shape

The default suite spans all shipped `v1` workflows, including agent-runtime repair, review-gated agent, retrieval, repair, SVA, cocotb, coverage, triage, import, coverage-import, and provider-response cases. It includes a larger fixture with a filelist, include file, package, core, and top wrapper, plus UVM-like logs, vendor build logs, CI imports, and malformed/partial provider responses. Use the report's `total` field as the source of truth for the installed suite size. The automated test suite verifies that the default benchmark run completes successfully, writes a report to the run store when run inside an initialized project, and runs without mutating a non-project directory by using scratch context.
