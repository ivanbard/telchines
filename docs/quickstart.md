# Quickstart

## Install

```bash
pip install telchines
```

Or from source:

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
```

Indexing scans the current project for RTL, docs, and logs and writes the retrieval index under `.tel/index/`.

## Inspect Providers

```bash
tel providers list
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

Generate a cocotb scaffold:

```bash
tel gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md --intent "smoke the start-bit path"
```

Plan coverage closure:

```bash
tel coverage-plan --report cov/coverage.json --rtl rtl/uart_rx.sv --spec docs/uart.md --format human
```

## Use The Shell

```bash
tel
```

Useful starting commands:

```text
/help
/providers
/index
/triage --logs logs/regressions
/gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
/exit
```
