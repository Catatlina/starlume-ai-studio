"""Contracts for the single canonical V7 prose generation path."""
from __future__ import annotations

import uuid


def test_generation_task_canonical_flag_routes_to_v7(monkeypatch):
    from app.workers import tasks

    calls = []
    monkeypatch.setattr(
        tasks,
        "_run_canonical_v7_task",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "canonical_engine": "v7",
            "status": "completed",
        },
    )

    result = tasks.gen_next_chapter_task.run(
        "novel-1",
        "project-1",
        canonical=True,
        chapter_number=4,
    )

    assert result["canonical_engine"] == "v7"
    assert calls[0][1]["chapter_number"] == 4


def test_canonical_bootstrap_marks_legacy_writer_nodes_as_delegated(monkeypatch):
    from app.workers import tasks

    class Cursor:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class DB:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            self.statements.append((" ".join(sql.split()), params))
            if "SELECT context FROM workflow_runs" in sql:
                return Cursor({"context": {"idea": "测试"}})
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    db = DB()
    monkeypatch.setattr(tasks, "connect", lambda: db)

    tasks._persist_canonical_bootstrap_result(
        "run-1",
        {
            "status": "completed",
            "run_id": "v7-run-1",
            "chapter_number": 1,
            "v6_content_id": "chapter-1",
            "review_score": 92,
            "dimension_scores": {"consistency": 90},
        },
    )

    node_updates = [
        statement for statement in db.statements if "UPDATE run_nodes" in statement[0]
    ]
    assert len(node_updates) == 8
    assert all(statement[1][4] == "run-1" for statement in node_updates)
    assert any("canonical_engine" in str(statement[1][1]) for statement in node_updates)


def test_canonical_bootstrap_keeps_pending_approval_truthful(monkeypatch):
    from app.workers import tasks

    class Cursor:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class DB:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            self.statements.append((" ".join(sql.split()), params))
            if "SELECT context FROM workflow_runs" in sql:
                return Cursor({"context": {}})
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    db = DB()
    monkeypatch.setattr(tasks, "connect", lambda: db)

    tasks._persist_canonical_bootstrap_result(
        "run-approval",
        {
            "status": "pending_approval",
            "run_id": "v7-run-approval",
            "chapter_number": 1,
            "blocked_reason": "confidence 0.45 below threshold 0.70",
        },
    )

    node_updates = [
        statement for statement in db.statements if "UPDATE run_nodes" in statement[0]
    ]
    assert node_updates[0][1][0] == "waiting_human"
    assert all(statement[1][0] in {"waiting_human", "pending"} for statement in node_updates)
    workflow_update = next(
        statement for statement in db.statements if "UPDATE workflow_runs" in statement[0]
    )
    assert workflow_update[1][2] == "waiting_human"


def test_provider_failure_is_retryable_but_missing_key_is_not():
    from app.v7.generation.generation_engine import is_retryable_provider_failure

    assert is_retryable_provider_failure(
        "plot assessment AI call failed: LLM call failed after 3 attempts: "
        "Server error '503 Service Unavailable'"
    ) is True
    assert is_retryable_provider_failure(
        "DEEPSEEK_API_KEY is not configured; refusing to fabricate output"
    ) is False
    assert is_retryable_provider_failure(
        "provider succeeded but V7 cost accounting failed"
    ) is False
    assert is_retryable_provider_failure(
        "single-pass chapter generation contract violation after bounded retry"
    ) is False


def test_cancelled_batch_slot_skips_provider_before_generation(monkeypatch):
    from app.workers import tasks

    class Cursor:
        def fetchone(self):
            return {"status": "cancelled", "cancel_requested": True}

    class DB:
        def execute(self, sql, params=()):
            return Cursor()

        def close(self):
            pass

    cleared = []
    monkeypatch.setattr(tasks, "connect", lambda: DB())
    monkeypatch.setattr(
        tasks,
        "_clear_batch_current_ordinal",
        lambda batch_id, ordinal: cleared.append((batch_id, ordinal)),
    )

    result = tasks._batch_slot_not_runnable("batch-cancelled", 3)

    assert result["status"] == "cancelled"
    assert result["reason"].endswith("Provider call skipped")
    assert cleared == [("batch-cancelled", 3)]


def test_chapter_title_normalizer_removes_meta_summary_lead():
    from app.v7.generation.generation_engine import ensure_unique_chapter_title

    title = ensure_unique_chapter_title(
        "旧宅回声",
        previous_titles=["旧宅回声"],
        chapter_number=6,
        hints=["主角在旧宅中发现一张旧照"],
    )

    assert title == "一张旧照"
    assert "主角" not in title
    assert "发现" not in title


def test_canonical_task_allows_complete_slow_v7_chain():
    from app.workers.tasks import gen_next_chapter_task

    assert gen_next_chapter_task.soft_time_limit == 1200
    assert gen_next_chapter_task.time_limit == 1500


