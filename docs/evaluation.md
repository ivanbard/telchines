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
- replayable release-over-release checks

## Release Gate Expectations

Before cutting a public release:

- the full test suite should pass
- the benchmark suite should pass
- the generated evaluation report should be inspectable and reproducible

## Current Default Suite Shape

The default suite spans all shipped `v1` workflows, including agent-runtime repair, review-gated agent, retrieval, repair, SVA, cocotb, coverage, triage, import, and coverage-import cases. Use the report's `total` field as the source of truth for the installed suite size. The automated test suite verifies that the default benchmark run completes successfully and writes a report to the run store when run inside an initialized project.
