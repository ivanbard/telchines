# Contributing

Telchines is a CLI-first verification workflow platform. Contributions should preserve three product properties:

- deterministic or inspectable workflow behavior
- clear evidence and replay artifacts
- stable user-facing interfaces in the `1.x` line

## Development Setup

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -e .[dev]
pytest
```

## What To Include In A Change

- code changes for the intended behavior
- tests for user-visible behavior or schema changes
- docs for any new command, config field, workflow, or contract change

## Change Guidelines

- keep workflows replayable
- preserve provenance in retrieval and generation outputs
- do not silently widen policy or egress behavior
- prefer additive CLI changes over breaking command renames
- keep generated artifacts reviewable by a human

## Adapters

If you add or extend an adapter:

- document the tool category and validation mode
- normalize outputs into structured observations
- define failure behavior clearly when the external tool is unavailable
- add isolated tests that cover parsing and command construction

See [docs/adapters.md](docs/adapters.md) for the current support contract.

## Providers

If you add provider behavior:

- preserve policy enforcement for `model_mode` and `no_egress`
- keep request and response artifacts replayable
- document new config fields in [docs/providers.md](docs/providers.md)

## Release Expectations

Before merging release-facing changes, verify:

- `pytest` passes
- CLI help and version still work
- docs match the implemented command surface

See [docs/release-checklist.md](docs/release-checklist.md) for the full release gate.
