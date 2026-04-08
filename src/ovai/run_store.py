from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ovai.config import ProjectConfig
from ovai.models import AgentTask, FailureCluster, Observation, PatchProposal, RetrievalContext, RetrievalHit, ToolReference, ValidationAttempt, VerificationRun
from ovai.utils import dataclass_to_dict, ensure_directory, read_json, write_json


class RunStore:
    def __init__(self, config: ProjectConfig) -> None:
        self.config = config
        self.root = config.project_root / config.store_dir
        self.runs_dir = ensure_directory(self.root / "runs")
        self.observations_dir = ensure_directory(self.root / "observations")
        self.contexts_dir = ensure_directory(self.root / "contexts")
        self.tasks_dir = ensure_directory(self.root / "tasks")
        self.patches_dir = ensure_directory(self.root / "patches")
        self.reports_dir = ensure_directory(self.root / "reports")

    def save_run(self, run: VerificationRun) -> None:
        payload = dataclass_to_dict(run)
        payload["tool"] = asdict(run.tool)
        write_json(self.runs_dir / f"{run.run_id}.json", payload)

    def load_run(self, run_id: str) -> VerificationRun:
        payload = read_json(self.runs_dir / f"{run_id}.json")
        payload["tool"] = ToolReference(**payload["tool"])
        return VerificationRun(**payload)

    def list_runs(self) -> list[VerificationRun]:
        runs = [self.load_run(path.stem) for path in sorted(self.runs_dir.glob("*.json"))]
        return sorted(runs, key=lambda run: run.started_at, reverse=True)

    def save_observations(self, observations: list[Observation]) -> None:
        for observation in observations:
            write_json(self.observations_dir / f"{observation.observation_id}.json", dataclass_to_dict(observation))

    def load_observation(self, observation_id: str) -> Observation:
        return Observation(**read_json(self.observations_dir / f"{observation_id}.json"))

    def load_observations(self, observation_ids: list[str]) -> list[Observation]:
        return [self.load_observation(observation_id) for observation_id in observation_ids]

    def save_context(self, context: RetrievalContext) -> None:
        payload = dataclass_to_dict(context)
        payload["hits"] = [asdict(hit) for hit in context.hits]
        write_json(self.contexts_dir / f"{context.context_id}.json", payload)

    def load_context(self, context_id: str) -> RetrievalContext:
        payload = read_json(self.contexts_dir / f"{context_id}.json")
        payload["hits"] = [RetrievalHit(**hit) for hit in payload["hits"]]
        return RetrievalContext(**payload)

    def save_task(self, task: AgentTask) -> None:
        write_json(self.tasks_dir / f"{task.task_id}.json", dataclass_to_dict(task))

    def save_patch(self, patch: PatchProposal) -> None:
        payload = dataclass_to_dict(patch)
        payload["validation_attempts"] = [asdict(attempt) for attempt in patch.validation_attempts]
        write_json(self.patches_dir / f"{patch.patch_id}.json", payload)

    def load_patch(self, patch_id: str) -> PatchProposal:
        payload = read_json(self.patches_dir / f"{patch_id}.json")
        payload["validation_attempts"] = [ValidationAttempt(**attempt) for attempt in payload["validation_attempts"]]
        return PatchProposal(**payload)

    def save_report(self, name: str, payload: dict[str, Any]) -> Path:
        path = self.reports_dir / f"{name}.json"
        write_json(path, payload)
        return path

    def load_report(self, name: str) -> dict[str, Any]:
        return read_json(self.reports_dir / f"{name}.json")

    def save_clusters(self, run_id: str, clusters: list[FailureCluster]) -> Path:
        path = self.reports_dir / f"{run_id}_clusters.json"
        write_json(path, [dataclass_to_dict(cluster) for cluster in clusters])
        return path
