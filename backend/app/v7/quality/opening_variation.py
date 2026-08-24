"""Deterministic opening variation and anti-template checks.

The writer still chooses the wording, but the application chooses a bounded
opening shape and rejects a draft that falls back to the same body-sensation
template.  This is intentionally local and explainable: it does not judge the
whole chapter and it never changes story facts.
"""
from __future__ import annotations

import re
from typing import Any


OPENING_MODES: tuple[str, ...] = (
    "action",
    "dialogue",
    "object",
    "external_event",
    "environment",
    "body_sensation",
)

OPENING_MODE_LABELS: dict[str, str] = {
    "action": "动作/选择开场",
    "dialogue": "对白冲突开场",
    "object": "物件异常开场",
    "external_event": "外部事件开场",
    "environment": "环境变化开场",
    "body_sensation": "身体感受开场",
}

OPENING_MODE_DIRECTIVES: dict[str, str] = {
    "action": "从正在发生的动作、决定或争执起笔，让主角在第一段做出选择并造成可见变化。",
    "dialogue": "从带有目的的质问、命令、交易或反常对白起笔，不先解释背景，让对白立刻制造压力。",
    "object": "从一个具体物件的异常、变化或结果起笔，让物件在前两段推动人物行动。",
    "external_event": "从外部事件、来客、警报、消息或现场变化起笔，让人物被迫立即回应。",
    "environment": "从会影响人物选择的环境变化起笔，让环境成为正在发生的压力，不写静态风景说明。",
    "body_sensation": "只有当伤势、异常身体状态本身就是本场核心时才使用身体感受；必须马上落到事件和选择。",
}

_BODY_PARTS = (
    "后脑勺", "胸口", "心口", "耳边", "耳朵", "眼前", "喉咙", "鼻腔",
    "手腕", "指尖", "肩膀", "腹部", "胃里", "腿上", "脚底", "皮肤",
    "骨头", "脑中", "身体", "浑身", "太阳穴",
)
_BODY_SENSATIONS = (
    "疼", "痛", "钝痛", "刺痛", "剧痛", "胀痛", "闷", "发紧", "发麻",
    "发冷", "发热", "震动", "轰鸣", "耳鸣", "眩晕", "恶心", "血腥",
    "酸麻", "抽搐", "麻木", "发沉",
)
_BODY_RE = re.compile(
    rf"(?:{'|'.join(map(re.escape, _BODY_PARTS))}).{{0,14}}(?:{'|'.join(map(re.escape, _BODY_SENSATIONS))})"
)
_BODY_CLICHE_RE = re.compile(
    r"(?:一阵|一股|一浪一浪|猛地|突然).{0,28}(?:疼|痛|闷|麻|轰鸣|眩晕|寒意|热意|袭来|涌来|顶上来)"
    r"|像有人.{0,32}(?:砸|攥|掐|撞|撕|捶)"
)
_DIALOGUE_RE = re.compile(r"^[\"“‘「『《〈]|^[^。！？\n]{1,24}[：:]\s*[\"“‘「『]")
_EXTERNAL_EVENT_RE = re.compile(
    r"(?:警报|电话|短信|消息|通知|敲门|脚步|爆炸|枪声|雷声|钟声|广播|来人|有人冲进|门外)"
)
_OBJECT_RE = re.compile(
    r"(?:手机|玉佩|令牌|钥匙|账本|合同|文件|屏幕|信封|药瓶|刀|剑|戒指|手表|门锁|门轴|门缝|门面|门板|门框|铜门|木门|灯|光线|微光|裂痕|字迹|灰尘|箱子).{0,20}(?:裂|亮|震|响|掉|翻|停|显|渗|开|断|出现|变|闪|暗|熄|灭|躺|悬|浮|摆|动)"
    r"|(?:[这那一某]?(?:卷|把|枚|块|张|封|件|个|本|道|片|根|柄|盒|袋|团|幅|扇|盏|只|东西)).{0,24}(?:裂|亮|震|响|掉|翻|停|显|渗|开|断|出现|变|闪|暗|躺|悬|浮|摆|散|裹|动|贴)"
)
_ENVIRONMENT_RE = re.compile(
    r"(?:雨|雾|风|雪|潮气|热浪|冷气|天光|灯光|地面|楼道|院子|街道|海面|山谷).{0,20}(?:压|卷|散|落|亮|暗|晃|漫|逼|涌|变)"
)
_ACTION_RE = re.compile(
    r"(?:抬手|转身|推开|推门|进门|冲出|拔出|按住|抓住|扣下|抵住|迈步|迈进|抬脚|走近|靠近|拎起|握紧|扑|躲|拦|挡|撕|砸|掏出|取出|站起|站到|跪|回头|走向|往.{0,8}走|上楼|下楼|决定|开口|划|扫|扫过|擦|拂过|踩|弯腰|俯身|抬头|伸手|捡起|拾起|掀|扯|提|拉|拽|踢|翻|掩|靠|贴|收回|压住|放下|抓起|捏|拍|丢|摔|蹲下|退后|退开|直起|停住|停下|顿住|顿了一下|摸到|碰到|挪|移|撬|踮)"
)


