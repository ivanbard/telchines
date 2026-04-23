# Worked Examples

These examples assume a small verification repo with `docs/`, `rtl/`, `logs/`, and optional `cov/` content.

## Example 1: Regression Triage

```bash
tel index
tel triage --logs logs/regressions --format human
```

Expected outcome:

- failures are clustered
- each cluster includes likely cause and suggested next action
- evidence citations point at logs, docs, or RTL
- nearby waveforms are attached when present

## Example 2: Spec To SVA

```bash
tel index
tel gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
```

Expected outcome:

- a generated assertion artifact is written under `.tel/artifacts/generated/`
- a validation status is returned
- property summaries include requirement linkage and evidence paths

## Example 3: DUT To Cocotb Plus Coverage Planning

```bash
tel index
tel gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md --intent "smoke start bit"
tel coverage-plan --report cov/coverage.json --rtl rtl/uart_rx.sv --spec docs/uart.md --format human
```

Expected outcome:

- a cocotb scaffold and manifest are generated
- the scaffold is syntax-checked with `py_compile`
- the coverage plan classifies likely causes such as `missing_stimulus`, `missing_checker`, or `unreachable`

## Notes

- `tel runs list` and `tel runs show <run_id>` let you inspect stored workflow history.
- `tel eval run` executes the built-in benchmark suite for release validation.
