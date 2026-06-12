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

Use `tel adapters check [NAME]` to verify required binaries, availability, and detected version:

```bash
tel adapters check
tel adapters check verilator
tel adapters check --category simulation
```

The command prints JSON and exits nonzero when a selected adapter is missing required binaries.

`gen-sva` can use adapters for generated assertion validation. After built-in structural checks pass, Telchines tries `generation.sva.validation_adapters` in order, defaulting to `slang` then `verilator`. The first enabled adapter available on `PATH` and supporting `generation_validation` is run against the DUT RTL plus generated SVA file. If no adapter can run, Telchines reports `builtin_structural` validation and records adapter fallback reasons in the validation run metadata.

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

## Real Tool Smoke Tests

Mocked tests cover command construction and parsing. For real binaries, run:

```bash
python scripts/tool_smoke.py --adapters verilator iverilog
```

The script creates tiny SystemVerilog fixtures in a temporary directory and runs:

- `verilator --lint-only` through the Verilator adapter
- `iverilog` plus `vvp` through the Icarus adapter

There is also a manual GitHub Actions workflow, `.github/workflows/tool-smoke.yml`, that installs Verilator and Icarus on Ubuntu and runs the same script. It is intentionally `workflow_dispatch` so normal CI remains lightweight while real-tool checks are available before releases.
