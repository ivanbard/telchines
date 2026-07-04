# Telchines Agentic FPGA/ASIC Verification Evaluation

Run date: July 3, 2026 America/New_York; many Telchines run artifacts use July 4, 2026 UTC timestamps.

Repository: `C:\Users\user\Desktop\pc\Repositories\telchines`

Evaluation area: `.test-work/agentic-eval-20260703/`

## Executive Summary

Telchines is a credible early CLI-first orchestration layer for small, evidence-backed hardware verification tasks. It is strongest when the task fits one of its explicit workflows: indexing a small RTL/docs/log corpus, retrieving cited context, clustering simple regression logs, proposing simple compile repairs through adapters, generating cocotb scaffolds, producing coverage-plan recommendations from normalized JSON, preserving run artifacts, and replaying stored commands with confirmation. The deterministic benchmark suite passed 20/20, and the full pytest suite passed 169 tests with 4 skips.

The product is not yet a full FPGA/ASIC verification assistant in the sense engineers would expect for daily use on real SoC, FPGA, UVM, formal, or vendor-tool projects. Its current behavior is fragile around real EDA integration, multi-file design semantics, simulator/formal execution, waveform analysis depth, provider/runtime status reporting, live-provider configuration, and UX clarity when a workflow partially succeeds. Several commands return superficially successful process exits while the embedded workflow payload reports a failed or no-op result. This is especially risky for agentic verification workflows because engineers may trust a green shell exit or a `passed` harness row while the actual candidate is missing, only syntax-checked, or validated by a narrow fixture.

Live LLM integration is real but uneven. The OpenRouter matrix completed successfully, including provider check, repair, SVA generation, cocotb generation, shell smoke, and an agent-runtime repair path. It was slow enough to matter operationally: the live OpenRouter study took about 164 seconds, with individual generation tasks taking roughly 33-60 seconds. The Anthropic direct provider failed live checks with HTTP 404, while the Anthropic agent-runtime rows still reported passed for some scenarios because they did not truly exercise the broken base provider. The OpenAI matrix failed before provider execution because no model env/default was available, and the harness treated that as a `MatrixError` after creating a scratch project rather than a clean skip.

The next development priority should be trust calibration: make status semantics unambiguous, make validation reflect real simulator/formal execution where possible, block false-green harness results, and report integration/configuration failures at the correct layer. After that, expand actual EDA coverage and hardware semantics: multi-file RTL elaboration, include/package handling, standard simulator and lint adapter flows, UVM/testbench awareness, richer waveform/log correlation, formal/SVA execution, vendor FPGA workflows, and project-scale retrieval controls.

## Methodology

The evaluation followed the requested pass structure:

1. Inventory visible Telchines capabilities from README, docs, CLI help, package metadata, source workflow entry points, tests, benchmarks, provider matrices, and adapter/provider docs.
2. Run baseline deterministic commands in a disposable project copied from `tests/fixtures/sample_project`.
3. Exercise realistic workflow surfaces: project init/index/retrieval, adapters, providers, repair, agent repair, triage, waveform inspection, generation, coverage planning, run import/show/replay, artifact review, artifact purge preview, shell smoke, benchmark suite, and pytest.
4. Stress weak areas with missing files, missing EDA tools, live provider access, direct provider failures, agent-runtime fallback, and status mismatch cases.
5. Synthesize findings into a development roadmap.

No Telchines source files were modified. Scratch projects, generated artifacts, command captures, and this report were written under `.test-work/`, which is ignored by the repository.

Secrets were not printed or copied. The local `.env` was loaded into process environment variables for provider checks, but API key values were not echoed. Provider-study summaries use Telchines' existing redaction path.

## Evidence Map

Primary captured outputs:

