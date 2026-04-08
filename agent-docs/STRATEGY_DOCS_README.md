# Telchines Strategy Docs

This folder contains initial strategy documents for an open-source AI platform focused on ASIC, FPGA, and RTL verification workflows.

## Files

- `ASIC_VERIFICATION_AI_MASTER_PLAN.md`
  - Master strategy, architecture, roadmap, repo structure, and Codex blueprint

- `ASIC_VERIFICATION_AI_PRODUCT_SPEC.md`
  - Product specification covering users, goals, functional requirements, workflows, and MVP scope

## Recommended Usage

1. Commit both files into `docs/strategy/`
2. Use the master plan as the long-form strategic source of truth
3. Use the product spec as the implementation-facing requirements document
4. Derive the next files from these docs:
   - roadmap
   - data model
   - workflow specs
   - benchmark plan
   - initial repository README

## Suggested Follow-up

Use these docs to prompt Codex to:
- scaffold the repository
- create schemas and adapters
- implement the run store
- add CLI commands
- build the first repair and triage workflows
