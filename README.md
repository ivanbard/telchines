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
- `tel repair`
- `tel triage`
- `tel eval run`
- `tel eval report`

## Notes

- The run store is filesystem-backed and lives under `.tel/`.
- Retrieval is local and provenance-aware.
- `telchines` is an equivalent fallback binary if `tel` collides with a local tool on a user machine.
- Model routing is abstracted behind a repair provider; the default implementation is heuristic and deterministic for benchmarkable MVP behavior.