def test_canonical_bootstrap_keeps_quality_rejection_actionable(monkeypatch):
    import json

    from app.workers import tasks

    class Cursor:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class DB:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            self.statements.append((" ".join(sql.split()), params))
            if "SELECT context FROM workflow_runs" in sql:
                return Cursor({"context": {}})
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    db = DB()
    monkeypatch.setattr(tasks, "connect", lambda: db)

    tasks._persist_canonical_bootstrap_result(
        "run-review",
        {
            "status": "needs_review",
            "run_id": "v7-run-review",
            "chapter_number": 1,
            "v6_content_id": "chapter-review",
            "review_score": 78,
            "issues": [{"dimension": "continuity", "description": "断点"}],
        },
    )

    node_updates = [
        statement for statement in db.statements if "UPDATE run_nodes" in statement[0]
    ]
    assert node_updates[0][1][0] == "needs_review"
    assert all(statement[1][0] in {"needs_review", "skipped"} for statement in node_updates)
    assert "草稿已保存" in str(node_updates[0][1][3])
    decoded_outputs = [json.loads(statement[1][1]) for statement in node_updates]
    assert all(output["retryable"] is False for output in decoded_outputs)
    assert decoded_outputs[0]["failure_kind"] == "quality_contract"


def test_v7_gateway_accepts_short_lived_provider_override():
    from app.v7.generation.generation_engine import AIGateway

    gateway = AIGateway(
        provider_config={
            "api_key": "request-only-key",
            "base_url": "https://provider.test/v1",
            "model": "request-model",
        }
    )

    assert gateway.api_key == "request-only-key"
    assert gateway.base_url == "https://provider.test/v1"
    assert gateway.default_model == "request-model"


def test_public_writer_agent_uses_canonical_v7_runtime(monkeypatch):
    from app.services.agent_registry import execute_agent

    captured = {}

    def fake_generate(novel_id, project_id, **kwargs):
        captured.update({"novel_id": novel_id, "project_id": project_id, **kwargs})
        return {
            "status": "completed",
            "canonical_engine": "v7",
            "chapter_number": 3,
            "title": "第三章",
            "content": "正文",
        }

    monkeypatch.setattr("app.v7.runtime.generate_v7_chapter_sync", fake_generate)
    result = execute_agent(
        "writer",
        "project-1",
        {"novel_id": "novel-1", "chapter_number": 3, "prompt": "继续承接"},
    )

    assert result["status"] == "succeeded"
    assert result["task_type"] == "v7_chapter_generation"
    assert result["output"]["canonical_engine"] == "v7"
    assert captured == {
        "novel_id": "novel-1",
        "project_id": "project-1",
        "chapter_number": 3,
        "prompt": "继续承接",
        "outline": None,
    }


def test_first_chapter_complete_brief_reaches_quality_gate_without_hiding_confidence():
    import asyncio

    from app.v7.director.story_director import StoryDirector

    class Permission:
        async def evaluate(self, _decision_type, _confidence):
            return {
                "allowed": False,
                "level": "auto",
                "threshold": 0.70,
                "blocked_reason": "confidence 0.55 below threshold 0.70",
            }

    class Brain:
        async def record_decision(self, *_args, **_kwargs):
            return {"id": "decision-1"}

    director = object.__new__(StoryDirector)
    director.permission_system = Permission()
    director.brain = Brain()
    result = asyncio.run(
        director._decide(
            1,
            {
                "confidence": 0.55,
                "plot_success": True,
                "context_ready": True,
                "blockers": [],
                "gaps": [],
            },
            run_id=uuid.uuid4(),
        )
    )

    assert result["allowed"] is True
    assert result["policy_override"] == "first_chapter_context_complete"


def test_first_chapter_structural_blocker_still_waits_for_review():
    import asyncio

    from app.v7.director.story_director import StoryDirector

    class Permission:
        async def evaluate(self, _decision_type, _confidence):
            return {
                "allowed": False,
                "level": "auto",
                "threshold": 0.70,
                "blocked_reason": "confidence 0.55 below threshold 0.70",
            }

    class Brain:
        async def record_decision(self, *_args, **_kwargs):
            return {"id": "decision-2"}

    director = object.__new__(StoryDirector)
    director.permission_system = Permission()
    director.brain = Brain()
    result = asyncio.run(
        director._decide(
            1,
            {
                "confidence": 0.55,
                "plot_success": True,
                "context_ready": True,
                "blockers": ["前情缺失"],
                "gaps": [],
            },
            run_id=uuid.uuid4(),
        )
    )

    assert result["allowed"] is False
    assert "confidence" in result["blocked_reason"]


