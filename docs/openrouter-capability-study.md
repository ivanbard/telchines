# OpenRouter Capability Study Harness

Telchines includes an opt-in live-provider harness for repeating the OpenRouter agentic workflow study without committing raw `.tel` artifacts.

Run a no-network preview:

```bash
python scripts/openrouter_capability_study.py --dry-run
```

Run the live study:

```bash
OPENROUTER_API_KEY=... python scripts/openrouter_capability_study.py
```

The harness copies benchmark fixtures into `.test-work/openrouter-capability-study`, writes `.tel/config.json` with OpenRouter provider entries that reference `OPENROUTER_API_KEY` by name, and runs provider checks, agent repair, SVA generation, cocotb generation, and a shell smoke. It emits:

- `.test-work/openrouter-capability-study/openrouter_capability_summary.json`
- `.test-work/openrouter-capability-study/openrouter_capability_summary.md`

Secrets are not printed or stored. Raw stdout and stderr are bounded in the JSON summary. Live mode exits as `skipped_missing_key` when `OPENROUTER_API_KEY` is absent, so CI can call the script without spending provider budget.

Use `scripts/tool_smoke.py --allow-missing` to check optional real-tool lanes. Missing Verilator, Slang, SymbiYosys, Icarus, or cocotb-related tooling is reported as a skip rather than a hard failure when that flag is set.
