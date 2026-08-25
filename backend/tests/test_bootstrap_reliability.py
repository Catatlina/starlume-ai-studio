"""Reliability and truthfulness contracts for Bootstrap AI nodes (NC-SC-003)."""
from __future__ import annotations

import pytest


class _Cursor:
    def __init__(self, one=None):
        self.one = one

    def fetchone(self):
        return self.one


class _TaskDb:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=()):
        compact = " ".join(sql.split())
        self.statements.append((compact, params))
        if "SELECT * FROM run_nodes" in compact:
            return _Cursor({"status": "pending", "kind": "agent"})
        if "SELECT * FROM workflow_runs" in compact:
            return _Cursor({"id": "run-1", "project_id": "project-1", "novel_id": "novel-1", "context": {}})
        return _Cursor()

    def commit(self):
        pass

    def close(self):
        pass


@pytest.mark.parametrize(
    ("node_key", "task_type", "invalid_output"),
    [
        # V2 four-stage nodes: malformed output must never persist as success.
        ("plan_idea", "plan_idea", {}),
        ("plan_world_architecture", "plan_world_architecture", {"worldview": {}}),
        ("plan_character_system", "plan_character_system", {"characters": []}),
        ("blueprint_chapter_outline", "blueprint_chapter_outline", {"chapter_outlines": []}),
        ("write_chapter_draft", "write_chapter_draft", {"chapter": {"title": "", "body": []}}),
    ],
)
def test_invalid_or_empty_bootstrap_output_is_not_persisted_or_succeeded(
    monkeypatch, node_key, task_type, invalid_output
):
    from app.workers import tasks

    db = _TaskDb()
    monkeypatch.setattr(tasks, "connect", lambda: db)
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(tasks, "complete", lambda **_kwargs: invalid_output)
    # Stage enrichment reads real tables; irrelevant to this contract.
    monkeypatch.setattr(tasks, "_enrich_blueprint_context", lambda context, _novel_id: context)
    monkeypatch.setattr(tasks, "_enrich_finalization_context", lambda context, _novel_id: context)
    monkeypatch.setattr(tasks, "_write_before_search", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(tasks, "_strategy_directive_for_chapter", lambda *_args, **_kwargs: ("", []))
    monkeypatch.setattr(tasks, "_create_checkpoint", lambda *_args, **_kwargs: "")
    if node_key == "write_chapter_draft":
        from app.gateway import OutputValidationError

        def invalid_canonical(*_args, **_kwargs):
            raise OutputValidationError("canonical V7 output schema invalid")

        monkeypatch.setattr(tasks, "_run_canonical_v7_task", invalid_canonical)
    monkeypatch.setattr(
        tasks,
        "_persist_output",
        lambda *_args: pytest.fail("invalid output must not reach persistence"),
    )

    result = tasks.execute_bootstrap.run("run-1", node_key)

    assert result["status"] == "invalid_output"
    assert result["node_key"] == node_key
    assert any(params and params[0] == "failed" for sql, params in db.statements if sql.startswith("UPDATE"))
    assert not any(params and params[0] == "succeeded" for sql, params in db.statements if sql.startswith("UPDATE"))


def test_deterministic_chapter_contract_failure_does_not_retry_the_whole_workflow(monkeypatch):
    import json

    from app.v7.generation.generation_engine import AIGatewayError
    from app.workers import tasks

    db = _TaskDb()
    monkeypatch.setattr(tasks, "connect", lambda: db)
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        tasks,
        "_run_canonical_v7_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AIGatewayError(
            "single-pass chapter generation contract violation after bounded retry: "
            "first_candidate=3994,final_candidate=3300,minimum=2200,maximum=3000"
        )),
    )

    result = tasks.execute_bootstrap.run("run-1", "write_chapter_draft")

    assert result == {"status": "non_retryable_failure", "node_key": "write_chapter_draft"}
    assert any(
        params and params[0] == "failed" and json.loads(params[2])["retryable"] is False
        for sql, params in db.statements
        if "UPDATE run_nodes" in sql and "output = %s" in sql
    )


