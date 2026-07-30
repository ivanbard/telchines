# Provider Reliability

Telchines treats live model access as a certification concern, not proof that a provider is useful for verification work. Offline tests cover deterministic behavior without credentials. Live certification uses copied fixtures, explicit opt-in, bounded requests, and redacted evidence.

## Initial certification profiles

The first-party profiles are pinned in `docs/provider-certifications/`:

- OpenAI: `openai.json`
- Anthropic: `anthropic.json`

Each manifest records the fixture-suite version, pinned model, three-run minimum, request ceiling, output-token ceiling, cost approval ceiling, and timeout. It contains only environment-variable names and no credentials.

Run certification only after setting the relevant provider credential and its provider-study gate, then explicitly enabling the shared certification gate:

```bash
TELCHINES_LIVE_CERTIFY=1 \
TELCHINES_LIVE_OPENAI=1 \
OPENAI_API_KEY=... \
tel certify providers docs/provider-certifications/openai.json --include-live
```

The command delegates to the provider capability study in an ignored scratch project. It preserves the study's redacted JSON and Markdown evidence and applies a hard request cap. The recorded token and cost ceilings are release-approval limits; do not approve a release until the provider report, dashboard billing, and configured model limits agree.

## What passes certification

Certification requires schema-compliant model responses, grounded citations, valid signal references, review-gated drafts, structural/adapter validation reporting, and safe refusal or clarification when inputs are insufficient. A structural or adapter pass remains distinct from functional simulation or formal proof.

OpenRouter is intentionally a follow-on compatibility lane. It must use the same fixture suite and scorecard after direct OpenAI and Anthropic profiles are stable.
