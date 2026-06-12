from __future__ import annotations

import json
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read())
    workflow_type = payload.get("workflow_type")
    if workflow_type == "provider_check":
        print(json.dumps({"status": "ok", "workflow_type": workflow_type}))
        return 0
    if workflow_type == "compile_repair":
        files = payload.get("files") or []
        print(
            json.dumps(
                {
                    "status": "no_patch",
                    "file_path": files[0] if files else "",
                    "explanation": "Example provider does not implement repair.",
                    "evidence_paths": [],
                }
            )
        )
        return 0
    if workflow_type == "spec_to_sva":
        output_file = payload.get("output_file", ".tel/artifacts/generated/example_assertions.sv")
        print(
            json.dumps(
                {
                    "status": "no_generation",
                    "file_path": output_file,
                    "explanation": "Example provider does not implement SVA generation.",
                    "evidence_paths": [],
                    "properties": [],
                }
            )
        )
        return 0
    if workflow_type == "dut_to_cocotb":
        output_dir = payload.get("output_dir", ".tel/artifacts/generated/cocotb")
        print(
            json.dumps(
                {
                    "status": "no_generation",
                    "file_path": f"{output_dir}/test_example.py",
                    "manifest_path": f"{output_dir}/example_cocotb_manifest.json",
                    "candidate_content": "",
                    "explanation": "Example provider does not implement cocotb generation.",
                    "assumptions": [],
                    "ports": [],
                    "evidence_paths": [],
                }
            )
        )
        return 0
    print(json.dumps({"status": "error", "message": f"unsupported workflow_type: {workflow_type}"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
