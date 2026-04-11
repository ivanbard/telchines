from __future__ import annotations

from collections import Counter
from pathlib import Path

from telchines.adapters.parsing import parse_common_output
from telchines.config import ProjectConfig
from telchines.models import FailureCluster, Observation, RetrievalContext, SimilarRunMatch, ToolReference, VerificationRun, WaveformSummary
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import stable_id, tokenize, unique_preserve_order, utc_now
from telchines.waveforms import discover_waveforms, ingest_waveform, summarize_for_cluster

SUPPORTED_LOG_EXTENSIONS = {".log", ".txt", ".out", ".err"}


def triage_logs(
    config: ProjectConfig,
    store: RunStore,
    retrieval: RetrievalService,
    logs_path: Path | list[Path],
    waveform_paths: list[Path] | None = None,
) -> tuple[VerificationRun, list[FailureCluster], RetrievalContext]:
    requested_paths = _normalize_input_paths(logs_path)
    log_files = _collect_log_files(requested_paths)
    explicit_waveforms = _normalize_input_paths(waveform_paths or [])
    waveform_files = _collect_waveform_files(explicit_waveforms) if explicit_waveforms else discover_waveforms(requested_paths)
    waveform_summaries = [ingest_waveform(config, store, path) for path in waveform_files]
    run_id = stable_id(
        "run",
        config.project.project_id,
        "triage",
        utc_now(),
        *[str(path) for path in requested_paths],
        str(len(store.list_runs_by_workflow("regression_triage"))),
    )
    all_observations: list[Observation] = []
    for path in log_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        observations = parse_common_output(run_id, text, default_type="sim_failure")
        all_observations.extend(observations)
    store.save_observations(all_observations)

    overall_query = " ".join(observation.message for observation in all_observations[:12]) or "triage failure summary"
    overall_focus = unique_preserve_order(observation.file for observation in all_observations if observation.file)
    context = retrieval.search(query=overall_query, mode="triage", focus_paths=overall_focus)
    store.save_context(context)

    clusters = build_clusters(store, retrieval, all_observations, waveform_summaries)
    clusters_path = store.save_clusters(run_id, clusters)
    run = VerificationRun(
        run_id=run_id,
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="regression_triage",
        tool=ToolReference(kind="triage", name="log_clusterer", version="0.3"),
        inputs={
            "logs_path": [str(path) for path in requested_paths],
            "log_file_count": len(log_files),
            "waveform_count": len(waveform_summaries),
        },
        status="passed",
        started_at=utc_now(),
        finished_at=utc_now(),
        exit_code=0,
        artifacts={
            "clusters_path": str(clusters_path),
            "context_id": context.context_id,
            "waveform_ids": ",".join(summary.waveform_id for summary in waveform_summaries),
        },
        observation_ids=[observation.observation_id for observation in all_observations],
        summary=f"clustered {len(all_observations)} observations from {len(log_files)} log files into {len(clusters)} evidence-backed failure clusters",
        replay_command=[],
    )
    store.save_run(run)
    return run, clusters, context


def build_clusters(
    store: RunStore,
    retrieval: RetrievalService,
    observations: list[Observation],
    waveform_summaries: list[WaveformSummary] | None = None,
) -> list[FailureCluster]:
    grouped = _group_observations(observations)
    previous_runs = store.list_runs_by_workflow("regression_triage")
    waveform_summaries = waveform_summaries or []
    clusters: list[FailureCluster] = []
    for items in sorted(grouped, key=lambda group: (-len(group), _cluster_signature(group))):
        signature = _cluster_signature(items)
        files = sorted({item.file for item in items if item.file})
        lead_message = items[0].message
        evidence_context = retrieval.search(
            query=" ".join([signature, *files, *(item.message for item in items[:3])]),
            mode="triage",
            focus_paths=files,
            limit=4,
        )
        store.save_context(evidence_context)
        clusters.append(
            FailureCluster(
                cluster_id=stable_id("cluster", signature, str(len(items)), lead_message),
                signature=signature,
                count=len(items),
                files=files,
                summary=_cluster_summary(items, signature, lead_message, files),
                observation_ids=[item.observation_id for item in items],
                likely_cause=_likely_cause(signature, files),
                suggested_action=_suggested_action(signature, files),
                evidence_context_id=evidence_context.context_id,
                evidence_hits=evidence_context.hits,
                similar_runs=_find_similar_runs(store, previous_runs, items),
                waveform_evidence=_waveform_evidence(signature, files, items, waveform_summaries),
            )
        )
    return clusters


def _normalize_input_paths(logs_path: Path | list[Path]) -> list[Path]:
    if isinstance(logs_path, Path):
        return [logs_path]
    return [path for path in logs_path]


