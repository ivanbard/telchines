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
}


@dataclass(slots=True)
class IndexedChunk:
    path: str
    kind: str
    start_line: int
    end_line: int
    hash: str
    text: str


class RetrievalService:
    def __init__(self, config: ProjectConfig) -> None:
        self.config = config
        self.index_root = config.project_root / config.index_dir
        self.index_path = self.index_root / INDEX_FILENAME

    def build_index(self) -> int:
        existing_chunks = self._load_existing_chunks()
        chunks: list[dict[str, object]] = []
        for path in self._iter_indexable_files():
            relative_path = path.relative_to(self.config.project_root)
            relative = str(relative_path)
            source_hash = sha256_file(path)
            cached = existing_chunks.get(relative, [])
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
            chunks.extend(asdict(chunk) for chunk in self._chunk_file(relative, kind, text, source_hash))
        write_json(
            self.index_path,
            {
                "format_version": INDEX_FORMAT_VERSION,
                "built_at": utc_now(),
                "chunk_count": len(chunks),
                "chunks": chunks,
            },
        )
        return len(chunks)

    def search(
        self,
        query: str,
        limit: int | None = None,
        *,
        mode: str = "general",
        focus_paths: list[str] | None = None,
    ) -> RetrievalContext:
        if not self.index_path.exists():
            self.build_index()
        payload = read_json(self.index_path)
        if payload.get("format_version") != INDEX_FORMAT_VERSION:
            self.build_index()
            payload = read_json(self.index_path)
        query_tokens = set(tokenize(query))
        focus_paths = [path.replace("\\", "/") for path in (focus_paths or []) if path]
        focus_tokens = set(tokenize(" ".join(focus_paths)))
        limit = limit or int(self.config.retrieval.get("max_hits", 5))
        boosts = MODE_KIND_BOOSTS.get(mode, MODE_KIND_BOOSTS["general"])

        scored: list[RetrievalHit] = []
        for chunk in payload["chunks"]:
            chunk_path = str(chunk["path"]).replace("\\", "/")
            chunk_text = str(chunk["text"])
            chunk_tokens = set(tokenize(chunk_text))
            path_tokens = set(tokenize(chunk_path))
            token_overlap = len(query_tokens & chunk_tokens)
            path_overlap = len((query_tokens | focus_tokens) & path_tokens)
            exact_focus_match = any(chunk_path == focus_path or chunk_path.endswith(focus_path) for focus_path in focus_paths)
            kind = str(chunk["kind"])
            if token_overlap == 0 and path_overlap == 0 and not exact_focus_match:
                continue
            coverage = token_overlap / max(len(query_tokens), 1)
            score = coverage
            score += 0.18 * path_overlap
            score += boosts.get(kind, 0.0)
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
                )
            )
        scored.sort(key=lambda hit: (-hit.score, hit.kind != "doc", hit.path, hit.start_line))
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

    def _load_existing_chunks(self) -> dict[str, list[dict[str, object]]]:
        if not self.index_path.exists():
            return {}
        payload = read_json(self.index_path)
        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        for chunk in payload.get("chunks", []):
            grouped[str(chunk["path"])].append(chunk)
        return grouped

    def _iter_indexable_files(self) -> list[Path]:
        files: list[Path] = []
        for path in sorted(self.config.project_root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            relative_path = path.relative_to(self.config.project_root)
            if any(part in SKIP_DIR_NAMES for part in relative_path.parts):
                continue
            files.append(path)
        return files

    def _chunk_file(self, relative_path: str, kind: str, text: str, source_hash: str) -> list[IndexedChunk]:
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