| Evidence | Path |
| --- | --- |
| CLI/project baseline commands | `.test-work/agentic-eval-20260703/12_project_init.*` through `21_runs_list_after_setup.*` |
| Workflow commands | `.test-work/agentic-eval-20260703/22_triage_human.*` through `35_runs_list_after_workflows.*` |
| Provider studies | `.test-work/provider-capability-study-eval/*/*_provider_capability_summary.md` and `.json` |
| Tool smoke | `.test-work/agentic-eval-20260703/46_tool_smoke_allow_missing.out.txt` |
| Full pytest | `.test-work/agentic-eval-20260703/47_pytest_full.out.txt` |
| Built-in benchmark suite | `.test-work/agentic-eval-20260703/50_tel_eval_run_sample.out.txt` and `51_tel_eval_report_sample.out.txt` |
| Fresh agent repair | `.test-work/agentic-eval-20260703/54_fresh_agent_repair.out.txt` |
| Run import/replay/artifacts/shell | `.test-work/agentic-eval-20260703/55_runs_import_dry.*` through `67_runs_replay_yes.*` |
| Scratch Telchines project | `.test-work/agentic-eval-20260703/sample_project/` |
| Fresh repair project | `.test-work/agentic-eval-20260703/fresh_repair_project/` |

## Test Matrix

| Area | Scenario | Outcome | Evidence |
| --- | --- | --- | --- |
| CLI entry point | `tel --version` via console script | Passed, prints `telchines 1.0.2` | direct console check |
| Module entry point | `python -m telchines.cli --version` | Exited 0 with no output; `python -m telchines --version` works | early smoke observation |
| Project setup | `project init`, `index`, `index status` in scratch project | Passed; 14 chunks, 9 sources initially | `12_project_init`, `13_index`, `14_index_status` |
| Retrieval | Query `uart timeout handling` | Passed; cited docs, RTL, logs with scores | `20_retrieve_uart_timeout.out.txt` |
| Providers | Default project provider list/check | Passed; only heuristic configured by default | `15_providers_list`, `16_providers_check_heuristic` |
| Adapters | Adapter list/check | Correctly reported Icarus present and Verilator/Slang/SymbiYosys/Verible missing; check exits 1 | `17_adapters_list`, `18_adapters_check_all` |
| Privacy | `doctor privacy` | Passed | `19_doctor_privacy` |
| Triage | Logs only, then logs plus VCD | Passed; two clusters | `22_triage_human`, `23_triage_json_with_vcd` |
| Waveforms | list/show/signals/inspect VCD | Basic parsing works; inspect requested `rx` but returned `clk` | `24`-`27`, especially `27_waveforms_inspect.out.txt` |
| Repair | Fixture repair review/apply | Proposed and validated semicolon fix; confusing top-level status in direct repair payload | `28_repair_fixture_review`, `29_repair_fixture_apply` |
| Agent repair | Fresh broken counter via agent | Passed as `review_required`; patch validated and saved | `54_fresh_agent_repair.out.txt` |
| SVA generation | Heuristic on sample UART RX | Returned `status: no_generation`; no artifact | `30_gen_sva.out.txt` |
| Cocotb generation | Heuristic on UART RX | Passed syntax/structure validation; no simulator execution | `31_gen_cocotb.out.txt` |
| Coverage planning | Missing-checker coverage JSON | Passed; one prioritized recommendation | `32_coverage_plan.out.txt` |
| Agent triage | Natural-language triage task with logs/VCD | Passed; same underlying clusters | `34_agent_triage.out.txt` |
| Run store | list/show/import/dry-run | Passed | `35`, `55`, `56`, `57`, `65` |
| Replay | Preview and `--yes` execution | Preview returns confirmation_required and exit 1; `--yes` executes command | `66_runs_replay_preview`, `67_runs_replay_yes` |
| Artifact review | Generated cocotb file | Passed; unchanged, zero diff | `58_artifacts_review_cocotb.out.txt` |
| Artifact purge | Dry-run purge | Passed; planned 42 files, 87,240 bytes | `59_artifacts_purge_preview.out.txt` |
| Index clean/rebuild | `index clean`, status, rebuild | Passed | `60`, `61`, `62` |
| Shell | Plain shell `/providers`, `/index status`, `/transcript` | Passed but output is duplicated/noisy | `64_shell_smoke.out.txt` |
| Missing file | `gen-cocotb --dut rtl/does_not_exist.sv` | Fails, but classified as provider error | `63_missing_file_failure.err.txt` |
| Optional tools | `scripts/tool_smoke.py --allow-missing` | Icarus pass; Verilator/Slang/SymbiYosys skipped missing | `46_tool_smoke_allow_missing.out.txt` |
| Unit tests | Full pytest | 169 passed, 4 skipped | `47_pytest_full.out.txt` |
| Benchmarks | `tel eval run/report` from initialized scratch project | 20/20 passed | `50_tel_eval_run_sample.out.txt` |
| Provider harness | local_command matrix | Passed all rows | `.test-work/provider-capability-study-eval/local_command/...md` |
| Provider harness | agent_runtime matrix | Passed, but uses fallback because LangGraph is not installed | `.test-work/provider-capability-study-eval/agent_runtime/...md` |
| Provider harness | OpenRouter live | Passed all rows; high latency | `.test-work/provider-capability-study-eval/openrouter/...md` |
| Provider harness | OpenAI live | Failed before execution due missing model env/default | `43_provider_study_openai_live.err.txt` |
| Provider harness | Anthropic live | Direct provider failed HTTP 404; agent-runtime rows falsely appeared passed | `.test-work/provider-capability-study-eval/anthropic/...json` |

