from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = "0.1"


@dataclass(slots=True)
class VerificationProject:
    project_id: str
    name: str
    root_path: str
    created_at: str
    schema_version: str = SCHEMA_VERSION
    config_path: str = ".tel/config.json"
    tool_policy: dict[str, Any] = field(default_factory=dict)
    model_policy: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DesignArtifact:
    artifact_id: str
    project_id: str
    path: str
    kind: str
    content_hash: str
    source: str
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class ToolReference:
    kind: str
    name: str
    version: str = "unknown"


@dataclass(slots=True)
class VerificationRun:
    run_id: str
    project_id: str
    commit_sha: str
    workflow_type: str
    tool: ToolReference
    inputs: dict[str, Any]
    status: str
    started_at: str
    finished_at: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    observation_ids: list[str] = field(default_factory=list)
    summary: str = ""
    replay_command: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class Observation:
    observation_id: str
    run_id: str
    type: str
    signature: str
    file: str | None
    line: int | None
    message: str
    severity: str
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class RetrievalHit:
    path: str
    kind: str
    score: float
    start_line: int
    end_line: int
    snippet: str


@dataclass(slots=True)
class RetrievalContext:
    context_id: str
    project_id: str
    query: str
    hits: list[RetrievalHit]
    created_at: str
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class AgentTask:
    task_id: str
    project_id: str
    workflow_type: str
    input_run_id: str | None
    status: str
    created_at: str
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class ValidationAttempt:
    attempt: int
    result: str
    run_id: str | None = None
    notes: str = ""


@dataclass(slots=True)
class PatchProposal:
    patch_id: str
    task_id: str
    based_on_observations: list[str]
    file_path: str
    diff: str
    candidate_content: str
    explanation: str
    status: str
    validation_attempts: list[ValidationAttempt] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class BenchmarkCase:
    benchmark_id: str
    task_type: str
    title: str
    fixture_root: str
    config: dict[str, Any]
    scoring: dict[str, Any]
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class CoverageItem:
    coverage_id: str
    project_id: str
    name: str
    status: str
    detail: str
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class FailureCluster:
    cluster_id: str
    signature: str
    count: int
    files: list[str]
    summary: str
    observation_ids: list[str]
    schema_version: str = SCHEMA_VERSION
