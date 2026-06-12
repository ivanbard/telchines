# Telchines Project Improvement Brief For A Future Codex Chat

Use this file as the main handoff prompt for improving Telchines. It was created after a repository-wide audit on 2026-06-11.

## Copy/Paste Prompt

You are working in the Telchines repository. Read this entire file first, then inspect the code before editing. Your job is to improve the project without breaking the documented `1.x` CLI/config/output compatibility promise.

Focus especially on:

- making the interactive shell/TUI more robust and better designed
- validating real provider integration with OpenAI-compatible APIs, local LLM servers, and local command providers
- validating real external verification tool integration instead of only mocked tests
- hardening edge cases around project paths, command parsing, run replay, generated artifacts, and retrieval
- preserving the strong parts of the current architecture: replay artifacts, deterministic validation gates, benchmark suite, structured run store, and CLI-first workflows

Before making changes:

1. Run `git status --short --branch`.
2. Read `README.md`, `docs/compatibility.md`, `docs/providers.md`, `docs/adapters.md`, `src/telchines/shell.py`, `src/telchines/providers.py`, `src/telchines/adapters/open_tools.py`, and the relevant tests.
3. Run `pytest` so you know the current baseline.
4. Do not rewrite the project wholesale. Make focused, tested improvements.

When you finish, run the relevant tests plus the full `pytest` suite if feasible, update docs if user-facing behavior changes, and summarize what changed.

## Current Repository Snapshot

Telchines is a Python 3.11+ CLI package for grounded AI-assisted ASIC/FPGA/RTL verification workflows. It exposes `tel` and `telchines` entrypoints through Typer.

The project currently includes:

- one-shot CLI commands for project initialization, indexing, retrieval, repair, triage, SVA generation, cocotb generation, coverage planning, waveform inspection, run history, providers, adapters, and evaluation
- an interactive shell opened by running `tel`
- a prompt_toolkit full-screen shell when stdin/stdout are TTYs, plus a basic stdin fallback for non-TTY/tests
- local JSON project config under `.tel/config.json`
- a run store under `.tel/` for runs, observations, contexts, tasks, patches, generations, waveforms, and reports
- retrieval over project-local RTL/docs/logs/scripts and optional curated local external roots
- built-in adapters for Verilator, Icarus/iverilog+vvp, Slang, Verible, and SymbiYosys
- provider kinds: `heuristic`, `openai_compatible`, and `local_command`
- workflows for compile repair, regression triage, spec-to-SVA, DUT-to-cocotb, coverage planning, waveform inspection, and offline evaluation
- bundled benchmark assets and an 18-case offline suite
- CI on Ubuntu and Windows for Python 3.11, 3.12, and 3.13
- packaging workflows for PyPI trusted publishing

Local verification result from this audit:

- `pytest` passes: `69 passed in 21.83s`
- working tree was clean at audit start: `## dev...origin/dev`
- latest visible commit: `9696ecd pypi publishing prep`

## High-Level Assessment

This project is substantially more than a sketch. The core workflow spine is real: commands call operations, operations load config/store/retrieval services, workflows save structured artifacts, and tests cover most offline happy paths.

The strongest areas are:

- clear CLI-first product definition in README/docs
- stable-ish command surface and documented `1.x` compatibility boundary
- structured run memory instead of ad hoc terminal output
- replayable task artifacts for model/provider interactions
- deterministic-ish validations for repair, SVA syntax shape, and cocotb Python syntax
- provider policy controls for local/hybrid/remote and `no_egress`
- benchmark assets shipped both in source and package data
- tests that cover CLI workflows, mocked remote provider calls, local command providers, retrieval, waveforms, packaging metadata, and adapters

The weakest areas are:

- interactive shell/TUI design and robustness
- real provider integration beyond mocks
- real EDA/toolchain integration beyond command construction and monkeypatched tests
- provider/API ergonomics and documentation for actual users
- generated artifact quality validation, especially SVA and cocotb
- parser/ranking heuristics that will likely be brittle on real RTL/logs/coverage reports
- security hardening around local command providers, replay artifacts, and generated file paths
- packaging/release confidence against clean install environments and real terminals

