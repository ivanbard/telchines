from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from telchines.adapters.parsing import parse_common_output
from telchines.config import ProjectConfig
from telchines.models import FailureCluster, Observation, RetrievalContext, ToolReference, VerificationRun
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import stable_id, utc_now


def triage_logs(config: ProjectConfig, store: RunStore, retrieval: RetrievalService, logs_path: Path) -> tuple[VerificationRun, list[FailureCluster], RetrievalContext]:
    run_id = stable_id("run", config.project.project_id, "triage", utc_now(), str(logs_path))
    all_observations: list[Observation] = []
    for path in sorted(logs_path.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".log", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8")
        observations = parse_common_output(run_id, text, default_type="sim_failure")
        all_observations.extend(observations)
    store.save_observations(all_observations)
    clusters = build_clusters(all_observations)
    query = " ".join(observation.message for observation in all_observations[:10])
    context = retrieval.search(query=query or "triage failure summary")
    store.save_context(context)
    clusters_path = store.save_clusters(run_id, clusters)
    run = VerificationRun(
        run_id=run_id,
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="regression_triage",
        tool=ToolReference(kind="triage", name="log_clusterer", version="0.1"),
        inputs={"logs_path": str(logs_path)},
        status="passed",
        started_at=utc_now(),
        finished_at=utc_now(),
        artifacts={"clusters_path": str(clusters_path)},
        observation_ids=[observation.observation_id for observation in all_observations],
        summary=f"clustered {len(all_observations)} observations into {len(clusters)} failure clusters",
        replay_command=[],
    )
    store.save_run(run)
    return run, clusters, context


def build_clusters(observations: list[Observation]) -> list[FailureCluster]:
    grouped: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.signature].append(observation)
    clusters: list[FailureCluster] = []
    for signature, items in sorted(grouped.items(), key=lambda entry: (-len(entry[1]), entry[0])):
        files = sorted({item.file for item in items if item.file})
        summary = f"{len(items)} failures matched {signature}"
        clusters.append(
            FailureCluster(
                cluster_id=stable_id("cluster", signature, str(len(items))),
                signature=signature,
                count=len(items),
                files=files,
                summary=summary,
                observation_ids=[item.observation_id for item in items],
            )
        )
    return clusters
