import asyncio
import json
import re
from contextlib import asynccontextmanager

import pytest

from app.prompt_registry import PROMPT_SEEDS, render_prompt
from app.services.quality_risks import build_quality_repair_contract
from app.v7.quality.deai_metrics import analyze_deai_patterns
from app.v7.quality.generation_naturalness import (
    inspect_generation_naturalness,
    render_generation_style_protocol,
    select_generation_style_path,
)
from app.v7.generation.generation_engine import (
    AIGateway,
    AIGatewayError,
    DeAIPipeline,
    GenerationEngine,
    SCENE_DEEPSEEK_OVERLONG_REPAIR_MARGIN,
    SCENE_DEEPSEEK_FINAL_TRUNCATION_REPAIR_MARGIN,
    SCENE_BUDGET_RETRY_COMPLETION_MARGIN,
    SCENE_BUDGET_RETRY_SMALL_SCENE_COMPLETION_MARGIN,
    SCENE_BUDGET_RETRY_SMALL_SCENE_MAX_CHARS,
    SCENE_MIXED_TRUNCATION_OVERLONG_REPAIR_MARGIN,
    SCENE_NATURAL_LENGTH_TOLERANCE,
    SCENE_NATURAL_LENGTH_TOLERANCE_CHARS,
    SCENE_NATURAL_LENGTH_SOFT_OVERFLOW_CHARS,
    SCENE_FUTURE_RESERVE_RATIO,
    CHAPTER_FINAL_SCENE_NATURAL_VARIANCE_CHARS,
    SCENE_PROVIDER_TOKEN_CAP,
    SCENE_TARGET_MAX_RATIO,
    SceneDirector,
    ensure_unique_chapter_title,
    validate_tomato_chapter_title,
)
from app.v7.quality.opening_variation import (
    build_opening_history,
    classify_opening,
    inspect_opening,
    select_opening_plan,
)
from app.v7.quality.prose_generation import (
    apply_segment_replacements,
    build_generation_critic_report,
    build_prose_feature_card,
    feature_card_from_style_card,
    render_prose_feature_card,
    sanitise_style_card_for_prompt,
)
from app.v7.quality.readability_contract import build_readability_plan, render_readability_plan
from app.services.quality_profiles import select_quality_profile


def _complete_scene_card(index: int) -> dict:
    return {
        "location": f"场景地点{index}",
        "time": "夜里",
        "characters": ["主角"],
        "goal": "确认当前异常",
        "obstacle": "异常阻止主角直接得到答案",
        "choice": "主角选择先试探再推进",
        "turn": "试探触发新的现场变化",
        "state_change": "线索和风险各增加一项",
        "knowledge_boundary": "主角只知道现场可见事实",
        "handoff": "下一场承接新的现场压力",
        "trigger": "上一动作留下的可见异常",
        "causal_link": "可见异常促使主角作出当前选择并产生结果",
    }


def test_repeated_chapter_title_gets_a_short_plot_hook():
    title = ensure_unique_chapter_title(
        "语音里的求救",
        previous_titles=["语音里的求救", "旧手机"],
        chapter_number=22,
        hints=["走廊尽头的钟声再次响起"],
    )

    assert title == "走廊尽头的钟声再次响起"
    assert title != "语音里的求救"

    compact = ensure_unique_chapter_title(
        "语音里的求救",
        previous_titles=["语音里的求救"],
        chapter_number=24,
        hints=["语音中传来一个陌生男人的声音"],
    )
    assert compact == "一个陌生男人"


def test_tomato_title_gate_rejects_summary_titles_and_accepts_reader_hooks():
    assert validate_tomato_chapter_title("江心岛迷雾·周衡在逃脱后，发现手机屏")[0] is False
    assert validate_tomato_chapter_title("密室现身")[0] is True


def test_opening_scheduler_is_global_and_does_not_default_to_body_sensation():
    first = select_opening_plan(1, previous_history=[])
    assert first["mode"] == "action"
    assert first["mode"] != "body_sensation"

    second = select_opening_plan(
        2,
        chapter_type="normal",
        previous_history=[{"chapter_number": 1, "mode": first["mode"]}],
    )
    assert second["mode"] != first["mode"]
    assert second["mode"] != "body_sensation"


def test_opening_history_prefers_persisted_observed_mode_over_legacy_classifier():
    history = build_opening_history([
        {
            "chapter_number": 11,
            "text": "门外传来警报，所有人同时回头。",
            "opening": {"observed_mode": "action"},
        }
    ])

    assert history[0]["mode"] == "action"


def test_explicit_repeated_opening_mode_is_replaced_by_safe_scheduler_choice():
    plan = select_opening_plan(
        12,
        previous_history=[
            {"chapter_number": 9, "mode": "action"},
            {"chapter_number": 10, "mode": "object"},
            {"chapter_number": 11, "mode": "dialogue"},
        ],
        plot_brief={"opening_mode": "action"},
    )

    assert plan["mode"] not in {"action", "object", "dialogue"}


