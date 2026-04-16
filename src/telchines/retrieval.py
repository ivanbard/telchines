from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from telchines.config import ProjectConfig
from telchines.models import RetrievalContext, RetrievalHit
from telchines.utils import load_text, read_json, sha256_file, stable_id, tokenize, utc_now, write_json

INDEX_FILENAME = "index.json"
INDEX_FORMAT_VERSION = 2
SUPPORTED_EXTENSIONS = {".sv", ".svh", ".v", ".vh", ".md", ".txt", ".log", ".out", ".err", ".py"}
SKIP_DIR_NAMES = {".git", ".venv", ".tel", ".tel-scratch", ".pytest_tmp", ".test-work", "__pycache__"}
LOG_MARKERS = ("error", "warning", "fail", "timeout", "assert")
MODE_KIND_BOOSTS = {
    "general": {"rtl": 0.15, "doc": 0.1, "log": 0.1, "script": 0.05},
    "repair": {"rtl": 0.45, "doc": 0.2, "log": 0.05, "script": 0.0},
    "triage": {"log": 0.45, "rtl": 0.25, "doc": 0.15, "script": 0.0},
    "generation": {"doc": 0.45, "rtl": 0.35, "log": 0.0, "script": 0.0},
    "coverage": {"doc": 0.35, "rtl": 0.35, "log": 0.05, "script": 0.0},
}
MODE_DOMAIN_BOOSTS = {
    "general": {"project": 0.12, "external": 0.0},
    "repair": {"project": 0.3, "external": -0.12},
    "triage": {"project": 0.35, "external": -0.15},
    "generation": {"project": 0.16, "external": 0.06},
    "coverage": {"project": 0.24, "external": -0.05},
}


@dataclass(slots=True)
class IndexedChunk:
    path: str
    kind: str
    start_line: int
    end_line: int
    hash: str
    text: str
    source_domain: str = "project"
    source_label: str = "project"
    source_uri: str = ""
    ingested_at: str = ""


