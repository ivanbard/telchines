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
GENERIC_SIGNAL_TOKENS = {"clk", "clock", "rst", "rst_n", "reset", "reset_n"}
LOW_VALUE_MATCH_TOKENS = {"dut", "tb", "test", "rtl", "sv", "v", "top", "module", "uart"}


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


def summarize_for_cluster(
    summary: WaveformSummary,
    signature: str,
    files: list[str],
    messages: list[str],
    metadata: list[dict[str, object]] | None = None,
) -> WaveformEvidence:
    metadata_text = _metadata_text(metadata or [])
    cluster_tokens = _expanded_tokens(" ".join([signature, *files, *messages, metadata_text]))
    cluster_identifiers = cluster_tokens - LOW_VALUE_MATCH_TOKENS - GENERIC_SIGNAL_TOKENS
    target_identifiers = _target_identifiers(files, messages, metadata or [])
    scored_samples: list[tuple[float, WaveformSample, list[str]]] = []
    for sample in summary.sampled_signals:
        signal_tokens = _expanded_tokens(sample.full_name)
        signal_name_tokens = _expanded_tokens(sample.signal_name)
        if _is_generic_signal(sample.signal_name) and not (target_identifiers & signal_name_tokens):
            continue
        matched_tokens = sorted((cluster_identifiers & signal_tokens) - LOW_VALUE_MATCH_TOKENS)
        scope_tokens = _expanded_tokens(sample.full_name.rsplit(".", 1)[0] if "." in sample.full_name else "")
        scope_overlap = sorted((cluster_identifiers & scope_tokens) - LOW_VALUE_MATCH_TOKENS)
        signal_overlap = sorted((cluster_identifiers & signal_name_tokens) - LOW_VALUE_MATCH_TOKENS)
        target_overlap = sorted((target_identifiers & signal_tokens) - LOW_VALUE_MATCH_TOKENS)
        target_signal_overlap = sorted((target_identifiers & signal_name_tokens) - LOW_VALUE_MATCH_TOKENS)
        score = 0.0
        reasons: list[str] = []
        if target_signal_overlap:
            score += 4.0 * len(target_signal_overlap)
            reasons.append(f"target signal {target_signal_overlap[0]}")
        if target_overlap:
            score += 2.0 * len(target_overlap)
            reasons.append(f"target context {target_overlap[0]}")
        if signal_overlap:
            score += 2.0 * len(signal_overlap)
            reasons.append(f"signal token {signal_overlap[0]}")
        if scope_overlap:
            score += 0.75 * len(scope_overlap)
        if {"start", "bit"} <= cluster_identifiers and ("start" in signal_tokens or "serial" in signal_tokens or "rx" in signal_tokens):
            score += 2.5
            reasons.append("start-bit timeout context")
        if {"valid", "ready"} & cluster_identifiers and ({"valid", "ready"} & signal_tokens):
            score += 1.5
            reasons.append("handshake context")
        if "timeout" in cluster_identifiers and ("start" in signal_tokens or "serial" in signal_tokens or "rx" in signal_tokens):
            score += 1.0
            reasons.append("timeout context")
        if score > 0:
            scored_samples.append((score, sample, reasons or target_overlap or signal_overlap or matched_tokens or scope_overlap))
    scored_samples.sort(key=lambda item: (-item[0], item[1].full_name))
    matched_samples = [sample for score, sample, _ in scored_samples if score >= 2.5][:3]
    if matched_samples:
        relevance = "matched"
        evidence_status = "strong"
        best_score = round(scored_samples[0][0], 3)
        reason_tokens = _prioritize_reason_tokens(scored_samples[0][2])
        reason = f"matched {reason_tokens[0]}" if reason_tokens else "matched waveform signal and failure context"
    elif scored_samples:
        matched_samples = [scored_samples[0][1]]
        relevance = "weak"
        evidence_status = "weak"
        best_score = round(scored_samples[0][0], 3)
        reason = "only weak waveform scope/context overlap; treat as supporting context, not root-cause evidence"
    else:
        relevance = "unrelated"
        evidence_status = "unrelated"
        best_score = 0.0
        reason = "no relevant non-generic signal overlap; generic clock/reset excerpts were not attached"
    matched_names = unique_preserve_order(sample.signal_name for sample in matched_samples)
    candidate_names = unique_preserve_order(sample.signal_name for _, sample, _ in scored_samples[:5])
    excerpt_lines: list[str] = []
    for sample in matched_samples[:2]:
        if not sample.transitions:
            continue
        excerpt_lines.append(f"{sample.signal_name}: {_activity_summary(sample)}")
    excerpt = "; ".join(excerpt_lines) or ("no relevant waveform signals matched" if relevance == "unrelated" else "no sampled transitions available")
    return WaveformEvidence(
        waveform_id=summary.waveform_id,
        source_path=summary.source_path,
        matched_signals=matched_names,
        excerpt=excerpt,
        relevance=relevance,
        evidence_status=evidence_status,
        score=best_score,
        reason=reason,
        candidate_signals=candidate_names,
    )


