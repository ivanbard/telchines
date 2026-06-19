from __future__ import annotations

from dataclasses import replace
from importlib.util import find_spec
from typing import Protocol

from telchines.config import ProjectConfig
from telchines.errors import ProviderError
from telchines.models import PatchProposal, ValidationAttempt, VerificationRun
from telchines.repair_validation import validate_patch
from telchines.run_store import RunStore
from telchines.utils import dataclass_to_dict


class RepairAgentRuntime(Protocol):
    def run_repair(self, request) -> object:
        ...


class LangGraphRepairRuntime:
    def __init__(self, config: ProjectConfig, provider_name: str, provider_config: dict, base_provider) -> None:
        self.config = config
        self.provider_name = provider_name
        self.provider_config = provider_config
        self.base_provider = base_provider
        self.max_iterations = int(provider_config.get("max_iterations", 3))
        self.langgraph_available = find_spec("langgraph") is not None

    def run_repair(self, request):
        from telchines.providers import RepairProviderResult

        store = RunStore(self.config)
        steps: list[dict[str, object]] = []
        feedback: list[dict[str, object]] = []
        best_proposal: PatchProposal | None = None
        final_validation: VerificationRun | None = None
        final_error = ""
        request_payload = self._agent_request_payload(request)

        steps.append(
            {
                "step": "build_context",
                "status": "passed",
                "context_id": request.retrieval_context.context_id,
                "observation_count": len(request.observations),
                "evidence_count": len(request.retrieval_context.hits),
            }
        )

        for attempt in range(1, self.max_iterations + 1):
            attempt_request = replace(request, feedback=list(feedback))
            try:
                provider_result = self.base_provider.propose_patch(attempt_request)
            except ProviderError as exc:
                final_error = str(exc)
                steps.append({"step": "propose_patch", "attempt": attempt, "status": "failed", "error": final_error})
                break

            proposal = provider_result.proposal
            steps.append(
                {
                    "step": "propose_patch",
                    "attempt": attempt,
                    "status": "proposed" if proposal else "no_patch",
                    "provider": provider_result.provider_name,
                    "summary": provider_result.summary,
                    "request": provider_result.request_payload,
                    "response": provider_result.response_payload,
                    "patch_id": proposal.patch_id if proposal else None,
                    "file_path": proposal.file_path if proposal else None,
                }
            )
            if proposal is None:
                break

            best_proposal = proposal
            validation_run = validate_patch(self.config, store, request.base_run, proposal, apply_patch=False)
            proposal.validation_attempts.append(
                ValidationAttempt(attempt=attempt, result=validation_run.status, run_id=validation_run.run_id, notes=validation_run.summary)
            )
            steps.append(
                {
                    "step": "validate_patch",
                    "attempt": attempt,
                    "status": validation_run.status,
                    "run_id": validation_run.run_id,
                    "summary": validation_run.summary,
                    "observation_ids": validation_run.observation_ids,
                }
            )
            if validation_run.status == "passed":
                final_validation = validation_run
                break

            observations = store.load_observations(validation_run.observation_ids)
            feedback.append(
                {
                    "attempt": attempt,
                    "validation_status": validation_run.status,
                    "validation_summary": validation_run.summary,
                    "observations": [
                        {
                            "signature": observation.signature,
                            "file": observation.file,
                            "line": observation.line,
                            "message": observation.message,
                        }
                        for observation in observations
                    ],
                }
            )
            steps.append(
                {
                    "step": "revise_or_stop",
                    "attempt": attempt,
                    "status": "retrying" if attempt < self.max_iterations else "budget_exhausted",
                }
            )

        if final_validation is not None and best_proposal is not None:
            best_proposal.provider = self.provider_name
            response_payload = self._response_payload(
                "validated",
                steps,
                validation_run=final_validation,
                final_patch=best_proposal,
            )
            return RepairProviderResult(
                provider_name=self.provider_name,
                request_payload=request_payload,
                response_payload=response_payload,
                proposal=best_proposal,
                summary=f"agent runtime repair validated after {len(best_proposal.validation_attempts)} attempt(s)",
            )

        response_payload = self._response_payload(
            "no_patch",
            steps,
            final_patch=best_proposal,
            final_error=final_error,
        )
        summary = "agent runtime exhausted repair budget without a validated patch"
        if final_error:
            summary = f"agent runtime stopped after provider error: {final_error}"
        return RepairProviderResult(
            provider_name=self.provider_name,
            request_payload=request_payload,
            response_payload=response_payload,
            proposal=None,
            summary=summary,
        )

    def _agent_request_payload(self, request) -> dict[str, object]:
        return {
            "provider": self.provider_name,
            "runtime": self.provider_config.get("runtime", "langgraph"),
            "runtime_mode": self._runtime_mode(),
            "base_provider": self.provider_config["base_provider"],
            "max_iterations": self.max_iterations,
            "task_id": request.task_id,
            "base_run_id": request.base_run.run_id,
            "context_id": request.retrieval_context.context_id,
            "observation_ids": [observation.observation_id for observation in request.observations],
        }

    def _response_payload(
        self,
        status: str,
        steps: list[dict[str, object]],
        *,
        validation_run: VerificationRun | None = None,
        final_patch: PatchProposal | None = None,
        final_error: str = "",
    ) -> dict[str, object]:
        agent_runtime: dict[str, object] = {
            "runtime": self.provider_config.get("runtime", "langgraph"),
            "runtime_mode": self._runtime_mode(),
            "base_provider": self.provider_config["base_provider"],
            "max_iterations": self.max_iterations,
            "steps": steps,
            "final_status": status,
            "validation_run_id": validation_run.run_id if validation_run else None,
        }
        if final_patch is not None:
            agent_runtime["final_patch"] = {
                "patch_id": final_patch.patch_id,
                "file_path": final_patch.file_path,
                "status": final_patch.status,
                "validation_attempts": [dataclass_to_dict(attempt) for attempt in final_patch.validation_attempts],
            }
        if final_error:
            agent_runtime["final_error"] = final_error
        return {
            "provider": self.provider_name,
            "status": status,
            "summary": status,
            "agent_runtime": agent_runtime,
        }

    def _runtime_mode(self) -> str:
        return "langgraph" if self.langgraph_available else "bounded_loop_no_langgraph"