def _collect_log_files(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    log_files: list[Path] = []
    for path in paths:
        if path.is_file():
            resolved = path.resolve()
            if path.suffix.lower() in SUPPORTED_LOG_EXTENSIONS and resolved not in seen:
                seen.add(resolved)
                log_files.append(path)
            continue
        if not path.exists():
            continue
        for candidate in sorted(path.rglob("*")):
            resolved = candidate.resolve()
            if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_LOG_EXTENSIONS and resolved not in seen:
                seen.add(resolved)
                log_files.append(candidate)
    return log_files


def _collect_waveform_files(paths: list[Path]) -> list[Path]:
    waveform_files = discover_waveforms(paths)
    if paths and not waveform_files:
        requested = ", ".join(str(path) for path in paths)
        raise ValueError(f"no supported waveform files were found under: {requested}")
    return waveform_files


def _group_observations(observations: list[Observation]) -> list[list[Observation]]:
    groups: list[list[Observation]] = []
    for observation in sorted(observations, key=lambda item: (item.file or "", item.line or 0, item.signature, item.message)):
        best_index: int | None = None
        best_score = 0.0
        for index, group in enumerate(groups):
            score = max(_observation_similarity(observation, candidate) for candidate in group)
            if score > best_score:
                best_score = score
                best_index = index
        if best_index is not None and best_score >= 0.6:
            groups[best_index].append(observation)
        else:
            groups.append([observation])
    return groups


def _observation_similarity(left: Observation, right: Observation) -> float:
    score = 0.0
    if left.signature == right.signature:
        score += 0.55
    if left.file and right.file and left.file == right.file:
        score += 0.2
    left_tokens = set(tokenize(left.message))
    right_tokens = set(tokenize(right.message))
    union = left_tokens | right_tokens
    if union:
        score += 0.4 * (len(left_tokens & right_tokens) / len(union))
    return min(score, 1.0)


def _cluster_signature(items: list[Observation]) -> str:
    counts = Counter(item.signature for item in items)
    return counts.most_common(1)[0][0]


def _cluster_summary(items: list[Observation], signature: str, lead_message: str, files: list[str]) -> str:
    target = files[0] if files else "the regression logs"
    if len(items) == 1:
        return f"1 failure in {target}: {lead_message}"
    return f"{len(items)} related failures in {target} around {signature.lower()}: {lead_message}"


def _likely_cause(signature: str, files: list[str]) -> str:
    target = files[0] if files else "the failing design path"
    if signature == "SIM_TIMEOUT":
        return f"Repeated timeout behavior suggests a stalled handshake or missing stimulus path near {target}."
    if signature == "SV_UNKNOWN_IDENTIFIER":
        return f"Identifier resolution is inconsistent in {target}, likely from a typo or missing declaration."
    if signature == "ASSERTION_FAILURE":
        return f"An assertion is firing repeatedly near {target}, which usually means the DUT and checker disagree on protocol behavior."
    if signature == "FILE_NOT_FOUND":
        return f"Build inputs are incomplete; Telchines could not resolve an expected source under {target}."
    return f"Related failures share the {signature.lower()} signature and likely originate from the same local root cause near {target}."


def _suggested_action(signature: str, files: list[str]) -> str:
    target = files[0] if files else "the primary failing file"
    if signature == "SIM_TIMEOUT":
        return f"Inspect the timeout site in {target}, then check the driving sequence and reset/ready conditions around that block."
    if signature == "SV_UNKNOWN_IDENTIFIER":
        return f"Audit declarations and nearby signal names in {target}, then rerun lint after fixing the mismatch."
    if signature == "ASSERTION_FAILURE":
        return f"Trace the first failing assertion in {target} and compare waveform or log evidence against the checker intent."
    if signature == "FILE_NOT_FOUND":
        return f"Confirm the missing file is present and included in the tool invocation before rerunning the regression."
    return f"Start from {target}, inspect the first cited evidence block, and rerun the smallest reproducer before scaling back to the full regression."


def _find_similar_runs(store: RunStore, previous_runs: list[VerificationRun], items: list[Observation]) -> list[SimilarRunMatch]:
    matches: list[SimilarRunMatch] = []
    for run in previous_runs:
        observations = store.load_observations(run.observation_ids)
        score = _run_similarity(items, observations)
        if score < 0.5:
            continue
        matches.append(SimilarRunMatch(run_id=run.run_id, score=round(score, 3), summary=run.summary))
    matches.sort(key=lambda match: (-match.score, match.run_id))
    return matches[:3]


def _waveform_evidence(
    signature: str,
    files: list[str],
    items: list[Observation],
    waveform_summaries: list[WaveformSummary],
):
    messages = [item.message for item in items[:3]]
    return [summarize_for_cluster(summary, signature, files, messages) for summary in waveform_summaries[:3]]


def _run_similarity(cluster_items: list[Observation], prior_items: list[Observation]) -> float:
    if not cluster_items or not prior_items:
        return 0.0
    cluster_signatures = Counter(item.signature for item in cluster_items)
    prior_signatures = Counter(item.signature for item in prior_items)
    shared_signatures = sum(min(cluster_signatures[signature], prior_signatures[signature]) for signature in cluster_signatures)
    signature_score = shared_signatures / max(len(cluster_items), 1)
    cluster_files = {item.file for item in cluster_items if item.file}
    prior_files = {item.file for item in prior_items if item.file}
    if cluster_files:
        file_score = len(cluster_files & prior_files) / len(cluster_files)
    else:
        file_score = 0.0
    cluster_tokens = set(tokenize(" ".join(item.message for item in cluster_items)))
    prior_tokens = set(tokenize(" ".join(item.message for item in prior_items)))
    union = cluster_tokens | prior_tokens
    message_score = (len(cluster_tokens & prior_tokens) / len(union)) if union else 0.0
    return min(1.0, 0.6 * signature_score + 0.25 * file_score + 0.15 * message_score)