The project is probably credible as an offline MVP, but it should not be treated as production-stable for real verification teams until the gaps below are addressed.

## Architecture Map

Important files:

- `src/telchines/cli.py`: Typer CLI entrypoints and command error handling.
- `src/telchines/shell.py`: interactive shell, full-screen prompt_toolkit TUI, slash parser, renderers, plain text intent routing.
- `src/telchines/config.py`: `.tel/config.json` model, validation, project discovery, provider/adapters policy fields.
- `src/telchines/operations.py`: service-level command functions used by CLI and shell.
- `src/telchines/providers.py`: heuristic provider, OpenAI-compatible provider, local command provider, request/response parsing, generation helpers.
- `src/telchines/adapters/open_tools.py`: external tool adapters.
- `src/telchines/adapters/base.py`: adapter execution base class.
- `src/telchines/adapters/parsing.py`: common error parsing.
- `src/telchines/retrieval.py`: indexing and retrieval ranking.
- `src/telchines/run_store.py`: persistence layer for `.tel/`.
- `src/telchines/waveforms.py`: VCD parsing and waveform evidence selection.
- `src/telchines/workflows/*.py`: workflow implementations.
- `src/telchines/eval.py`: offline benchmark suite runner.
- `tests/test_cli.py`: broadest integration-ish test file.
- `tests/test_shell.py`: currently mostly renderer/help tests, not full-screen TUI tests.
- `benchmarks/` and `src/telchines/benchmarks/`: source and bundled benchmark fixtures.
- `docs/`: public docs.
- `ops/github-backlog.json`: backlog milestones and candidate issues.
- `agent-docs/`: long-form strategy/product planning docs.

## What Has Been Done Well

### CLI and Workflow Surface

- The README accurately describes a meaningful set of v1 workflows.
- Typer commands exist for all documented top-level workflows.
- CLI error handling catches config/provider/adapter/value errors and uses nonzero exits.
- JSON output is the default for workflow commands, with human/CI formats where relevant.
- `tel` with no args opens a shell; `tel shell` also works.
- `--version` exists and is tested against `pyproject.toml`.

### Run Store and Replay Mindset

- Runs, observations, contexts, tasks, patches, generation candidates, waveforms, and reports are persisted.
- Repair/SVA/cocotb/coverage workflows save request, response, replay, and evidence artifacts.
- `tel runs list/show/replay` gives users a path back to prior state.
- Validation runs are represented as first-class run records.

### Providers

- There is a real provider abstraction.
- Default heuristic repair handles missing semicolon, unknown identifier, missing `endmodule`, and missing `end`.
- OpenAI-compatible transport posts to `/chat/completions` style endpoints with configurable base URL, model, key env var, timeout, endpoint, and headers.
- Local command provider reads JSON over stdin and parses JSON from stdout.
- Policy enforcement blocks remote providers when `model_mode=local` or `no_egress=true`, and blocks local command providers when `model_mode=remote`.
- Tests cover mocked OpenAI-compatible repair, missing API key, local command repair, local command SVA generation, and policy blocks.

### Adapters

- Built-in adapter registry exists.
- Adapters expose descriptors with category, validation mode, supported workflows, required binaries, and availability.
- Icarus has a real compile+run shape with `iverilog` and `vvp`.
- SymbiYosys result parsing extracts status, property IDs, counterexample paths, and report paths from text.
- Tests cover registry categories, SymbiYosys parsing, and a monkeypatched Icarus compile+run.

### Retrieval

- Indexing supports RTL, docs, logs, text, and Python scripts.
- It skips common generated/cache directories.
- It chunks RTL/docs/logs differently.
- It supports external roots with source provenance.
- Ranking is task-aware by mode: general, repair, triage, generation, coverage.
- Tests cover incremental refresh, larger fixture corpus, and external corpus provenance.

### Evaluation

- There is an offline 18-case benchmark suite spanning repair, triage, retrieval, SVA, cocotb, and coverage.
- Installed-package fallback to bundled benchmarks exists.
- Tests assert benchmark totals and key aggregate metrics.
- This is a strong differentiator and should be preserved.

### Documentation and Packaging

