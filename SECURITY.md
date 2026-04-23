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

## Response Goals

- confirm receipt promptly
- reproduce and assess severity
- prepare a fix and tests
- publish a documented release once the issue is patched
