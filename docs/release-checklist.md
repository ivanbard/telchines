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
- verify `tel eval run` executes 18 bundled benchmark cases from the clean install

## Docs

- confirm README quickstart still matches the actual CLI
- update `CHANGELOG.md`
- review provider and adapter docs for new fields or contracts

## Release Artifacts

- confirm the PyPI trusted publisher matches the `pypi` GitHub environment and `.github/workflows/publish-pypi.yml`
- tag the release as `vX.Y.Z`
- publish GitHub release notes
- publish to PyPI

## Manual Smoke Checks

- `tel project init .`
- `tel index`
- `tel retrieve "query"`
- one workflow command such as `tel triage --logs logs/regressions`
