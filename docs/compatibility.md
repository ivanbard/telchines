# Compatibility Promise

Telchines `1.x` is a CLI-first product line. The following interfaces are treated as stable unless explicitly deprecated in the changelog.

## Stable Interfaces

- top-level CLI command names
- `.tel/config.json` field names and general layout
- run-store replay artifacts used by `tel runs replay`; executing a stored replay command requires explicit confirmation with `--yes`
- JSON output contracts for:
  - `retrieve`
  - `repair`
  - `triage`
  - `gen-sva`
  - `gen-cocotb`
  - `coverage-plan`
  - `providers list`
  - `adapters list`
  - `eval report`

Benchmark reports may add new case types, per-case calibration fields, and
aggregate readiness counters. Existing `eval report` consumers should treat
unknown fields as additive metadata and use each report's `total`,
`validation_mode`, `validation_status`, and `execution_backing` fields rather
than assuming a fixed suite size or tool-backed execution.

## What May Change In A Backward-Compatible Way

- additional JSON fields
- additional providers or adapters
- expanded benchmark coverage
- richer shell presentation and formatting

## What Requires Explicit Deprecation

- renaming an existing command
- removing a documented config field
- changing the meaning of `model_mode` or `no_egress`
- changing required JSON keys for a documented workflow output

## Non-Goals For Compatibility

`v1` does not promise stability for:

- internal Python module APIs
- undocumented run-store internals beyond replay-relevant artifacts
- roadmap shell affordances that have not shipped yet
