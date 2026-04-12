# Telchines

MVP scaffold for Telchines, a CLI-first verification workflow platform focused on:

- structured run storage
- repo/spec/log retrieval
- open-tool adapter abstractions
- compile repair
- regression triage
- replayable evaluation

## Quick Start

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -e .[dev]
tel
telchines --help
pytest
```

Running `tel` with no arguments now opens the interactive Telchines shell. One-shot commands still work when you pass arguments such as `tel repair ...` or `tel providers list`.

## Current MVP Surface

- `tel`
- `tel shell`
- `tel project init`
- `tel index`
- `tel retrieve`
- `tel runs list`
- `tel runs show`
- `tel runs replay`
- `tel adapters list`
- `tel providers list`
- `tel repair`
- `tel triage`
- `tel waveforms list`
- `tel waveforms show`
- `tel waveforms signals`
- `tel waveforms inspect`
- `tel gen-sva`
- `tel eval run`
- `tel eval report`

## Interactive Shell

Telchines now supports a persistent full-screen shell with a console-first layout, compact status context, slash commands, and lightweight plain-text prompts:

```text
tel> /help
tel> /providers
tel> /index
tel> /triage --logs logs/regressions
tel> /waveforms show logs/regressions/uart_rx_trace.vcd
tel> /gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
tel> show my providers
tel> triage the regression logs
tel> /exit
```

Use `tel shell` to enter the same experience explicitly.

## Notes

- The run store is filesystem-backed and lives under `.tel/`.
- Retrieval is local and provenance-aware.
- `tel adapters list` now reports compile-only vs compile-and-run validation backends.
- `telchines` is an equivalent fallback binary if `tel` collides with a local tool on a user machine.
- Model routing is capability-based and provider-agnostic; the default implementation is heuristic and deterministic for benchmarkable MVP behavior.
- The first spec-to-SVA flow writes a generated assertion artifact and returns a concise inline property summary.

## Spec-to-SVA

Telchines now supports a first spec-to-SVA workflow:

```bash
tel gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
```

The command:

- builds an SVA-specific retrieval context from the spec and RTL target
- routes generation through the configured `generation` provider capability
- writes a generated assertion artifact under `.tel/artifacts/generated/`
- persists request, response, replay, and validation artifacts in the run store
- validates the generated artifact through the built-in SVA syntax gate

Use `--output` to override the default generated artifact path and `--provider` to pick a specific configured generation provider.

## Waveform Debug

Telchines now supports a first waveform-aware debug slice with native VCD parsing and shell/CLI inspection commands:

```bash
tel triage --logs logs/regressions --waveform logs/regressions/uart_rx_trace.vcd
tel waveforms list
tel waveforms show logs/regressions/uart_rx_trace.vcd
tel waveforms signals logs/regressions/uart_rx_trace.vcd --filter start
tel waveforms inspect logs/regressions/uart_rx_trace.vcd --signal start_seen
```

The current implementation:

- parses VCD metadata and signal transitions into `.tel/waveforms/`
- auto-discovers nearby VCD files during triage when present under the same log tree
- attaches waveform-backed evidence summaries to triage clusters
- renders signal inventory and simple transition timelines in the shell and one-shot CLI

## Adapter Validation Modes

Telchines distinguishes compile-only validation from compile-and-run validation:

- `verilator`, `slang`, and `verible` are compile-oriented validation backends
- `iverilog` uses `iverilog` plus `vvp` as a run-capable validation backend
- `symbiyosys` persists structured formal summaries such as property IDs, reports, and counterexample paths

Inspect the current adapter surface with:

```bash
tel adapters list
tel adapters list --category simulation
```

## Model Provider Setup

Telchines can route repair requests to:

- the built-in `heuristic` provider
- an `openai_compatible` hosted endpoint
- a `local_command` provider that reads JSON on stdin and writes JSON on stdout

Example hosted provider config:

```json
{
  "model_mode": "hybrid",
  "no_egress": false,
  "project": {
    "model_policy": {
      "default_provider_by_capability": {
        "repair": "remote-repair",
        "generation": "remote-repair"
      },
      "providers": {
        "heuristic": {
          "kind": "heuristic",
          "capabilities": ["repair", "generation"]
        },
        "remote-repair": {
          "kind": "openai_compatible",
          "capabilities": ["repair", "generation"],
          "base_url": "https://example-provider.local/v1",
          "model": "demo-model",
          "api_key_env": "TELCHINES_API_KEY",
          "timeout_seconds": 30
        }
      }
    }
  }
}
```

Example local command provider config:

```json
{
  "model_mode": "hybrid",
  "project": {
    "model_policy": {
      "default_provider_by_capability": {
        "repair": "local-repair",
        "generation": "local-repair"
      },
      "providers": {
        "heuristic": {
          "kind": "heuristic",
          "capabilities": ["repair", "generation"]
        },
        "local-repair": {
          "kind": "local_command",
          "capabilities": ["repair", "generation"],
          "command": "python",
          "args": ["tools/local_provider.py"],
          "timeout_seconds": 30
        }
      }
    }
  }
}
```

`model_mode=local` blocks remote providers, `model_mode=remote` blocks local command providers, and `no_egress=true` blocks all networked providers. Use `tel providers list` to inspect configured providers, defaults, and policy blockers.

## GitHub Backlog Automation

- Backlog definition: `ops/github-backlog.json`
- Local sync script: `scripts/sync-github-backlog.ps1`
- Manual GitHub Action: `.github/workflows/sync-backlog.yml`

Example local dry run:

```powershell
./scripts/sync-github-backlog.ps1 -Repo OWNER/REPO -WhatIf
```
