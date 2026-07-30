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

Inside an initialized Telchines project, `tel eval run` preserves that project's provider, egress, and model policy and persists `latest_eval` for `tel eval report`. Benchmark fixtures run only in disposable copied evaluation projects; Telchines enables their fixture-local commands there so the offline suite can exercise its agent and provider cases without changing the project's normal local-command policy. The report identifies this isolated fixture execution. Outside a project, `tel eval run` creates a temporary scratch project, uses local `benchmarks/` when present or bundled benchmarks otherwise, prints the report, and marks it with `project_context: "scratch"` and `report_persisted: false`. Scratch reports are intentionally not available through `tel eval report`.

Optional EDA adapters are evaluated only when they are runnable in the active host. When no compatible SVA adapter is installed, the suite scores the built-in structural validation and records `structure_only`; an unavailable or cross-host executable does not invalidate an otherwise valid offline benchmark. A compatible adapter that actually runs and fails remains a failing validation result.

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
with `validation_status`, `validation_mode`, `structural_status`,
`syntax_status`, `adapter_status`, `formal_status`, `proof_status`, and
`overall_status`. A passing fixture row is not evidence that a production
simulator, formal engine, or vendor build flow ran unless the backing field says
so.

For SVA rows, `benchmark_status` is separate from the boolean `passed` score.
When required benchmark criteria pass but optional formal execution fails, the
row is `passed_with_warnings`, the report `status` is also
`passed_with_warnings`, and `metrics.sva.formal_failure_count` is nonzero. This
prevents a benchmark score from being read as a hidden formal or proof pass.
`proof_status: not_proved` remains expected for the shipped bounded-BMC formal
smoke, including when `formal_status: passed`.

## Imported Run Replay

Imported regression records explicitly carry replayability evidence. A manifest
run with a `command` is `replayable`; one without it is
`not_replayable` with the reason preserved in the stored run and import output.
`tel runs replay RUN_ID` rejects non-replayable imports as a normal CLI input
error. It also reports a missing replay executable cleanly instead of exposing a
`FileNotFoundError` traceback.

## Release Gate Expectations

Before cutting a public release:

- the full test suite should pass
- the benchmark suite should pass
- the generated evaluation report should be inspectable and reproducible

## Current Default Suite Shape

The default suite spans all shipped `v1` workflows, including agent-runtime repair, review-gated agent, retrieval, repair, SVA, cocotb, coverage, triage, import, coverage-import, and provider-response cases. It includes a larger fixture with a filelist, include file, package, core, and top wrapper, plus UVM-like logs, vendor build logs, CI imports, and malformed/partial provider responses. Use the report's `total` field as the source of truth for the installed suite size. The automated test suite verifies that the default benchmark run completes successfully, writes a report to the run store when run inside an initialized project, and runs without mutating a non-project directory by using scratch context.
