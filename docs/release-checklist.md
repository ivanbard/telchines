# Release Checklist

## Code And Tests

- run `pytest`
- run `tel eval run`
- verify `tel --version`
- verify `tel --help`

## Packaging

- build sdist and wheel
- install the built package in a clean environment
- verify both `tel` and `telchines` entrypoints

## Docs

- confirm README quickstart still matches the actual CLI
- update `CHANGELOG.md`
- review provider and adapter docs for new fields or contracts

## Release Artifacts

- tag the release as `vX.Y.Z`
- publish GitHub release notes
- publish to PyPI

## Manual Smoke Checks

- `tel project init .`
- `tel index`
- `tel retrieve "query"`
- one workflow command such as `tel triage --logs logs/regressions`