def test_batch_confidence_observation_reaches_prose_gate_without_waiving_blockers():
    import asyncio

    from app.v7.director.story_director import (
        BATCH_AUTOGENERATION_CONFIDENCE_FLOOR,
        StoryDirector,
    )

    class Permission:
        async def evaluate(self, _decision_type, _confidence):
            return {
                "allowed": False,
                "level": "auto",
                "threshold": 0.70,
                "blocked_reason": "confidence 0.60 below threshold 0.70",
            }

    class Brain:
        async def record_decision(self, *_args, **_kwargs):
            return {"id": "decision-batch"}

    director = object.__new__(StoryDirector)
    director.generation_metadata = {"batch_id": "batch-1"}
    director.permission_system = Permission()
    director.brain = Brain()
    result = asyncio.run(
        director._decide(
            7,
            {
                "confidence": 0.60,
                "plot_success": True,
                "context_ready": False,
                "blockers": [],
                "gaps": ["state is still warming up"],
            },
            run_id=uuid.uuid4(),
        )
    )

    assert result["allowed"] is True
    assert result["policy_override"] == "batch_quality_observation"
    assert result["confidence_floor"] == BATCH_AUTOGENERATION_CONFIDENCE_FLOOR
    assert result["confidence_warning"]


def test_batch_confidence_below_valid_floor_still_waits_for_review():
    import asyncio

    from app.v7.director.story_director import StoryDirector

    class Permission:
        async def evaluate(self, _decision_type, _confidence):
            return {
                "allowed": False,
                "level": "auto",
                "threshold": 0.70,
                "blocked_reason": "confidence 0.04 below threshold 0.70",
            }

    class Brain:
        async def record_decision(self, *_args, **_kwargs):
            return {"id": "decision-batch-low"}

    director = object.__new__(StoryDirector)
    director.generation_metadata = {"batch_id": "batch-1"}
    director.permission_system = Permission()
    director.brain = Brain()
    result = asyncio.run(
        director._decide(
            7,
            {
                "confidence": 0.04,
                "plot_success": True,
                "context_ready": False,
                "blockers": [],
                "gaps": [],
            },
            run_id=uuid.uuid4(),
        )
    )

    assert result["allowed"] is False
    assert "confidence" in result["blocked_reason"]


def test_quality_rework_feedback_formats_labeled_risk_failures():
    from app.v7.director.story_director import _format_quality_failure

    assert _format_quality_failure({
        "dimension": "ai_feel",
        "actual": "high",
        "minimum": "resolved",
    }) == "ai_feel high/resolved"

    assert _format_quality_failure({
        "dimension": "pacing",
        "actual": 78,
        "minimum": 85,
    }) == "pacing 78/85"


def test_public_writer_agent_requires_novel_id():
    from app.services.agent_registry import execute_agent

    try:
        execute_agent("writer", "project-1", {})
    except ValueError as exc:
        assert "novel_id" in str(exc)
    else:
        raise AssertionError("writer agent must not reopen the legacy V6 path")


def test_public_review_agents_share_the_canonical_v7_audit_contract(monkeypatch):
    import uuid

    from app.services import agent_registry

    novel_id = str(uuid.uuid4())
    content = {
        "id": str(uuid.uuid4()),
        "project_id": "project-1",
        "parent_id": novel_id,
        "type": "chapter",
        "seq": 2,
        "meta": {},
        "body": "旧正文",
    }
    monkeypatch.setattr(
        agent_registry,
        "_load_review_target",
        lambda project_id, variables: (content, "当前编辑器正文"),
    )
    captured = []

    def fake_review(target, text, **kwargs):
        captured.append((target, text, kwargs))
        return {
            "canonical_engine": "v7",
            "overall_score": 91.0,
            "dimension_scores": {"consistency": 91},
            "audit_report": {"schema_version": "33d-v1", "count": 33, "items": {}},
            "continuity": {"status": "continuous", "checked": True},
            "provenance": {
                "engine": "v7",
                "prompt_name": "v7.review.33_dimension",
                "prompt_version": "1.1.0",
            },
            "issues": [],
        }

    monkeypatch.setattr("app.v7.review_service.review_chapter_v7_sync", fake_review)
    for agent_id in ("reviewer", "consistency-checker"):
        result = agent_registry.execute_agent(
            agent_id,
            "project-1",
            {"content_id": content["id"]},
        )
        assert result["status"] == "succeeded"
        assert result["task_type"] == "v7_review_33_dimension"
        assert result["output"]["canonical_engine"] == "v7"
        assert result["output"]["score"] == 91.0
        assert result["output"]["dimensions"] == {"consistency": 91}

    assert [item[1] for item in captured] == ["当前编辑器正文", "当前编辑器正文"]
    assert all(
        item[2]["use_cache"] is False
        and item[2]["model"] == ""
        for item in captured
    )


def test_v6_is_only_used_as_compatibility_fact_source():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    main_source = (root / "backend/app/main.py").read_text(encoding="utf-8")
    assert "canonical=True" in main_source
    runtime_source = (root / "backend/app/v7/runtime.py").read_text(encoding="utf-8")
    assert "Canonical V7 chapter runtime" in runtime_source
    assert "v6_compat_import" in runtime_source
