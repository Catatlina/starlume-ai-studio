"""Generation-first readability and human-texture contract.

The final quality gate can reject a weak chapter, but it cannot teach a writer
what the reader should feel before the prose exists.  This module creates a
small, application-owned pre-generation contract that is carried by scene
planning, writing, continuation and semantic repair.

It deliberately describes observable writing decisions instead of asking for
"natural" or "human" prose.  The provider still writes the actual sentences;
the application only rotates bounded choices so every chapter does not fall
back to the same explanatory cadence.
"""
from __future__ import annotations

from typing import Any


READABILITY_PLAN_SCHEMA_VERSION = "readability-plan-v1"

_DELIVERY_MODES: tuple[tuple[str, str], ...] = (
    ("action_consequence", "用动作和立刻发生的后果交付信息"),
    ("dialogue_subtext", "用带目的的对白、打断和潜台词交付信息"),
    ("object_evidence", "用具体物件的变化、缺口或使用方式交付信息"),
    ("reaction_feedback", "用人物反应、关系变化或现场反馈交付信息"),
    ("choice_pressure", "用取舍、代价和主动选择交付信息"),
    ("mixed_scene", "在动作、对白和具体细节之间自然切换，不按固定顺序轮换"),
)

_RHYTHMS: tuple[tuple[str, str], ...] = (
    ("impact_bursts", "冲突处短促，承接处放长；爆点前收紧，爆点后留一个有重量的停顿"),
    ("dialogue_tension", "对白承担推进，回答不必完整；用停顿、打断和动作露出关系变化"),
    ("mixed_cadence", "短句负责冲击，长句负责空间和因果；避免整章句长均匀"),
    ("pressure_release", "压力段加快，选择前放慢半拍，结果落地后立即给反馈或新压力"),
    ("aftermath_weight", "动作后留具体余波，少解释情绪，让人物反应和环境变化代替总结"),
)

_PARAGRAPH_TEXTURES: tuple[tuple[str, str], ...] = (
    ("action_then_reaction", "段落常从动作或变化起笔，随后给人物反应；不要每段都以人名起笔"),
    ("dialogue_breaks", "让对白被动作、物件或现场阻碍打断，避免连续整齐的问答"),
    ("object_anchor", "每个主要场景抓住一两个会被使用或改变的具体物件，不铺满感官清单"),
    ("consequence_cuts", "在结果出现处及时断段，把读者注意力交给可见后果，不用总结收尾"),
    ("scene_pressure", "段落长短随压力变化，转折前留线索，爆发后不重复解释刚发生的事"),
)

_VOICE_ANCHORS: tuple[str, ...] = (
    "对白先体现角色目的，再传递信息；不同角色不能共享同一种完整、圆滑、解释型口吻。",
    "允许人物说半句、改口、避答或用动作代替回答；但每句对白都要改变关系、信息或压力。",
    "叙述只跟随当前视角人物能感知和推断的内容，不替人物提前宣布答案。",
    "情绪通过选择、动作、停顿和具体反应呈现，避免直接把情绪结论说满。",
)

_ANTI_TEMPLATE_RULES: tuple[str, ...] = (
    "不要用‘先概括、再解释、最后总结’串联每个段落；事件先发生，解释只在读者确实需要时出现。",
    "不要连续使用同一主语、同一动作开头或同一种句末；变化要服从场景，不要机械轮换。",
    "避免把每个人的反应都写成整齐的震惊、倒吸凉气、脸色大变；写出与身份和利益有关的具体反应。",
    "避免用泛化的‘空气凝固、气氛压抑、众人震惊’替代现场变化；至少落到一个动作、物件或选择。",
    "不要把段落全部修成干净、完整、均匀的说明句；保留人物口吻、短句、碎片和必要停顿。",
)


def _text(value: Any, limit: int = 180) -> str:
    return str(value or "").strip()[:limit]