## Capabilities That Work Reliably

### Project Indexing And Retrieval

Indexing is fast and produces useful, inspectable status metadata. Retrieval returns structured hits with citations, snippets, source hashes, source domains, and line ranges. On the UART timeout query it correctly retrieved the UART docs, UART spec notes, and failing regression logs. This is one of the strongest foundations in the system because it makes model and heuristic outputs auditable.

Limitations remain: the evaluated projects are tiny. I did not observe hierarchy-aware ranking, package/import awareness, macro/include resolution, generated-file suppression beyond the current index rules, or semantic understanding of multi-module connectivity.

### Regression Triage

Triage clusters simple log patterns well. The UART fixture produced two clusters: repeated `timeout waiting for start bit` failures and an `unknown identifier tx_fifo_level` failure. The output includes likely cause, suggested action, evidence hits, similar prior runs, and optional waveform evidence.

This is valuable for repeated regression failures. It is not yet a replacement for a real regression triage assistant on a large verification farm because the parser and clustering appear pattern-driven and the waveform association is shallow.

### Run Store, Replay, Import, And Artifact Review

The run store is useful and concrete. Commands create replayable run records, task artifacts, context snapshots, patch records, generated artifacts, and imported regression runs. `runs replay` correctly requires confirmation before executing a stored command. `runs import` supports dry-run and persisted import from a simple external regression manifest. `artifacts review` gives a clear baseline/current diff result for generated artifacts.

This data-plane orientation is a major strength for verification workflows because engineers need replay, provenance, and evidence more than chat-style answers.

### Deterministic Benchmarks And Tests

The full test suite passed:

```text
169 passed, 4 skipped in 44.50s
```

The built-in benchmark suite passed 20/20 in an initialized project. It covers agent repair, repair, retrieval, SVA, cocotb, coverage planning, and triage. Metrics included 100% retrieval citation coverage, 100% cocotb validation pass rate, and a 66.7% SVA validation pass rate by design because one invalid SVA case expects failed validation.

These are meaningful regression guards. They should not be interpreted as proof of real-world FPGA/ASIC readiness because the suite uses controlled small fixtures and local scripted providers for several generation paths.

## Capabilities That Work Under Narrow Conditions

### Repair And Agent Repair

Repair works for simple fixture-detected RTL syntax/name issues. In the fresh broken-counter project, the agent retrieved context, ran the fixture adapter, generated a semicolon patch, validated it through the same adapter, and left the patch review-gated. This is the right shape for hardware work.

However, status semantics are confusing. In the fresh agent repair output, the outer task status is `review_required`, step `validate_candidate` is `passed`, and a patch id exists, but `result.status` remains `failed` because it refers to the original adapter run. Direct `repair` outputs similarly show `status: failed` while also reporting `validation_status: passed`. This makes machine consumers and humans work too hard to decide whether there is a validated candidate.

### Cocotb Generation

Cocotb generation can infer simple ports, clock/reset names, write a syntactically valid Python test scaffold, produce a manifest, and record validation limitations. The sample UART RX generated a 34-line scaffold and passed `py_compile` plus structural checks.

