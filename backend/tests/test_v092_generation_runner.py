from __future__ import annotations

import importlib.util
from pathlib import Path


RUNNER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "v092_20_chapter_run.py"
SPEC = importlib.util.spec_from_file_location("v092_20_chapter_run", RUNNER_PATH)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def _result(**overrides):
    result = {
        "status": "completed",
        "passed_review": True,
        "quality_gate": {"passed": True, "failures": []},
        "continuity": {"passed": True, "issues": []},
        "reader_chapter_budget": {"minimum_chars": 1944, "maximum_chars": 3000},
        "generation_quality": {"generation_hard_max_chars": 3000},
        "review_score": 91.0,
    }
    result.update(overrides)
    return result


def test_generation_scope_does_not_require_publication_gates():
    report = RUNNER.validate_generation_result(_result(), "字" * 2863)

    assert report["passed"] is True
    assert report["scope"] == "generation"
    assert report["failures"] == []


def test_generation_scope_rejects_v7_quality_failure():
    report = RUNNER.validate_generation_result(
        _result(
            passed_review=False,
            quality_gate={"passed": False, "failures": [{"code": "continuity"}]},
        ),
        "字" * 2500,
    )

    assert report["passed"] is False
    assert {item["code"] for item in report["failures"]} >= {"v7_review", "v7_quality_gate"}


def test_generation_scope_rejects_only_the_hard_budget_overflow():
    report = RUNNER.validate_generation_result(_result(), "字" * 3001)

    assert report["passed"] is False
    assert report["failures"] == [
        {"code": "above_generation_maximum", "actual": 3001, "maximum": 3000}
    ]


def test_generation_scope_accepts_the_runtime_execution_envelope():
    report = RUNNER.validate_generation_result(
        {"status": "completed", "success": True, "raw": _result()},
        "字" * 2863,
    )

    assert report["passed"] is True
    assert report["review_score"] == 91.0
