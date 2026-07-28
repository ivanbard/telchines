# Quickstart

## Install

Install the current release from PyPI:

```bash
pip install telchines
```

For local development from a checkout:

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -e .[dev]
```

Verify the install:

```bash
tel --version
tel --help
```

## Initialize A Project

From the root of a verification repo:

```bash
tel get-started
tel get-started --init
```

`tel get-started` only inspects the directory. `--init` asks for confirmation before creating `.tel/` and then builds the first retrieval index. It selects the next suggested workflow from available logs, coverage data, RTL, and documentation.

Experienced users can initialize directly:

```bash
tel project init .
tel project init . --template uvm
tel project templates
```

This creates `.tel/config.json` and the local storage directories used for indexing, runs, generated artifacts, and reports.
Templates add minimal local scaffolds for common setups such as `basic-rtl`, `cocotb`, `uvm`, `vivado`, `quartus`, and `libero`.
Their coverage sample is kept at `examples/coverage_template.json`; supply a real report before running `coverage-plan`.

## Build The Index

```bash
tel index
tel index status
```

Indexing scans the current project for RTL, docs, and logs and writes the retrieval index under `.tel/index/`.

Use `tel index status` to inspect freshness, chunk counts, and missing/stale/deleted source counts. Use `tel index clean` to remove the local project and external retrieval indexes before rebuilding.

For project-specific vocabulary, add `retrieval.aliases` in `.tel/config.json` so searches for team nicknames can expand to signal names or protocol terms. See `docs/external-retrieval-policy.md`.

## Inspect Providers

```bash
tel providers list
tel providers check heuristic
```

The default project uses the built-in `heuristic` provider for both `repair` and `generation`.

## Run Common Workflows

Retrieve:

```bash
tel retrieve "uart start bit behavior"
```

Triage regressions:

```bash
tel triage --logs logs/regressions --format human
```

Generate assertions:

```bash
tel gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
```

The JSON result includes `validation_mode`, `validation_status`, `validation_limitations`, `formal_status`, `command_artifacts`, and `setup_diagnostics`. Built-in SVA validation checks structure and obvious bind references; when Slang or Verilator is enabled and available, Telchines also runs adapter-backed parser/lint validation. SymbiYosys formal execution is attempted when configured and available.

Check optional open-source EDA tools before relying on adapter-backed validation:

```bash
tel adapters check
python scripts/tool_smoke.py --allow-missing
```

Missing Verilator, Slang, or SymbiYosys entries include Windows/MSYS2, Linux, source, Python fallback, or OSS CAD Suite setup hints where applicable. Slang validation can use either a `slang` executable or the optional `pyslang` package; Verilator and SymbiYosys still require external tool installs visible to the current shell.

Generate a cocotb scaffold:

```bash
tel gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md --intent "smoke the start-bit path"
```

The default cocotb path always runs `syntax_plus_structure`, which uses `py_compile` and confirms basic cocotb test shape. When cocotb, make, and an enabled simulator are available, Telchines also attempts an executable smoke and reports `compile_and_run`. Naming, output directories, and real-tool modes can be configured in `.tel/config.json` under `generation.sva` and `generation.cocotb`; see `docs/generated-artifacts.md`.

To enable executable cocotb smoke locally or in CI, install the optional dependency and run the explicit smoke lane:

```bash
python -m pip install -e ".[cocotb-smoke]"
python scripts/tool_smoke.py --adapters iverilog --cocotb
```

This requires `make`, `iverilog`, and `vvp`. Cocotb makefiles may be discovered through either `cocotb-config` or `python -m cocotb_tools.config`, so Python environments without the console-script shim can still run.

Plan coverage closure:

```bash
tel coverage-plan --report cov/coverage.json --rtl rtl/uart_rx.sv --spec docs/uart.md --format human
```

Run the review-gated agent path:

```bash
tel agent "fix the broken counter compile failure" --tool verilator --file rtl/broken_counter.sv
```

The agent command retrieves context, plans bounded tool actions, runs the selected workflow, validates candidates, and saves request, plan, response, and replay artifacts. Repair patches are not applied unless `--apply` is supplied.

## Import Regression Runs

Use `tel runs import MANIFEST` to bring external regression-manager or test-runner results into the local run store. The manifest is JSON with `schema_version: "0.1"`, a `tool` identity, and a `runs` list. Each run can include status, seed, logs, artifacts, waveforms, metadata, and an optional replay command.

```json
{
  "schema_version": "0.1",
  "tool": {"kind": "regression_manager", "name": "nightly", "version": "2026.06"},
  "runs": [
    {
      "name": "uart_rx_seed_1",
      "status": "failed",
      "seed": 1,
      "logs": ["logs/regressions/run_a.log"],
      "waveforms": ["logs/regressions/uart_rx_trace.vcd"],
      "metadata": {"suite": "smoke"}
    }
  ]
}
```

Imported runs use workflow type `regression_import`, appear in `tel runs list` and `tel runs show`, and their parsed log observations can be matched as similar prior runs during later triage.

Common CI/regression exports can be normalized into the same run store:

```bash
tel runs import-junit reports/junit.xml --dry-run
tel runs import-github-actions reports/gha-run.json
tel runs import-jenkins reports/jenkins-build.json
```

## Import Coverage Exports

Normalize coverage exports before planning closure:

```bash
tel coverage import reports/ucis.json --format ucis-json --output cov/coverage.json
tel coverage import reports/questa.txt --format questa-text --output cov/coverage.json
tel coverage-plan --report cov/coverage.json --format human
```

Supported first-pass import formats are `telchines-json`, `ucis-json`, `vivado`, `quartus`, and `questa-text`. Unsupported source lines are reported as import warnings.

## Use The Shell

```bash
tel
tel shell --plain
```

Shell history is private and disabled by default. Enable it only if you want commands stored beside your user-level Telchines settings:

```bash
tel history enable
tel history status
tel history clear
```

Useful starting commands:

```text
/help
/providers
/providers check heuristic
/index
/index status
/agent "fix the broken counter compile failure" --tool verilator --file rtl/broken_counter.sv
/triage --logs logs/regressions
/gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
/exit
```

## Privacy And Artifacts

```bash
tel doctor privacy
tel artifacts purge
tel artifacts purge --scope task-artifacts --older-than-days 30
tel artifacts purge --yes
tel artifacts review CANDIDATE_OR_VALIDATION_RUN_ID
tel runs replay RUN_ID
tel runs replay RUN_ID --yes
```

The purge and replay commands run as safe previews unless `--yes` is supplied. `artifacts purge` accepts `--scope` and `--older-than-days` when you want to enforce a narrower retention window. `artifacts review` compares the saved generated draft to the current workspace file so human edits stay visible.

Telchines stores replayable workflow evidence under `.tel/`, including generated artifacts, patches, reports, waveform summaries, replay metadata, and task artifacts. Task artifacts keep prompts, retrieved RTL/spec/log snippets, and provider responses so runs can be audited and replayed. Credential-looking dictionary fields are redacted before task artifacts are saved, but proprietary design content is intentionally retained until you purge it. Purge removes artifact payloads while preserving run records, retrieval contexts, observations, project config, and workspace files. Use local providers, `model_mode=local`, or `no_egress=true` when remote context sharing is not acceptable.
