# Release Checklist

## Code And Tests

- run `pytest`
- run `tel eval run`
- verify `tel --version`
- verify `tel --help`
- run the plain shell smoke through `python -m telchines shell --plain`
- run `pytest tests/test_shell.py` to cover shell parser, completion, and the full-screen prompt_toolkit harness
- run `tel providers check heuristic`
- run `python scripts/provider_capability_study.py --matrix docs/provider-matrices/local_command.json --dry-run`
- optionally run `pytest tests/test_provider_integration.py` with provider matrix live env vars set
- optionally run `python scripts/provider_capability_study.py --matrix docs/provider-matrices/openrouter.json --include-live` with `TELCHINES_LIVE_OPENROUTER=1`
- optionally run `python scripts/provider_capability_study.py --matrix docs/provider-matrices/anthropic.json --include-live` with `TELCHINES_LIVE_ANTHROPIC=1`
- run `tel index status` before and after `tel index`
- run `tel doctor privacy`
- run `tel adapters check --category simulation` or note unavailable local tools

## Packaging

- build sdist and wheel
- install the built package in a clean environment
- verify both `tel` and `telchines` entrypoints
- verify `tel eval run` reports a nonzero bundled benchmark total from the clean install
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
- `python -m telchines shell --plain` with `/pwd`, `/providers`, an invalid command, and EOF or `/exit`
- one workflow command such as `tel triage --logs logs/regressions`
- `tel runs doctor`
- `tel runs replay RUN_ID` and confirm it previews without execution
- one generation command such as `tel gen-cocotb --dut rtl/uart_rx.sv` and inspect `validation_mode`
- one SVA generation command and confirm `validation_mode` is `adapter_backed` when Slang or Verilator is installed, otherwise `structure_only` with fallback reasons
- `tel artifacts review CANDIDATE_OR_VALIDATION_RUN_ID` for a generated artifact
- optionally run `python -m pip install -e ".[cocotb-smoke]"` plus `pytest tests/test_cocotb_smoke.py` on a machine with cocotb, `make`, Icarus, and `vvp`
- `tel artifacts purge` as a dry run
- `python scripts/tool_smoke.py --adapters verilator iverilog` when those tools are installed
