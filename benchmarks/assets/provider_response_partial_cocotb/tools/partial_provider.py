from __future__ import annotations

import json
import sys

payload = json.loads(sys.stdin.read())
sys.stdout.write(
    json.dumps(
        {
            "status": "proposed",
            "file_path": "tests/test_uart_rx.py",
            "manifest_path": "tests/test_uart_rx_manifest.json",
            "top_module": "uart_rx",
            "explanation": "Intentionally missing candidate_content for benchmark coverage.",
            "evidence_paths": [payload["dut"]["path"]],
            "assumptions": ["partial response fixture"],
            "ports": [],
        }
    )
)
