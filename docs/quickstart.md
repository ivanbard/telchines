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
tel project init .
```

This creates `.tel/config.json` and the local storage directories used for indexing, runs, generated artifacts, and reports.

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

The JSON result includes `validation_mode`, `validation_status`, and `validation_limitations`. Built-in SVA validation checks structure and obvious bind references; when Slang or Verilator is enabled and available, Telchines also runs adapter-backed parser/lint validation.

Generate a cocotb scaffold:

```bash
tel gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md --intent "smoke the start-bit path"
```

The default cocotb validation mode is `python_syntax_plus_structure`, which runs `py_compile` and confirms basic cocotb test shape. Naming and output directories can be configured in `.tel/config.json` under `generation.sva` and `generation.cocotb`; see `docs/generated-artifacts.md`.

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

## Use The Shell

```bash
tel
tel shell --plain
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
tel artifacts purge --yes
tel artifacts review CANDIDATE_OR_VALIDATION_RUN_ID
tel runs replay RUN_ID
tel runs replay RUN_ID --yes
```

The purge and replay commands run as safe previews unless `--yes` is supplied. `artifacts review` compares the saved generated draft to the current workspace file so human edits stay visible.