- README, quickstart, provider docs, adapter docs, evaluation docs, compatibility promise, contributing guide, security policy, changelog, and release checklist exist.
- GitHub Actions run tests across OS/Python matrix and build/check packages.
- Package data includes benchmark JSON/assets.

## What Has Not Been Done Or Is Still Weak

### 1. Interactive Shell/TUI Is Iffy

The shell works in tests through the plain fallback, but the full-screen TUI has not been tested with real terminal automation.

Current issues and risks:

- No automated test drives `_run_fullscreen_shell`.
- No pexpect/terminal integration tests for prompt_toolkit behavior.
- No tests for window resizing, narrow terminals, long paths, long output, long command lines, keyboard history, cursor movement, paste behavior, or Ctrl-C/Ctrl-D in full-screen mode.
- The header can become extremely long because it embeds full `cwd`.
- The sidebar has fixed preferred/max width and could truncate important state.
- Help is implemented as an overlay but the behavior is unconventional: Enter hides help, Escape/q also hide help.
- There is no autocomplete, command palette, command history navigation, suggestions, path completion, or `@file` mentions.
- Slash command parsing is manual and inconsistent with Typer parsing.
- The shell has two command systems: CLI/Typer and manual shell parsers. They can drift.
- Full-screen mode depends on TTY detection and `TELCHINES_PLAIN_SHELL`; there is no user-facing setting or `--plain` option.
- Plain text intent routing is keyword-based and can fire surprising commands.
- Error display in full-screen mode uses `[error]` text rather than rich-styled panels.
- There is no copy/export transcript command.
- There is no clear affordance for raw JSON vs human panels except `/raw`.
- No startup diagnostics tell users whether they are in full-screen or plain shell.

Suggested direction:

- Stabilize shell parser and UX before adding more workflows.
- Use one command spec source if possible so CLI and shell do not drift.
- Add terminal integration tests.
- Add autocomplete/path completion before more visual polish.
- Consider offering `tel shell --plain`, `tel shell --fullscreen`, and/or config/env flags.
- Improve layout for long paths and small terminals.

### 2. Real LLM/API Key Testing Is Mostly Not Done

Tests simulate an OpenAI-compatible HTTP endpoint locally and verify missing credential behavior. That is useful, but it is not equivalent to proving real provider integrations.

Missing:

- Smoke tests against an actual OpenAI-compatible endpoint with a real API key, gated behind env vars.
- Tests against local OpenAI-compatible servers such as Ollama, LM Studio, llama.cpp server, vLLM, or LiteLLM.
- Documentation showing complete config examples for real OpenAI, Azure OpenAI-compatible deployments if supported, Ollama, LM Studio, and a local command provider.
- Tests for provider responses wrapped in Markdown fences, extra text, malformed JSON, empty choices, non-chat payload shapes, streaming responses, and tool-call style outputs.
- Tests for HTTP 401/403/429/500 and timeout behavior.
- Tests that custom headers cannot accidentally override or leak credentials in bad ways.
- Tests for base URLs with and without trailing slashes, custom endpoints, proxy environments, and self-hosted HTTPS.
- A provider dry-run command that validates credentials/config without running a full repair/generation workflow.
- Redaction of sensitive provider data in persisted request/response artifacts. Current persisted transport request does not include the API key, which is good, but future headers/env fields need explicit redaction rules.

Suggested direction:

- Add opt-in integration tests marked/skipped unless env vars are present.
- Add `tel providers check [NAME]` or similar.
- Add docs for real provider setup with explicit safety notes.
- Add a fixtures-based provider response torture suite.

### 3. Local LLM Integration Is Under-Specified

The `local_command` provider is flexible, but "local LLM" can mean multiple things:

- local command wrapper that reads JSON and calls a model
- local OpenAI-compatible HTTP server
- a direct Python package/runtime
- an enterprise gateway running on localhost

Missing:

- canonical examples for each local model path
- helper scripts/templates for local command providers
- payload schema examples for repair, SVA, and cocotb generation
- documentation of expected JSON output for each capability
- support for model-specific response quirks
- tests for local server tools that are slow, noisy, or produce logs around JSON
- tests for local command provider environment variables and working directory assumptions
- tests for local command provider stdout/stderr volume limits

Suggested direction:

