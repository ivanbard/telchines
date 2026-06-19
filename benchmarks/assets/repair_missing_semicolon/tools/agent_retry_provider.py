from __future__ import annotations

import json
import sys
from pathlib import Path


payload = json.loads(sys.stdin.read())
target_file = payload["files"][0]
original = Path(target_file).read_text(encoding="utf-8")
if payload.get("previous_attempts"):
    candidate = original.replace("count <= 4'd0", "count <= 4'd0;")
else:
    candidate = original

sys.stdout.write(
    json.dumps(
        {
            "status": "proposed",
            "file_path": target_file,
            "candidate_content": candidate,
            "explanation": "Use validation feedback to add the missing semicolon.",
            "evidence_paths": ["rtl/broken_counter.sv"],
        }
    )
)