class RetrievalService:
    def __init__(self, config: ProjectConfig) -> None:
        self.config = config
        self.index_root = config.project_root / config.index_dir
        self.index_path = self.index_root / INDEX_FILENAME
        external_index_dir = str(config.retrieval.get("external_index_dir", ".tel/external-index"))
        self.external_index_root = config.project_root / external_index_dir
        self.external_index_path = self.external_index_root / INDEX_FILENAME

    def build_index(self) -> int:
        built_at = utc_now()
        project_chunks = self._build_chunks(
            self._iter_project_indexable_files(),
            self._load_existing_chunks(self.index_path),
            built_at=built_at,
        )
        external_chunks = self._build_chunks(
            self._iter_external_indexable_files(),
            self._load_existing_chunks(self.external_index_path),
            built_at=built_at,
        )
        self._write_index(self.index_path, project_chunks, built_at=built_at)
        self._write_index(self.external_index_path, external_chunks, built_at=built_at)
        return len(project_chunks) + len(external_chunks)

    def search(
        self,
        query: str,
        limit: int | None = None,
        *,
        mode: str = "general",
        focus_paths: list[str] | None = None,
    ) -> RetrievalContext:
        if not self.index_path.exists() or not self.external_index_path.exists():
            self.build_index()
        payload = self._load_index_payload(self.index_path)
        external_payload = self._load_index_payload(self.external_index_path)
        query_tokens = set(tokenize(query))
        focus_paths = [path.replace("\\", "/") for path in (focus_paths or []) if path]
        focus_tokens = set(tokenize(" ".join(focus_paths)))
        limit = limit or int(self.config.retrieval.get("max_hits", 5))
        boosts = MODE_KIND_BOOSTS.get(mode, MODE_KIND_BOOSTS["general"])
        domain_boosts = MODE_DOMAIN_BOOSTS.get(mode, MODE_DOMAIN_BOOSTS["general"])

        scored: list[RetrievalHit] = []
        for chunk in [*payload.get("chunks", []), *external_payload.get("chunks", [])]:
            chunk_path = str(chunk["path"]).replace("\\", "/")
            chunk_text = str(chunk["text"])
            chunk_tokens = set(tokenize(chunk_text))
            path_tokens = set(tokenize(chunk_path))
            token_overlap = len(query_tokens & chunk_tokens)
            path_overlap = len((query_tokens | focus_tokens) & path_tokens)
            exact_focus_match = any(chunk_path == focus_path or chunk_path.endswith(focus_path) for focus_path in focus_paths)
            kind = str(chunk["kind"])
            source_domain = str(chunk.get("source_domain", "project"))
            if token_overlap == 0 and path_overlap == 0 and not exact_focus_match:
                continue
            coverage = token_overlap / max(len(query_tokens), 1)
            score = coverage
            score += 0.18 * path_overlap
            score += boosts.get(kind, 0.0)
            score += domain_boosts.get(source_domain, 0.0)
            if exact_focus_match:
                score += 0.55
            elif focus_tokens and path_overlap:
                score += 0.15
            scored.append(
                RetrievalHit(
                    path=chunk_path,
                    kind=kind,
                    score=round(score, 3),
                    start_line=int(chunk["start_line"]),
                    end_line=int(chunk["end_line"]),
                    snippet=chunk_text,
                    citation=self.format_citation(chunk_path, int(chunk["start_line"]), int(chunk["end_line"])),
                    source_hash=str(chunk["hash"]),
                    source_domain=source_domain,
                    source_label=str(chunk.get("source_label", source_domain)),
                    source_uri=str(chunk.get("source_uri", chunk_path)),
                    ingested_at=str(chunk.get("ingested_at", payload.get("built_at") or external_payload.get("built_at") or "")),
                )
            )
        scored.sort(key=lambda hit: (-hit.score, hit.source_domain != "project", hit.kind != "doc", hit.path, hit.start_line))
        selected = self._select_hits(scored, limit)
        return RetrievalContext(
            context_id=stable_id("ctx", self.config.project.project_id, mode, query, utc_now()),
            project_id=self.config.project.project_id,
            query=query,
            hits=selected,
            created_at=utc_now(),
            mode=mode,
            metadata={"focus_paths": focus_paths},
        )

    def format_citation(self, path: str, start_line: int, end_line: int) -> str:
        if start_line == end_line:
            return f"{path}:{start_line}"
        return f"{path}:{start_line}-{end_line}"

    def _load_index_payload(self, path: Path) -> dict[str, object]:
        if not path.exists():
            return {"format_version": INDEX_FORMAT_VERSION, "built_at": "", "chunk_count": 0, "chunks": []}
        payload = read_json(path)
        if payload.get("format_version") != INDEX_FORMAT_VERSION:
            self.build_index()
            payload = read_json(path)
        return payload

    def _load_existing_chunks(self, index_path: Path) -> dict[str, list[dict[str, object]]]:
        if not index_path.exists():
            return {}
        payload = read_json(index_path)
        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        for chunk in payload.get("chunks", []):
            key = self._cache_key(str(chunk["path"]), str(chunk.get("source_domain", "project")), str(chunk.get("source_label", "project")))
            grouped[key].append(chunk)
        return grouped

    def _iter_project_indexable_files(self) -> list[tuple[Path, str, str, str]]:
        files: list[tuple[Path, str, str, str]] = []
        external_roots = [root.resolve() for root, _ in self._configured_external_roots()]
        for path in sorted(self.config.project_root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            relative_path = path.relative_to(self.config.project_root)
            if any(part in SKIP_DIR_NAMES for part in relative_path.parts):
                continue
            resolved = path.resolve()
            if any(self._is_relative_to(resolved, root) for root in external_roots):
                continue
            relative = str(relative_path).replace("\\", "/")
            files.append((path, relative, "project", "project"))
        return files

    def _iter_external_indexable_files(self) -> list[tuple[Path, str, str, str]]:
        files: list[tuple[Path, str, str, str]] = []
        for root, label in self._configured_external_roots():
            candidates = [root] if root.is_file() else sorted(root.rglob("*"))
            for path in candidates:
                if not path.is_file():
                    continue
                if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                relative_parts = path.relative_to(root).parts if self._is_relative_to(path.resolve(), root.resolve()) else path.parts
                if any(part in SKIP_DIR_NAMES for part in relative_parts):
                    continue
                try:
                    relative = str(path.resolve().relative_to(self.config.project_root.resolve())).replace("\\", "/")
                except ValueError:
                    relative = f"{label}/{path.name}"
                files.append((path, relative, "external", label))
        return files

    def _configured_external_roots(self) -> list[tuple[Path, str]]:
        roots: list[tuple[Path, str]] = []
        for value in self.config.retrieval.get("external_roots", []):
            normalized = str(value).replace("\\", "/")
            candidate = (self.config.project_root / value).resolve()
            if not candidate.exists():
                continue
            roots.append((candidate, normalized))
        return roots

    def _chunk_file(self, relative_path: str, kind: str, text: str, source_hash: str) -> list[IndexedChunk]:
        return self._chunk_file_with_metadata(
            relative_path,
            kind,
            text,
            source_hash,
            source_domain="project",
            source_label="project",
            source_uri=relative_path,
            ingested_at=utc_now(),
        )

    def _chunk_file_with_metadata(
        self,
        relative_path: str,
        kind: str,
        text: str,
        source_hash: str,
        *,
        source_domain: str,
        source_label: str,
        source_uri: str,
        ingested_at: str,
    ) -> list[IndexedChunk]:
        lines = text.splitlines()
        if not lines:
            return []
        ranges = self._chunk_ranges(kind, lines)
        return [
            IndexedChunk(
                path=relative_path,
                kind=kind,
                start_line=start_line,
                end_line=end_line,
                hash=source_hash,
                text="\n".join(lines[start_line - 1 : end_line]),
                source_domain=source_domain,
                source_label=source_label,
                source_uri=source_uri,
                ingested_at=ingested_at,
            )
            for start_line, end_line in ranges
        ]

    def _chunk_ranges(self, kind: str, lines: list[str]) -> list[tuple[int, int]]:
        base_chunk_lines = int(self.config.retrieval.get("chunk_lines", 20))
        if kind == "rtl":
            return self._rtl_ranges(lines, max_lines=max(8, base_chunk_lines))
        if kind == "doc":
            return self._doc_ranges(lines, max_lines=max(6, base_chunk_lines * 2))
        if kind == "log":
            return self._log_ranges(lines, window=max(2, base_chunk_lines // 4))
        return self._fixed_ranges(lines, max_lines=base_chunk_lines)

    def _fixed_ranges(self, lines: list[str], max_lines: int) -> list[tuple[int, int]]:
        return [(start + 1, min(len(lines), start + max_lines)) for start in range(0, len(lines), max_lines)]

    def _rtl_ranges(self, lines: list[str], max_lines: int) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        start = 0
        structural_tokens = ("module", "interface", "package", "function", "task", "always", "initial")
        for index, line in enumerate(lines):
            stripped = line.strip().lower()
            should_split = (
                index > start
                and any(stripped.startswith(token) for token in structural_tokens)
                and (index - start) >= max(4, max_lines // 3)
            )
            if should_split or (index - start + 1) >= max_lines:
                ranges.append((start + 1, index))
                start = index
        ranges.append((start + 1, len(lines)))
        return self._normalized_ranges(ranges)

    def _doc_ranges(self, lines: list[str], max_lines: int) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        start = 0
        for index, line in enumerate(lines):
            stripped = line.strip()
            should_split = index > start and (
                stripped.startswith("#")
                or (not stripped and (index - start) >= max(3, max_lines // 3))
                or (index - start + 1) >= max_lines
            )
            if should_split:
                ranges.append((start + 1, index))
                start = index + (0 if stripped.startswith("#") else 1)
        if start < len(lines):
            ranges.append((start + 1, len(lines)))
        return self._normalized_ranges(ranges)

    def _log_ranges(self, lines: list[str], window: int) -> list[tuple[int, int]]:
        interesting: list[tuple[int, int]] = []
        for index, line in enumerate(lines):
            lowered = line.lower()
            if any(marker in lowered for marker in LOG_MARKERS):
                interesting.append((max(0, index - window), min(len(lines) - 1, index + window)))
        if not interesting:
            return self._fixed_ranges(lines, max(6, int(self.config.retrieval.get("chunk_lines", 20)) // 2))
        merged: list[tuple[int, int]] = []
        for start, end in interesting:
            if not merged or start > (merged[-1][1] + 1):
                merged.append((start, end))
                continue
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        return [(start + 1, end + 1) for start, end in merged]

    def _normalized_ranges(self, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        normalized: list[tuple[int, int]] = []
        for start, end in ranges:
            if start > end:
                continue
            normalized.append((start, end))
        return normalized

    def _select_hits(self, hits: list[RetrievalHit], limit: int) -> list[RetrievalHit]:
        selected: list[RetrievalHit] = []
        per_path_counts: dict[str, int] = defaultdict(int)
        for hit in hits:
            if per_path_counts[hit.path] >= 2:
                continue
            selected.append(hit)
            per_path_counts[hit.path] += 1
            if len(selected) == limit:
                break
        return selected

    def _kind_for_path(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".sv", ".svh", ".v", ".vh"}:
            return "rtl"
        if suffix in {".log", ".out", ".err"}:
            return "log"
        if suffix == ".py":
            return "script"
        return "doc"

    def _build_chunks(
        self,
        entries: list[tuple[Path, str, str, str]],
        existing_chunks: dict[str, list[dict[str, object]]],
        *,
        built_at: str,
    ) -> list[dict[str, object]]:
        chunks: list[dict[str, object]] = []
        for path, relative, source_domain, source_label in entries:
            source_hash = sha256_file(path)
            cache_key = self._cache_key(relative, source_domain, source_label)
            cached = existing_chunks.get(cache_key, [])
            if cached and all(str(chunk.get("hash", "")) == source_hash for chunk in cached):
                chunks.extend(cached)
                continue
            try:
                text = load_text(path)
            except UnicodeDecodeError:
                continue
            if not text.strip():
                continue
            kind = self._kind_for_path(path)
            source_uri = source_label if source_domain == "external" else relative
            chunks.extend(
                asdict(chunk)
                for chunk in self._chunk_file_with_metadata(
                    relative,
                    kind,
                    text,
                    source_hash,
                    source_domain=source_domain,
                    source_label=source_label,
                    source_uri=source_uri,
                    ingested_at=built_at,
                )
            )
        return chunks

    def _write_index(self, path: Path, chunks: list[dict[str, object]], *, built_at: str) -> None:
        write_json(
            path,
            {
                "format_version": INDEX_FORMAT_VERSION,
                "built_at": built_at,
                "chunk_count": len(chunks),
                "chunks": chunks,
            },
        )

    def _cache_key(self, relative_path: str, source_domain: str, source_label: str) -> str:
        return f"{source_domain}::{source_label}::{relative_path}"

    def _is_relative_to(self, candidate: Path, root: Path) -> bool:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            return False