def test_explicit_chapter_promise_gets_one_planning_retry_before_prose(monkeypatch):
    from app.workers import tasks

    idea = "第一章就是军事行动，装甲车撞开午门，三分钟控制紫禁城，慈禧被架起来。"
    db = _TaskDb()
    original_execute = db.execute

    def execute(sql, params=()):
        compact = " ".join(sql.split())
        if "SELECT * FROM workflow_runs" in compact:
            return _Cursor({
                "id": "run-1",
                "project_id": "project-1",
                "novel_id": "novel-1",
                "context": {"idea": idea},
            })
        if "SELECT * FROM run_nodes" in compact and params[1] != "blueprint_chapter_outline":
            return _Cursor(None)
        return original_execute(sql, params)

    db.execute = execute
    calls = []
    persisted = []

    def chapter(seq, outline, beats, **extra):
        return {
            "volume": 1,
            "seq": seq,
            "title": f"第{seq}章",
            "outline": outline,
            "beats": beats,
            "foreshadow_plant": [],
            "foreshadow_reap": [],
            "function_type": "开篇吸引" if seq == 1 else "冲突升级",
            "chapter_goal": outline,
            "reader_expectation": "下一步结果",
            "payoff_contract": {},
            **extra,
        }

    bad = {"chapter_outlines": [
        chapter(1, "确认时空锚点并准备行动", ["测试锚点", "制定计划"]),
        chapter(2, "装甲车撞开午门并控制紫禁城", ["撞开午门", "控制紫禁城"]),
        chapter(3, "处理行动余波", ["清点结果"]),
    ]}
    good = {"chapter_outlines": [
        chapter(
            1,
            "装甲车撞开午门，部队三分钟控制紫禁城，慈禧被士兵架起。",
            ["装甲车撞开午门", "控制紫禁城", "架起慈禧"],
            delivery_state="completed_in_chapter",
            must_deliver=["撞开午门", "控制紫禁城", "架起慈禧"],
            visible_result="紫禁城被控制，慈禧被架起",
        ),
        chapter(2, "处理城内反应", ["确认新压力"]),
        chapter(3, "推进下一目标", ["建立新冲突"]),
    ]}

    def complete(**kwargs):
        calls.append(kwargs)
        return bad if len(calls) == 1 else good

    monkeypatch.setattr(tasks, "connect", lambda: db)
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(tasks, "complete", complete)
    monkeypatch.setattr(tasks, "_enrich_blueprint_context", lambda context, _novel_id: context)
    monkeypatch.setattr(tasks, "_create_checkpoint", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(tasks, "_persist_output", lambda *_args: persisted.append(_args[3]))

    result = tasks.execute_bootstrap.run("run-1", "blueprint_chapter_outline")

    assert result["status"] == "error"
    assert len(calls) == 2
    assert calls[0]["variables"]["quality_retry_feedback"] == ""
    assert "不得延后" in calls[1]["variables"]["quality_retry_feedback"]
    assert persisted[0]["chapter_outlines"][0]["delivery_state"] == "completed_in_chapter"


def test_mock_route_is_rejected(monkeypatch):
    from app import gateway
    from app.gateway import ProviderError

    monkeypatch.setenv("NOVELCRAFT_ENV", "development")
    class Db:
        def execute(self, _sql, _params=()):
            return _Cursor()

        def close(self):
            pass

    monkeypatch.setattr(gateway, "connect", lambda: Db())
    monkeypatch.setattr(gateway, "_load_prompt_and_route", lambda *_args: ("prompt", "mock", "mock", {}))
    monkeypatch.setattr(gateway, "_assert_budget", lambda *_args: None)

    with pytest.raises(ProviderError, match="mock"):
        gateway.complete(
            run_id="run-1",
            node_key="n3",
            project_id="project-1",
            task_type="gen_synopsis",
            prompt_name="bootstrap.gen_synopsis",
            variables={},
            client_mutation_id="bootstrap:run-1:n3",
        )


def test_test_environment_still_rejects_mock_provider(monkeypatch):
    from app import gateway
    from app.gateway import ProviderError

    class Db:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            self.statements.append((" ".join(sql.split()), params))
            return _Cursor()

        def commit(self):
            pass

        def close(self):
            pass

    db = Db()
    monkeypatch.setenv("NOVELCRAFT_ENV", "test")
    monkeypatch.setattr(gateway, "connect", lambda: db)
    monkeypatch.setattr(gateway, "_load_prompt_and_route", lambda *_args: ("prompt", "mock", "mock", {}))
    monkeypatch.setattr(gateway, "_assert_budget", lambda *_args: None)

    with pytest.raises(ProviderError, match="unsupported real provider: mock"):
        gateway.complete(
            run_id="run-1", node_key="n3", project_id="project-1",
            task_type="gen_synopsis", prompt_name="bootstrap.gen_synopsis", variables={},
            client_mutation_id="bootstrap:run-1:n3",
        )


def test_bootstrap_complete_uses_stable_node_mutation_id(monkeypatch):
    from app.workers import tasks

    db = _TaskDb()
    calls = []
    monkeypatch.setattr(tasks, "connect", lambda: db)
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(tasks, "_create_checkpoint", lambda *_args, **_kwargs: "")

    def complete(**kwargs):
        calls.append(kwargs)
        if kwargs["task_type"] == "audit_plan_fidelity":
            return {
                "passed": True,
                "score": 100,
                "matched_requirements": ["主角职业", "核心能力", "追查目标"],
                "contradictions": [],
                "omissions": [],
            }
        return {"idea_expanded": "一位创作者发现自己写下的故事正在现实中逐字发生，必须找出幕后执笔者。",
                "core_hook": "你写的每一个字都在杀人",
                "target_audience": "18-35 岁悬疑读者",
                "title_candidates": ["《甲》", "《乙》", "《丙》"],
                "source_facts": ["主角是作者", "文字影响现实", "主角必须追查真相"],
                "design_additions": [],
                "forbidden_changes": ["不得改变主角职业", "不得取消能力代价", "不得用巧合破局"],
                "downstream_deliverables": ["生成分卷总纲、章节细纲和第一章正文"],
                "creative_bible": (
                    "核心设定：主角写下的故事逐字发生，必须找到幕后执笔者并夺回现实叙事权。"
                    "开局节奏：第一章通过停电和短信确认异常，第二章寻找旁证，第三章发现现实被修订的痕迹。"
                    "能力边界：文字只能推动已有因果，不能凭空创造；每次改写都会留下可追踪旁证。"
                    "长篇路线：求证、保命、结盟、追查执笔会、反制规则、夺回叙事权。"
                    "人物关系：主角逃避又敏锐，盟友理性但有秘密，反派坚信控制现实是必要牺牲。"
                    "叙事禁忌：禁止说明书式设定，禁止巧合破局，禁止反派降智。"
                    "持续校验：检查旁证、代价、人物已知信息、伏笔回收和章节钩子。"
                    "黄金三章必须完成异常确认、证据闭环和第一次规则反击；中后期每次改写都要付出更高代价，"
                    "并让主角面对亲人安全、公众真相和自我欲望之间的冲突。故事推进必须依靠调查、验证、"
                    "选择和代价，不能用作者旁白直接解释全部规则。"
                )}

    monkeypatch.setattr(tasks, "complete", complete)
    monkeypatch.setattr(tasks, "_persist_output", lambda *_args: None)
    # Stop after plan_idea: the next node lookup returns missing.
    original_execute = db.execute

    def execute(sql, params=()):
        if "SELECT * FROM run_nodes" in sql and params[1] != "plan_idea":
            return _Cursor(None)
        return original_execute(sql, params)

    db.execute = execute

    result = tasks.execute_bootstrap.run("run-1", "plan_idea")

    assert result["status"] == "error"
    assert calls[0]["client_mutation_id"] == "bootstrap:run-1:plan_idea:fidelity-v2:cycle:1:plan:1"
    assert calls[1]["client_mutation_id"] == "bootstrap:run-1:plan_idea:fidelity-v2:cycle:1:audit:1"


def test_gateway_replay_by_mutation_id_returns_existing_output_without_new_writes(monkeypatch):
    from app import gateway

    existing = {"synopsis": "已生成简介", "selling_points": ["卖点"]}

    class Db:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            compact = " ".join(sql.split())
            self.statements.append((compact, params))
            if compact.startswith("SELECT output FROM ai_calls"):
                return _Cursor({"output": existing})
            return _Cursor()

        def close(self):
            pass

    db = Db()
    monkeypatch.setattr(gateway, "connect", lambda: db)

    output = gateway.complete(
        run_id="run-1", node_key="n3", project_id="project-1",
        task_type="gen_synopsis", prompt_name="bootstrap.gen_synopsis", variables={},
        client_mutation_id="bootstrap:run-1:n3",
    )

    assert output == existing
    assert len(db.statements) == 1
    assert db.statements[0][0].startswith("SELECT output FROM ai_calls")


def test_bootstrap_task_clears_request_context_after_completion(monkeypatch):
    from app.gateway import _request_api_base_url, _request_api_key, _request_model
    from app.workers import tasks

    db = _TaskDb()
    # No run/node makes the task return immediately; cleanup must still happen in finally.
    db.execute = lambda *_args, **_kwargs: _Cursor(None)
    monkeypatch.setattr(tasks, "connect", lambda: db)

    tasks.execute_bootstrap.run("missing-run", "n3", "secret-key", "https://provider.test", "model-x")

    assert _request_api_key.get() is None
    assert _request_api_base_url.get() is None
    assert _request_model.get() is None