def _opening_sample(text: Any, limit: int = 360) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return compact[:limit]


def _opening_head(text: Any, limit: int = 160) -> str:
    """Return the first sentence where the opening operator is actually chosen."""
    sample = _opening_sample(text, limit=360)
    first_sentence = re.split(r"[。！？!?\n]", sample, maxsplit=1)[0]
    return first_sentence[:limit].strip()


def classify_opening(text: Any) -> str:
    """Classify only the dominant surface form of the opening."""
    sample = _opening_sample(text, 220)
    head = _opening_head(sample)
    if not head:
        return "unknown"
    leading = re.sub(r"^[\s\"“”‘’「」『』（(]+", "", head)
    if _DIALOGUE_RE.search(head):
        return "dialogue"
    # A body sensation is an opening shape only when the body cue starts the
    # opening.  A later consequence such as "扫帚划过砖缝，脚底震了一下"
    # must remain an action opening; matching any body phrase in the first
    # 180 characters caused real-provider false blocks.
    body_match = _BODY_RE.search(leading)
    if body_match and body_match.start() <= 8:
        return "body_sensation"
    surface_matches = [
        (match.start(), priority, mode)
        for priority, (mode, pattern) in enumerate(
            (
                ("external_event", _EXTERNAL_EVENT_RE),
                ("object", _OBJECT_RE),
                ("action", _ACTION_RE),
                ("environment", _ENVIRONMENT_RE),
            )
        )
        for match in [pattern.search(head)]
        if match is not None
    ]
    if surface_matches:
        return min(surface_matches)[2]
    return "unknown"


def build_opening_history(chapters: list[dict[str, Any]] | None, *, limit: int = 5) -> list[dict[str, Any]]:
    """Create compact history, preferring the persisted observed mode.

    Reclassifying every old opening from a short text sample can collapse
    distinct openings into ``external_event``.  New chapters persist the
    actual gate result, so the scheduler can use that evidence and only fall
    back to classification for legacy chapters.
    """
    result: list[dict[str, Any]] = []
    for chapter in (chapters or [])[-limit:]:
        if not isinstance(chapter, dict):
            continue
        text = str(chapter.get("text") or "")
        opening = chapter.get("opening") or chapter.get("opening_quality") or {}
        persisted_mode = (
            opening.get("observed_mode")
            if isinstance(opening, dict)
            else chapter.get("opening_mode")
        )
        mode = str(persisted_mode or "").strip().lower()
        if mode not in OPENING_MODES:
            mode = classify_opening(text)
        result.append({
            "chapter_number": chapter.get("chapter_number"),
            "mode": mode,
            "sample": _opening_sample(text, 80),
        })
    return result


