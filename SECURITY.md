# Security Policy

## Supported Versions

Security fixes are applied to the latest `1.x` release line.

## Reporting A Vulnerability

Do not open public GitHub issues for suspected vulnerabilities.

Report privately to the maintainers with:

- affected version
- impact summary
- reproduction steps
- any relevant logs or proof-of-concept material

If the issue involves model-provider handling, retrieval policy boundaries, local command execution, or artifact disclosure, include the relevant `.tel/config.json` settings and the exact CLI command used.

## Scope

Security-sensitive areas include:

- local command provider execution
- remote provider configuration and credential handling
- retrieval boundary enforcement for `external_roots`
- replay artifact persistence
- policy enforcement for `model_mode` and `no_egress`

## Local Privacy Checks

Run:

```bash
tel doctor privacy
tel artifacts purge
```

`doctor privacy` reports provider and artifact-storage risks such as local command execution and remote-provider egress. `artifacts purge` is a dry run by default; add `--yes` to remove generated artifacts, task artifacts, saved patch/generation payloads, waveform summaries, and reports while preserving run metadata.

`runs replay` is also confirmation-gated. `tel runs replay RUN_ID` prints the stored command and exits without executing it; add `--yes` only after reviewing the command. Validation workflows copy projects to temporary directories for isolated checks and skip `.tel`, VCS/build/cache directories, and symlinks so replay artifacts, local metadata, and external symlink targets are not copied into validator workspaces.

## Response Goals

- confirm receipt promptly
- reproduce and assess severity
- prepare a fix and tests
- publish a documented release once the issue is patched
