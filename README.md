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
tel --help
telchines --help
pytest
```

## Current MVP Surface

- `tel project init`
- `tel index`
- `tel retrieve`
- `tel runs list`
- `tel runs show`
- `tel runs replay`
- `tel providers list`
- `tel repair`
- `tel triage`
- `tel eval run`
- `tel eval report`

## Notes

- The run store is filesystem-backed and lives under `.tel/`.
- Retrieval is local and provenance-aware.
- `telchines` is an equivalent fallback binary if `tel` collides with a local tool on a user machine.
- Model routing is capability-based and provider-agnostic; the default implementation is heuristic and deterministic for benchmarkable MVP behavior.

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
        "repair": "remote-repair"
      },
      "providers": {
        "heuristic": {
          "kind": "heuristic",
          "capabilities": ["repair"]
        },
        "remote-repair": {
          "kind": "openai_compatible",
          "capabilities": ["repair"],
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
        "repair": "local-repair"
      },
      "providers": {
        "heuristic": {
          "kind": "heuristic",
          "capabilities": ["repair"]
        },
        "local-repair": {
          "kind": "local_command",
          "capabilities": ["repair"],
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
