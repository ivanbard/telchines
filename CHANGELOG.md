# Changelog

## 1.0.1 - Unreleased

Release hardening patch.

- packaged the offline benchmark suite in the installed wheel
- made `tel eval run` fall back to bundled benchmarks when a project does not provide a local `benchmarks/` directory
- configured the PyPI publish workflow to use the `pypi` GitHub environment for trusted publishing
- improved temporary benchmark copying so installed packages do not need write access to package directories

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
