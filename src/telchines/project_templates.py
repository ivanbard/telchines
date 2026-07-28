from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from telchines.config import ProjectConfig
from telchines.utils import ensure_directory, write_json


TEMPLATE_PACKAGE = "telchines.templates"


def list_project_templates() -> list[dict[str, object]]:
    return [
        {
            "name": template["name"],
            "description": template["description"],
            "directories": template.get("directories", []),
        }
        for template in _catalog()["templates"]
    ]


def apply_project_template(config: ProjectConfig, template_name: str) -> dict[str, object]:
    template = _template(template_name)
    created: list[str] = []
    skipped: list[str] = []
    for directory in template.get("directories", []):
        path = config.project_root / str(directory)
        if not path.exists():
            created.append(str(path.relative_to(config.project_root)).replace("\\", "/"))
        ensure_directory(path)
    for item in template.get("files", []):
        relative = str(item["path"])
        path = config.project_root / relative
        if path.exists():
            skipped.append(relative)
            continue
        ensure_directory(path.parent)
        if item.get("kind") == "json":
            write_json(path, item.get("content", {}))
        else:
            path.write_text(str(item.get("content", "")), encoding="utf-8")
        created.append(relative)
    for relative, payload in _default_json_examples().items():
        path = config.project_root / relative
        if path.exists():
            continue
        write_json(path, payload)
        created.append(relative)
    coverage_readme = config.project_root / "cov" / "README.md"
    if not coverage_readme.exists():
        ensure_directory(coverage_readme.parent)
        coverage_readme.write_text(
            "# Coverage input\n\n"
            "`coverage-plan` requires a real coverage export. Start with `examples/coverage_template.json` only as a schema reference, "
            "then import or write your real report to `cov/coverage.json`.\n",
            encoding="utf-8",
        )
        created.append("cov/README.md")
    aliases = config.retrieval.get("aliases", {})
    if not isinstance(aliases, dict):
        aliases = {}
    for key, values in template.get("aliases", {}).items():
        if isinstance(values, list):
            aliases[str(key)] = [str(value) for value in values]
    config.retrieval["aliases"] = aliases
    config.save()
    return {
        "template": template_name,
        "created": created,
        "skipped": skipped,
        "alias_count": len(aliases),
    }


def _template(name: str) -> dict[str, Any]:
    for template in _catalog()["templates"]:
        if template["name"] == name:
            return template
    supported = ", ".join(template["name"] for template in _catalog()["templates"])
    raise ValueError(f"unknown project template: {name}; supported templates: {supported}")


def _catalog() -> dict[str, Any]:
    text = resources.files(TEMPLATE_PACKAGE).joinpath("catalog.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("templates"), list):
        raise ValueError("template catalog is malformed")
    return payload


def _default_json_examples() -> dict[str, dict[str, Any]]:
    return {
        "examples/regression_manifest.json": {
            "schema_version": "0.1",
            "tool": {"kind": "regression_manager", "name": "example", "version": "0.1"},
            "runs": [],
        },
        "examples/coverage_template.json": {
            "schema_version": "0.1",
            "tool": "template",
            "generated_at": "1970-01-01T00:00:00+00:00",
            "design": "example",
            "items": [
                {
                    "item_id": "example_smoke",
                    "module": "example",
                    "metric": "functional",
                    "name": "smoke",
                    "hits": 0,
                    "goal": 1,
                    "coverage": 0.0,
                    "detail": "Replace this template item with real coverage export data.",
                }
            ],
            "focus_paths": ["rtl"],
            "exclusions": [],
            "reachability_hints": [],
        },
    }
