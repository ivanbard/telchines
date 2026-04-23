# Adapter Support And Contribution Contract

Telchines uses adapters to normalize external tool execution into a common workflow surface.

## Built-In Adapters In v1

| Adapter | Category | Validation mode | Notes |
| --- | --- | --- | --- |
| `verilator` | simulation | compile-only | compile-oriented validation backend |
| `iverilog` | simulation | compile-and-run | uses `iverilog` plus `vvp` |
| `slang` | simulation | compile-only | compile-oriented validation backend |
| `verible` | lint | compile-only | parser/lint-oriented validation backend |
| `symbiyosys` | formal | structured formal summaries | stores property/report/counterexample metadata |

Use `tel adapters list` to inspect the live adapter registry and enabled status.

## v1 Support Promise

Built-in adapters in the `1.x` line should:

- construct commands deterministically
- normalize observations into structured records
- persist artifacts and replayable metadata
- fail clearly when the external tool is not installed or not usable

## Contribution Contract For New Adapters

A contributed adapter should:

- declare a clear tool category
- document whether it is compile-only or compile-and-run
- normalize output into structured observations rather than raw text blobs alone
- surface replay inputs and generated artifacts
- include tests for command construction and output parsing
- avoid hard-coding organization-specific assumptions into the shared adapter layer

## Interoperability Roadmap

Post-`v1` areas under consideration:

- broader simulator coverage
- richer regression-manager and test-runner integration points
- stronger community adapter packaging conventions

These are roadmap items, not `v1` release blockers.
