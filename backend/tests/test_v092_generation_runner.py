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
        "reader_chapter_budget": {"minimum_chars": 2200, "maximum_chars": 3000},
        "generation_quality": {"passed": True, "generation_hard_max_chars": 3000},
        "review_score": 91.0,
    }
    result.update(overrides)
    return result


def test_generation_scope_does_not_require_publication_gates():
    report = RUNNER.validate_generation_result(_result(), "字" * 2863)

    assert report["passed"] is True
    assert report["scope"] == "generation"
    assert report["failures"] == []


def test_generation_scope_keeps_review_failure_as_human_edit_warning():
    report = RUNNER.validate_generation_result(
        _result(
            passed_review=False,
            quality_gate={"passed": False, "failures": [{"code": "continuity"}]},
        ),
        "字" * 2500,
    )

    assert report["passed"] is True
    assert report["review_is_blocking"] is False
    assert report["review_observation"]["passed_review"] is False


def test_generation_scope_still_rejects_generation_quality_failure():
    report = RUNNER.validate_generation_result(
        _result(generation_quality={
            "passed": False,
            "generation_hard_max_chars": 3000,
            "failures": [{"code": "opening_mode_mismatch"}],
        }),
        "字" * 2500,
    )

    assert report["passed"] is False
    assert {item["code"] for item in report["failures"]} == {"generation_quality"}


def test_generation_scope_rejects_only_the_hard_budget_overflow():
    report = RUNNER.validate_generation_result(_result(), "字" * 3001)

    assert report["passed"] is False
    assert report["failures"] == [
        {"code": "above_generation_maximum", "actual": 3001, "maximum": 3000}
    ]


def test_generation_scope_ignores_paragraph_formatting_in_budget_count():
    report = RUNNER.validate_generation_result(
        _result(),
        "字" * 2990 + "\n\n" + "字" * 10,
    )

    assert report["passed"] is True
    assert report["text_length"] == 3000


def test_generation_scope_accepts_the_runtime_execution_envelope():
    report = RUNNER.validate_generation_result(
        {"status": "completed", "success": True, "raw": _result()},
        "字" * 2863,
    )

    assert report["passed"] is True
    assert report["review_score"] == 91.0
