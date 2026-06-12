# Telchines

<p align="center">
  <img src="docs/assets/readme-hero.svg" alt="Telchines hero banner" width="100%">
</p>

<p align="center">
  <a href="https://github.com/ivanbard/telchines/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/ivanbard/telchines/ci.yml?branch=main&style=flat-square&label=CI" alt="CI status"></a>
  <img src="https://img.shields.io/badge/version-v1.0.1-0f172a?style=flat-square" alt="Version 1.0.1">
  <img src="https://img.shields.io/badge/python-3.11%2B-0b7285?style=flat-square" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-14532d?style=flat-square" alt="MIT License">
  <img src="https://img.shields.io/badge/benchmarks-18%20cases-7c2d12?style=flat-square" alt="18 benchmark cases">
</p>

<p align="center">
  CLI-first verification workflows for hardware teams that want grounded, replayable AI instead of generic repo chat.
</p>

<p align="center">
  <a href="#quick-start"><strong>Quick Start</strong></a> |
  <a href="#what-you-can-do-with-v1"><strong>v1 Workflows</strong></a> |
  <a href="#how-it-works"><strong>How It Works</strong></a> |
  <a href="#documentation"><strong>Documentation</strong></a>
</p>

## Why Telchines

Generic coding assistants are weak at verification work because the real loop is not just text generation. It is specs, RTL, logs, tool feedback, coverage holes, prior runs, and the ability to replay what happened.

Telchines is built around that loop:

- retrieve evidence from RTL, docs, logs, and run history
- run verification-native workflows instead of generic chat prompts
- preserve replay artifacts and citations
- validate outputs through adapters and deterministic gates
- keep model routing local-first and policy-aware

> Telchines is not trying to replace simulators or formal tools. It is the orchestration layer that helps engineers move from signal to next action faster.

## What You Can Do With v1

| Workflow | What it does | Primary command |
| --- | --- | --- |
| Retrieval | Builds task-aware evidence packs from repo and run history | `tel retrieve "query"` |
| Repair | Proposes and validates minimal fixes from tool output | `tel repair --tool ... --file ...` |
| Triage | Clusters repeated regressions with evidence and history | `tel triage --logs logs/regressions` |
| Spec-to-SVA | Generates first-pass assertions from spec plus RTL | `tel gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv` |
| DUT-to-Cocotb | Scaffolds a grounded cocotb starter testbench | `tel gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md` |
| Coverage Planning | Classifies uncovered items and ranks next actions | `tel coverage-plan --report cov/coverage.json ...` |
| Waveform Debug | Parses VCDs and inspects signals from CLI or shell | `tel waveforms show ...` |
| Benchmarks | Runs the built-in offline evaluation suite | `tel eval run` |

## What It Looks Like

```text
$ tel

tel[uart] sample_project> /index
Index Complete
indexed 18 chunks

tel[uart] sample_project> /triage --logs logs/regressions
Triage Summary
run run_... produced 2 cluster(s)

1. UART receiver start-bit regressions grouped together
likely cause: missing stimulus or broken start-bit detection path
suggested action: inspect uart_rx start-bit handling and nearby waveform evidence
evidence: docs/uart.md#L1, rtl/uart_rx.sv#L1, logs/regressions/run_a.log#L1

tel[uart] sample_project> /gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
Spec-to-SVA Result
provider: heuristic
status: validated
artifact: .tel/artifacts/generated/uart_rx_assertions.sv
validation: passed
```

## How It Works

<p align="center">
  <img src="docs/assets/readme-workflow.svg" alt="Telchines workflow diagram" width="100%">
</p>

Telchines keeps the loop explicit:

1. Index the project and build retrieval context.
2. Run a verification workflow such as repair, triage, generation, or coverage planning.
3. Store evidence, artifacts, validation output, and replay metadata under `.tel/`.
4. Reuse prior runs and benchmark the behavior over time.

## Architecture Snapshot

<p align="center">
  <img src="docs/assets/readme-architecture.svg" alt="Telchines architecture diagram" width="100%">