The current validation does not run cocotb, compile RTL, elaborate a simulator, or check that behavior is actually tested. It is useful as a starter scaffold, not as proof that a verification test works.

### SVA Generation

The benchmark SVA paths work with local SVA provider fixtures and validation adapters. Live OpenRouter also generated SVA that passed Telchines validation in the provider study.

The default heuristic provider returned `no_generation` for the sample UART RX SVA request. That is honest, but from a UX standpoint engineers may expect either a meaningful draft or a stronger explanation of what input is missing. SVA validation is also limited by missing real tools in this environment: Slang and Verilator were not available, and formal execution was not available.

### Coverage Planning

Coverage planning works on small normalized coverage JSON fixtures. It correctly classified a missing checker and produced a prioritized recommendation with rationale and evidence.

It does not yet integrate with vendor coverage databases, UCIS, coverage closure dashboards, exclusion review workflows, or simulator-specific report formats. The action output is useful but narrow.

### Live LLM Providers

The OpenRouter live provider study passed all rows:

- provider check: 8.839 seconds
- agent repair: 33.272 seconds
- SVA generation: 59.563 seconds
- cocotb generation: 32.892 seconds
- OpenRouter agent repair: 23.243 seconds

That demonstrates real LLM integration through Telchines' provider abstraction. The results were still validated only through Telchines' narrow gates, not through full simulation/formal workflows.

## Capabilities That Fail Or Mislead

### False-Green Provider Harness Results

The Anthropic direct provider failed live transport with HTTP 404. Direct repair, SVA, and cocotb commands failed with provider errors. Despite that, `anthropic-agent` provider check passed because the agent-runtime check only reported runtime metadata and did not verify the base provider transport. Its `agent_repair` row also had process exit 0 and the harness marked it passed, even though the parsed Telchines payload had `status: failed`, `generate_or_repair_candidate: no_patch`, and no validation run.

Impact: release or provider certification could show green rows while an actual base LLM integration is broken.

Recommendation: provider check for `agent_runtime` must optionally verify the base provider; provider-study pass/fail must inspect parsed workflow status, patch/candidate presence, and validation status, not only process exit.

### Missing OpenAI Model Causes Harness Crash, Not Clean Skip

The OpenAI provider matrix has `model_env: TELCHINES_OPENAI_MODEL` and no default. Since that env var was not set, the study failed with `MatrixError: provider openai is missing model or model_env` after creating a scratch project. This should be a clean skip or invalid-config result before project setup.

Impact: confusing live-provider readiness and CI behavior.

### Missing Input Files Are Reported As Provider Errors

`gen-cocotb --dut rtl/does_not_exist.sv` failed with:

```text
provider error: dut file does not exist: rtl\does_not_exist.sv
```

This is an input/configuration error, not a provider error. Misclassifying it sends users to the wrong layer.

### Waveform Inspect Can Return The Wrong Signal

`waveforms inspect logs/regressions/uart_rx_trace.vcd --signal rx --window 8` returned `signal_name: clk` and transitions for `uart_rx_tb.clk`. Available signals were `clk`, `rst_n`, `serial_i`, and `start_seen`; there was no `rx` signal. The command should either require an exact match or clearly report no match / ambiguous match. Returning `clk` is dangerous in debug workflows.

### Waveform Evidence Is Too Shallow For Debugging

Triage attached the same VCD evidence to both RX timeout and TX unknown-identifier clusters, matching only `clk` and `rst_n`. For the TX identifier issue, the RX waveform is not relevant. For the RX timeout issue, the useful signals are likely `serial_i` and `start_seen`, but the excerpt only included `clk` and `rst_n`.

Impact: waveform support currently helps prove that a VCD was ingested, but not that Telchines understands the debug situation.

### Shell Output Is Duplicated And Noisy

The plain shell works, but startup panels and command outputs are duplicated in captured sessions. `/transcript` also reprints earlier outputs inline. That may be acceptable for a toy shell but is fatiguing in CI logs or engineer transcripts.

### `tel eval run` Requires An Initialized Project

Running `tel eval run` from the repository root failed because the root was not initialized as a Telchines project. Running it from the initialized scratch project passed. Documentation and error recovery should clarify that the benchmark runner needs a Telchines project context or should initialize/use an internal scratch project automatically.