- Add `docs/local-llms.md`.
- Add `examples/providers/` with tiny local command and OpenAI-compatible examples.
- Add integration tests that can run against `TELCHINES_LOCAL_OPENAI_BASE_URL` and `TELCHINES_LOCAL_MODEL`.

### 4. Real External Tool Integration Is Not Proven

Adapters exist, but most tests avoid requiring actual tools.

Missing:

- CI job that installs open-source EDA tools and runs real smoke tests, at least on Ubuntu.
- Real `verilator --lint-only` smoke test.
- Real `iverilog` + `vvp` compile+run smoke test.
- Real `verible-verilog-lint` smoke test if installable.
- Real `sby`/SymbiYosys smoke test if installable.
- Real `slang` smoke test if installable.
- Version detection for adapters.
- Better adapter-specific output parsers. Current `parse_common_output` is generic and will miss many real formats.
- Tests for missing binaries on PATH and partial availability, especially Icarus requiring both `iverilog` and `vvp`.
- Tests for include paths, defines, filelists, generated build directories, top modules, and multi-file projects.
- Tests for simulation runtime failures vs compile failures.
- Tests for formal output directories, nested reports, traces, and proof modes.

Suggested direction:

- Add an optional `tool-smoke` CI workflow or matrix job, even if non-blocking initially.
- Add adapter parser fixtures from real tool outputs.
- Add `tel adapters doctor` or `tel adapters check`.

### 5. Generated Artifact Validation Is Too Shallow

SVA validation is a built-in regex/count check. Cocotb validation is `py_compile`.

These are good first gates, but weak for real users.

Missing:

- SVA validation with a real SystemVerilog parser where available.
- Validation that generated SVA actually binds to real DUT ports/signals.
- Detection of invalid bind statements, missing clock/reset, wrong module name, undeclared internal signal references, and invalid property syntax beyond simple block counts.
- Cocotb validation that can actually run with a simulator on a toy DUT.
- Checks for generated cocotb imports/dependencies and Makefile/runner scaffolding.
- Generated artifact diff display and review ergonomics.
- Configurable project style/conventions for output paths, naming, reset polarity, clock names, and test templates.

Suggested direction:

- Keep built-in validators as fallback, but add adapter-backed validation when tools exist.
- Add a small executable cocotb smoke fixture if cocotb is an optional dev dependency.
- Add user-facing validation mode in outputs.

### 6. Retrieval Is Useful But Heuristic

Current retrieval is simple token overlap plus boosts. That is OK for offline MVP but brittle at scale.

Risks:

- No stemming/synonyms/domain aliases beyond token overlap.
- No semantic embeddings or hybrid search.
- No ranking evaluation against larger/real corpora.
- Large files may create huge indexes and slow search.
- Binary-ish text or generated logs can pollute results.
- External roots are only relative and local, by policy, but there is no freshness/metadata manifest.
- Index invalidation is hash-based per chunk source, which is good, but no clear UX for stale vs fresh index.
- Search auto-builds index if missing, which may surprise users on huge repos.
- Retrieval indexes `.py`, which can include provider scripts and tools, but may not be desirable in all contexts.

Suggested direction:

- Add explicit `tel index status` and `tel index clean`.
- Add config include/exclude patterns.
- Add larger retrieval benchmarks.
- Add optional semantic/hybrid retrieval behind dependencies/config, not as a required MVP dependency.

### 7. Security and Privacy Need More Hardening

Security policy identifies the right sensitive areas, but code needs more hardening before real-world use.

Risks:

- `local_command` provider can execute arbitrary configured commands. That may be expected, but UX/docs should make risk clear.
- Provider env fields are accepted and passed through. Secrets can easily be stored in `.tel/config.json`.
- Request/response artifacts may persist proprietary RTL/spec/log snippets under `.tel/task-artifacts`.
- `replay_run` executes stored replay commands without additional confirmation in CLI.
- Generated file paths from providers are normalized for some generation paths, but repair provider `file_path` handling and validation should be audited for path traversal and symlink edge cases.
- Copying project trees to temp for validation may copy `.tel`, generated artifacts, secrets, large files, or external corpora unless excluded by `copy_tree_to_temp`.
- There is no artifact redaction, retention, or purge command.
- No audit log for remote egress decisions beyond provider status/errors.

