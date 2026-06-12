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

## Index Controls

Use `tel index status` to inspect whether project and external indexes exist, when they were built, and whether sources are missing, stale, or deleted. Use `tel index clean` to remove the local indexes before rebuilding with `tel index`.

Large or noisy projects can narrow indexed files with retrieval glob patterns in `.tel/config.json`:

```json
{
  "retrieval": {
    "chunk_lines": 20,
    "max_hits": 5,
    "external_roots": [],
    "external_index_dir": ".tel/external-index",
    "include_patterns": ["rtl/**", "docs/**", "logs/**/*.log"],
    "exclude_patterns": ["logs/generated/**", "vendor/**"],
    "aliases": {
      "framing pulse": ["start bit", "serial_i"],
      "objection drain": ["timeout", "uvm_objection"]
    }
  }
}
```

Patterns are project-relative. Telchines still skips generated/cache directories such as `.git`, `.tel`, `.venv`, `.test-work`, and `__pycache__`.

`retrieval.aliases` expands query tokens at search time without rewriting the index. Use it for team vocabulary, protocol nicknames, and signal-name synonyms that pure token overlap would otherwise miss. Aliases are intentionally local and explicit; they are not semantic embeddings.
