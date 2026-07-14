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
Missing open-source tools include `setup_diagnostics` with platform-oriented hints. Telchines only reports these
instructions; it does not install simulators, linters, formal engines, or commercial tools for you.

Common open-source setup routes:

- Verilator: install from your Linux package manager, build from upstream, or use the MSYS2 UCRT64 package on Windows.
- Slang: install a `slang` command from upstream prebuilt releases or build it from source. If the CLI is not available, Telchines can fall back to `pyslang`; install it with `python -m pip install -e ".[slang-smoke]"` or `python -m pip install pyslang`.
- SymbiYosys: install `sby` through OSS CAD Suite or from the YosysHQ `sby` source tree with Yosys and solvers available.

After setup, restart the shell and rerun:

```bash
tel adapters check verilator
tel adapters check slang
tel adapters check symbiyosys
```

Adapter-backed commands accept compile context through `--filelist`, `--include-dir`, `--define`, `--top`, `--worklib`, and `--adapter-arg`. Filelists support blank lines, comments, source paths, `+incdir+...`, and `+define+...`. Telchines stores the expanded run spec, command argv, cwd, adapter version, and redacted env summary in run metadata.

`gen-sva` can use adapters for generated assertion validation. After built-in structural checks pass, Telchines tries `generation.sva.validation_adapters` in order, defaulting to `slang` then `verilator`. The first enabled adapter available on `PATH` and supporting `generation_validation` is run against the DUT RTL plus generated SVA file. If no adapter can run, Telchines reports `structure_only` validation and records adapter fallback reasons in the validation run metadata. When `generation.sva.formal.mode` is `auto` or `required`, Telchines can also run the configured formal adapter, defaulting to SymbiYosys, and reports `formal_run`.

Commercial formal or simulator tools should integrate through a wrapper-command adapter that emits stable exit codes, logs, and artifact paths. Native vendor adapters are intentionally out of scope for the v1 real-tool pass.

## v1 Support Promise

Built-in adapters in the `1.x` line should:

- construct commands deterministically
- normalize observations into structured records
- persist artifacts and replayable metadata
- fail clearly when the external tool is not installed or not usable

The shared parser recognizes common Verilator, Icarus, Slang, Verible, and SymbiYosys-style diagnostics, including line/column forms such as `%Error: file:line:column`, `file:line:column: syntax error`, Slang's `error: file:line:column`, and assertion failure summaries. Parser fixtures live in `tests/test_adapters.py`; add real output samples there whenever adapter support is expanded.

## Contribution Contract For New Adapters

A contributed adapter should:

- declare a clear tool category
- document whether it is compile-only or compile-and-run
- normalize output into structured observations rather than raw text blobs alone
- surface replay inputs and generated artifacts
- include tests for command construction and output parsing
- avoid hard-coding organization-specific assumptions into the shared adapter layer

## Adapter Request Checklist

Open one issue per proposed tool adapter. Include:

- target tool name, version range, and category
- workflow it should support, such as repair validation, generation validation, or formal validation
- command construction contract, including required binaries and key arguments
- expected artifacts, such as logs, reports, executables, traces, or counterexamples
- representative parser samples from real output
- mocked tests for command construction and parsing
- optional real-tool smoke path when the dependency is practical in CI or a manual workflow

## Interoperability Roadmap

Telchines v1 keeps the integration surface deliberately small: log-directory triage, explicit tool adapters, replayable run-store artifacts, and generated artifact review. Larger integrations should build on those surfaces rather than bypass them.

Post-`v1` work should be tracked as focused follow-ups:

- adapter expansion for specific tools, with parser fixtures and command-contract tests
- a normalized regression manager or test-runner import manifest for larger verification environments
- shell UX polish, in order: richer option completion, project-aware `@file` insertion, then command-palette style interactions
- stronger community adapter packaging conventions

These are roadmap items, not `v1` release blockers.

## Real Tool Smoke Tests

Mocked tests cover command construction and parsing. For real binaries, run:

```bash
python scripts/tool_smoke.py --adapters verilator iverilog slang symbiyosys --allow-missing
```

The script creates tiny SystemVerilog fixtures in a temporary directory and runs:

- `verilator --lint-only` through the Verilator adapter
- `iverilog` plus `vvp` through the Icarus adapter
- `slang --lint-only` through the Slang adapter, or the `pyslang` fallback when the CLI is absent
- `sby` against a bounded `smoke_counter.sby` file through the SymbiYosys adapter
- filelist/include/define/top command construction for adapters that support it

To include Telchines' executable cocotb smoke path in the same manual check, install the optional dependency and add `--cocotb`:

```bash
python -m pip install -e ".[cocotb-smoke]"
python scripts/tool_smoke.py --adapters iverilog --cocotb
```

The cocotb lane creates a tiny counter DUT, validates a generated cocotb test with `generation.cocotb.executable_smoke=required`, and reports setup diagnostics when cocotb, its makefiles, `make`, Icarus, `vvp`, or the cocotb simulator binding are unavailable. It records separate Python, structure, simulator-compile, simulator-launch, cocotb-init, and test-result stages. Telchines accepts cocotb makefile discovery through `cocotb-config` or `python -m cocotb_tools.config`, which helps Python 3.13 environments where the console script shim is absent.

There is also a manual GitHub Actions workflow, `.github/workflows/tool-smoke.yml`, that installs Verilator and Icarus on Ubuntu and runs the same script. It is intentionally `workflow_dispatch` so normal CI remains lightweight while real-tool checks are available before releases.