## Blocked Or Partial Areas

### Real EDA Tool Coverage

Only Icarus Verilog was available in the local environment. Missing tools:

- Verilator
- Slang
- SymbiYosys
- Verible

Because of that, real SystemVerilog lint/elaboration/formal validation could not be exercised. This is a major blocker for evaluating production-grade FPGA/ASIC workflows. The adapter checks correctly reported the missing binaries, so this is partly an environment limitation. It is also a product limitation if Telchines cannot help users install, configure, or route around missing toolchains.

### Vendor FPGA/ASIC Workflows

No Vivado, Quartus, Libero, VCS, Xcelium, Questa, Riviera, JasperGold, VC Formal, SpyGlass, AscentLint, or Verdi/DVE integrations were exercised. Telchines currently exposes generic adapters and import manifests, not deep vendor flow integration.

### UVM/Testbench Semantics

No UVM build, sequence, scoreboard, objection, phase, factory, virtual-interface, register-model, or constrained-random workflow was exercised. Retrieval can ingest docs/logs, but there is no evidence of UVM-aware analysis.

### Large Designs And Multi-File RTL

The evaluated designs are small single-module fixtures. There is no demonstrated support for:

- include directories and macros
- packages/interfaces/modports
- parameterized hierarchy
- generated RTL
- filelists
- mixed Verilog/SystemVerilog/VHDL
- IP blocks and encrypted sources
- cross-file signal tracing

### Real Simulator Execution Of Generated Tests

Cocotb generation validation does not run cocotb or a simulator by default. SVA validation depends on available adapters and, in the default sample, produced no generation. The product is closer to "scaffold and review" than "generate, run, debug, and iterate."

## Agentic Verification Risks

### LLM Nondeterminism

Live OpenRouter worked in this run, but the evaluation did not repeat each live task multiple times because of cost/latency. There is no visible stability metric for prompt variance, model fallback, partial JSON compliance, or generation drift over time. The provider study records attempts, candidate IDs, validation status, and bounded stdout/stderr, which is a good base, but it should add repeated-run stability tests and compare semantic equivalence.

### Validation Narrowness

Many results are "validated" by syntax, fixture replay, or structural checks. That is much better than raw generation, but still weak relative to hardware engineer expectations. A syntactically valid cocotb file can fail to elaborate; a syntactically valid SVA file can bind incorrectly or assert the wrong behavior; a repair can pass a narrow lint fixture and break simulation semantics.

### Status Ambiguity

Multiple payloads contain both successful and failed status fields. This undermines trust and automation. A verification engineer needs a clear answer to: Did the original tool fail? Was a candidate produced? Was it applied? Did validation pass? What remains for review?

### Evidence Overreach

Triage likely causes and waveform evidence are presented with confident language even when the underlying evidence is shallow. In real workflows, wrong confidence costs time.

### Prompt/Artifact Persistence And IP Exposure

Telchines stores task artifacts, prompts, snippets, RTL/spec context, and model responses under `.tel/task-artifacts`. Secret-looking dictionary keys are redacted, but proprietary design content is intentionally preserved for replay. That is a reasonable tradeoff for local-first workflows, but it needs a stronger enterprise/IP story: retention controls, artifact scopes, encryption or secure storage options, and explicit warnings before using remote providers.

## UX And Workflow Fit

What feels good:

- CLI-first commands are discoverable and scriptable.
- JSON outputs are detailed enough for automation.
- Human outputs exist for triage and coverage.
- Review-gated repair is the right default.
- Run store, replay, and artifact review match verification needs.
- Provider policy controls are visible and inspectable.

What creates friction:

- Some common errors are attributed to the wrong layer.
- Shell output is duplicated and visually noisy.
- Some commands need project context but docs examples can look globally runnable.
- Default project config only includes heuristic provider, so live LLM setup requires manual config/harness use.
- Adapter availability is passive; missing real tools remain a dead end.
- Generated artifacts are validated narrowly, but the word `validated` can sound stronger than it is.
- Agent-runtime fallback reports `langgraph` runtime configured but actually uses `bounded_loop_no_langgraph` because LangGraph is not installed.