Suggested direction:

- Add `tel doctor privacy` or similar to surface config risks.
- Redact known secret fields in persisted artifacts.
- Add warnings/docs for `local_command`.
- Add tests for provider path traversal and symlink behavior.
- Add `.tel` artifact retention/purge commands.

### 8. Packaging/Release Confidence Needs Clean-Install Testing

CI builds packages and checks metadata. Tests cover package data conceptually. Still missing:

- installing built wheel into a fresh venv and running `tel eval run`
- ensuring `dist/` artifacts are clean and not committed accidentally
- verifying README rendering on PyPI
- verifying package includes docs/assets/benchmarks exactly as expected
- testing console entrypoints from installed wheel, not only source tree/PYTHONPATH
- deciding whether `Development Status :: 5 - Production/Stable` is too strong for the current maturity

Suggested direction:

- Extend CI package job to install the wheel into a temp venv and run smoke commands.
- Consider changing classifier to Beta or keep Stable only after real integration tests exist.

## Priority Workstreams

### P0: Shell/TUI Stabilization

Goal: Make the interactive shell reliable and pleasant enough for daily use.

Tasks:

- Add `tel shell --plain` and `tel shell --fullscreen` or equivalent explicit mode controls.
- Add command history navigation if not already provided by TextArea defaults.
- Add autocomplete for slash commands and common options.
- Add path completion for `--logs`, `--file`, `--rtl`, `--spec`, `--dut`, `--report`, `cd`, and waveform targets.
- Add narrow-terminal behavior tests or a small terminal harness.
- Refactor manual shell parsing to reduce drift from Typer commands.
- Improve header/sidebar for long paths and small screens.
- Add `/clear`, `/history`, `/transcript`, and `/doctor` if scope allows.
- Improve error panels and status feedback while commands run.

Acceptance criteria:

- Existing tests pass.
- New tests cover plain shell and at least parser/autocomplete behavior.
- Manual smoke test works for `/help`, `/providers`, `/index`, `/retrieve`, `/triage`, `/gen-sva`, `/gen-cocotb`, `/coverage-plan`, `/waveforms`, `/runs`, `/raw`, `/cd`, Ctrl-C/Ctrl-D, and small terminal widths.
- Docs mention explicit shell modes and key commands.

### P0: Provider Integration Hardening

Goal: Make model-provider support real, diagnosable, and safe.

Tasks:

- Add `tel providers check [NAME]` to validate config, credentials, policy, and transport.
- Add docs for real OpenAI-compatible provider setup.
- Add docs/examples for Ollama/LM Studio/local OpenAI-compatible servers.
- Add docs/examples for local command providers.
- Add provider response parsing tests for malformed/empty/fenced/noisy responses.
- Add HTTP error and timeout tests.
- Add redaction tests for persisted provider artifacts.
- Add optional integration tests gated by environment variables.

Acceptance criteria:

- Users can configure at least one real remote OpenAI-compatible provider from docs.
- Users can configure at least one local OpenAI-compatible provider from docs.
- Users can configure a local command provider from docs.
- Provider check gives actionable output and respects policy.
- Missing/invalid keys produce clear errors.

### P0: Real Tool Smoke Tests

Goal: Prove adapters work with actual verification tools, not only mocks.

Tasks:

- Add optional GitHub Actions workflow for open-source EDA smoke tests.
- Install/run Verilator and Icarus at minimum if practical.
- Add real fixture projects for compile-only and compile+run flows.
- Add parser fixtures from actual tool outputs.
- Add adapter version detection.
- Add `tel adapters check` or `doctor` command.

Acceptance criteria:

- At least Verilator and Icarus smoke tests run on Ubuntu in CI or documented local script.
- Missing tools produce friendly output.
- Adapter parsing covers actual observed messages.

### P1: Generated Artifact Quality

Goal: Generated SVA/cocotb artifacts should be structurally valid and useful, not just syntactically plausible.

Tasks:

- Add adapter-backed SVA validation when Slang/Verilator is available.
- Validate generated SVA references against DUT module/ports where possible.
- Add cocotb executable smoke test behind optional dependencies.
- Improve cocotb scaffold templates with monitors, reset/clock helpers, Makefile/runner hints, and clearer TODOs.
- Add user-configurable naming/output conventions.

Acceptance criteria:

- Generated artifacts include validation mode and limitations.
- SVA/cocotb benchmarks include at least one failure case per validator.
- Generated cocotb can run against at least one tiny fixture DUT when optional deps/tools are installed.

### P1: Retrieval and Index UX

Goal: Make project indexing transparent and scalable.

Tasks:

- Add `tel index status`.
- Add include/exclude config patterns.
- Add stale index warnings.
- Add larger fixture corpus tests.
- Add an index summary in shell sidebar that handles missing/stale/large states.

Acceptance criteria:

- Users can understand what was indexed and why.
- Large/noisy directories can be excluded.
- External roots preserve provenance and do not crowd out project-local sources in triage/repair.

### P1: Security/Privacy

Goal: Make sensitive behavior explicit and auditable.

Tasks:

- Add artifact redaction for provider configs, headers, env, and known secret-looking keys.
- Add tests for path traversal in provider-returned paths.
- Add docs warning that local command providers execute arbitrary commands.
- Add a purge command for `.tel` artifacts.
- Add confirmation or clear docs around `runs replay`.

Acceptance criteria:

- Secret fields do not appear in saved task artifacts.
- Provider paths cannot write outside the project.
- Users have a way to inspect and clean stored artifacts.

### P2: Packaging and Release Hardening

Goal: Make release claims match reality.

Tasks:

- Add clean-wheel install smoke test to CI.
- Run `tel eval run` from installed wheel.
- Reconsider production/stable classifier.
- Verify README assets render in source distribution and PyPI.
- Update changelog and release checklist with integration gates.

Acceptance criteria:

- A fresh venv installed from the wheel can run `tel --version`, `tel --help`, `tel project init`, `tel index`, and `tel eval run`.

## Edge Cases To Consider

### Shell/TUI Edge Cases

- Non-TTY stdin/stdout.
- Windows Terminal, PowerShell, cmd, Git Bash, WSL, Linux terminals, CI pseudo-terminals.
- Tiny terminal widths/heights.
- Very long project root paths.
- Unicode paths and spaces in paths.
- Backslashes vs forward slashes in shell commands.
- Quoted arguments with spaces.
- Repeated options such as multiple `--logs`, `--file`, `--rtl`, `--spec`.
- Missing option values, e.g. `/repair --tool`.
- Unknown slash commands.
- Plain text requests containing accidental keywords such as "run" or "provider".
- Ctrl-C while a workflow is running.
- Ctrl-D at empty prompt.
- Help overlay open while user types or resizes terminal.
- Large outputs from retrieval/runs/eval.
- Commands that take noticeable time.
- Shell starting outside a Telchines project.
- Shell changing directories into and out of projects.

### Provider Edge Cases

- Missing API key env var.
- Empty API key env var.
- Bad base URL.
- Base URL with path prefix.
- Endpoint with leading slash vs relative endpoint.
- HTTP 401/403/404/429/500.
- Timeout.
- Invalid JSON HTTP response.
- Valid JSON but no choices.
- Choice content is empty.
- Choice content is fenced Markdown.
- Choice content has text before/after JSON.
- Choice content is a JSON array instead of object.
- Provider returns `status=no_patch` or `status=no_generation`.
- Provider returns absolute output paths.
- Provider returns `../outside` paths.
- Provider returns huge candidate content.
- Provider returns binary/non-UTF output.
- Provider returns headers or metadata containing secrets.
- Local command not found.
- Local command exits nonzero.
- Local command times out.
- Local command prints logs before JSON.
- Local command writes useful data to stderr.
- Local command requires cwd assumptions.
- Local OpenAI-compatible server has no auth or different auth.
- Model does not follow JSON-only instructions.

### Adapter/EDA Edge Cases