def select_opening_plan(
    chapter_number: int,
    *,
    chapter_type: str | None = None,
    previous_history: list[dict[str, Any]] | None = None,
    plot_brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select a non-repeating opening operator without overriding continuity."""
    history = [
        str(item.get("mode") or "")
        for item in (previous_history or [])
        if isinstance(item, dict) and item.get("mode") in OPENING_MODES
    ]
    recent = history[-3:]
    explicit = str((plot_brief or {}).get("opening_mode") or "").strip().lower()
    if explicit not in OPENING_MODES:
        explicit = ""

    chapter_type_key = str(chapter_type or (plot_brief or {}).get("chapter_type") or "normal").lower()
    if explicit:
        # An outline may request a mode, but it cannot silently override the
        # global no-repeat contract.  Keep the requested mode first and add a
        # deterministic safe alternative for the rare collision.
        candidates = [explicit]
    elif chapter_number <= 1:
        # Cold opens should not default to pain, waking up or body status.
        candidates = ["action", "dialogue", "external_event", "object", "environment"]
    elif chapter_type_key == "relationship":
        candidates = ["dialogue", "object", "action", "external_event", "environment"]
    elif chapter_type_key == "suspense":
        candidates = ["external_event", "object", "environment", "action", "dialogue"]
    elif chapter_type_key == "aftermath":
        candidates = ["object", "environment", "dialogue", "action", "external_event"]
    else:
        candidates = ["action", "object", "external_event", "dialogue", "environment"]

    if recent:
        selected = next((mode for mode in candidates if mode not in recent), candidates[0])
        if selected in recent and explicit:
            selected = next(
                (mode for mode in ("action", "object", "dialogue", "environment", "external_event") if mode not in recent),
                selected,
            )
    else:
        # Compatibility writers may only expose summaries, not prior prose.
        # Keep their openings varied by chapter number instead of falling back
        # to the first candidate forever.
        selected = candidates[(max(1, int(chapter_number)) - 1) % len(candidates)]
    # An explicit body opening is allowed only when the author/plot contract
    # asks for it. It is still subject to the cliche gate below.
    if selected == "body_sensation" and not explicit:
        selected = next((mode for mode in ("action", "object", "external_event") if mode not in recent), "action")

    return {
        "mode": selected,
        "label": OPENING_MODE_LABELS[selected],
        "directive": OPENING_MODE_DIRECTIVES[selected],
        "recent_modes": recent,
        "forbidden_recent_modes": recent,
        "history_window": 3,
        "source": "deterministic_opening_scheduler",
    }


def opening_prompt_block(plan: dict[str, Any] | None) -> str:
    """Render a short writer-facing opening contract."""
    plan = plan if isinstance(plan, dict) else {}
    mode = str(plan.get("mode") or "action")
    label = plan.get("label") or OPENING_MODE_LABELS.get(mode, mode)
    directive = plan.get("directive") or OPENING_MODE_DIRECTIVES.get(mode, "")
    recent = "、".join(str(item) for item in (plan.get("forbidden_recent_modes") or [])) or "无"
    first_sentence_directive = {
        "action": "首句必须直接出现正在发生的动作、决定或争执，并造成一个可见变化。",
        "dialogue": "首句必须直接是带目的的对白或对白冲突，不得先写环境和日常动作。",
        "object": "首句必须直接出现具体物件及其异常、变化或结果；后文的动作不能代替物件开场。",
        "external_event": "首句必须直接出现外部事件、来客、警报、消息或现场变化，并迫使人物回应。",
        "environment": "首句必须直接出现会改变人物选择的环境变化，不得只是静态风景。",
        "body_sensation": "首句必须直接出现核心身体异常，并立即落到事件和选择。",
    }.get(mode, "首句必须让指定开场类型可被直接识别。")
    return (
        "【开场多样性硬约束】\n"
        f"本章指定开场类型：{label}（{mode}）。{directive}\n"
        f"{first_sentence_directive}\n"
        f"最近三章已使用开场类型：{recent}；本章不要重复这些类型。\n"
        "前300字必须出现具体压力、异常、目标或选择，并完成一次可见推进；"
        "先写正在发生的事，不写醒来、疼痛、空泛环境或背景说明作为默认开场。\n"
        "除非本章指定类型就是 body_sensation 且伤势是核心事件，否则禁止以身体部位+疼痛/发闷/轰鸣/一阵袭来/‘像有人……’起笔。"
    )


def inspect_opening(
    text: Any,
    *,
    requested_mode: str | None = None,
    chapter_number: int = 1,
    recent_modes: list[str] | None = None,
) -> dict[str, Any]:
    """Return a hard-gate result for the first 360 characters."""
    sample = _opening_sample(text)
    requested = str(requested_mode or "").strip().lower()
    observed = classify_opening(sample)
    # A short scene-setting lead followed immediately by a visible choice is
    # still a usable action opening.  Real Provider prose exposed this case:
    # a brief heat/stone detail was followed by the character raising a hand
    # within the first few sentences.  Requiring the first sentence itself to
    # contain an action made the fallback repair spend another Provider call
    # and then reject an otherwise readable, fact-preserving chapter.  Keep
    # this bounded to an initially unclassified opening and an action within
    # the first 160 characters; static openings and later actions remain hard
    # mismatches.
    early_action = _ACTION_RE.search(sample[:220])
    if (
        requested == "action"
        and observed == "unknown"
        and early_action is not None
        and early_action.start() <= 160
    ):
        observed = "action"
    recent = [str(mode) for mode in (recent_modes or []) if mode in OPENING_MODES]
    first_sentence = re.split(r"[。！？!?\n]", sample, maxsplit=1)[0]
    flags: list[dict[str, Any]] = []

    if requested in OPENING_MODES and observed != requested:
        flags.append({
            "code": "opening_mode_mismatch",
            "severity": "high",
            "message": f"指定开场类型为 {requested}，但首句实际呈现为 {observed}",
            "evidence": first_sentence[:120],
        })

    if observed == "body_sensation" and requested != "body_sensation":
        flags.append({
            "code": "opening_body_sensation_default",
            "severity": "high",
            "message": "开头退化为身体部位/疼痛/感官模板，未执行指定的开场类型",
            "evidence": first_sentence[:120],
        })
    if observed == "body_sensation" and _BODY_CLICHE_RE.search(first_sentence[:180]):
        flags.append({
            "code": "opening_body_sensation_cliche",
            "severity": "high",
            "message": "开头命中‘一阵/像有人/身体感受’模板化句式",
            "evidence": first_sentence[:120],
        })
    if observed in recent[-3:]:
        flags.append({
            "code": "opening_mode_repetition",
            "severity": "high",
            "message": f"开场类型 {observed} 在最近三章重复",
            "evidence": first_sentence[:120],
        })
    if chapter_number == 1 and observed == "body_sensation":
        flags.append({
            "code": "opening_first_chapter_body_default",
            "severity": "high",
            "message": "首章不得默认从身体疼痛或醒来状态起笔",
            "evidence": first_sentence[:120],
        })

    return {
        "passed": not flags,
        "requested_mode": requested or None,
        "observed_mode": observed,
        "sample": sample[:360],
        "flags": flags,
    }