def select_signal(summary: WaveformSummary, signal_name: str) -> WaveformSample:
    lowered = signal_name.lower()
    full_matches = [sample for sample in summary.sampled_signals if sample.full_name.lower() == lowered]
    if full_matches:
        return full_matches[0]
    leaf_matches = [sample for sample in summary.sampled_signals if sample.signal_name.lower() == lowered]
    if len(leaf_matches) == 1:
        return leaf_matches[0]
    if len(leaf_matches) > 1:
        full_names = ", ".join(sample.full_name for sample in leaf_matches[:8])
        raise ValueError(f"signal name is ambiguous: {signal_name}; matches: {full_names}")
    available = ", ".join(sample.full_name for sample in _signal_suggestions(summary, signal_name))
    detail = f"; available signals: {available}" if available else ""
    raise ValueError(f"signal was not found in waveform: {signal_name}{detail}")


def _signal_suggestions(summary: WaveformSummary, signal_name: str) -> list[WaveformSample]:
    query_tokens = _expanded_tokens(signal_name)
    lowered = signal_name.lower()
    candidates: list[tuple[int, WaveformSample]] = []
    for sample in summary.sampled_signals:
        sample_tokens = _expanded_tokens(sample.full_name)
        score = len(query_tokens & sample_tokens)
        if lowered and lowered in sample.full_name.lower():
            score += 2
        candidates.append((score, sample))
    candidates.sort(key=lambda item: (-item[0], item[1].full_name))
    return [sample for _, sample in candidates[:8]]


def _expanded_tokens(text: str) -> set[str]:
    pieces: list[str] = []
    for token in tokenize(text):
        pieces.append(token)
        pieces.extend(part for part in re.split(r"[_./\\:-]+", token) if part)
    return {piece.lower() for piece in pieces if piece}


def _metadata_text(metadata: list[dict[str, object]]) -> str:
    values: list[str] = []
    for item in metadata:
        for key in ("component", "code", "time", "raw_line", "module", "signal", "identifier"):
            value = item.get(key)
            if value:
                values.append(str(value))
    return " ".join(values)


def _prioritize_reason_tokens(reasons: list[str]) -> list[str]:
    semantic_prefixes = ("start-bit", "timeout", "handshake", "target signal", "signal token")
    semantic = [reason for reason in reasons if reason.startswith(semantic_prefixes)]
    fallback = [reason for reason in reasons if reason not in semantic]
    return semantic + fallback


def _target_identifiers(files: list[str], messages: list[str], metadata: list[dict[str, object]]) -> set[str]:
    text = " ".join([*files, *messages, _metadata_text(metadata)])
    identifiers: set[str] = set()
    patterns = [
        r"(?:unknown|undeclared)\s+(?:identifier|signal|net|wire|reg)?\s*['`\"]?(?P<name>[A-Za-z_][A-Za-z0-9_$]*)",
        r"(?:identifier|signal|net|wire|reg)\s+['`\"](?P<name>[A-Za-z_][A-Za-z0-9_$]*)['`\"]",
        r"\b(?P<name>[A-Za-z_][A-Za-z0-9_$]*(?:_i|_o|_n|_q|_d|_seen|_valid|_ready|_level|_fifo))\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            identifiers.update(_expanded_tokens(match.group("name")))
    for path_text in files:
        stem = Path(path_text).stem
        identifiers.update(_expanded_tokens(stem))
    return identifiers - LOW_VALUE_MATCH_TOKENS


def _is_generic_signal(signal_name: str) -> bool:
    tokens = _expanded_tokens(signal_name)
    return signal_name.lower() in GENERIC_SIGNAL_TOKENS or bool(tokens & GENERIC_SIGNAL_TOKENS)


def _activity_summary(sample: WaveformSample) -> str:
    transitions = sample.transitions
    if not transitions:
        return "no sampled transitions"
    toggle_count = max(len(transitions) - 1, 0)
    first_high = next((transition.timestamp for transition in transitions if transition.value == "1"), None)
    first_low = next((transition.timestamp for transition in transitions[1:] if transition.value == "0"), None)
    parts = [f"toggles={toggle_count}"]
    if first_high is not None:
        parts.append(f"first_assertion={first_high}")
    if first_low is not None:
        parts.append(f"first_fall={first_low}")
    parts.append("samples=" + ", ".join(f"{transition.timestamp}:{transition.value}" for transition in transitions[:4]))
    return ", ".join(parts)


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
