# Changelog

## 1.0.1 - Unreleased

Release hardening patch.

- packaged the offline benchmark suite in the installed wheel
- made `tel eval run` fall back to bundled benchmarks when a project does not provide a local `benchmarks/` directory
- configured the PyPI publish workflow to use the `pypi` GitHub environment for trusted publishing
- improved temporary benchmark copying so installed packages do not need write access to package directories
- added `tel providers check` for policy, credential, and live transport diagnostics
- added env-gated live OpenAI-compatible provider integration smoke tests
- added `tel adapters check`, adapter version detection, and optional Verilator/Icarus real-tool smoke scaffolding
- expanded adapter diagnostic parsing for Verilator, Icarus, Slang, Verible, and SymbiYosys-style output fixtures
- added explicit `tel shell --plain` and `tel shell --fullscreen` modes plus shell command/path completion
- added subprocess smoke coverage for `tel shell --plain` stdin/stdout behavior, error handling, and EOF exit
- added `tel index status`, `tel index clean`, and retrieval include/exclude patterns
- added `retrieval.aliases` for explicit domain synonym expansion during search
- added `tel doctor privacy` and dry-run-first `tel artifacts purge`
- hardened provider-returned paths and redacted secret-looking task artifact fields
- added generation convention config plus explicit SVA/cocotb validation modes and limitations
- added optional adapter-backed SVA validation through configured Slang/Verilator-style generation adapters
- added an optional executable cocotb/Icarus smoke test for generated cocotb scaffolds
- added `tel artifacts review` for generated artifact drift/diff inspection
- made `tel runs replay` confirmation-gated and excluded `.tel`/build/cache/symlink content from validation temp copies
- added clean-wheel install smoke checks to CI packaging

## 1.0.0 - 2026-04-23

First public CLI-first `v1` release target.

Highlights:

- stabilized the documented `1.x` CLI surface
- promoted package metadata and versioning for public distribution
- added `--version` support to `tel` and `telchines`
- documented install, quickstart, providers, adapters, evaluation, compatibility, and release process
- added public repo assets: license, contributing guide, security policy, changelog
- added GitHub Actions for CI and release publishing
- formalized the external retrieval policy and adapter contribution contract

Workflow surface included in `v1`:

- repair
- triage
- retrieval
- waveform inspection
- spec-to-SVA generation
- DUT-to-cocotb generation
- coverage planning
- benchmark evaluation
