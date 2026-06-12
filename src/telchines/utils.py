from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

TEMP_COPY_EXCLUDE_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tel",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path

def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def write_json(path: Path, payload: Any) -> None:
    ensure_directory(path.parent)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dataclass_to_dict(value: Any) -> Any:
    if is_dataclass(value):
        return _normalize(asdict(value))
    return _normalize(value)


SECRET_KEY_RE = re.compile(r"(api[_-]?key|authorization|bearer|token|secret|password|credential)", re.IGNORECASE)


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, inner in value.items():
            if SECRET_KEY_RE.search(str(key)):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_sensitive(inner)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def _normalize(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _normalize(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", text.lower())


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def relative_to(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def copy_tree_to_temp(source: Path) -> Path:
    source = source.resolve()
    destination = Path(tempfile.mkdtemp(prefix=f"telchines-{source.name}-{uuid.uuid4().hex}-"))
    shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=_copytree_ignore())
    return destination


def _copytree_ignore():
    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        directory_path = Path(directory)
        for name in names:
            if name in TEMP_COPY_EXCLUDE_NAMES:
                ignored.add(name)
                continue
            candidate = directory_path / name
            if candidate.is_symlink():
                ignored.add(name)
        return ignored

    return ignore


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
