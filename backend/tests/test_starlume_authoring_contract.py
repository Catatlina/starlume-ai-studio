"""M1-M7 contract tests for the Starlume human-led authoring control plane."""
from __future__ import annotations

import pytest


def test_authoring_routes_are_registered():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/api/v1/authoring/sessions" in paths
    assert "/api/v1/authoring/sessions/current" in paths
    assert "/api/v1/authoring/context/{content_id}" in paths
    assert "/api/v1/authoring/chapters/{chapter_id}/skeleton" in paths
    assert "/api/v1/authoring/chapters/{chapter_id}/skeletons" in paths
    assert "/api/v1/authoring/chapters/{chapter_id}/skeletons/save" in paths
    assert "/api/v1/authoring/chapters/{chapter_id}/draft" in paths
    assert "/api/v1/authoring/chapters/{chapter_id}/drafts" in paths
    assert "/api/v1/authoring/chapters/{chapter_id}/drafts/save" in paths
    assert "/api/v1/authoring/story-bible/{item_id}/impact" in paths
    assert "/api/v1/authoring/provider-roles" in paths
    assert "/api/v1/authoring/writing-events" in paths
    assert "/api/v1/authoring/runs/{run_id}/clean" in paths
    assert "/api/v1/authoring/publication-variants/{variant_id}/human-receipt" in paths


def test_role_provider_status_never_claims_unconfigured_provider(monkeypatch):
    from app.api.v1.authoring import _provider_status

    monkeypatch.delenv("DOUBAO_API_KEY", raising=False)
    status = _provider_status("doubao")
    assert status["implemented"] is True
    assert status["key_configured"] is False
    assert status["status"] == "needs_key"