</p>

## Quick Start

Install from source while PyPI trusted publishing is being finalized:

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -e .[dev]
```

After the PyPI publisher is configured, the package install path will be:

```bash
pip install telchines
```

Validate the install:

```bash
tel --version
telchines --help
```

Initialize and index a project:

```bash
tel project init .
tel index
tel index status
tel providers list
tel providers check heuristic
```

Run a few common workflows:

```bash
tel retrieve "uart timeout handling"
tel triage --logs logs/regressions --format human
tel gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
tel gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md --intent "smoke the start-bit path"
tel coverage-plan --report cov/coverage.json --rtl rtl/uart_rx.sv --spec docs/uart.md --format human
```

## Interactive Shell

Running `tel` with no arguments opens the shell. Use `tel shell --plain` for the stdin/stdout shell or `tel shell --fullscreen` to request the prompt_toolkit full-screen shell explicitly. Slash commands and lightweight plain-text intents are both supported.

```text
tel> /help
tel> /providers
tel> /providers check heuristic
tel> /index
tel> /index status
tel> /triage --logs logs/regressions
tel> /gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
tel> show my providers
tel> triage the regression logs
tel> /exit
```

## Adapter And Provider Model

Built-in adapters in `v1`:

- `verilator`
- `iverilog`
- `slang`
- `verible`
- `symbiyosys`

Provider kinds in `v1`:

- `heuristic`
- `openai_compatible`
- `local_command`

Policy controls:

- `model_mode=local` blocks remote providers
- `model_mode=remote` blocks local command providers
- `model_mode=hybrid` allows both
- `no_egress=true` blocks networked providers

See [docs/adapters.md](docs/adapters.md), [docs/providers.md](docs/providers.md), and [docs/generated-artifacts.md](docs/generated-artifacts.md) for the exact support contract.

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

The current default suite contains **18 cases** and is part of the `v1` credibility story, not optional polish.

## v1 Scope

Included:

- interactive shell and one-shot CLI
- project indexing and retrieval
- run storage and replay
- repair, triage, waveform inspection, `gen-sva`, `gen-cocotb`, and `coverage-plan`
- provider policy controls for `local`, `hybrid`, `remote`, and `no_egress`
- built-in benchmark suite

Not in `v1`:

- web UI
- hosted service
- enterprise-only integrations
- richer shell affordances like command palettes and `@file` mentions
- broader regression-manager integrations beyond the current adapter surface

## Documentation

- [Quickstart](docs/quickstart.md)
- [Worked Examples](docs/examples.md)
- [Provider Configuration](docs/providers.md)
- [Local LLMs](docs/local-llms.md)
- [Adapter Support And Contribution Contract](docs/adapters.md)
- [External Retrieval Policy](docs/external-retrieval-policy.md)
- [Evaluation And Benchmarks](docs/evaluation.md)
- [Compatibility Promise](docs/compatibility.md)
- [Release Checklist](docs/release-checklist.md)

## Stability Promise For v1

The `1.x` line treats these interfaces as stable unless explicitly deprecated:

- top-level CLI command names
- `.tel/config.json` layout
- run-store replay artifacts
- JSON output shape for the main workflow commands

See [docs/compatibility.md](docs/compatibility.md) for the exact boundary.

<details>
<summary><strong>Full CLI Surface</strong></summary>

- `tel`
- `tel shell`
- `tel project init`
- `tel index`
- `tel index status`
- `tel index clean`
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
- `tel runs replay RUN_ID` preview and `tel runs replay RUN_ID --yes` execution
- `tel adapters list`
- `tel adapters check`
- `tel providers list`
- `tel providers check`
- `tel artifacts purge`
- `tel doctor privacy`
- `tel eval run`
- `tel eval report`

</details>

## Development

```bash
pip install -e .[dev]
pytest
```

Project contribution and disclosure guidance:

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [SECURITY.md](SECURITY.md)
- [CHANGELOG.md](CHANGELOG.md)
