from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from telchines.config import ProjectConfig
from telchines.models import (
    AgentTask,
    CocotbCandidate,
    CocotbPort,
    FailureCluster,
    Observation,
    PatchProposal,
    RetrievalContext,
    SvaCandidate,
    ToolReference,
    VerificationRun,
    WaveformSample,
    WaveformSignal,
    WaveformSummary,
    WaveformTransition,
)
from telchines.utils import dataclass_to_dict, ensure_directory, read_json, redact_sensitive, write_json


class RunStore:
    def __init__(self, config: ProjectConfig) -> None:
        self.config = config
        self.root = config.project_root / config.store_dir
        self.runs_dir = ensure_directory(self.root / "runs")
        self.observations_dir = ensure_directory(self.root / "observations")
        self.contexts_dir = ensure_directory(self.root / "contexts")
        self.tasks_dir = ensure_directory(self.root / "tasks")
        self.task_artifacts_dir = ensure_directory(self.root / "task-artifacts")
        self.patches_dir = ensure_directory(self.root / "patches")
        self.generations_dir = ensure_directory(self.root / "generations")
        self.waveforms_dir = ensure_directory(self.root / "waveforms")
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
        runs, _ = self._load_runs_with_issues()
        return sorted(runs, key=lambda run: run.started_at, reverse=True)

    def list_run_load_issues(self) -> list[dict[str, str]]:
        _, issues = self._load_runs_with_issues()
        return issues

    def _load_runs_with_issues(self) -> tuple[list[VerificationRun], list[dict[str, str]]]:
        runs: list[VerificationRun] = []
        issues: list[dict[str, str]] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                runs.append(self.load_run(path.stem))
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                issues.append(
                    {
                        "run_id": path.stem,
                        "path": str(path.relative_to(self.root)).replace("\\", "/"),
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
        return runs, issues

    def list_runs_by_workflow(self, workflow_type: str) -> list[VerificationRun]:
        return [run for run in self.list_runs() if run.workflow_type == workflow_type]

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

    def save_task(self, task: AgentTask) -> None:
        write_json(self.tasks_dir / f"{task.task_id}.json", dataclass_to_dict(task))

    def save_task_artifact(self, task_id: str, name: str, payload: dict[str, Any]) -> Path:
        path = self.task_artifacts_dir / f"{task_id}_{name}.json"
        write_json(path, redact_sensitive(payload))
        return path

    def save_patch(self, patch: PatchProposal) -> None:
        payload = dataclass_to_dict(patch)
        payload["validation_attempts"] = [asdict(attempt) for attempt in patch.validation_attempts]
        write_json(self.patches_dir / f"{patch.patch_id}.json", payload)

    def save_sva_candidate(self, candidate: SvaCandidate) -> None:
        payload = dataclass_to_dict(candidate)
        payload["properties"] = [asdict(item) for item in candidate.properties]
        payload["validation_attempts"] = [asdict(attempt) for attempt in candidate.validation_attempts]
        write_json(self.generations_dir / f"{candidate.candidate_id}.json", payload)

    def save_cocotb_candidate(self, candidate: CocotbCandidate) -> None:
        payload = dataclass_to_dict(candidate)
        payload["ports"] = [asdict(item) for item in candidate.ports]
        payload["validation_attempts"] = [asdict(attempt) for attempt in candidate.validation_attempts]
        write_json(self.generations_dir / f"{candidate.candidate_id}.json", payload)

    def save_waveform_summary(self, summary: WaveformSummary) -> None:
        payload = dataclass_to_dict(summary)
        payload["signals"] = [asdict(item) for item in summary.signals]
        payload["sampled_signals"] = [
            {
                "signal_name": sample.signal_name,
                "full_name": sample.full_name,
                "transitions": [asdict(transition) for transition in sample.transitions],
            }
            for sample in summary.sampled_signals
        ]
        write_json(self.waveforms_dir / f"{summary.waveform_id}.json", payload)

    def load_waveform_summary(self, waveform_id: str) -> WaveformSummary:
        payload = read_json(self.waveforms_dir / f"{waveform_id}.json")
        payload["signals"] = [WaveformSignal(**item) for item in payload["signals"]]
        payload["sampled_signals"] = [
            WaveformSample(
                signal_name=sample["signal_name"],
                full_name=sample["full_name"],
                transitions=[WaveformTransition(**transition) for transition in sample["transitions"]],
            )
            for sample in payload["sampled_signals"]
        ]
        return WaveformSummary(**payload)

    def list_waveform_summaries(self) -> list[WaveformSummary]:
        summaries = [self.load_waveform_summary(path.stem) for path in sorted(self.waveforms_dir.glob("*.json"))]
        return sorted(summaries, key=lambda item: item.source_path)

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
