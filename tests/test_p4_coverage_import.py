from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from telchines.config import ProjectConfig
from telchines.coverage_import import _item, import_coverage_report
from telchines.operations import coverage_plan
from telchines.utils import read_json, write_json
from telchines.workflows.coverage import load_coverage_report


IDENT = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,12}", fullmatch=True)
METRIC = st.sampled_from(["functional", "assertion", "code", "fsm"])
COUNT = st.integers(min_value=-25, max_value=500)
GOAL = st.integers(min_value=-5, max_value=500)


@settings(max_examples=75)
@given(item_id=st.text(min_size=0, max_size=30), module=IDENT, metric=METRIC, name=IDENT, hits=COUNT, goal=GOAL, detail=st.text(max_size=60))
def test_coverage_item_normalization_invariants(item_id: str, module: str, metric: str, name: str, hits: int, goal: int, detail: str) -> None:
    normalized = _item(item_id=item_id, module=module, metric=metric, name=name, hits=hits, goal=goal, detail=detail)

    assert normalized["item_id"]
    assert "/" not in normalized["item_id"]
    assert normalized["module"] == module
    assert normalized["metric"] == metric
    assert normalized["name"] == name
    assert normalized["hits"] >= 0
    assert normalized["goal"] >= 1
    assert normalized["coverage"] == round(normalized["hits"] / normalized["goal"], 3)


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    module=IDENT,
    coverpoint=IDENT,
    bin_name=IDENT,
    metric=METRIC,
    hits=st.integers(min_value=0, max_value=20),
    goal=st.integers(min_value=1, max_value=20),
    include_bad_entries=st.booleans(),
)
def test_ucis_json_import_property_shapes(
    sample_project: Path,
    module: str,
    coverpoint: str,
    bin_name: str,
    metric: str,
    hits: int,
    goal: int,
    include_bad_entries: bool,
) -> None:
    source = sample_project / "cov" / "generated_ucis.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    covergroups: list[object] = [
        {
            "name": f"{module}_cg",
            "module": module,
            "coverpoints": [
                {
                    "name": coverpoint,
                    "metric": metric,
                    "source": "rtl/uart_rx.sv",
                    "bins": [{"name": bin_name, "count": hits, "at_least": goal}],
                }
            ],
        }
    ]
    if include_bad_entries:
        covergroups.append("not an object")
        covergroups[0]["coverpoints"].append("not an object")  # type: ignore[index, union-attr]
    write_json(
        source,
        {
            "tool": "ucis-json",
            "generated_at": "2026-04-15T00:00:00+00:00",
            "design": module,
            "focus_paths": ["rtl/uart_rx.sv"],
            "covergroups": covergroups,
            "exclusions": [{"item_id": f"{module}_{coverpoint}_{bin_name}", "reason": "waived"}],
        },
    )

    imported = import_coverage_report(ProjectConfig.load(sample_project), source, source_format="ucis-json", output=Path("cov/imported_ucis.json"))
    report = load_coverage_report(sample_project / "cov" / "imported_ucis.json")

    assert imported["item_count"] == 1
    assert imported["excluded_count"] == 1
    assert report.items[0].module == module
    assert report.items[0].metric == metric
    assert report.items[0].hits == hits
    assert report.items[0].goal == goal
    assert imported["warning_count"] >= (2 if include_bad_entries else 0)


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    source_format=st.sampled_from(["vivado", "quartus", "questa-text"]),
    module=IDENT,
    metric=METRIC,
    name=IDENT,
    hits=st.integers(min_value=0, max_value=10),
    goal=st.integers(min_value=1, max_value=10),
)
def test_text_coverage_import_property_shapes(sample_project: Path, source_format: str, module: str, metric: str, name: str, hits: int, goal: int) -> None:
    source = sample_project / "cov" / f"{source_format}.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    line = f"COVERAGE: {module} {metric} {name} {hits}/{goal} rtl/uart_rx.sv\n"
    source.write_text(line, encoding="utf-8")

    imported = import_coverage_report(ProjectConfig.load(sample_project), source, source_format=source_format, output=Path(f"cov/{source_format}_imported.json"))
    payload = read_json(sample_project / "cov" / f"{source_format}_imported.json")

    assert imported["status"] == "imported"
    assert imported["item_count"] == 1
    assert payload["items"][0]["module"] == module
    assert payload["items"][0]["metric"] == metric
    assert payload["items"][0]["name"] == name
    assert payload["items"][0]["hits"] == hits
    assert payload["items"][0]["goal"] == goal
    assert payload["focus_paths"] == ["rtl/uart_rx.sv"]


def test_coverage_import_warnings_errors_passthrough_and_planning(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    source = sample_project / "cov" / "mixed.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "\n".join(
            [
                "Coverage summary could not parse this coverpoint",
                "Coverpoint uart_rx.start_bit_seen 0/2 rtl/uart_rx.sv",
            ]
        ),
        encoding="utf-8",
    )

    imported = import_coverage_report(config, source, source_format="questa-text", output=Path("cov/mixed_imported.json"))
    plan = coverage_plan(sample_project, Path("cov/mixed_imported.json"))

    assert imported["warning_count"] == 1
    assert plan["recommendation_count"] == 1
    assert plan["recommendations"][0]["item_id"] == "uart_rx_start_bit_seen"

    telchines_source = sample_project / "cov" / "native.json"
    write_json(
        telchines_source,
        {
            "schema_version": "0.1",
            "tool": "native",
            "generated_at": "2026-04-15T00:00:00+00:00",
            "design": "uart_rx",
            "items": [{"item_id": "native_bin", "module": "uart_rx", "metric": "functional", "name": "native_bin", "hits": 0, "goal": 1}],
        },
    )
    native = import_coverage_report(config, telchines_source, source_format="telchines-json", output=Path("cov/native_imported.json"))
    assert native["item_count"] == 1

    unsupported = sample_project / "cov" / "unsupported.txt"
    unsupported.write_text("Coverage summary only\n", encoding="utf-8")
    with pytest.raises(ValueError, match="did not contain any supported coverage lines"):
        import_coverage_report(config, unsupported, source_format="questa-text", output=Path("cov/nope.json"))
    with pytest.raises(ValueError, match="unsupported coverage format"):
        import_coverage_report(config, source, source_format="unknown", output=Path("cov/nope.json"))
    with pytest.raises(ValueError, match="coverage source does not exist"):
        import_coverage_report(config, sample_project / "cov" / "missing.json", source_format="ucis-json", output=Path("cov/nope.json"))