def test_doubao_adapter_fails_closed_without_real_credentials(monkeypatch):
    from app.ai.providers import call_doubao

    monkeypatch.delenv("DOUBAO_API_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="DOUBAO_API_KEY"):
        call_doubao("{}")


def test_editor_role_key_is_bounded():
    from app.schemas import AiEditRequest

    assert AiEditRequest(selection="当前正文", role_key="scene_expander").role_key == "scene_expander"
    with pytest.raises(ValueError):
        AiEditRequest(selection="当前正文", role_key="fake_writer")


def test_context_body_extraction_is_lossless_for_tiptap_shape():
    from app.api.v1.authoring import _text_from_body

    assert _text_from_body({"type": "doc", "content": [{"type": "paragraph", "text": "第一段"}, {"text": "第二段"}]}) == "第一段\n\n第二段"
    assert _text_from_body("人工正文") == "人工正文"


def test_chapter_skeleton_is_structured_and_not_final_prose():
    from app.gateway import validate_task_output
    from app.api.v1.authoring import _skeleton_char_count

    skeleton_text = "骨架节点。" * 140
    assert 700 <= _skeleton_char_count(skeleton_text) <= 1000
    output = validate_task_output("chapter_skeleton", {
        "title": "门后的账本",
        "chapter_goal": "主角拿到关键账本",
        "current_state": "主角被堵在旧仓库",
        "main_conflict": "取证和暴露身份只能选一个",
        "scenes": [
            {"title": "入口", "purpose": "建立压力", "trigger": "门外传来脚步", "action": "试探", "choice": "先取证还是先躲藏", "conflict": "被监视", "cost": "暴露退路", "outcome": "发现暗门", "visible_change": "出口从一个变成两个", "characters": ["主角"]},
            {"title": "暗门", "purpose": "推进线索", "trigger": "暗门后的灯亮起", "action": "取证", "choice": "拿走账本还是保留伪装", "conflict": "证据不完整", "cost": "留下指纹", "outcome": "留下代价", "visible_change": "反派得到主角来过的证据", "characters": ["主角"]},
            {"title": "反咬", "purpose": "形成结果", "trigger": "反派翻到最后一页", "action": "选择", "choice": "抢回账本或救下同伴", "conflict": "身份将暴露", "cost": "失去最后一页", "outcome": "反派先一步认出他", "visible_change": "对手从猜测进入确认", "characters": ["主角", "反派"]},
        ],
        "character_moves": ["主角从试探转为主动承担风险"],
        "mainline_progress": "取得账本并暴露下一层对手",
        "payoff": "主角拿到可验证证据",
        "foreshadowing": ["账本缺少最后一页"],
        "continuity_warnings": [],
        "next_hook": "反派用最后一页反过来设局",
        "reader_experience_plan": {
            "opening_anchor": "旧仓库门外的脚步逼近",
            "reader_discovery": "账本不是唯一证据，反派已经追到现场",
            "interest_change": "读者从想看主角取证转为担心身份暴露",
            "aftertaste": "主角拿到账本却失去最后一页",
            "continuation_question": "反派会如何利用最后一页设局",
        },
        "skeleton_text": skeleton_text,
    })
    assert output["skeleton_text"] == skeleton_text


def test_chapter_skeleton_prompt_has_a_real_length_budget():
    from app.prompt_registry import PROMPT_SEEDS

    name, version, _model, template = next(item for item in PROMPT_SEEDS if item[0] == "authoring.chapter_skeleton")
    assert name == "authoring.chapter_skeleton"
    assert version == "1.4.0"
    assert "不能写成几百字摘要" in template
    assert "7 个小节" in template
    assert "7 个小节合计目标为 760-900 个可见字" in template
    assert "作者先行，读者可跟随" in template
    assert "reader_experience_plan" in template
    assert "7 个小节合计目标为 760-900 个可见字" in template


def test_chapter_skeleton_protocol_rejects_a_flat_scene_summary():
    from app.api.v1.authoring import _validate_chapter_skeleton_protocol

    issues = _validate_chapter_skeleton_protocol({
        "chapter_goal": "拿到证据",
        "main_conflict": "取证会暴露身份",
        "payoff": "拿到证据",
        "next_hook": "对手追来",
        "scenes": [
            {"title": "场景", "outcome": "继续调查", "characters": ["主角"]},
            {"title": "场景", "outcome": "继续调查", "characters": ["主角"]},
            {"title": "场景", "outcome": "继续调查", "characters": ["主角"]},
        ],
        "reader_experience_plan": {"opening_anchor": "开门"},
        "skeleton_text": "待补充。" * 200,
    })
    assert "scene_1.choice is empty" in issues
    assert "reader_experience_plan.continuation_question is empty" in issues
    assert "skeleton_text contains a placeholder" in issues


def test_chapter_draft_uses_full_text_budget_and_keeps_prose_separate():
    from app.api.v1.authoring import ChapterDraftRequest, _draft_char_count, _validate_chapter_draft_protocol
    from app.gateway import validate_task_output

    paragraphs = ["他没有解释，只把门推开，里面的风迎面撞了出来。" * 16 for _ in range(6)]
    output = validate_task_output("chapter_draft", {
        "chapter": {"title": "门后的账本", "body": paragraphs},
    })
    text = "\n\n".join(paragraphs)
    assert 2200 <= _draft_char_count(text) <= 3000
    assert _validate_chapter_draft_protocol(output["chapter"]) == []
    assert ChapterDraftRequest().target_chars == 2600
    with pytest.raises(ValueError):
        ChapterDraftRequest(target_chars=1000)


def test_chapter_draft_prompt_is_prose_not_skeleton():
    from app.prompt_registry import PROMPT_SEEDS

    name, version, _model, template = next(item for item in PROMPT_SEEDS if item[0] == "authoring.chapter_draft")
    assert name == "authoring.chapter_draft"
    assert version == "1.0.0"
    assert "完整正文" in template
    assert "2200-3000" in template
    assert "章节规划" in template
    assert "不提朱雀或 AIGC" in template