- External binary missing.
- Required paired binary missing, e.g. `iverilog` present but `vvp` absent.
- Tool installed but too old.
- Tool exits zero with warnings that matter.
- Tool exits nonzero without parseable error.
- Include paths and defines required.
- Filelists instead of explicit files.
- Multiple top modules.
- Generated files and build directories.
- Relative paths in tool output.
- Absolute paths outside project.
- Windows path formats in tool output.
- Verilator/Slang/Verible/SymbiYosys message formats not covered by regexes.
- Formal outputs with nested engine directories.
- Counterexamples in FST instead of VCD.
- Simulation runtime failure after compile succeeds.
- Hanging simulation.

### Retrieval Edge Cases

- Huge repository.
- Large generated logs.
- Non-UTF files.
- Empty files.
- Files with CRLF.
- Vendored dependencies.
- Generated `.tel` directories.
- Symlinks and junctions.
- External root missing.
- External root points to very large corpus.
- External root overlaps project files.
- Query with no matching tokens.
- Query with only file names.
- Focus paths that do not exist.
- Stale index after file deletion.
- Multiple chunks from the same file crowding out others.
- Sensitive files accidentally indexed.

### Run Store Edge Cases

- Corrupt JSON in `.tel`.
- Missing run/observation/context files.
- Concurrent commands writing `.tel`.
- Very large artifacts.
- User deletes generated artifact but run metadata remains.
- Replay command no longer valid after project changes.
- Run IDs collide if generated in same timestamp window.
- Project moved after `.tel/config.json` records old root path.

### Generated Artifact Edge Cases

- SVA property refers to internal signals not visible from bind context.
- SVA generated without clock/reset.
- `bind` uses wrong module name.
- Multiple modules in RTL file.
- Interfaces/packages/macros in RTL.
- ANSI and non-ANSI port declarations.
- Parameterized modules.
- Active-low reset naming not matching `_n`.
- Cocotb generated for combinational DUT with no clock.
- Cocotb generated for multi-clock DUT.
- Generated cocotb imports unavailable dependency.
- Generated file overwrites user file.
- Output dir outside project.

### Coverage Edge Cases

- Coverage JSON missing required fields.
- Tool-specific coverage schema not matching Telchines schema.
- Hits greater than goal.
- Goal zero or missing.
- Duplicate item IDs.
- Exclusions conflict with reachability hints.
- Formal run ID missing or unrelated.
- Coverage item names do not overlap RTL/spec tokens.
- Large coverage reports with thousands of items.

## Concrete First Steps For The Next Codex Session

Start with a narrow but high-impact slice:

1. Add provider diagnostics and tests:
   - implement `tel providers check [NAME]`
   - cover missing key, policy block, local command success/failure, mocked HTTP success/failure
   - document examples

2. Improve shell command ergonomics:
   - add explicit `tel shell --plain`
   - add path-safe parsing tests for repeated/missing options
   - add autocomplete if prompt_toolkit can support it cleanly without large refactor

3. Add real-tool smoke scaffolding:
   - create a non-default CI workflow or script for Verilator/Icarus
   - document how to run it locally

After each slice, run:

```bash
pytest
tel --version
tel --help
tel eval run
```

If tool integration changes are made, also run any new smoke script/workflow locally where possible.

## Compatibility Constraints

Do not casually break:

- top-level CLI command names
- `.tel/config.json` field names and general layout
- JSON output keys for documented workflows
- run-store replay behavior
- existing benchmark suite expectations

Additive changes are preferred:

- new commands are OK
- new JSON fields are OK
- new provider/adapters are OK
- richer shell presentation is OK

Breaking changes require docs, changelog, migration notes, and strong justification.

## Quality Bar

For each improvement:

- Add or update tests.
- Update docs when user-facing behavior changes.
- Preserve deterministic/replayable behavior.
- Keep provider/tool failures clear and actionable.
- Avoid adding heavy dependencies unless optional.
- Avoid hiding uncertainty: show validation mode and limitations.
- Keep generated artifacts reviewable by a human.

## Final Notes

The project direction is good. The right next move is not more feature breadth. It is trust-building:

- prove the shell feels good in real terminals
- prove model providers work with real endpoints and local setups
- prove adapters run against real open-source tools
- prove generated artifacts survive stronger validation
- prove privacy and artifact persistence are controlled

That is the shortest path from "impressive offline MVP" to "credible tool a verification engineer can actually try on a real repo."
