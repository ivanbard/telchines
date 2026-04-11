from __future__ import annotations

import re
import shutil
from pathlib import Path

from telchines.config import ProjectConfig
from telchines.models import WaveformEvidence, WaveformSample, WaveformSignal, WaveformSummary, WaveformTransition
from telchines.run_store import RunStore
from telchines.utils import relative_to, sha256_file, stable_id, tokenize, unique_preserve_order

NATIVE_WAVEFORM_EXTENSIONS = {".vcd"}
KNOWN_WAVEFORM_EXTENSIONS = {".vcd", ".fst"}


def ingest_waveform(config: ProjectConfig, store: RunStore, path: Path) -> WaveformSummary:
    summary = parse_waveform(config, path)
    store.save_waveform_summary(summary)
    return summary


def parse_waveform(config: ProjectConfig, path: Path) -> WaveformSummary:
    resolved = path.resolve()
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"waveform file does not exist: {resolved}")
    suffix = resolved.suffix.lower()
    if suffix not in KNOWN_WAVEFORM_EXTENSIONS:
        raise ValueError(f"unsupported waveform format: {suffix}")
    if suffix == ".fst":
        tool = _detect_external_tool()
        detail = f"; detected external tool {tool}" if tool else "; no external tool available"
        raise ValueError(f"unsupported waveform format: .fst{detail}")
    return _parse_vcd(config, resolved)


def discover_waveforms(paths: list[Path]) -> list[Path]:
    discovered: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        root = path if path.is_dir() else path.parent
        if not root.exists():
            continue
        for candidate in sorted(root.rglob("*")):
            resolved = candidate.resolve()
            if candidate.is_file() and candidate.suffix.lower() in NATIVE_WAVEFORM_EXTENSIONS and resolved not in seen:
                seen.add(resolved)
                discovered.append(candidate)
    return discovered


def summarize_for_cluster(summary: WaveformSummary, signature: str, files: list[str], messages: list[str]) -> WaveformEvidence:
    cluster_tokens = set(tokenize(" ".join([signature, *files, *messages])))
    matched_samples = []
    for sample in summary.sampled_signals:
        signal_tokens = set(tokenize(sample.full_name))
        if cluster_tokens & signal_tokens:
            matched_samples.append(sample)
    if not matched_samples:
        matched_samples = summary.sampled_signals[:2]
    matched_names = unique_preserve_order(sample.signal_name for sample in matched_samples)
    excerpt_lines: list[str] = []
    for sample in matched_samples[:2]:
        if not sample.transitions:
            continue
        excerpt_lines.append(
            f"{sample.signal_name}: "
            + ", ".join(f"{transition.timestamp}:{transition.value}" for transition in sample.transitions[:4])
        )
    excerpt = "; ".join(excerpt_lines) or "no sampled transitions available"
    return WaveformEvidence(
        waveform_id=summary.waveform_id,
        source_path=summary.source_path,
        matched_signals=matched_names,
        excerpt=excerpt,
    )


def select_signal(summary: WaveformSummary, signal_name: str) -> WaveformSample:
    lowered = signal_name.lower()
    for sample in summary.sampled_signals:
        if sample.signal_name.lower() == lowered or sample.full_name.lower() == lowered:
            return sample
    for sample in summary.sampled_signals:
        if sample.full_name.lower().endswith(f".{lowered}") or lowered in sample.full_name.lower():
            return sample
    raise ValueError(f"signal was not found in waveform: {signal_name}")


def _parse_vcd(config: ProjectConfig, path: Path) -> WaveformSummary:
    scope_stack: list[str] = []
    signals_by_id: dict[str, WaveformSignal] = {}
    transition_map: dict[str, list[WaveformTransition]] = {}
    current_time = 0
    timescale = "unknown"
    in_definitions = True
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if in_definitions:
            if line.startswith("$timescale"):
                timescale = " ".join(token for token in line.replace("$timescale", "").replace("$end", "").split() if token)
                continue
            if line.startswith("$scope"):
                parts = line.split()
                if len(parts) >= 3:
                    scope_stack.append(parts[2])
                continue
            if line.startswith("$upscope"):
                if scope_stack:
                    scope_stack.pop()
                continue
            if line.startswith("$var"):
                parts = line.split()
                if len(parts) >= 5:
                    width = int(parts[2])
                    identifier = parts[3]
                    name = parts[4]
                    scope = ".".join(scope_stack)
                    full_name = ".".join([scope, name]) if scope else name
                    signals_by_id[identifier] = WaveformSignal(
                        name=name,
                        full_name=full_name,
                        scope=scope,
                        width=width,
                        identifier=identifier,
                    )
                    transition_map.setdefault(identifier, [])
                continue
            if line.startswith("$enddefinitions"):
                in_definitions = False
            continue
        if line.startswith("#"):
            current_time = int(line[1:])
            continue
        if line[0] in {"0", "1", "x", "X", "z", "Z"}:
            identifier = line[1:].strip()
            _append_transition(transition_map, identifier, current_time, line[0])
            continue
        vector_match = re.match(r"^[bBrR]([01xXzZ]+)\s+(\S+)$", line)
        if vector_match:
            value, identifier = vector_match.groups()
            _append_transition(transition_map, identifier, current_time, value)
    ordered_signals = sorted(signals_by_id.values(), key=lambda item: item.full_name)
    sampled_signals = [
        WaveformSample(
            signal_name=signal.name,
            full_name=signal.full_name,
            transitions=transition_map.get(signal.identifier, [])[:16],
        )
        for signal in ordered_signals[:64]
    ]
    relative_path = relative_to(path, config.project_root)
    return WaveformSummary(
        waveform_id=stable_id("wave", relative_path, sha256_file(path)),
        project_id=config.project.project_id,
        source_path=relative_path,
        source_hash=sha256_file(path),
        format="vcd",
        timescale=timescale,
        top_scopes=unique_preserve_order(signal.scope.split(".")[0] for signal in ordered_signals if signal.scope),
        signals=ordered_signals,
        sampled_signals=sampled_signals,
        external_tool=_detect_external_tool(),
        notes=f"parsed {len(ordered_signals)} signal(s) from VCD",
    )


def _append_transition(
    transition_map: dict[str, list[WaveformTransition]],
    identifier: str,
    timestamp: int,
    value: str,
) -> None:
    if identifier not in transition_map:
        return
    transitions = transition_map[identifier]
    if transitions and transitions[-1].value == value:
        return
    transitions.append(WaveformTransition(timestamp=timestamp, value=value))


def _detect_external_tool() -> str:
    for name in ("gtkwave",):
        if shutil.which(name):
            return name
    return ""
