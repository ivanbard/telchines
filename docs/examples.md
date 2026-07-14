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
- nearby waveforms are attached when present; waveform evidence includes `evidence_status` (`strong`, `weak`, or `unrelated`) plus `reason`, so generic or unrelated traces are disclosed instead of treated as root-cause evidence

For a targeted VCD investigation, keep the existing exact signal path or use a
clear hierarchy fragment, then constrain the timestamp window and correlate a
simulator log when it carries time annotations:

```bash
tel waveforms inspect logs/regressions/failure.vcd --signal u_rx.data_byte --start-time 120 --end-time 180 --log logs/regressions/failure.log --tolerance-ticks 2
```

The result keeps the original transition list and adds a time-window summary,
binary/hex bus values where VCD values are known, and project-local log-time
correlations.

## Example 2: Spec To SVA

```bash
tel index
tel gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
```

Expected outcome:

- a generated assertion artifact is written under `.tel/artifacts/generated/`
- validation mode, status, summary, and limitations are returned
- property summaries include requirement linkage and evidence paths

## Example 3: DUT To Cocotb Plus Coverage Planning

```bash
tel index
tel gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md --intent "smoke start bit"
tel coverage-plan --report cov/coverage.json --rtl rtl/uart_rx.sv --spec docs/uart.md --format human
```

Expected outcome:

- a cocotb scaffold and manifest are generated
- the scaffold is syntax-checked with `py_compile` and basic cocotb structure checks
- the coverage plan classifies likely causes such as `missing_stimulus`, `missing_checker`, or `unreachable`

## Notes

- `tel runs list` and `tel runs show <run_id>` let you inspect stored workflow history.
- `tel eval run` executes the built-in benchmark suite for release validation.
- `docs/generated-artifacts.md` documents generation conventions and validation limitations.