## Comparison Against Standard FPGA/ASIC Workflows

| Expected workflow need | Telchines today | Gap |
| --- | --- | --- |
| Understand project hierarchy and filelists | Indexes files and retrieves snippets | No demonstrated elaboration/filelist/package/include model |
| Run lint/sim/formal tools | Adapter abstraction exists; Icarus available locally | Many key tools missing; no vendor flow depth |
| Generate testbench code | Generates cocotb scaffold | Does not run simulator; limited behavioral depth |
| Generate SVA/formal checks | Works with provider fixtures/live OpenRouter in narrow cases | Default heuristic may no-op; formal execution not demonstrated |
| Debug compile failures | Simple repair loop works | Status ambiguity; narrow validation; simple patterns |
| Triage regression logs | Works for simple logs | Needs richer parsers, farm metadata, and duplicate suppression |
| Analyze waveforms | Basic VCD parsing | Wrong signal selection, shallow matching, no transaction-level insight |
| Integrate with external regression systems | Import manifest works | No native Jenkins/GitHub Actions/vendor/farm integrations beyond manifest |
| Preserve provenance | Strong run store and artifacts | IP retention and artifact lifecycle need enterprise controls |
| Use LLMs safely | Provider policies and redaction exist | Live-provider config rough; nondeterminism not quantified |

## Detailed Findings

### F1: Provider-study pass/fail logic can produce false greens

Severity: High

Evidence: Anthropic provider study JSON. Direct Anthropic failed HTTP 404, while `anthropic-agent` provider check and repair rows were marked passed. The parsed agent repair payload actually had `status: failed`, `no_patch`, and no validation run.

Impact: provider certification can claim readiness while the agent cannot produce a candidate.

Recommended fix: mark provider-study rows failed when parsed workflow payload is failed/no_patch/no_generation unless the scenario explicitly expects that. For `agent_runtime`, verify the base provider transport when live checks are requested.

### F2: Repair/agent payload status semantics are ambiguous

Severity: High

Evidence: `54_fresh_agent_repair.out.txt`, `28_repair_fixture_review.out.txt`, `29_repair_fixture_apply.out.txt`, OpenRouter provider study.

Impact: downstream automation may interpret `result.status: failed` as overall failure even when a patch was produced and validation passed, or may interpret exit 0 as success when no patch exists.

Recommended fix: separate fields clearly:

- `initial_tool_status`
- `candidate_status`
- `validation_status`
- `workflow_status`
- `review_status`

Keep `status` as the final workflow state only.

### F3: Waveform signal selection is unsafe

Severity: High

Evidence: `27_waveforms_inspect.out.txt` returned `clk` for requested `--signal rx`.

Impact: waveform debug can mislead engineers during root cause analysis.

Recommended fix: require exact signal or full-name match by default; support fuzzy search only when returning candidate matches and asking for disambiguation.

### F4: Generated cocotb "validated" does not mean executable test passed

Severity: Medium-High

Evidence: `31_gen_cocotb.out.txt` validation limitations explicitly state no simulator run.

Impact: users may overtrust generated tests.

Recommended fix: rename validation mode prominently in human output and JSON, and add optional executable smoke paths for installed simulators.

### F5: Anthropic provider default/config path failed live with HTTP 404

Severity: Medium-High

Evidence: `.test-work/provider-capability-study-eval/anthropic/anthropic_provider_capability_summary.json`.

Impact: native Anthropic provider not reliable in this environment despite key and live flag presence.

Recommended fix: verify endpoint construction, default model compatibility, and error messages. Add a dedicated Anthropic integration test that checks the exact URL and request shape without logging secrets.

### F6: OpenAI provider harness fails after setup when model env is missing

Severity: Medium

Evidence: `43_provider_study_openai_live.err.txt`.

Impact: poor setup feedback and dirty partial scratch setup.

Recommended fix: validate all required env/default values before scratch project creation; report skipped/missing-env consistently.

### F7: Missing file reported as provider error

Severity: Medium

Evidence: `63_missing_file_failure.err.txt`.

Impact: poor troubleshooting path.

