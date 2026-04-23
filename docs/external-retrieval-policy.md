# External Retrieval Policy

Telchines supports external retrieval roots through `retrieval.external_roots`, but `v1` treats this as curated, local, and documentation-heavy rather than open-ended scraping.

## v1 Policy

Allowed source classes:

- project-adjacent internal notes checked out locally
- locally mirrored protocol or verification references
- team-curated verification notes stored in plain files under approved roots

Not included in `v1`:

- live web crawling
- automatic remote sync
- opaque binary corpora with no review path
- unlicensed third-party content copied into the retrieval index without review

## Requirements For External Sources

Each external root should be:

- explicitly configured relative to the project root
- locally present before indexing
- reviewable by a human
- permitted by the source license or internal policy

## Ranking Behavior

Project-local evidence remains the primary source of truth.

External documents are intended to supplement, not replace, project memory. Retrieval should not crowd out repository specs, RTL, logs, or prior run evidence when those sources are available and relevant.

## Provenance Expectations

External hits should preserve:

- file path or local citation
- retrieval mode
- enough source identity for a reviewer to inspect the original material

## Recommended Use

Use external roots for:

- protocol reminders
- debugging heuristics
- domain-specific verification notes

Do not use them as a substitute for repo-specific requirements or authoritative design intent.
