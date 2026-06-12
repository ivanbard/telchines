# Quickstart

## Install

Use the source install while PyPI trusted publishing is being finalized:

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -e .[dev]
```

After the PyPI publisher is configured, the package install path will be:

```bash
pip install telchines
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
/triage --logs logs/regressions
/gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
/exit
```

## Privacy And Artifacts

```bash
tel doctor privacy
tel artifacts purge
tel artifacts purge --yes
tel runs replay RUN_ID
tel runs replay RUN_ID --yes
```

The purge and replay commands run as safe previews unless `--yes` is supplied.
