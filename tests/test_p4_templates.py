from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import telchines.project_templates as project_templates_module
from telchines.config import ProjectConfig
from telchines.import_manifest import import_regression_manifest
from telchines.project_templates import apply_project_template, list_project_templates
from telchines.run_store import RunStore


TEMPLATE_NAMES = [item["name"] for item in list_project_templates()]


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(template_name=st.sampled_from(TEMPLATE_NAMES))
def test_all_project_templates_create_valid_local_scaffolds(work_root: Path, template_name: str) -> None:
    project_root = work_root / f"templated_{template_name}"
    config = ProjectConfig.init_project(project_root)

    result = apply_project_template(config, template_name)
    loaded = ProjectConfig.load(project_root)
    store = RunStore(loaded)
    imported = import_regression_manifest(loaded, store, Path("examples/regression_manifest.json"), dry_run=True)
    user_files = [path for path in project_root.rglob("*") if path.is_file() and ".tel" not in path.relative_to(project_root).parts]

    assert result["template"] == template_name
    assert result["created"] or result["skipped"]
    assert (project_root / "README.telchines.md").exists()
    assert not (project_root / "cov" / "coverage.json").exists()
    assert (project_root / "examples" / "coverage_template.json").exists()
    assert "real coverage export" in (project_root / "cov" / "README.md").read_text(encoding="utf-8")
    assert imported["imported_count"] == 0
    assert isinstance(loaded.retrieval["aliases"], dict)
    assert not any(str(project_root) in path.read_text(encoding="utf-8", errors="ignore") for path in user_files)
    assert (project_root / "filelists").is_dir()
    assert (project_root / "include").is_dir()
    assert (project_root / "generated").is_dir()

    if template_name in {"vivado", "quartus", "libero"}:
        assert any(path.suffix == ".f" for path in (project_root / "filelists").iterdir())
        assert loaded.retrieval["aliases"]["compile options"] == ["filelist", "incdir", "define", "generated"]


def test_project_template_application_is_idempotent_and_preserves_user_files(work_root: Path) -> None:
    project_root = work_root / "idempotent"
    config = ProjectConfig.init_project(project_root)
    first = apply_project_template(config, "uvm")
    readme = project_root / "README.telchines.md"
    readme.write_text("human note\n", encoding="utf-8")

    second = apply_project_template(ProjectConfig.load(project_root), "uvm")
    loaded = ProjectConfig.load(project_root)

    assert first["created"]
    assert "README.telchines.md" in second["skipped"]
    assert readme.read_text(encoding="utf-8") == "human note\n"
    assert loaded.retrieval["aliases"]["vif"] == ["virtual interface", "config_db"]


def test_project_template_catalog_and_package_data_are_available() -> None:
    catalog = resources.files("telchines.templates").joinpath("catalog.json")
    bundled_benchmark = resources.files("telchines.benchmarks").joinpath("coverage_import_ucis.json")
    bundled_fixture = resources.files("telchines.benchmarks").joinpath("assets/triage_uvm_logs/logs/uvm_timeout.log")

    assert catalog.is_file()
    assert bundled_benchmark.is_file()
    assert bundled_fixture.is_file()
    assert {"basic-rtl", "cocotb", "uvm", "vivado", "quartus", "libero"} <= set(TEMPLATE_NAMES)


def test_project_template_unknown_and_malformed_catalog_errors(work_root: Path, monkeypatch) -> None:
    config = ProjectConfig.init_project(work_root / "unknown")
    with pytest.raises(ValueError, match="unknown project template"):
        apply_project_template(config, "not-a-template")

    class BadCatalog:
        def joinpath(self, name: str) -> "BadCatalog":
            return self

        def read_text(self, encoding: str = "utf-8") -> str:
            return '{"templates": {}}'

    monkeypatch.setattr(project_templates_module.resources, "files", lambda package: BadCatalog())
    with pytest.raises(ValueError, match="template catalog is malformed"):
        list_project_templates()
