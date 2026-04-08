from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from telchines.config import ProjectConfig
from telchines.models import RetrievalContext, RetrievalHit
from telchines.utils import load_text, read_json, sha256_file, stable_id, tokenize, utc_now, write_json

INDEX_FILENAME = "index.json"
SUPPORTED_EXTENSIONS = {".sv", ".svh", ".v", ".vh", ".md", ".txt", ".log", ".py"}
SKIP_DIR_NAMES = {".git", ".venv", ".tel", ".tel-scratch", ".pytest_tmp", ".test-work", "__pycache__"}


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
        chunks: list[dict[str, object]] = []
        chunk_lines = int(self.config.retrieval.get("chunk_lines", 20))
        for path in sorted(self.config.project_root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            relative_path = path.relative_to(self.config.project_root)
            if any(part in SKIP_DIR_NAMES for part in relative_path.parts):
                continue
            relative = str(relative_path)
            kind = self._kind_for_path(path)
            try:
                lines = load_text(path).splitlines()
            except UnicodeDecodeError:
                continue
            if not lines:
                continue
            for start in range(0, len(lines), chunk_lines):
                block = lines[start : start + chunk_lines]
                chunk = IndexedChunk(
                    path=relative,
                    kind=kind,
                    start_line=start + 1,
                    end_line=start + len(block),
                    hash=sha256_file(path),
                    text="\n".join(block),
                )
                chunks.append(asdict(chunk))
        write_json(self.index_path, {"chunks": chunks, "built_at": utc_now()})
        return len(chunks)

    def search(self, query: str, limit: int | None = None) -> RetrievalContext:
        if not self.index_path.exists():
            self.build_index()
        payload = read_json(self.index_path)
        query_tokens = tokenize(query)
        limit = limit or int(self.config.retrieval.get("max_hits", 5))
        scored: list[RetrievalHit] = []
        for chunk in payload["chunks"]:
            chunk_text = str(chunk["text"])
            chunk_tokens = tokenize(chunk_text)
            overlap = len(set(query_tokens) & set(chunk_tokens))
            if overlap == 0:
                continue
            score = overlap / max(len(set(query_tokens)), 1)
            scored.append(
                RetrievalHit(
                    path=str(chunk["path"]),
                    kind=str(chunk["kind"]),
                    score=round(score, 3),
                    start_line=int(chunk["start_line"]),
                    end_line=int(chunk["end_line"]),
                    snippet=chunk_text,
                )
            )
        scored.sort(key=lambda hit: (-hit.score, hit.path, hit.start_line))
        return RetrievalContext(
            context_id=stable_id("ctx", self.config.project.project_id, query, utc_now()),
            project_id=self.config.project.project_id,
            query=query,
            hits=scored[:limit],
            created_at=utc_now(),
        )

    def _kind_for_path(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".sv", ".svh", ".v", ".vh"}:
            return "rtl"
        if suffix == ".log":
            return "log"
        if suffix == ".py":
            return "script"
        return "doc"