def _chapter_type(plot_brief: dict[str, Any] | None, explicit: str | None) -> str:
    value = explicit or (plot_brief or {}).get("chapter_type") or (plot_brief or {}).get("chapter_mode")
    value = _text(value, 40).lower()
    return value if value in {"normal", "aftermath", "relationship", "suspense"} else "normal"


def build_readability_plan(
    chapter_number: int,
    *,
    chapter_type: str | None = None,
    plot_brief: dict[str, Any] | None = None,
    quality_profile: dict[str, Any] | None = None,
    opening_plan: dict[str, Any] | None = None,
    style_card: dict[str, Any] | None = None,
    recent_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a compact, deterministic pre-generation prose contract.

    The contract is intentionally not a prose score and is never the final
    acceptance decision.  It makes the writing request concrete before the
    provider sees the chapter prompt.
    """
    seq = max(1, int(chapter_number or 1))
    chapter_key = _chapter_type(plot_brief, chapter_type)
    history = [
        str(item.get("delivery_mode") or "")
        for item in (recent_history or [])
        if isinstance(item, dict)
    ]
    delivery_candidates = list(_DELIVERY_MODES)
    if chapter_key == "relationship":
        delivery_candidates = [
            _DELIVERY_MODES[1], _DELIVERY_MODES[3], _DELIVERY_MODES[4],
            _DELIVERY_MODES[5], _DELIVERY_MODES[0], _DELIVERY_MODES[2],
        ]
    elif chapter_key == "suspense":
        delivery_candidates = [
            _DELIVERY_MODES[2], _DELIVERY_MODES[0], _DELIVERY_MODES[4],
            _DELIVERY_MODES[3], _DELIVERY_MODES[5], _DELIVERY_MODES[1],
        ]
    elif chapter_key == "aftermath":
        delivery_candidates = [
            _DELIVERY_MODES[3], _DELIVERY_MODES[2], _DELIVERY_MODES[4],
            _DELIVERY_MODES[5], _DELIVERY_MODES[0], _DELIVERY_MODES[1],
        ]
    selected_delivery = next(
        (item for item in delivery_candidates if item[0] not in history),
        delivery_candidates[(seq - 1) % len(delivery_candidates)],
    )
    rhythm_key, rhythm = _RHYTHMS[(seq - 1) % len(_RHYTHMS)]
    if chapter_key == "relationship":
        rhythm_key, rhythm = "dialogue_tension", dict(_RHYTHMS)["dialogue_tension"]
    elif chapter_key == "suspense":
        rhythm_key, rhythm = "pressure_release", dict(_RHYTHMS)["pressure_release"]
    elif chapter_key == "aftermath":
        rhythm_key, rhythm = "aftermath_weight", dict(_RHYTHMS)["aftermath_weight"]

    paragraph_key, paragraph_texture = _PARAGRAPH_TEXTURES[(seq - 1) % len(_PARAGRAPH_TEXTURES)]
    opening_mode = _text((opening_plan or {}).get("mode"), 40) or "action"
    opening_label = _text((opening_plan or {}).get("label"), 60) or opening_mode
    brief = plot_brief or {}
    payoff = brief.get("payoff_contract") or {}
    profile = quality_profile or {}
    genre = _text(profile.get("genre") or profile.get("subgenre") or "", 80)
    reader_promise = _text(brief.get("reader_promise") or payoff.get("reader_promise"), 240)
    emotional_target = _text(brief.get("emotional_target"), 180)
    hook = _text(brief.get("hook"), 180)
    style = style_card if isinstance(style_card, dict) else {}
    voice_hint = _text(
        style.get("voice") or style.get("narrative_voice") or style.get("dialogue_style"),
        180,
    )
    if not voice_hint:
        voice_hint = _VOICE_ANCHORS[(seq - 1) % len(_VOICE_ANCHORS)]

    reader_effect = reader_promise or emotional_target or hook or "让读者在本章得到一次明确推进，并带着新的压力继续读。"
    scene_execution = [
        "先让人物面对具体压力或目标，再让信息在行动中出现，不先写百科式背景。",
        f"本章主信息优先通过‘{selected_delivery[1]}’，必要时再用其他方式补足，不用旁白一次性讲完。",
        "每个主要节拍都要留下至少一项变化：位置、关系、资源、认知、伤势或规则后果。",
        "爆发前给可回看的线索或动作依据；爆发后立刻写出具体反馈，再留下代价或新压力。",
    ]
    if seq <= 3:
        scene_execution.append(
            "前三章前 20% 正文必须完成第一次可见转折；异常出现后最多保留一个反应段，"
            "立即让主角作出选择、付出代价或得到反馈，不要回到重复职业流程。"
        )
    return {
        "schema_version": READABILITY_PLAN_SCHEMA_VERSION,
        "chapter_number": seq,
        "chapter_type": chapter_key,
        "reader_effect": reader_effect,
        "reader_promise": reader_promise,
        "emotional_target": emotional_target,
        "hook": hook,
        "opening": {"mode": opening_mode, "label": opening_label},
        "information_delivery": {
            "mode": selected_delivery[0],
            "directive": selected_delivery[1],
            "recent_modes": history[-3:],
        },
        "rhythm": {"mode": rhythm_key, "directive": rhythm},
        "paragraph_texture": {"mode": paragraph_key, "directive": paragraph_texture},
        "voice_anchor": voice_hint,
        "scene_execution": scene_execution,
        "anti_template": list(_ANTI_TEMPLATE_RULES),
        "source": "deterministic_readability_scheduler",
        "genre_hint": genre,
    }


def render_readability_plan(plan: dict[str, Any] | None, *, compact: bool = False) -> str:
    """Render the pre-generation contract for a planner or writer prompt."""
    plan = plan if isinstance(plan, dict) else {}
    delivery = plan.get("information_delivery") or {}
    rhythm = plan.get("rhythm") or {}
    paragraph = plan.get("paragraph_texture") or {}
    opening = plan.get("opening") or {}
    lines = [
        "【生成前可读性预案：先执行，再写正文】",
        f"读者本章应得到的体验：{_text(plan.get('reader_effect'), 260)}",
        f"信息落地方式：{_text(delivery.get('directive'), 180)}；本章不要和最近方式机械重复。",
        f"句段节奏：{_text(rhythm.get('directive'), 180)}",
        f"段落肌理：{_text(paragraph.get('directive'), 180)}",
        f"开场衔接：{_text(opening.get('label') or opening.get('mode'), 80)}；开头先发生事，不先解释。",
        f"人物声音：{_text(plan.get('voice_anchor'), 220)}",
        "执行顺序：具体压力/目标 → 人物行动与选择 → 信息通过现场落地 → 可见结果/反馈 → 余波或新压力。",
    ]
    scene_execution = plan.get("scene_execution") or []
    if scene_execution:
        lines.append("本章推进规则（生成时必须执行）：")
        lines.extend(f"- {_text(item, 260)}" for item in scene_execution[:6])
    if not compact:
        lines.append("本章避免的同构写法：")
        lines.extend(f"- {item}" for item in (plan.get("anti_template") or [])[:5])
        lines.append("本章每个主要节拍至少推进一项状态，不能只换地点或重复解释已知信息。")
    return "\n".join(lines)


def readability_plan_metadata(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Return the auditable, small subset stored with a generated chapter."""
    plan = plan if isinstance(plan, dict) else {}
    return {
        "schema_version": plan.get("schema_version", READABILITY_PLAN_SCHEMA_VERSION),
        "chapter_number": plan.get("chapter_number"),
        "chapter_type": plan.get("chapter_type"),
        "delivery_mode": (plan.get("information_delivery") or {}).get("mode"),
        "rhythm_mode": (plan.get("rhythm") or {}).get("mode"),
        "paragraph_texture": (plan.get("paragraph_texture") or {}).get("mode"),
        "opening_mode": (plan.get("opening") or {}).get("mode"),
        "source": plan.get("source"),
    }
