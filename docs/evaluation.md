# Evaluation And Benchmarks

Telchines ships with an offline benchmark suite. In a source checkout the suite lives under `benchmarks/`; installed packages use the bundled benchmark assets.

Covered task types:

- repair
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

The default suite currently includes 19 cases spanning all shipped `v1` workflows, including one optional agent-runtime repair retry case. The automated test suite verifies that the default benchmark run completes successfully and writes a report to the run store.
