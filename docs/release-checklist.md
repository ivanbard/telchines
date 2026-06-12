# Release Checklist

## Code And Tests

- run `pytest`
- run `tel eval run`
- verify `tel --version`
- verify `tel --help`
- run `tel providers check heuristic`
- optionally run `pytest tests/test_provider_integration.py` with `TELCHINES_INTEGRATION_OPENAI_*` set
- run `tel index status` before and after `tel index`
- run `tel doctor privacy`
- run `tel adapters check --category simulation` or note unavailable local tools

## Packaging

- build sdist and wheel
- install the built package in a clean environment
- verify both `tel` and `telchines` entrypoints
- verify `tel eval run` executes 18 bundled benchmark cases from the clean install
- confirm the CI package job's clean-wheel smoke test passed

## Docs

- confirm README quickstart still matches the actual CLI
- update `CHANGELOG.md`
- review provider and adapter docs for new fields or contracts
- review `docs/generated-artifacts.md` when generation validation or convention fields change
- review `docs/local-llms.md` when provider examples change

## Release Artifacts

- confirm the PyPI trusted publisher matches the `pypi` GitHub environment and `.github/workflows/publish-pypi.yml`
- tag the release as `vX.Y.Z`
- publish GitHub release notes
- publish to PyPI

## Manual Smoke Checks

- `tel project init .`
- `tel index`
- `tel index status`
- `tel retrieve "query"`
- one workflow command such as `tel triage --logs logs/regressions`
- `tel runs replay RUN_ID` and confirm it previews without execution
- one generation command such as `tel gen-cocotb --dut rtl/uart_rx.sv` and inspect `validation_mode`
- one SVA generation command and confirm `validation_mode` is `adapter_backed` when Slang or Verilator is installed, otherwise `builtin_structural` with fallback reasons
- `tel artifacts purge` as a dry run
- `python scripts/tool_smoke.py --adapters verilator iverilog` when those tools are installed
