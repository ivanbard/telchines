from __future__ import annotations

import json
from pathlib import Path


def test_backlog_definition_has_milestones_and_issues() -> None:
    backlog_path = Path(__file__).resolve().parents[1] / "ops" / "github-backlog.json"
    payload = json.loads(backlog_path.read_text(encoding="utf-8"))
    milestones = payload["milestones"]
    assert milestones
    for milestone in milestones:
        assert milestone["title"]
        assert milestone["description"]
        assert milestone["issues"]
        for issue in milestone["issues"]:
            assert issue["title"]
            assert issue["body"]