def test_json_gateway_retries_truncated_output_with_compact_larger_budget():
    gateway = object.__new__(AIGateway)
    calls = []

    async def fake_generate(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if len(calls) == 1:
            return {
                "text": '{"chapter_title":"截断',
                "tokens_input": 3,
                "tokens_output": 10,
                "cost": 0.01,
                "model": "test",
                "provider": "deepseek",
                "finish_reason": "length",
            }
        return {
            "text": '{"chapter_title":"完整计划"}',
            "tokens_input": 3,
            "tokens_output": 12,
            "cost": 0.01,
            "model": "test",
            "provider": "deepseek",
            "finish_reason": "stop",
        }

    gateway.generate = fake_generate
    result = asyncio.run(gateway.generate_json("输出计划", max_tokens=100))

    assert result["data"]["chapter_title"] == "完整计划"
    assert calls[1]["max_tokens"] == 150
    assert "完整 JSON" in calls[1]["prompt"]


def test_plain_gateway_never_returns_provider_truncated_prose(monkeypatch):
    from types import SimpleNamespace

    from app.v7.generation import generation_engine as generation_module

    gateway = object.__new__(AIGateway)
    gateway.provider = "deepseek"
    gateway.api_key = "configured-for-test"
    gateway.base_url = "https://example.invalid/v1"
    gateway.default_model = "deepseek-chat"
    gateway.timeout = 1
    gateway.max_retries = 2
    gateway.db = None
    gateway.novel_id = None
    gateway.project_id = None
    gateway.tracer = None
    gateway._route_resolved = False
    requested_limits = []

    class FakeGateway:
        def __init__(self, **_kwargs):
            pass

        async def complete_async(self, _prompt, **kwargs):
            requested_limits.append(kwargs["max_tokens"])
            if len(requested_limits) == 1:
                return SimpleNamespace(
                    content="正文在对白中途截断",
                    prompt_tokens=5,
                    completion_tokens=100,
                    finish_reason="length",
                )
            return SimpleNamespace(
                content="完整正文。",
                prompt_tokens=5,
                completion_tokens=20,
                finish_reason="stop",
            )

    monkeypatch.setattr(generation_module, "UnifiedAIGateway", FakeGateway)
    result = asyncio.run(gateway.generate("写一章正文", max_tokens=100))

    assert result["text"] == "完整正文。"
    assert requested_limits == [100, 700]


def test_bounded_scene_truncation_does_not_expand_provider_budget(monkeypatch):
    from types import SimpleNamespace

    from app.v7.generation import generation_engine as generation_module

    gateway = object.__new__(AIGateway)
    gateway.provider = "deepseek"
    gateway.api_key = "configured-for-test"
    gateway.base_url = "https://example.invalid/v1"
    gateway.default_model = "deepseek-chat"
    gateway.timeout = 1
    gateway.max_retries = 3
    gateway.db = None
    gateway.novel_id = None
    gateway.project_id = None
    gateway.tracer = None
    gateway._route_resolved = False
    requested_limits = []

    class FakeGateway:
        def __init__(self, **_kwargs):
            pass

        async def complete_async(self, _prompt, **kwargs):
            requested_limits.append(kwargs["max_tokens"])
            return SimpleNamespace(
                content="场景被截断",
                prompt_tokens=5,
                completion_tokens=100,
                finish_reason="length",
            )

    monkeypatch.setattr(generation_module, "UnifiedAIGateway", FakeGateway)
    result = asyncio.run(gateway.generate(
        "写一个场景",
        max_tokens=100,
        expand_on_truncation=False,
    ))

    assert result["truncated"] is True
    assert requested_limits == [100]


def test_provider_opening_repair_only_replaces_first_paragraph():
    class Gateway:
        async def generate_json(self, *_args, **_kwargs):
            return {
                "data": {"opening_text": "他抬手按住门把，门内立刻传来第二声敲击。"},
                "usage": {"tokens_input": 2, "tokens_output": 4, "cost": 0.01},
            }

    source = "警报声突然响起，所有人被迫转身。\n\n林越盯着门缝，没有松手。"
    result = asyncio.run(
        DeAIPipeline(Gateway()).repair_opening(
            source,
            chapter_number=12,
            opening_plan={"mode": "action", "forbidden_recent_modes": ["external_event"]},
        )
    )

    assert result["quality_gate"]["passed"] is True
    assert result["opening"]["observed_mode"] == "action"
    assert result["processed_text"].endswith("林越盯着门缝，没有松手。")


def test_opening_gate_rejects_the_repeated_body_sensation_template():
    text = "后脑勺的钝痛一浪一浪地顶上来，像有人攥着他的后脑勺往地上砸。"
    result = inspect_opening(
        text,
        requested_mode="action",
        chapter_number=1,
    )
    assert result["passed"] is False
    assert result["observed_mode"] == "body_sensation"
    assert {
        item["code"] for item in result["flags"]
    } >= {
        "opening_body_sensation_default",
        "opening_body_sensation_cliche",
        "opening_first_chapter_body_default",
    }


def test_action_opening_with_environmental_consequences_is_not_misclassified():
    text = (
        "苏长庚推门进藏经阁时，天刚擦黑。扫帚靠在门边，他弯腰拎起来，"
        "凭熟路往楼梯口走。一楼的灰被鞋底带起，在昏光里浮起又落下。"
    )

    assert classify_opening(text) == "action"
    result = inspect_opening(
        text,
        requested_mode="action",
        chapter_number=2,
        recent_modes=["environment"],
    )
    assert result["passed"] is True
    assert result["observed_mode"] == "action"


def test_action_opening_with_late_body_feedback_is_not_misclassified():
    text = "竹扫帚划到第七十七道砖缝时，苏长庚的脚底忽然震了一下。"

    assert classify_opening(text) == "action"
    result = inspect_opening(
        text,
        requested_mode="action",
        chapter_number=1,
    )
    assert result["passed"] is True
    assert result["observed_mode"] == "action"


def test_natural_action_opening_with_a_pause_is_classified_as_action():
    text = "帚尖抵住台阶边缘，苏长庚手腕一顿。楼上的门缝里没有声音。"

    assert classify_opening(text) == "action"
    result = inspect_opening(
        text,
        requested_mode="action",
        chapter_number=1,
    )
    assert result["passed"] is True


def test_action_opening_allows_short_scene_lead_before_early_visible_action():
    text = (
        "午后的日头正毒，外门广场的青石砖被晒得发烫。"
        "周衡站在高台上，抬手示意执事弟子把顾沉押上来。"
    )

    assert classify_opening(text) == "unknown"
    result = inspect_opening(
        text,
        requested_mode="action",
        chapter_number=1,
    )
    assert result["passed"] is True
    assert result["observed_mode"] == "action"


def test_action_opening_recognises_common_chinese_action_verbs():
    text = "演武场上的尘土还没落定，周衡已经站到了石阶最高处。他手里捏着一卷竹简，在掌心拍了两下。"

    result = inspect_opening(text, requested_mode="action", chapter_number=1)

    assert result["passed"] is True
    assert result["observed_mode"] == "action"


def test_action_opening_does_not_accept_action_that_arrives_too_late():
    text = (
        "午后的日头正毒，外门广场的青石砖被晒得发烫。"
        "人群围在高台下，没人说话。"
        "风从山门方向吹来，卷起一层灰，落在每个人的鞋面上。"
        "高台边的旗角随风轻轻摆动，木杆发出细微的碰撞声。"
        "执事弟子站在台阶两侧，目光始终落在广场中央。"
        "有人低声咳了一下，很快又把声音压了回去。"
        "远处的钟声敲过两遍，晒热的石面仍旧没有凉下来。"
        "围观的人换了几次站姿，却没有一个人先离开。"
        "周衡低头看完手里的名册，过了很久才抬手。"
    )

    result = inspect_opening(
        text,
        requested_mode="action",
        chapter_number=1,
    )
    assert result["passed"] is False
    assert any(item["code"] == "opening_mode_mismatch" for item in result["flags"])


def test_object_opening_is_not_reclassified_by_a_later_action():
    text = "门缝里的光变了。苏长庚抬脚走近，伸手去碰那道裂痕。"

    assert classify_opening(text) == "object"
    result = inspect_opening(
        text,
        requested_mode="object",
        chapter_number=2,
        recent_modes=["action"],
    )
    assert result["passed"] is True
    assert result["observed_mode"] == "object"


def test_object_extinguishing_before_later_action_is_not_misclassified():
    text = "门缝下的青光在苏长庚踏进去的瞬间熄了，像被他的脚踩灭的。"

    assert classify_opening(text) == "object"
    result = inspect_opening(
        text,
        requested_mode=None,
        chapter_number=2,
        recent_modes=["action"],
    )
    assert result["passed"] is True
    assert result["observed_mode"] == "object"


def test_object_opening_recognises_door_hardware_change():
    text = "门轴先响了一声。苏长庚站在第七层门前，门缝里的青白光线往外漫。"

    result = inspect_opening(
        text,
        requested_mode="object",
        chapter_number=2,
        recent_modes=["action"],
    )

    assert result["passed"] is True
    assert result["observed_mode"] == "object"


def test_object_opening_recognises_subtle_object_motion_and_contact():
    for text in (
        "门缝里的灰又动了。苏长庚抬眼看向第七层。",
        "天还没亮透，怀里那本册子贴着胸口发硬。苏长庚停在楼梯口。",
    ):
        result = inspect_opening(
            text,
            requested_mode="object",
            chapter_number=2,
            recent_modes=["action"],
        )
        assert result["passed"] is True
        assert result["observed_mode"] == "object"


def test_later_action_does_not_hide_an_unfulfilled_object_opening():
    text = "清晨，藏经阁还没亮透，苏长庚的帚尖已经落在第七层台阶上。门缝里的光随后才亮起来。"

    assert classify_opening(text) == "unknown"
    result = inspect_opening(
        text,
        requested_mode="object",
        chapter_number=2,
        recent_modes=["action"],
    )
    assert result["passed"] is False
    assert any(item["code"] == "opening_mode_mismatch" for item in result["flags"])
    assert not any(item["code"] == "opening_mode_repetition" for item in result["flags"])


def test_natural_measure_phrase_counts_as_an_object_opening():
    text = "那卷黄绸裹着的东西静静躺在门内正中央，边缘散着几块碎玉。"

    assert classify_opening(text) == "object"
    result = inspect_opening(
        text,
        requested_mode="object",
        chapter_number=2,
        recent_modes=["action"],
    )
    assert result["passed"] is True


def test_object_opening_accepts_light_seeping_through_a_door_gap():
    text = "门缝里透出一线白，不是黑气，是光。"

    assert classify_opening(text) == "object"
    result = inspect_opening(text, requested_mode="object", chapter_number=2)

    assert result["passed"] is True
    assert result["observed_mode"] == "object"


def test_generation_naturalness_blocks_explanation_metaphor_and_action_loops():
    text = (
        "不是灰尘，而是门缝里透出的一线光。他终于意识到这意味着封印松动，"
        "那光像冬天的月亮，也像有人在门后吐息。苏长庚把竹帚放回墙角，又重新拿起，"
        "转身走开后又回头。"
    )

    report = inspect_generation_naturalness(text)
    codes = {item["code"] for item in report["flags"]}
    assert "scene_explanatory_narration" in codes
    assert "scene_metaphor_density" not in codes  # short snippets do not overfire
    assert "scene_repeated_action_loop" in codes

    long_text = text + (
        "具体动作落在门锁、灰尘和台阶上。"
        "墙上的影子像被水冲散。"
        "灯芯像被风压低。"
        "门缝里的声音像贴着地面爬行。"
        "灰尘像细沙一样落回砖缝。"
    ) * 40
    long_report = inspect_generation_naturalness(long_text)
    long_codes = {item["code"] for item in long_report["flags"]}
    assert "scene_metaphor_density" in long_codes


def test_generation_naturalness_blocks_state_echo_and_subject_motion_loop():
    text = "\n\n".join([
        "顾沉没有力气回答，沙地的温度从身体里抽走，心跳又慢又重。",
        "顾沉连抬头的力气都没有，身体里的东西一点点流干，心跳慢得像要停下。",
        "他扶着树干往里走，松针落在肩上，脚下的枯枝发出脆响。",
        "他走了百步，停下来喘气，靠在老松上，手指扣进树皮。",
        "天色暗下来，他数着心跳，继续往前走，每走几步就停一下。",
        "他又扶着树干往前挪，腿一软，靠在树上喘了很久。",
        "他正要继续，忽然听见身后有脚步，声音只响了一下。",
        "他没有回头，屏住呼吸，等了十息，才松开树皮继续往前走。",
        "岔路分成两条，他看了看天色，又看了看两条路，最后选了往下的一条。",
    ])

    report = inspect_generation_naturalness(text)
    codes = {item["code"] for item in report["flags"]}

    assert "scene_state_echo" in codes
    assert "scene_subject_opening" in codes
    assert report["state_echo"]["group_count"] >= 2


def test_generation_naturalness_counts_dialogue_before_prose_filtering():
    text = "\n\n".join([
        "他推开门，里面没有人。",
        "“谁？”他压低声音。",
        "门后传来回答：“别进来。”",
        "他退了半步，手仍按着门把。",
        "楼梯上落下一粒石子。",
        "他没有回头，等着那声音再次出现。",
        "风从门缝里钻出来，吹动了桌上的纸。",
    ])

    report = inspect_generation_naturalness(text)

    assert report["procedural_motion"]["dialogue_count"] == 2


def test_generation_naturalness_flags_route_log_without_early_turn():
    text = "\n\n".join([
        "他扶着树干往里走，松针落在肩上，脚下的枯枝发出脆响，泥水顺着鞋面往下流。",
        "他走了百步，停下来喘气，靠在老松上，低头看自己的手，手背沾着一层湿泥。",
        "天色暗得很快，他继续往前走，沿着土径一点点往下挪，树枝擦过他的衣摆。",
        "他走几步就停下来喘一阵，扶着树干换个姿势，再继续往前，脚底陷进松软的土里。",
        "他拨开灌木，绕过石头，沿着坡脚往低处走，脚底踩过湿土，鞋底越来越沉。",
        "他停下来，抬头看天，又低头看脚下，继续沿着土径往下，手指在树皮上留下几道白痕。",
        "直到听见身后有一声脚步，他才停住，回头看向林子。",
        "他选了右边的岔路，扶着树枝往下走，枝条刮过衣摆。",
    ])

    codes = {item["code"] for item in inspect_generation_naturalness(text)["flags"]}

    assert "scene_procedural_motion" in codes


def test_generation_naturalness_does_not_treat_two_characters_turning_as_a_loop():
    text = (
        "赵小胖回头看了一眼。苏长庚背对着他，扫帚一下一下扫着。"
        "赵小胖张了张嘴，最终没再出声，转身走了。"
    )

    codes = {item["code"] for item in inspect_generation_naturalness(text)["flags"]}
    assert "scene_repeated_action_loop" not in codes


def test_generation_protocol_uses_strict_baseline_and_selected_route():
    protocol = render_generation_style_protocol("object_consequence")

    assert "generation-style-protocol-v4" in protocol
    assert "物件与后果推进" in protocol
    assert "非对白不使用类比" in protocol
    assert "仿佛" not in protocol
    assert "平、快、干" in protocol
    assert "拿错、找不到、被打断" in protocol
    assert "不复制词句" in protocol
    assert "门栓先动了一下" in protocol
    assert "同一身体状态" in protocol
    assert "最多连续两段没有新事件" in protocol


def test_generation_style_repairs_change_the_prose_path():
    assert select_generation_style_path(1, 1, 0) == "event_action_dialogue"
    assert select_generation_style_path(1, 1, 1) == "event_action_dialogue"
    assert select_generation_style_path(1, 1, 2) == "plain_factual"


def test_generation_naturalness_flags_one_non_dialogue_simile_at_scene_scale():
    text = "门把手先动了一下。苏长庚没有推门，先看向脚边的水痕。" + "他没有回答。" * 80

    report = inspect_generation_naturalness(text.replace("水痕", "像水一样的痕迹"))
    assert not any(item["code"] == "scene_metaphor_density" for item in report["flags"])

    chained = text.replace("水痕", "像水一样的痕迹") + (
        "地面像被细线划开。"
        "墙角的灰像被手指拨过。"
        "灯影像一块布压在门上。"
        "门后的声音像贴着砖缝走。"
        "风像从门底钻进来。"
        "水痕像有人刚刚拖过一把湿伞。"
        "鞋面像刚浸过冷水。"
        "门板像被重物顶住。"
        "灰线像被指甲刮过。"
        "灯芯像快要断掉。"
    )
    chained_report = inspect_generation_naturalness(chained)
    assert any(item["code"] == "scene_metaphor_density" for item in chained_report["flags"])


def test_generation_naturalness_does_not_block_six_short_comparisons_in_a_normal_scene():
    comparisons = "".join([
        "地面像被水擦过。",
        "墙角像落了一层灰。",
        "灯影像贴在门上。",
        "风像从缝里挤进来。",
        "他的声音像压低了一格。",
        "纸边像被火烫过。",
    ])
    text = comparisons + "门锁没有再响。" * 140

    report = inspect_generation_naturalness(text)

    assert report["narrative_chars"] >= 900
    assert not any(item["code"] == "scene_metaphor_density" for item in report["flags"])


def test_generation_naturalness_keeps_mild_metaphor_density_as_a_warning():
    comparisons = "".join([
        "地面像被水擦过。",
        "墙角像落了一层灰。",
        "灯影像贴在门上。",
        "风像从缝里挤进来。",
        "他的声音像压低了一格。",
        "纸边像被火烫过。",
        "鞋面像刚浸过冷水。",
        "门板像被重物顶住。",
        "灰线像被指甲刮过。",
    ])
    report = inspect_generation_naturalness(comparisons + "门锁没有再响。" * 140)

    assert any(item["code"] == "scene_metaphor_density_warning" for item in report["warnings"])
    assert not any(item["code"] == "scene_metaphor_density" for item in report["flags"])
    assert report["passed"] is True


def test_scene_retry_feedback_does_not_replay_failed_prose_examples():
    evidence = GenerationEngine._safe_scene_retry_evidence({
        "code": "scene_metaphor_density",
        "evidence": {
            "count": 8,
            "examples": ["像取不尽似的", "像是被什么从下面顶出来的"],
        },
    })

    assert evidence == {"count": 8, "baseline": "non_dialogue_simile_density"}
    assert "像取不尽似的" not in json.dumps(evidence, ensure_ascii=False)


def test_structured_plain_scene_path_accepts_complete_factual_paragraphs():
    class FakeGateway:
        provider = "deepseek"

        async def generate_json(self, prompt, **kwargs):
            assert "现场写作" in prompt
            return {
                "data": {
                    "paragraphs": [
                        "门栓先动了一下。苏长庚把手收回来，侧身挡住楼梯口，扫帚柄横在门前。",
                        "门后没有回答，门缝下漫出一线水，沿着石缝往外走，沾湿了他的鞋尖。",
                        "他低头看了眼手里的纸包，把纸包交给身后的人，又把灯芯往上拨了一格。",
                        "‘拿稳。’他说完退开半步，楼梯口的灯灭了，门里的水也停在了台阶边。",
                    ]
                },
                "usage": {"tokens_input": 10, "tokens_output": 20, "cost": 0.1, "model": "deepseek-chat"},
            }

    engine = GenerationEngine.__new__(GenerationEngine)
    engine.ai_gateway = FakeGateway()
    result = asyncio.run(engine._generate_structured_plain_scene_candidate(
        chapter_number=1,
        scene_index=1,
        context={"context_layers": {}},
        scene_card={"target_words": 180, "content": "完成一次现场选择"},
        previous_scene_tail="",
        current_state={},
        previous_handoffs=[],
        min_scene_chars=120,
        max_scene_chars=500,
    ))

    assert result["accepted"] is True
    assert result["text"].count("\n\n") == 3


def test_readability_plan_is_deterministic_and_changes_delivery_texture():
    first = build_readability_plan(
        1,
        chapter_type="normal",
        plot_brief={
            "reader_promise": "看主角在公开场合反击",
            "emotional_target": "压迫 -> 爆发 -> 追杀",
            "hook": "对手拿出主角不该知道的证据",
        },
        opening_plan={"mode": "action", "label": "动作/选择开场"},
    )
    second = build_readability_plan(
        2,
        chapter_type="relationship",
        plot_brief={"reader_promise": "看两人的关系彻底撕开"},
        opening_plan={"mode": "dialogue", "label": "对白冲突开场"},
    )

    assert first["information_delivery"]["mode"] == "action_consequence"
    assert second["information_delivery"]["mode"] == "dialogue_subtext"
    rendered = render_readability_plan(first)
    assert "生成前可读性预案" in rendered
    assert "事件先发生" in rendered
    assert "避免把每个人的反应都写成整齐的震惊" in rendered


def test_opening_gate_blocks_recent_mode_reuse_but_allows_explicit_body_contract():
    repeated = inspect_opening(
        "他抬手按住门把，门内立刻传来第二声敲击。",
        requested_mode="action",
        chapter_number=4,
        recent_modes=["action", "dialogue", "action"],
    )
    assert repeated["passed"] is False
    assert any(item["code"] == "opening_mode_repetition" for item in repeated["flags"])

    explicit_body = inspect_opening(
        "胸口的刺痛逼得他弯下腰，血顺着衣襟滴到地上。",
        requested_mode="body_sensation",
        chapter_number=4,
    )
    assert explicit_body["passed"] is True


def test_legacy_prompt_templates_have_a_safe_global_opening_fallback():
    seeds = {name: template for name, _version, _model, template in PROMPT_SEEDS}
    for name in (
        "bootstrap.gen_chapter1",
        "narrative.gen_next_chapter",
        "bootstrap.write_chapter_draft",
    ):
        rendered = render_prompt(seeds[name], {})
        assert "$opening_contract" not in rendered
        assert "开场多样性硬约束" in rendered
        assert "身体部位+疼痛" in rendered


def test_opening_gate_is_a_blocking_quality_repair_category():
    contract = build_quality_repair_contract({
        "overall_score": 95,
        "dimensions": {"opening_quality": 95},
        "issues": [{
            "dimension": "opening_quality",
            "severity": "high",
            "description": "开场类型门禁未通过",
        }],
    })
    assert contract["passed"] is False
    assert "opening_quality" in contract["blocking_categories"]


def test_structural_ai_smell_triggers_semantic_rewrite_instead_of_remaining_advisory():
    text = "\n\n".join(
        [
            "沈夜看着门口的灯，缓缓地走向前方，心里明白事情不会如此简单。" * 2
            for _ in range(20)
        ]
    )
    metrics = analyze_deai_patterns(text, profile={"platform": "fanqie"})
    assert any(flag["code"] == "structural_ai_smell" for flag in metrics["flags"])
    assert "structural_ai_smell" in {
        flag["code"] for flag in metrics["flags"]
    }


def test_deterministic_opening_repair_preserves_paragraphs_and_reaches_target():
    paragraphs = [
        f"陆沉在第{i}次试剑时记住了一个细节，剑锋擦过石面，留下了一道新痕。"
        for i in range(18)
    ] + [
        "周长老站在廊下，没有打断这场练习。",
        "晨雾散开时，远处的钟声敲了三下。",
    ]
    source = "\n\n".join(paragraphs)

    repaired, evidence = DeAIPipeline._deterministic_paragraph_opening_repair(
        source,
        "陆沉",
    )

    assert evidence["applied"] is True
    assert evidence["replaced_count"] == 13
    assert evidence["after_ratio"] < 0.30
    assert len(repaired.split("\n\n")) == len(paragraphs)
    assert repaired.count("陆沉") < source.count("陆沉")


def test_deai_opening_provider_failure_keeps_fallback_but_blocks_quality_gate():
    paragraphs = [
        f"陆沉在第{i}次试剑时记住了一个细节，剑锋擦过石面，留下了一道新痕。"
        for i in range(18)
    ] + [
        "周长老站在廊下，没有打断这场练习。",
        "晨雾散开时，远处的钟声敲了三下。",
    ]
    source = "\n\n".join(paragraphs)

    class Gateway:
        def __init__(self):
            self.calls = 0

        async def generate_json(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "data": {
                        "humanized_text": source,
                        "changes": ["保留正文事实"],
                        "ai_patterns_removed": [],
                    },
                    "usage": {"tokens_input": 1, "tokens_output": 1, "cost": 0.0},
                }
            raise AIGatewayError("opening repair provider returned empty output")

    result = asyncio.run(DeAIPipeline(Gateway()).process(source))

    assert result["quality_gate"]["passed"] is False
    assert result["quality_gate"]["mode"] == "deterministic_fallback"
    assert result["quality_gate"]["code"] == "opening_repair_provider_failed"
    assert result["quality_gate"]["after_ratio"] < 0.30
    assert any(
        layer["layer"] == "deterministic_paragraph_opening_repair"
        and layer["applied"] is True
        for layer in result["layers_applied"]
    )


def test_scene_plan_contract_rejects_empty_or_incomplete_provider_plan():
    with pytest.raises(AIGatewayError, match="beats must contain 4-6"):
        SceneDirector.validate_scene_plan_contract(
            {"chapter_title": "门后是什么", "chapter_type": "suspense", "beats": []},
            target_word_count=3000,
        )


def test_scene_plan_rejects_a_slow_middle_interlude_for_long_chapters():
    phases = ["pressure", "build", "burst", "feedback", "aftershock"]
    plan = {
        "chapter_title": "门后的声音",
        "chapter_type": "normal",
        "pacing": "开场迅速进入异常，中段通过求助放缓节奏，章末再拉高",
        "beats": [
            {
                "name": f"beat-{i}",
                "content": "推进主线并留下结果",
                "target_words": 600,
                "payoff_phase": phase,
                "scene_card": _complete_scene_card(i),
            }
            for i, phase in enumerate(phases)
        ],
    }

    with pytest.raises(AIGatewayError, match="pacing_interlude_without_mainline_turn"):
        SceneDirector.validate_scene_plan_contract(plan, target_word_count=3000)


def test_scene_plan_adds_executable_texture_when_provider_omits_optional_style_fields():
    plan = SceneDirector._ensure_prose_texture_plan(
        {"chapter_title": "门后的声音"},
        chapter_number=1,
    )

    texture = plan["prose_texture_plan"]
    assert texture["source"] == "deterministic_texture_scheduler"
    assert texture["narrator_bias"]
    assert texture["sensory_anchor"]
    assert texture["subtext"]
    assert texture["rhythm"]


def test_scene_plan_preserves_provider_texture_fields():
    plan = SceneDirector._ensure_prose_texture_plan(
        {
            "prose_texture_plan": {
                "narrator_bias": "只跟着角色听见的脚步声走",
                "sensory_anchor": "磨旧的铜扣",
                "subtext": "嘴上答应，手却压住门",
                "rhythm": "短句打断长句",
                "voice_anchor": "先问价再答应",
                "information_delivery": "对白和物件",
            }
        },
        chapter_number=2,
    )

    assert plan["prose_texture_plan"]["source"] == "provider"
    assert plan["prose_texture_plan"]["sensory_anchor"] == "磨旧的铜扣"


def test_long_scene_plan_contract_rejects_empty_scene_cards():
    phases = ["pressure", "build", "burst", "feedback", "aftershock"]
    plan = {
        "chapter_title": "门后是什么",
        "chapter_type": "suspense",
        "beats": [
            {
                "name": f"beat-{i}",
                "content": "继续推进",
                "target_words": 600,
                "payoff_phase": phase,
                "scene_card": {},
            }
            for i, phase in enumerate(phases)
        ],
    }

    with pytest.raises(AIGatewayError, match="scene_card_(missing|fields_missing)"):
        SceneDirector.validate_scene_plan_contract(plan, target_word_count=3000)


def test_scene_plan_repairs_semantically_incomplete_provider_plan():
    class Gateway:
        def __init__(self):
            self.calls = 0

        async def generate_json(self, _prompt, **_kwargs):
            self.calls += 1
            usage = {"tokens_input": 10, "tokens_output": 5, "cost": 0.01, "model": "test"}
            phases = ["pressure", "build", "burst", "feedback", "aftershock"]
            if self.calls == 1:
                return {
                    "data": {
                        "chapter_title": "旧门",
                        "chapter_type": "normal",
                        "beats": [
                            {"name": f"beat-{i}", "content": "继续推进", "target_words": 600, "payoff_phase": phase, "scene_card": _complete_scene_card(i)}
                            for i, phase in enumerate(phases[:-1])
                        ],
                    },
                    "usage": usage,
                }
            return {
                "data": {
                    "chapter_title": "旧门后的答案",
                    "chapter_type": "normal",
                    "beats": [
                        {"name": f"beat-{i}", "content": "继续推进", "target_words": 600, "payoff_phase": phase, "scene_card": _complete_scene_card(i)}
                        for i, phase in enumerate(phases)
                    ],
                },
                "usage": usage,
            }

    gateway = Gateway()
    result = asyncio.run(SceneDirector(None, gateway).plan_scene(
        1,
        {"rendered_context": ""},
        target_word_count=3000,
        quality_profile={},
    ))

    assert gateway.calls == 1
    assert result["chapter_title"] == "旧门"
    assert result["_usage"]["tokens_output"] == 5


def test_generation_phase_repair_only_adds_provable_aftershock_label():
    plan = {
        "chapter_title": "门后的声音",
        "chapter_type": "suspense",
        "hook": "楼梯口的人亮出钥匙",
        "payoff_contract": {"next_pressure": "钥匙对应的门在身后打开"},
        "beats": [
            {"name": "压迫", "content": "确认门内有人", "payoff_phase": "pressure", "target_words": 300},
            {"name": "试探", "content": "试探锁孔", "payoff_phase": "build", "target_words": 300},
            {"name": "反击", "content": "门板撞开", "payoff_phase": "burst", "target_words": 300},
            {"name": "反馈", "content": "楼道灯亮起", "payoff_phase": "feedback", "target_words": 300},
        ],
    }

    repaired = SceneDirector._repair_generation_phase_labels(plan)
    assert repaired is not None
    assert repaired["beats"][-1]["payoff_phases"] == ["feedback", "aftershock"]
    assert SceneDirector.validate_scene_plan_contract(
        repaired,
        target_word_count=1200,
    )["passed"] is True

    no_anchor = {**plan, "hook": "", "payoff_contract": {}}
    assert SceneDirector._repair_generation_phase_labels(no_anchor) is None


def test_incomplete_plot_brief_falls_through_to_repair_capable_scene_planner():
    class Gateway:
        def __init__(self):
            self.calls = 0

        async def generate_json(self, _prompt, **_kwargs):
            self.calls += 1
            phases = ["pressure", "build", "burst", "feedback", "aftershock"]
            return {
                "data": {
                    "chapter_title": "补上的余波",
                    "chapter_type": "normal",
                    "beats": [
                        {"name": f"beat-{i}", "content": "继续推进", "target_words": 600, "payoff_phase": phase, "scene_card": _complete_scene_card(i)}
                        for i, phase in enumerate(phases)
                    ],
                },
                "usage": {"tokens_input": 10, "tokens_output": 5, "cost": 0.01, "model": "test"},
            }

    gateway = Gateway()
    incomplete_brief = {
        "chapter_title_hint": "旧门",
        "suggested_beats": [
            {"name": "压境", "content": "有人逼近", "target_words": 600, "payoff_phase": "pressure"},
            {"name": "试探", "content": "主角试探", "target_words": 600, "payoff_phase": "build"},
            {"name": "反击", "content": "主角反击", "target_words": 600, "payoff_phase": "burst"},
            {"name": "反馈", "content": "对手退让", "target_words": 600, "payoff_phase": "feedback"},
        ],
    }

    result = asyncio.run(SceneDirector(None, gateway).plan_scene(
        1,
        {"rendered_context": ""},
        target_word_count=3000,
        plot_brief=incomplete_brief,
        quality_profile={},
    ))

    assert gateway.calls == 1
    assert result["chapter_title"] == "补上的余波"


def test_generation_payoff_floor_is_repaired_before_prose():
    phases = ["pressure", "build", "burst", "feedback", "aftershock"]
    beats = [
        {
            "name": f"beat-{index}",
            "content": "顾沉依据现场压力作出选择并留下具体结果",
            "target_words": 600,
            "payoff_phase": phase,
            "scene_card": _complete_scene_card(index),
        }
        for index, phase in enumerate(phases)
    ]
    weak_contract = {
        "reader_promise": "顾沉拿到第一次签到机会",
        "pressure": "顾沉濒死且周衡仍在监视",
        "active_choice": "顾沉接受绑定并前往后山",
        "payoff_type": "system_reward",
        "visible_result": "系统面板出现，伤势暂时稳定",
        "payoff_feedback": "周衡派人盯梢，弟子停止嘲笑",
        "payoff_intensity": "high",
        "payoff_arc": phases,
        "cost": "丹田无法恢复",
        "next_pressure": "顾沉必须在倒计时结束前完成签到",
    }
    strong_contract = {**weak_contract, "payoff_intensity": "peak"}

    class Gateway:
        def __init__(self):
            self.calls = []

        async def generate_json(self, prompt, **_kwargs):
            self.calls.append(prompt)
            if len(self.calls) == 1:
                return {
                    "data": {
                        "chapter_title": "废人也能签到",
                        "chapter_type": "suspense",
                        "scene_goal": "顾沉必须活着抵达后山",
                        "conflict": "周衡的监视与濒死状态",
                        "hook": "签到点出现新的规则",
                        "payoff_contract": weak_contract,
                        "beats": beats,
                    },
                    "usage": {"tokens_input": 10, "tokens_output": 5, "cost": 0.01, "model": "test"},
                }
            return {
                "data": {
                    "repairable": True,
                    "payoff_contract": strong_contract,
                    "beats": [
                        {**beat, "content": "顾沉主动承受代价，完成选择并迫使现场状态发生变化"}
                        for beat in beats
                    ],
                },
                "usage": {"tokens_input": 10, "tokens_output": 5, "cost": 0.01, "model": "test"},
            }

    gateway = Gateway()
    result = asyncio.run(SceneDirector(None, gateway).plan_scene(
        1,
        {"rendered_context": ""},
        target_word_count=3000,
        quality_profile=select_quality_profile(
            platform="番茄",
            genre="玄幻",
            subgenre="xuanhuan_system",
        ),
    ))

    assert len(gateway.calls) == 2
    assert result["payoff_contract"]["payoff_intensity"] == "peak"
    assert result["generation_payoff_repair"]["source"] == "real_provider_pre_generation_repair"
    assert "不是把 payoff_intensity 字段从 high 改成 peak" in gateway.calls[1]


def test_scene_plan_uses_bounded_structural_repair_after_provider_repeats_missing_feedback():
    class Gateway:
        def __init__(self):
            self.calls = 0

        async def generate_json(self, _prompt, **_kwargs):
            self.calls += 1
            phases = ["pressure", "build", "burst", "aftershock"]
            return {
                "data": {
                    "chapter_title": "缺了一拍",
                    "chapter_type": "normal",
                    "hook": "门外有人敲响第三下",
                    "beats": [
                        {
                            "name": f"beat-{i}",
                            "content": "继续推进并留下具体后果",
                            "target_words": 500,
                            "payoff_phase": phase,
                            "scene_card": _complete_scene_card(i),
                        }
                        for i, phase in enumerate(phases)
                    ],
                },
                "usage": {"tokens_input": 1, "tokens_output": 1, "cost": 0.0, "model": "test"},
            }

    gateway = Gateway()
    result = asyncio.run(SceneDirector(None, gateway).plan_scene(
        1,
        {"rendered_context": ""},
        target_word_count=3000,
        quality_profile={},
    ))

    assert gateway.calls == 2
    assert result["generation_phase_repair"]["applied"] == ["feedback"]
    assert any(
        "feedback" in (beat.get("payoff_phases") or [])
        for beat in result["beats"]
    )


def test_generation_prompt_carries_reader_promise_and_cross_chapter_hooks():
    prompt = GenerationEngine._build_generation_prompt(
        None,
        12,
        {"rendered_context": "上一章尾部：门内又响了三下。"},
        {
            "chapter_title": "门后的声音",
            "scene_goal": "确认门后的人是谁",
            "conflict": "开门会暴露位置",
            "pacing": "medium",
            "reader_promise": "揭开门后异常的一层真相",
            "emotional_target": "警惕 -> 逼近 -> 惊疑",
            "opening_anchor": "手仍按在门缝上",
            "hook": "门后传来主角自己的声音",
            "beats": [],
        },
        "保持现实背景",
        3000,
    )

    assert "揭开门后异常的一层真相" in prompt
    assert "手仍按在门缝上" in prompt
    assert "每约 800-1200 字" in prompt
    assert "读者推荐预算为 1800-2700 字" in prompt
    assert "本次生成硬范围为 1944-3000 字" in prompt
    assert "章末必须把钩子落实" in prompt
    assert "压制→蓄力→爆发→反馈→余波" in prompt
    assert "反馈必须落到对手、组织、资源、规则或旁观者的可见变化" in prompt


def test_generation_prompt_carries_readability_contract_before_writing():
    plan = build_readability_plan(
        12,
        chapter_type="normal",
        plot_brief={"reader_promise": "看主角当场翻盘"},
        opening_plan={"mode": "object", "label": "物件异常开场"},
    )
    prompt = GenerationEngine._build_generation_prompt(
        None,
        12,
        {"rendered_context": "", "context_layers": {"readability_plan": plan}},
        {
            "chapter_title": "门后是什么",
            "reader_promise": "看主角当场翻盘",
            "beats": [],
            "readability_plan": plan,
        },
        None,
        3000,
    )

    assert "【生成前可读性预案：先执行，再写正文】" in prompt
    assert "用动作和立刻发生的后果交付信息" in prompt
    assert "不是事后润色" not in prompt


def test_plot_brief_preserves_payoff_phase_labels_for_the_writer():
    plan = SceneDirector._adopt_plot_brief(
        1,
        3000,
        {
            "chapter_title_hint": "旧印初鸣",
            "suggested_beats": [
                {
                    "name": "压境",
                    "content": "敌人封住退路",
                    "target_words": 600,
                    "payoff_phase": "pressure",
                },
                {
                    "name": "试印",
                    "content": "主角试探旧印并承担代价",
                    "target_words": 600,
                    "payoff_phases": ["build"],
                },
                {
                    "name": "反击",
                    "content": "主角用已知规则反击",
                    "target_words": 600,
                    "payoff_phase": "burst",
                },
                {
                    "name": "余波",
                    "content": "对手退让，新的追兵出现",
                    "target_words": 600,
                    "payoff_phases": ["feedback", "aftershock"],
                },
            ],
        },
    )

    assert plan is not None
    assert plan["beats"][0]["payoff_phase"] == "pressure"
    assert plan["beats"][1]["payoff_phases"] == ["build"]
    assert plan["beats"][3]["payoff_phases"] == ["feedback", "aftershock"]


def test_continuation_prompt_does_not_reset_chapter_context():
    prompt = GenerationEngine._build_continuation_prompt(
        "门内又响了三下。",
        {"hook": "门后传来主角自己的声音", "beats": []},
        1200,
    )

    assert "不要重新开场" in prompt
    assert "时间线、地点、人物状态和情绪" in prompt


def test_deai_skips_provider_for_rule_clean_text():
    result = asyncio.run(DeAIPipeline(None).process("他推开门。屋里没人。"))

    assert result["semantic_humanize"] is False
    assert result["quality_gate"] == {"passed": True, "mode": "deterministic_only"}
    assert result["processed_text"] == "他推开门。屋里没人。"


def test_deai_blocks_duplicate_paragraphs_without_semantic_rewrite():
    paragraph = (
        "沈夜推开门，看见院里的灯还亮着，便停在门口没有进去。屋里没有人回应，"
        "只有桌上的茶还冒着热气，像是有人刚刚离开。"
    )
    result = asyncio.run(DeAIPipeline(None).process(f"{paragraph}\n\n{paragraph}"))

    assert result["quality_gate"]["passed"] is False
    assert result["quality_gate"]["code"] == "duplicate_paragraph"
    assert result["semantic_humanize"] is False


def test_deai_provider_invalid_json_becomes_auditable_quality_failure():
    text = "\n\n".join(
        ["顾沉低头看了一眼门缝，手指没有离开锁扣。" for _ in range(8)]
        + [
            "林岚把灯光压低，示意他先别出声。",
            "陈姨站在门外，迟迟没有敲门。",
            "赵启明收起文件，转身走向电梯。",
            "雨水沿着窗框往下淌，屋里没人说话。",
        ]
    )

    class BrokenGateway:
        async def generate_json(self, *_args, **_kwargs):
            raise AIGatewayError("invalid provider JSON")

    result = asyncio.run(DeAIPipeline(BrokenGateway()).process(text))

    assert result["processed_text"]
    assert result["semantic_humanize"] is False
    assert result["quality_gate"]["passed"] is False
    assert result["quality_gate"]["code"] == "rewrite_candidate_rejected"
    assert any(
        flag["code"] == "rewrite_candidate_rejected"
        for flag in result["metrics"]["after"]["flags"]
    )


def test_deai_low_risk_repeated_phrase_keeps_auditable_rule_only_path():
    text = "\n\n".join(
        [
            "风从窗缝里进来，吹动桌上的纸。",
            "风从窗缝里进来，带着一点潮气。",
            "风从窗缝里进来，灯影跟着晃了晃。",
            "林岚收起钥匙，没有立刻说话。",
            "陈姨站在门边，望着楼道尽头。",
            "赵启明把文件压在掌下，等着回答。",
            "雨水沿着玻璃往下淌，屋里没人出声。",
        ]
    )

    class GatewayThatMustNotBeCalled:
        def __init__(self):
            self.calls = 0

        async def generate_json(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("low-risk repeated phrase must not call Provider")

    gateway = GatewayThatMustNotBeCalled()
    result = asyncio.run(DeAIPipeline(gateway).process(text))

    assert result["processed_text"]
    assert result["semantic_humanize"] is False
    assert result["quality_gate"]["passed"] is True
    assert result["quality_gate"]["mode"] == "deterministic_only"
    assert result["metrics"]["semantic_trigger_flags"] == []
    assert result["metrics"]["after"]["flags"] == [
        {
            "code": "repeated_phrase",
            "severity": "low",
            "message": "存在跨句重复短语，需要确认是否为刻意回环",
        }
    ]
    assert gateway.calls == 0


def test_deai_forced_local_repair_calls_provider_without_detector_flag():
    text = "沈砚推开门，风从窗缝里进来，桌上的纸被吹得轻轻发响。" * 3

    class Gateway:
        def __init__(self):
            self.calls = 0

        async def generate_json(self, *_args, **_kwargs):
            self.calls += 1
            return {
                "data": {
                    "humanized_text": text,
                    "changes": ["保留原文事实并修复表达层门禁"],
                    "ai_patterns_removed": [],
                },
                "usage": {"tokens_input": 1, "tokens_output": 1, "cost": 0.0},
            }

    gateway = Gateway()
    result = asyncio.run(DeAIPipeline(gateway).process(
        text,
        force_semantic_rewrite=True,
    ))

    assert result["semantic_humanize"] is True
    assert result["metrics"]["semantic_trigger_flags"] == ["forced_local_repair"]
    assert gateway.calls == 1


def test_generation_uses_serial_scene_handoffs_and_skips_full_chapter_rewrite():
    scene_texts = [
        "沈夜按住门锁，门内的水声忽然停了。他没有推门，先把耳朵贴上去，听见里面有人拖动椅脚。林薇抬手拦住他，指了指门缝下那道刚刚亮起的红线。那道光贴着地面一闪一闪，像在等他们先犯错。楼道里的声控灯灭了，黑暗把两人的影子压在门上。林薇没有说话，只把短棍横在身前，目光停在那道红线上。",
        "红线沿着地砖爬到墙角，像有人在里面重新接通了电。沈夜退半步，取出旧钥匙试探锁孔，钥匙没有转动，门后却传来一声低低的笑。林薇压低声音问他要不要离开，他却盯住了锁眼里的微光。锁芯里有细小的齿轮响了一下，像在回应他的犹豫。楼上传来水管敲击声，门内的笑声随即停住。",
        "他把钥匙收回掌心，改用鞋尖压住门槛。门板猛地向外撞来，林薇侧身避开，短棍砸在门框上，震落了一层灰。沈夜趁那一瞬看清了屋里的黑布，布角沾着水，水珠正逆着地面往回流。门缝里的红线随之抬高，贴到了他的鞋面。鞋底传来一阵冰凉，他立刻把脚收了回来。楼道里有人咳了一声。",
        "黑布后面摆着一台亮着绿灯的旧收音机。沈夜没有进屋，只伸手切断电源，收音机却换成了他的声音。门内再次敲响三下，楼道尽头也亮起了灯。林薇回头时，楼梯口已经站着一个看不清脸的人。那人没有上前，只把手里的钥匙朝他们晃了晃。钥匙上的铜牌撞出脆响，像给这场僵持定了一个期限。",
    ]

    class Step:
        def set_output(self, *_args, **_kwargs):
            pass

    class Tracer:
        @asynccontextmanager
        async def trace_step(self, *_args, **_kwargs):
            yield Step()

    class ContextAssembler:
        async def assemble_context(self, *_args, **_kwargs):
            return {
                "rendered_context": "",
                "rendered_chars": 0,
                "truncated": False,
                "previous_chapters": 0,
                "context_layers": {},
            }

    class SceneDirector:
        async def plan_scene(self, *_args, **_kwargs):
            return {
                "chapter_title": "门后的声音",
                "hook": "门内再次敲响",
                "chapter_type": "suspense",
                "beats": [
                    {"name": "试探", "content": "沈夜确认门内异常", "target_words": 180, "payoff_phase": "pressure"},
                    {"name": "找规则", "content": "两人试探红线和锁", "target_words": 180, "payoff_phase": "build"},
                    {"name": "撞门", "content": "门后反击并露出黑布", "target_words": 180, "payoff_phase": "burst"},
                    {"name": "回声", "content": "收音机与楼道灯形成新压迫", "target_words": 180, "payoff_phases": ["feedback", "aftershock"]},
                ],
                "_usage": {},
            }

    class Gateway:
        def __init__(self):
            self.calls = []
            self.call_kwargs = []
            self.json_calls = []

        async def generate(self, prompt, **_kwargs):
            self.calls.append(prompt)
            self.call_kwargs.append(_kwargs)
            text = scene_texts[len(self.calls) - 1]
            return {
                "text": text,
                "tokens_input": 0,
                "tokens_output": 0,
                "cost": 0.0,
                "model": "test",
            }

        async def generate_json(self, prompt, **_kwargs):
            self.json_calls.append((prompt, _kwargs))
            return {
                "data": {
                    "time": "夜里",
                    "location": "旧楼三层门口",
                    "known_facts": [f"第{len(self.json_calls)}场已确认有异常"],
                    "state_changes": [f"第{len(self.json_calls)}场留下新压力"],
                    "open_threads": ["收音机的声音来源未明"],
                    "continuity_warnings": ["下一场必须承接门内敲击"],
                    "next_bridge": "门内再次敲响三下",
                },
                "usage": {"tokens_input": 1, "tokens_output": 1, "cost": 0.0, "model": "test"},
            }

    class Deai:
        def __init__(self):
            self.calls = []

        async def process(self, text, **_kwargs):
            self.calls.append(_kwargs)
            return {
                "processed_text": text,
                "layers_applied": [],
                "total_changes": 0,
                "semantic_humanize": False,
                "humanize_changes": [],
                "ai_patterns_removed": [],
                "metrics": {},
                "quality_gate": {"passed": True},
                "usage": {},
            }

    class EventBus:
        async def publish(self, *_args, **_kwargs):
            pass

    engine = GenerationEngine.__new__(GenerationEngine)
    engine.tracer = Tracer()
    engine.context_assembler = ContextAssembler()
    engine.scene_director = SceneDirector()
    engine.ai_gateway = Gateway()
    engine.deai_pipeline = Deai()
    engine.event_bus = EventBus()

    result = asyncio.run(engine.generate_chapter(1, target_word_count=600))

    assert result["text"] == "\n\n".join(scene_texts)
    assert len(engine.ai_gateway.calls) == 4
    assert len(engine.ai_gateway.json_calls) == 4
    assert all("上一场末尾原文" in prompt for prompt in engine.ai_gateway.calls)
    assert result["generation_quality"]["generation_mode"] == "scene_serial"
    assert result["scene_serial"]["generation_mode"] == "scene_serial"
    assert result["generation_quality"]["scene_serial"]["handoff_count"] == 4
    assert engine.deai_pipeline.calls == []
    # Scene targets are pacing hints.  The Provider receives completion
    # headroom rather than a token ceiling derived from the old hard scene
    # budget.
    assert engine.ai_gateway.call_kwargs[0]["max_tokens"] > 245


def test_scene_serial_moves_opening_pacing_constraints_into_generation_contract():
    cards = GenerationEngine._normalise_scene_cards(
        {
            "beats": [
                {"name": "扫地日常", "target_words": 800, "content": "先写日常"},
                {"name": "异常", "target_words": 500, "content": "门后出现异常"},
            ]
        },
        target_word_count=3000,
    )

    assert cards[0]["target_words"] == 420
    assert "前120字内" in cards[0]["opening_constraint"]
    assert "前420字内" in cards[0]["opening_constraint"]
    assert "前两句" in cards[0]["opening_constraint"]
    assert "前120字" in cards[0]["opening_constraint"]
    assert "前两句" in cards[1]["opening_constraint"]
    assert sum(card["target_share"] for card in cards) == 1.0

    engine = GenerationEngine.__new__(GenerationEngine)
    prompt = engine._build_scene_generation_prompt(
        chapter_number=1,
        context={"context_layers": {}, "rendered_context": ""},
        scene_plan={
            "chapter_title": "藏经阁的门",
            "opening_plan": {
                "mode": "object",
                "label": "物件异常开场",
                "directive": "从具体物件的异常起笔。",
                "forbidden_recent_modes": ["action"],
            },
            "chapter_contract": {"cost": "封印磨损加速"},
            "payoff_contract": {"visible_result": "门上字迹", "cost": "封印磨损加速"},
            "causal_ledger": [{"event": "指点弟子", "cost": "封印磨损加速"}],
        },
        scene_card=cards[0],
        scene_index=1,
        scene_count=2,
        previous_scene_tail="",
        current_state={},
        previous_handoffs=[],
    )

    assert "前120字内必须出现" in prompt
    assert "本章指定开场类型：物件异常开场（object）" in prompt
    assert "从具体物件的异常起笔" in prompt
    assert "触发动作→当场可见/可感知反馈→人物确认这就是代价或规则后果" in prompt
    assert "不能连续用日常拖慢开局" in prompt
    assert "前420字内必须让人物感知一个具体威胁" in prompt
    assert "碑文、账册、书信、纸条等直接文字" in prompt
    assert "同一个两字人名不能连续占据多个段首" in prompt
    assert "破折号只在对白中确有停顿、打断或转折时使用" in prompt
    assert "人物重返已经出现过的地点、门、物件或线索时" in prompt
    assert "跨场景桥接硬要求" in prompt
    assert "不得把‘决定去某地’当作已经到达" in prompt
    assert "不要把每个段落都写成‘现象→判断→解释→总结’的完整闭环" in prompt
    assert "本场 prose_texture_plan 指定的限知叙述偏向" in prompt
    assert "本章叙述质地与人物声音（生成前执行）" in prompt
    assert "自然段首编排（硬结构，生成期执行，不要输出清单）" in prompt
    assert "每连续 8 段中同一个两字姓名最多只能作为 2 段的首词" in prompt
    assert "重大袭击、对抗或爆发结束后" in prompt
    assert "本章前半推进硬要求" in prompt
    assert "不得把本场写成独立的教学、闲聊、帮忙或日常缓冲" in prompt
    assert "前两段内必须落下本场第一次阻碍" in prompt
    assert "最迟不超过前240字" in prompt
    assert "关键异常、开门、封印松动、袭击、修炼变化或新能力必须先写可见前提/征兆" in prompt
    assert "碑文、幻象、梦境或他人话语里的数字/年代属于原说话者" in prompt
    assert "本场建议约写" in prompt
    assert "参考节拍范围" in prompt
    assert "本次生成期本场可用章节剩余额度上限" in prompt
    assert "这是章节预算的生成期硬边界" in prompt

    closing_prompt = engine._build_scene_generation_prompt(
        chapter_number=1,
        context={"context_layers": {}, "rendered_context": ""},
        scene_plan={
            "chapter_title": "门后的声音",
            "payoff_contract": {
                "visible_result": "门开启一线",
                "cost": "封印磨损加速，影煞势力感知",
                "next_pressure": "影煞势力开始行动",
            },
        },
        scene_card={"target_words": 500, "content": "完成章末结果"},
        scene_index=2,
        scene_count=2,
        previous_scene_tail="门缝里的光亮了起来。",
        current_state={},
        previous_handoffs=[],
    )

    assert "章末兑现硬约束" in closing_prompt
    assert "封印磨损加速，影煞势力感知" in closing_prompt
    assert "必须给出可被人物或读者观察到的信号" in closing_prompt


def test_scene_prompt_uses_effective_budget_when_reader_budget_is_smaller_than_plan():
    engine = GenerationEngine.__new__(GenerationEngine)

    prompt = engine._build_scene_generation_prompt(
        chapter_number=1,
        context={"context_layers": {}, "rendered_context": ""},
        scene_plan={"chapter_title": "预算收束"},
        scene_card={"target_words": 1200, "content": "完成一次现场推进"},
        scene_index=2,
        scene_count=3,
        previous_scene_tail="门缝里的光忽然熄灭。",
        current_state={},
        previous_handoffs=[],
        max_scene_chars=960,
    )

    assert "本场建议约写 960 字" in prompt
    assert "本次生成期本场可用章节剩余额度上限为 960 字" in prompt
    assert "这是章节预算的生成期硬边界" in prompt
    assert "本场建议约写 1200 字" not in prompt


def test_scene_card_preserves_trigger_and_causal_link_from_plan():
    cards = GenerationEngine._normalise_scene_cards(
        {
            "beats": [
                {
                    "name": "开门",
                    "target_words": 600,
                    "content": "门缝出现冷风",
                    "scene_card": {
                        "trigger": "上一场账册出现新字",
                        "causal_link": "新字让主角判断封印正在松动",
                    },
                }
            ]
        },
        target_word_count=600,
    )

    assert cards[0]["trigger"] == "上一场账册出现新字"
    assert cards[0]["causal_link"] == "新字让主角判断封印正在松动"


def test_scene_serial_retries_repeated_name_opening_across_accepted_scenes():
    accepted = "\n\n".join(
        ["苏长庚抬手压住门缝，听见里面的脚步停了。" for _ in range(8)]
    )
    candidate = "\n\n".join(
        ["苏长庚把手机扣在掌心，示意身后的人别出声。" for _ in range(5)]
        + ["雨水顺着窗框落下，屋里亮起一线冷光。"]
    )

    flags = GenerationEngine._scene_naturalness_flags(
        candidate,
        accepted_text=accepted,
    )

    assert any(flag["code"] == "repeated_paragraph_opening" for flag in flags)


def test_scene_serial_catches_repeated_name_opening_in_a_short_scene():
    candidate = "\n\n".join(
        ["苏长庚抬头看向门缝，手指扣紧了扫帚柄。" for _ in range(4)]
        + [
            "雨声敲在窗纸上，屋里的火光轻轻一晃。",
            "赵小胖把册子压在桌角，没敢再问。",
            "门轴发出一声轻响，楼道里的脚步停了。",
            "灯芯爆开一点火星，照亮了地上的旧印记。",
        ]
    )

    flags = GenerationEngine._scene_naturalness_flags(candidate)

    assert any(flag["code"] == "repeated_paragraph_opening" for flag in flags)


def test_scene_serial_does_not_blame_one_handoff_paragraph_for_chapter_opening_ratio():
    accepted = "\n\n".join(
        ["顾沉把门缝压住，听见里面的脚步停了。" for _ in range(8)]
    )
    candidate = "\n\n".join([
        "顾沉抬手示意身后的人停下。",
        "雨水顺着窗框落进灰尘里。",
        "赵宁把纸条折回袖口，没有解释。",
        "门轴轻轻一响，屋里的灯灭了。",
    ])

    flags = GenerationEngine._scene_naturalness_flags(
        candidate,
        accepted_text=accepted,
    )

    assert not any(flag["code"] == "repeated_paragraph_opening" for flag in flags)


def test_final_scene_requires_an_observable_payoff_cost_anchor():
    missing = GenerationEngine._scene_naturalness_flags(
        "门缝里的光熄灭了，苏长庚把纸条收进袖中。",
        payoff_contract={"cost": "封印磨损加速"},
    )
    assert any(flag["code"] == "scene_payoff_cost_missing" for flag in missing)

    present = GenerationEngine._scene_naturalness_flags(
        "门缝里的光熄灭了，青砖上的封印磨损了一圈。",
        payoff_contract={"cost": "封印磨损加速"},
    )
    assert not any(flag["code"] == "scene_payoff_cost_missing" for flag in present)


def test_scene_serial_does_not_retry_dash_density_that_only_occurs_in_dialogue():
    candidate = "\n\n".join([
        "“你——先别动。”他把手按在门锁上。",
        "门后没有回应，灰尘从门缝里落下来。",
    ])

    flags = GenerationEngine._scene_naturalness_flags(candidate)

    assert not any(flag["code"] == "dash_density" for flag in flags)


def test_scene_serial_can_filter_narrative_dash_density_without_runtime_name_error():
    candidate = "\n\n".join([
        "门外的脚步停了——又向后退去。" * 4,
        "他把纸条收进袖口，转身去找楼梯。",
    ])

    flags = GenerationEngine._scene_naturalness_flags(candidate)

    assert isinstance(flags, list)


def test_prose_feature_card_uses_sample_statistics_without_verbatim_payload():
    card = build_prose_feature_card([
        {"text": "门锁先响了。雨水沿着窗框往下淌。她没有回答，只把纸条折回去。", "label": "positive"},
        {"text": "真正的问题是他已经明白了一切。也就是说，事情只能这样发展。", "label": "negative"},
    ], provider="deepseek", detector="zhuque")

    rendered = render_prose_feature_card(card)
    assert card["schema_version"] == "prose-feature-card-v1"
    assert card["positive_sample_count"] == 1
    assert card["negative_sample_count"] == 1
    assert card["source_hashes"]
    assert "门锁先响了" not in rendered
    assert "真正的问题是" not in rendered
    assert "只学习统计质地" in rendered

    prompt_card = sanitise_style_card_for_prompt({
        "author_card": {
            "sample_prose": "门锁先响了。",
            "voice": "克制",
        }
    })
    assert "sample_prose" not in json.dumps(prompt_card, ensure_ascii=False)
    assert prompt_card["prose_feature_card"]["positive_sample_count"] == 1

    engine = GenerationEngine.__new__(GenerationEngine)
    prompt = engine._build_scene_generation_prompt(
        chapter_number=1,
        context={
            "context_layers": {
                "style_card": {"author_card": {"sample_prose": "门锁先响了。"}},
            },
            "rendered_context": "",
        },
        scene_plan={"chapter_title": "现场", "prose_texture_plan": {}},
        scene_card={"target_words": 300, "content": "完成一次选择"},
        scene_index=1,
        scene_count=1,
        previous_scene_tail="",
        current_state={},
        previous_handoffs=[],
    )
    assert "门锁先响了" not in prompt
    assert "自然叙事特征卡" in prompt


def test_generation_critic_locks_low_risk_paragraphs_and_replaces_only_risk_ranges():
    source = "\n\n".join([
        "顾沉抬手按住门缝，听见里面的脚步停了。",
        "顾沉把手机扣在掌心，示意身后的人别出声。",
        "雨声敲在窗纸上，屋里的火光轻轻一晃。",
    ])
    report = build_generation_critic_report(source)
    assert report["segments"]
    assert 2 in report["locked_paragraph_indexes"]

    repaired = apply_segment_replacements(
        source,
        [{"paragraph_index": 1, "text": "门把手在掌心里转了半圈，顾沉没有立刻松开。"}],
    )
    assert repaired is not None
    assert "顾沉抬手按住门缝" in repaired
    assert "门把手在掌心里转了半圈" in repaired
    assert "雨声敲在窗纸上" in repaired


def test_local_segment_candidates_are_best_of_three_and_reject_regression():
    class Gateway:
        def __init__(self):
            self.calls = 0

        async def generate_json(self, prompt, **_kwargs):
            self.calls += 1
            match = re.search(r"需要修订的局部段落：(\[.*\])\n只输出", prompt, re.DOTALL)
            segments = json.loads(match.group(1))
            if self.calls == 1:
                text_prefix = "顾沉"
            else:
                text_prefix = ("门锁", "雨声", "窗边", "纸条")[self.calls - 2]
            return {
                "data": {
                    "replacements": [
                        {
                            "paragraph_index": item["paragraph_index"],
                            "text": f"{text_prefix}留下了新的现场结果，人物没有急着解释。",
                        }
                        for item in segments
                    ]
                },
                "usage": {"tokens_input": 1, "tokens_output": 1, "cost": 0.01, "model": "test"},
            }

    source = "\n\n".join([
        *[f"顾沉把第{i}件物品压在桌上，屋里没有人回答。" for i in range(8)],
        "雨水顺着窗框落下，灯影在地面上晃了一下。",
        "门轴发出轻响，走廊里的脚步停住了。",
        "赵宁把纸条塞回袖口，没有解释。",
        "火星从灯芯上跳开，照出一小块湿痕。",
        "楼下传来车门合上的声音，随后归于安静。",
        "桌角的灰尘被风卷起，又落回原处。",
    ])
    engine = GenerationEngine.__new__(GenerationEngine)
    engine.ai_gateway = Gateway()
    result = asyncio.run(engine._generate_best_local_segment_candidate(
        chapter_number=1,
        scene_index=1,
        context={"context_layers": {}},
        scene_card={"name": "局部修订", "target_words": 800},
        source_text=source,
        metrics=analyze_deai_patterns(source),
        issues=[{"code": "repeated_paragraph_opening", "severity": "medium"}],
    ))

    assert engine.ai_gateway.calls == 3
    assert result["accepted"] is True
    assert result["reason"] == "best_of_3"
    assert result["locked_paragraph_count"] >= 1
    assert "顾沉把第7件物品" in result["text"]
    assert "门锁留下了新的现场结果" in result["text"] or "雨声留下了新的现场结果" in result["text"]


def test_scene_serial_rescales_provider_plan_to_reader_target_before_writing():
    cards = GenerationEngine._normalise_scene_cards(
        {
            "beats": [
                {"name": f"节拍{i}", "target_words": 900, "content": "推进", "payoff_phase": "pressure"}
                for i in range(4)
            ]
        },
        target_word_count=2700,
    )

    assert len(cards) == 3
    assert sum(card["target_words"] for card in cards) == 2700
    assert all(card["target_share"] > 0 for card in cards)


def test_scene_serial_keeps_a_complete_opening_when_provider_plan_starts_too_small():
    cards = GenerationEngine._normalise_scene_cards(
        {
            "beats": [
                {"name": "开场", "target_words": 300, "content": "异常"},
                {"name": "推进", "target_words": 1200, "content": "推进"},
                {"name": "收束", "target_words": 1200, "content": "钩子"},
            ]
        },
        target_word_count=2700,
    )

    assert cards[0]["target_words"] >= 400
    assert sum(card["target_words"] for card in cards) <= 2700


def test_scene_length_bounds_make_pacing_budget_a_generation_contract():
    minimum, maximum = GenerationEngine._scene_length_bounds(
        {"target_words": 600},
        scene_index=3,
    )
    assert minimum == 270
    assert maximum == int(600 * SCENE_TARGET_MAX_RATIO)
    assert maximum < 600 * 1.35
    assert GenerationEngine._scene_allowed_max_chars(
        {"target_words": 600},
        scene_index=3,
    ) == (
        int(maximum * SCENE_NATURAL_LENGTH_TOLERANCE)
        + SCENE_NATURAL_LENGTH_TOLERANCE_CHARS
    )


def test_future_scene_reserve_is_proportional_and_not_a_fixed_scene_budget():
    cards = [
        {"target_words": 1000},
        {"target_words": 1000},
        {"target_words": 1000},
    ]

    first_reserve = GenerationEngine._future_scene_completion_reserve_chars(
        cards,
        current_scene_number=1,
        accepted_chars=0,
        chapter_max_chars=3000,
    )
    second_reserve = GenerationEngine._future_scene_completion_reserve_chars(
        cards,
        current_scene_number=2,
        accepted_chars=1000,
        chapter_max_chars=3000,
    )
    final_reserve = GenerationEngine._future_scene_completion_reserve_chars(
        cards,
        current_scene_number=3,
        accepted_chars=2000,
        chapter_max_chars=3000,
    )

    assert SCENE_FUTURE_RESERVE_RATIO == 1.0
    assert first_reserve == 1468
    assert second_reserve == 468
    assert final_reserve == 0
    assert first_reserve > sum(
        GenerationEngine._scene_length_bounds(card, scene_index=index)[0]
        for index, card in enumerate(cards[1:], start=2)
    )


def test_scene_structural_smell_requires_a_scene_sized_sample():
    short_scene = "\n\n".join([
        "苏长庚把扫帚靠在墙边，抬头看了眼门缝里的水。",
        "苏长庚没有立刻推门，先把湿掉的纸包塞进袖口。",
        "门后的声音停了一下，台阶上多出一圈水印。",
        "他把灯芯往上拨，楼梯口仍旧没有人影。",
        "苏长庚退开半步，手指按住门闩。",
        "门闩又动了一下，纸包里的硬物碰到了他的手腕。",
    ])

    flags = GenerationEngine._scene_naturalness_flags(short_scene)

    assert not any(item["code"] == "structural_ai_smell" for item in flags)
    assert analyze_deai_patterns(
        short_scene,
        structural_min_chars=800,
        structural_min_paragraphs=8,
    )["structural_ai_smell"] is None


def test_chapter_completion_uses_reader_budget_range_not_nominal_target():
    assert GenerationEngine._chapter_within_reader_budget(
        word_count=2578,
        minimum_chars=1944,
        maximum_chars=3000,
    ) is True
    assert GenerationEngine._chapter_within_reader_budget(
        word_count=1900,
        minimum_chars=1944,
        maximum_chars=3000,
    ) is False
    assert GenerationEngine._chapter_within_reader_budget(
        word_count=3001,
        minimum_chars=1944,
        maximum_chars=3000,
    ) is False


def test_scene_token_budget_uses_provider_margin_and_current_chapter_envelope():
    from types import SimpleNamespace

    engine = GenerationEngine.__new__(GenerationEngine)
    engine.ai_gateway = SimpleNamespace(provider="deepseek")
    card = {"target_words": 600}

    deepseek_limit = engine._scene_generation_max_tokens(
        card,
        scene_index=2,
        max_scene_chars=850,
    )
    engine.ai_gateway.provider = "openai"
    openai_limit = engine._scene_generation_max_tokens(
        card,
        scene_index=2,
        max_scene_chars=850,
    )

    assert deepseek_limit == int(850 * 1.05)
    assert openai_limit == int(850 * 1.10)
    assert deepseek_limit < openai_limit
    assert engine._scene_generation_max_tokens(
        card,
        scene_index=2,
        max_scene_chars=850,
        token_margin=1.35,
    ) == int(850 * 1.35)


def test_scene_truncation_retry_can_grow_past_the_old_fixed_ceiling():
    from types import SimpleNamespace

    engine = GenerationEngine.__new__(GenerationEngine)
    engine.ai_gateway = SimpleNamespace(provider="deepseek")
    card = {"target_words": 1600}

    retry_limit = engine._scene_generation_max_tokens(
        card,
        scene_index=3,
        max_scene_chars=2200,
        token_margin=1.35,
    )

    assert retry_limit == int(2200 * 1.35)
    assert retry_limit > 2400

    large_scene_retry_limit = engine._scene_generation_max_tokens(
        card,
        scene_index=3,
        max_scene_chars=4500,
        token_margin=1.35,
    )
    assert large_scene_retry_limit == SCENE_PROVIDER_TOKEN_CAP


def test_scene_overlong_retry_keeps_completion_headroom():
    from types import SimpleNamespace

    engine = GenerationEngine.__new__(GenerationEngine)
    engine.ai_gateway = SimpleNamespace(provider="deepseek")

    retry_limit = engine._scene_generation_max_tokens(
        {"target_words": 600},
        scene_index=2,
        max_scene_chars=850,
        token_margin=SCENE_DEEPSEEK_OVERLONG_REPAIR_MARGIN,
    )

    assert retry_limit == int(850 * SCENE_DEEPSEEK_OVERLONG_REPAIR_MARGIN)
    assert retry_limit < 850


def test_truncated_scene_never_enters_overlong_envelope_shrink_mode():
    assert GenerationEngine._should_shrink_retry_envelope(
        previous_issue_codes={"scene_provider_truncated"},
        attempt=2,
        compression_mode=False,
    ) is False
    assert GenerationEngine._should_shrink_retry_envelope(
        previous_issue_codes={"scene_reader_budget_overrun"},
        attempt=1,
        compression_mode=False,
    ) is True
    assert GenerationEngine._should_shrink_retry_envelope(
        previous_issue_codes={"scene_chapter_budget_overrun"},
        attempt=2,
        compression_mode=False,
    ) is True
    assert GenerationEngine._should_shrink_retry_envelope(
        previous_issue_codes={"scene_reader_budget_overrun", "scene_provider_truncated"},
        attempt=2,
        compression_mode=False,
    ) is False


def test_style_only_scene_retry_does_not_need_the_previous_candidate_body():
    assert GenerationEngine._is_style_only_retry({"repeated_paragraph_opening"}) is True
    assert GenerationEngine._is_style_only_retry({"dash_density", "ai_phrase"}) is True
    assert GenerationEngine._is_style_only_retry({"scene_overlong"}) is False
    assert GenerationEngine._is_style_only_retry({"repeated_paragraph_opening", "scene_overlong"}) is False


def test_scene_length_soft_overflow_is_bounded_by_reader_pacing_contract():
    assert SCENE_NATURAL_LENGTH_SOFT_OVERFLOW_CHARS == 64


def test_reader_budget_overrun_is_not_a_second_hard_scene_gate():
    # The chapter ceiling and future-scene minimums remain hard constraints;
    # the reader target only guides planning and may be exceeded by one scene.
    assert GenerationEngine._scene_exceeds_chapter_budget(
        accepted_chars=674,
        candidate_chars=2153,
        future_minimum_chars=200,
        chapter_max_chars=3192,
    ) is False


def test_final_scene_variance_never_overrides_reader_chapter_budget():
    assert GenerationEngine._final_scene_budget_variance_allowed(
        projected_chars=3192 + CHAPTER_FINAL_SCENE_NATURAL_VARIANCE_CHARS,
        chapter_max_chars=3192,
        future_minimum_chars=0,
        future_target_chars=0,
    ) is False
    assert GenerationEngine._final_scene_budget_variance_allowed(
        projected_chars=3192 + CHAPTER_FINAL_SCENE_NATURAL_VARIANCE_CHARS + 1,
        chapter_max_chars=3192,
        future_minimum_chars=0,
        future_target_chars=0,
    ) is False
    assert GenerationEngine._final_scene_budget_variance_allowed(
        projected_chars=3192 + 10,
        chapter_max_chars=3192,
        future_minimum_chars=1,
        future_target_chars=0,
    ) is False


def test_final_scene_variance_does_not_cover_a_complete_3500_char_chapter():
    assert GenerationEngine._final_scene_budget_variance_allowed(
        projected_chars=3545,
        chapter_max_chars=3192,
        future_minimum_chars=0,
        future_target_chars=0,
    ) is False


def test_scene_truncation_retry_has_a_bounded_escalation():
    assert SCENE_DEEPSEEK_FINAL_TRUNCATION_REPAIR_MARGIN == 1.70
    assert SCENE_MIXED_TRUNCATION_OVERLONG_REPAIR_MARGIN == 1.10


def test_scene_truncation_can_get_one_final_attempt_but_other_repairs_cannot():
    assert GenerationEngine._can_extend_truncation_retry(
        previous_issue_codes={"scene_provider_truncated"},
        attempt=1,
        max_attempts=2,
    ) is True
    assert GenerationEngine._can_extend_truncation_retry(
        previous_issue_codes={"scene_overlong"},
        attempt=1,
        max_attempts=2,
    ) is False
    assert GenerationEngine._can_extend_truncation_retry(
        previous_issue_codes={"scene_provider_truncated"},
        attempt=2,
        max_attempts=3,
    ) is False


def test_bounded_budget_retry_covers_mixed_scene_overrun_once():
    assert GenerationEngine._can_extend_budget_retry(
        previous_issue_codes={"scene_chapter_budget_overrun"},
        attempt=1,
        max_attempts=2,
        projected_chars=3031,
        chapter_max_chars=3000,
        future_minimum_chars=0,
        future_target_chars=0,
    ) is True
    assert GenerationEngine._can_extend_budget_retry(
        previous_issue_codes={"scene_chapter_budget_overrun"},
        attempt=1,
        max_attempts=2,
        projected_chars=3480,
        chapter_max_chars=3000,
        future_minimum_chars=0,
        future_target_chars=0,
    ) is True
    assert GenerationEngine._can_extend_budget_retry(
        previous_issue_codes={"scene_chapter_budget_overrun"},
        attempt=1,
        max_attempts=2,
        projected_chars=4201,
        chapter_max_chars=3000,
        future_minimum_chars=0,
        future_target_chars=0,
    ) is False
    assert GenerationEngine._can_extend_budget_retry(
        previous_issue_codes={"scene_chapter_budget_overrun"},
        attempt=1,
        max_attempts=2,
        projected_chars=3031,
        chapter_max_chars=3000,
        future_minimum_chars=120,
        future_target_chars=0,
    ) is True
    assert GenerationEngine._can_extend_budget_retry(
        previous_issue_codes={"scene_chapter_budget_overrun", "scene_provider_truncated"},
        attempt=1,
        max_attempts=2,
        projected_chars=3031,
        chapter_max_chars=3000,
        future_minimum_chars=0,
        future_target_chars=0,
    ) is False


def test_budget_retry_envelope_preserves_scene_minimum_and_chapter_ceiling():
    # A tight final-scene remainder must not be reduced below the amount
    # needed for a complete scene, even when the previous candidate was much
    # larger.  The chapter ceiling still wins over the completion headroom.
    assert GenerationEngine._budget_retry_max_chars(
        remaining_scene_budget=520,
        minimum_scene_chars=300,
        previous_candidate_chars=1100,
    ) == 472
    assert GenerationEngine._budget_retry_max_chars(
        remaining_scene_budget=1000,
        minimum_scene_chars=300,
        previous_candidate_chars=1100,
    ) == 880
    assert GenerationEngine._budget_retry_max_chars(
        remaining_scene_budget=350,
        minimum_scene_chars=300,
        previous_candidate_chars=1100,
    ) == 302


def test_budget_retry_uses_the_calibrated_deepseek_completion_margin():
    # 1.05 allowed the real Provider to overshoot a 3000-char chapter after a
    # bounded retry; 0.86 is the measured complete-scene calibration.
    assert SCENE_BUDGET_RETRY_COMPLETION_MARGIN == 0.86
    assert SCENE_BUDGET_RETRY_SMALL_SCENE_MAX_CHARS == 800
    assert SCENE_BUDGET_RETRY_SMALL_SCENE_COMPLETION_MARGIN == 1.05


def test_expression_only_scene_retry_can_get_one_fresh_style_path():
    assert GenerationEngine._can_extend_style_retry(
        previous_issue_codes={"scene_explanatory_narration", "scene_metaphor_density"},
        attempt=1,
        max_attempts=2,
    ) is True
    assert GenerationEngine._can_extend_style_retry(
        previous_issue_codes={"scene_chapter_budget_overrun"},
        attempt=1,
        max_attempts=2,
    ) is False
    assert GenerationEngine._can_extend_style_retry(
        previous_issue_codes={"scene_metaphor_density"},
        attempt=2,
        max_attempts=3,
    ) is False
    assert GenerationEngine._is_style_only_retry(
        {"scene_opening_contract", "scene_metaphor_density"}
    ) is True


def test_scene_budget_guard_rejects_candidate_that_consumes_future_scene_minimums():
    accepted_chars = 4300
    candidate_chars = 700
    future_minimum_chars = 120
    chapter_max_chars = 4950

    assert GenerationEngine._scene_exceeds_chapter_budget(
        accepted_chars=accepted_chars,
        candidate_chars=candidate_chars,
        future_minimum_chars=future_minimum_chars,
        chapter_max_chars=chapter_max_chars,
    ) is True
    assert GenerationEngine._scene_exceeds_chapter_budget(
        accepted_chars=4300,
        candidate_chars=500,
        future_minimum_chars=future_minimum_chars,
        chapter_max_chars=chapter_max_chars,
    ) is False


def test_reader_budget_rebalances_future_scene_targets_before_rewriting_current_prose():
    cards = [
        {"target_words": 638},
        {"target_words": 1221},
        {"target_words": 1404},
    ]

    assert GenerationEngine._rebalance_future_scene_targets(
        cards,
        future_start=2,
        excess_chars=293,
    ) is True
    assert cards[2]["target_words"] == 1111


def test_story_director_defaults_to_generation_first_without_post_write_rework():
    import inspect
    from app.v7.director.story_director import StoryDirector

    parameter = inspect.signature(StoryDirector.generate_chapter).parameters["allow_rework"]
    assert parameter.default is False
