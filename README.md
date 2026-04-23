# Telchines

Telchines is a CLI-first verification workflow platform for hardware teams that want grounded, replayable AI assistance instead of generic repo chat.

It focuses on:

- provenance-aware retrieval over RTL, specs, logs, and prior runs
- replayable run storage under `.tel/`
- compile-repair and regression triage workflows
- spec-to-SVA and DUT-to-cocotb generation
- coverage-closure recommendations
- local-first, provider-agnostic model routing
- benchmarkable release validation

## v1 Status

`v1.0.0` is the first public, CLI-first release target.

Included in `v1`:

- interactive shell and one-shot CLI
- project indexing and retrieval
- run storage and replay
- repair, triage, waveform inspection, `gen-sva`, `gen-cocotb`, and `coverage-plan`
- provider policy controls for `local`, `hybrid`, `remote`, and `no_egress`
- built-in benchmark suite

Explicitly out of scope for `v1`:

- web UI
- hosted service
- enterprise-only integrations
- command palette and richer shell UX affordances
- regression manager integrations beyond the current adapter surface

## Install

From PyPI:

```bash
pip install telchines
```

From source:

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -e .[dev]
```

Validate the install:

```bash
tel --version
telchines --help
```

## Quick Start

Initialize a project, build the retrieval index, and inspect the configured providers:

```bash
tel project init .
tel index
tel providers list
```

Run a few common workflows:

```bash
tel retrieve "uart timeout handling"
tel triage --logs logs/regressions --format human
tel gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
tel gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md --intent "smoke the start-bit path"
tel coverage-plan --report cov/coverage.json --rtl rtl/uart_rx.sv --spec docs/uart.md --format human
```

Launch the interactive shell:

```bash
tel
```

## CLI Surface

- `tel`
- `tel shell`
- `tel project init`
- `tel index`
- `tel retrieve`
- `tel repair`
- `tel triage`
- `tel coverage-plan`
- `tel gen-sva`
- `tel gen-cocotb`
- `tel waveforms list`
- `tel waveforms show`
- `tel waveforms signals`
- `tel waveforms inspect`
- `tel runs list`
- `tel runs show`
- `tel runs replay`
- `tel adapters list`
- `tel providers list`
- `tel eval run`
- `tel eval report`

## Interactive Shell

Running `tel` with no arguments opens the shell. Slash commands and lightweight plain-text intents are both supported.

```text
tel> /help
tel> /providers
tel> /index
tel> /triage --logs logs/regressions
tel> /gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
tel> show my providers
tel> triage the regression logs
tel> /exit
```

## Supported Workflows

### Retrieval

`tel retrieve` builds a task-aware evidence pack from indexed project artifacts and stores the retrieval context for replay.

### Repair

`tel repair` runs a tool adapter, stores observations, routes patch generation through the configured repair provider, and optionally applies the validated patch.

### Triage

`tel triage` clusters repeated failures, attaches evidence hits, surfaces similar historical runs, and optionally links waveform/formal evidence.

### Spec-to-SVA

`tel gen-sva` generates a candidate SVA artifact, stores request/response/replay artifacts, and validates the result through the built-in syntax gate.

### DUT-to-Cocotb

`tel gen-cocotb` generates a starter cocotb scaffold and manifest, records generation artifacts, and validates the emitted Python with `py_compile`.

### Coverage Planning

`tel coverage-plan` ingests normalized coverage JSON, classifies uncovered items, and emits cited next-step recommendations.

### Waveform Inspection

`tel waveforms` commands parse and persist VCD summaries, list signals, and inspect transition windows from the CLI or shell.

## Adapter Surface

Current built-in adapters:

- `verilator`
- `iverilog`
- `slang`
- `verible`
- `symbiyosys`

Current validation modes:

- compile-oriented: `verilator`, `slang`, `verible`
- compile-and-run: `iverilog`
- structured formal summaries: `symbiyosys`

Inspect the live adapter surface with:

```bash
tel adapters list
tel adapters list --category simulation
tel adapters list --category formal
```

See [docs/adapters.md](docs/adapters.md) for support expectations and contribution rules.

## Provider Model

Telchines supports three provider kinds:

- `heuristic`
- `openai_compatible`
- `local_command`

Provider routing is capability-based. `repair` and `generation` can point at different providers.

Policy controls:

- `model_mode=local` blocks remote providers
- `model_mode=remote` blocks local command providers
- `model_mode=hybrid` allows both, subject to capability routing
- `no_egress=true` blocks all networked providers

See [docs/providers.md](docs/providers.md) for configuration examples.

## Benchmarks And Release Validation

Telchines ships with an offline benchmark suite covering:

- repair
- triage
- retrieval
- spec-to-SVA
- DUT-to-cocotb
- coverage planning

Run it with:

```bash
tel eval run
tel eval report
```

See [docs/evaluation.md](docs/evaluation.md) for expected outputs and release-gate usage.

## Stability Promise For v1

The `1.x` line treats these interfaces as stable unless explicitly deprecated:

- top-level CLI command names
- `.tel/config.json` layout
- run-store replay artifacts
- JSON output shape for the main workflow commands

Details are documented in [docs/compatibility.md](docs/compatibility.md).

## Documentation

- [Quickstart](docs/quickstart.md)
- [Worked Examples](docs/examples.md)
- [Provider Configuration](docs/providers.md)
- [Adapter Support And Contribution Contract](docs/adapters.md)
- [External Retrieval Policy](docs/external-retrieval-policy.md)
- [Evaluation And Benchmarks](docs/evaluation.md)
- [Compatibility Promise](docs/compatibility.md)
- [Release Checklist](docs/release-checklist.md)

## Development

```bash
pip install -e .[dev]
pytest
```

Project contribution and disclosure guidelines:

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [SECURITY.md](SECURITY.md)

## GitHub Backlog Automation

- backlog definition: `ops/github-backlog.json`
- local sync script: `scripts/sync-github-backlog.ps1`
- manual workflow: `.github/workflows/sync-backlog.yml`

Dry run:

```powershell
./scripts/sync-github-backlog.ps1 -Repo OWNER/REPO -WhatIf
```
