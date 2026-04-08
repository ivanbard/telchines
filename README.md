# Open Verification AI Platform

MVP scaffold for an open-source, CLI-first verification workflow platform focused on:

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
ovai --help
pytest
```

## Current MVP Surface

- `ovai project init`
- `ovai index`
- `ovai retrieve`
- `ovai runs list`
- `ovai runs show`
- `ovai runs replay`
- `ovai repair`
- `ovai triage`
- `ovai eval run`
- `ovai eval report`

## Notes

- The run store is filesystem-backed and lives under `.ovai/`.
- Retrieval is local and provenance-aware.
- Model routing is abstracted behind a repair provider; the default implementation is heuristic and deterministic for benchmarkable MVP behavior.
