# Changelog

## 1.1.1 - 2026-07-13

### Fixed

- Handle absent optional cocotb modules during clean-wheel evaluation instead of raising `ModuleNotFoundError`.
- Avoid dereferencing a missing MSYS Verilator wrapper when open-tool binaries are unavailable.
- Isolate missing-tool tests from runner-specific optional dependencies.

## 1.1.0 - 2026-07-13

Verification execution, diagnostics, and release-readiness expansion.

- hardened cocotb validation with separate syntax, structure, simulator compile/launch, cocotb initialization, and test-result stages
- made cocotb smoke subprocesses time-bounded and explicitly classified unsupported Windows/MSYS/native-Icarus environments
- separated SVA artifact, syntax, adapter, formal, proof, benchmark, and overall workflow statuses so bounded checks cannot imply complete proof
- added actionable provider HTTP diagnostics, bounded readiness probes, transport-versus-workflow reporting, and repeat stability/drift metrics
- exposed the actual LangGraph or bounded-fallback runtime and installation remedy in provider and agent results
- made imported-run replayability explicit and replaced missing-run or missing-executable tracebacks with CLI errors
- expanded waveform investigation with hierarchical and fuzzy matching, bus decoding, time windows, and log timestamp correlation
- expanded retrieval and benchmark coverage for nested filelists, packages, includes, macros, generated RTL, vendor compile options, and compile-order failures
- added CI and coverage importers, broader UVM/vendor triage fixtures, project templates, scratch evaluation, and scoped artifact lifecycle controls
- added a `pyslang` fallback for Slang validation and strengthened Verilator, Icarus, and SymbiYosys adapter smoke coverage
- expanded the provider capability harness and model matrices for local, hosted, OpenAI-compatible, Anthropic, OpenRouter, and agent-runtime paths
- restored the default bundled evaluation suite to a fully passing result while preserving explicit formal warnings and execution-backing metadata

## 1.0.2 - 2026-06-22

PyPI and public repository presentation polish.

- added a cleaned transparent Telchines logo asset for the README
- made the README logo and documentation links render correctly from PyPI
- removed local agent skill files and internal planning/backlog artifacts from the public tree
- updated release metadata and badges for the refreshed package page
- included the release-hardening work originally prepared under the unpublished `1.0.1` version

- packaged the offline benchmark suite in the installed wheel
- made `tel eval run` fall back to bundled benchmarks when a project does not provide a local `benchmarks/` directory
- configured the PyPI publish workflow to use the `pypi` GitHub environment for trusted publishing
- improved temporary benchmark copying so installed packages do not need write access to package directories
- added `tel providers check` for policy, credential, and live transport diagnostics
- added env-gated live OpenAI-compatible provider integration smoke tests
- accepted OpenAI-compatible function/tool-call arguments as provider JSON responses
- config-validated OpenAI-compatible custom headers and blocked Authorization overrides
- config-validated OpenAI-compatible base URLs as explicit HTTP(S) URLs
- preserved OpenAI-compatible base URL path prefixes when custom endpoints start with `/`
- bounded and config-validated persisted stdout/stderr diagnostics for noisy `local_command` providers
- added `tel adapters check`, adapter version detection, and optional Verilator/Icarus real-tool smoke scaffolding
- expanded adapter diagnostic parsing for Verilator, Icarus, Slang, Verible, and SymbiYosys-style output fixtures
- added explicit `tel shell --plain` and `tel shell --fullscreen` modes plus shell command/path completion
- added subprocess smoke coverage for `tel shell --plain` stdin/stdout behavior, error handling, and EOF exit
- added a prompt_toolkit pipe-input harness that drives the full-screen shell without a real terminal
- added `tel index status`, `tel index clean`, and retrieval include/exclude patterns
- added `retrieval.aliases` for explicit domain synonym expansion during search
- added `tel doctor privacy` and dry-run-first `tel artifacts purge`
- hardened provider-returned paths and redacted secret-looking task artifact fields
- made run listing tolerate corrupt run JSON records while exposing load diagnostics
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