Recommended fix: introduce `InputError`/`WorkflowInputError` and map it separately in CLI.

### F8: Shell output duplication reduces transcript quality

Severity: Medium

Evidence: `64_shell_smoke.out.txt`.

Impact: noisy logs make CI and support transcripts harder to read.

Recommended fix: de-duplicate startup render, command render, and transcript replay behavior.

### F9: Adapter surface is too thin for real verification flows

Severity: Medium

Evidence: `18_adapters_check_all.out.txt`, `46_tool_smoke_allow_missing.out.txt`.

Impact: without installed/configured toolchains and filelist support, Telchines cannot execute most realistic RTL verification tasks.

Recommended fix: expand adapter configuration to include filelists, include dirs, defines, top modules, work libraries, simulator args, and artifacts. Add adapter setup diagnostics and install guidance.

### F10: Benchmarks are useful but too fixture-shaped

Severity: Medium

Evidence: `50_tel_eval_run_sample.out.txt`.

Impact: passing benchmarks may overstate readiness.

Recommended fix: add larger multi-file designs, intentionally ambiguous logs, missing include cases, UVM-like logs, generated malformed LLM outputs, repeated live-provider runs, and real simulator/formal executions where tools exist.

## Recommended Development Priorities

### P0: Trust And Status Correctness

1. Redesign workflow status fields to avoid mixed `failed`/`passed` meanings in one payload.
2. Make provider-study pass/fail inspect semantic payloads, not only process exit.
3. Ensure agent-runtime live checks validate base provider reachability.
4. Classify input/configuration errors separately from provider errors.
5. Make validation labels precise: syntax-only, structure-only, adapter replay, simulator run, formal run.

### P1: Real Tool Execution

1. Add filelist/include/define/top-module support to adapters.
2. Add executable cocotb smoke mode for Icarus/Verilator when available.
3. Add SVA/formal execution paths for SymbiYosys or commercial formal tools.
4. Improve adapter setup diagnostics and remediation instructions.
5. Preserve tool command lines and environment summaries without leaking secrets.

### P2: Waveform And Debug Depth

1. Fix signal matching.
2. Add exact/full-name signal selection and ambiguity reporting.
3. Correlate log file/line/module names to relevant waveform scopes/signals.
4. Add transaction-level summaries for common buses where feasible.
5. Surface "waveform evidence is weak/unrelated" instead of attaching generic clock/reset excerpts.

### P3: LLM Reliability

1. Add repeated live-provider stability runs with bounded budgets.
2. Record model, provider, latency, retry count, JSON repair attempts, and validation deltas.
3. Add prompt-injection and context-contamination tests using RTL/docs/log corpora.
4. Add malformed/partial provider response tests to the benchmark suite.
5. Add model-specific capability matrices and deprecation checks.

### P4: Workflow Breadth

1. Add UVM/testbench log parsing and workflow-specific retrieval modes.
2. Add regression-manager connectors beyond JSON import, or provide documented adapters for common CI/farm systems.
3. Add coverage import for UCIS/vendor reports.
4. Add vendor FPGA build log support for Vivado/Quartus/Libero.
5. Add project templates for common verification setups.

### P5: UX And Documentation

1. Clarify that `tel eval run` requires project context or provide automatic scratch initialization.
2. Reduce shell duplicate output.
3. Add "what validation did not prove" prominently in human outputs.
4. Add a quick provider setup command or wizard that writes config entries referencing env vars, not secrets.
5. Add artifact retention/privacy guidance for proprietary RTL.

## Overall Assessment

Telchines has the right architectural instincts for agentic verification work: explicit workflows, retrieval with citations, adapter-backed validation, review-gated patches, stored artifacts, replay, and provider policies. Those choices directly address weaknesses in generic repo-chat assistants.

Its current implementation is best described as a strong v1 skeleton with several useful narrow workflows, not as a production-ready FPGA/ASIC verification assistant. The most urgent work is not adding more flashy generation; it is making success/failure impossible to misread, strengthening validation against real tools, and making live-provider and waveform behavior trustworthy under failure. Once those trust boundaries are firm, Telchines can expand into larger designs, richer EDA integrations, and genuinely agentic debug loops with much less risk.
