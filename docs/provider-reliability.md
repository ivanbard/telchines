# Provider Reliability

Telchines treats live model access as a blocking release-certification concern, not proof that a provider is useful for verification work. Offline tests cover deterministic behavior without credentials. Live certification uses copied fixtures, explicit opt-in, hard budget reservation, and redacted evidence.

## Initial certification profiles

The first-party profiles are pinned in `docs/provider-certifications/`:

- OpenAI: `openai.json`
- Anthropic: `anthropic.json`

Each v2 manifest records the exact suite digest, requested and allowed observed model IDs, exactly three repeats, request/input/output-token/cost ceilings, and timeouts. It contains only environment-variable names and no credentials.

Run certification only after setting the relevant provider credential and its provider-study gate, then explicitly enabling the shared certification gate:

```bash
TELCHINES_LIVE_CERTIFY=1 \
TELCHINES_LIVE_OPENAI=1 \
OPENAI_API_KEY=... \
tel certify providers docs/provider-certifications/openai.json --include-live
```

The command delegates live workflows to an isolated scratch project. It refuses an over-budget run before dispatch, reserves the maximum allowed spend when provider usage is unavailable, removes raw task-response artifacts after extracting the review bundle, and emits a redacted certificate. OpenAI Responses requests use `store: false`.

The release workflow runs the two providers independently on Linux, uploads each redacted certificate and fixture-only review bundle, then waits for the `release-certification` GitHub Environment approval. Configure that environment with required maintainer reviewers and repository secrets `TELCHINES_LIVE_CERTIFY=1`, the provider live gates, and the two API keys. A missing, failed, stale, unapproved, or model-mismatched certificate blocks PyPI publication.

## What passes certification

Certification requires three passes for each live provider probe, repair, SVA, Cocotb, and shell case; deterministic contracts cover triage grounding, missing-context and malicious-instruction refusal, malformed-output recovery, policy enforcement, redaction, and review gates. A maintainer reviews each provider's fixture-only bundle before approval. A structural or adapter pass remains distinct from functional simulation or formal proof.

OpenRouter is intentionally a follow-on compatibility lane. It must use the same fixture suite and scorecard after direct OpenAI and Anthropic profiles are stable.
