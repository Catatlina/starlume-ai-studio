"""Generation-time readability checks for Chinese web-fiction prose.

These checks are deliberately narrow.  They do not try to estimate an
external detector score or force a house style; they catch high-confidence
signals that make a scene read like an explanation of a story instead of the
story happening on the page.  The generation engine uses the result as retry
feedback before accepting a scene.

The prompt protocol in this module is intentionally short.  A long list of
style prohibitions competes with the scene facts and often makes a Provider
produce the very template we are trying to avoid.  The protocol therefore
gives the writer one concrete delivery path, a small positive example of
information landing on the page, and a strict preflight baseline.
"""
from __future__ import annotations

import re
from typing import Any


_DIALOGUE_RE = re.compile(r"[“「『\"].*?[”」』\"]", re.S)
_EXPLANATION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("explicit_conclusion", r"真正的(?:问题|原因|答案)|也就是说|换句话说|这意味着|这说明"),
    ("mind_conclusion", r"(?:他|她|人物)(?:终于)?(?:明白|意识到|知道自己|心里清楚)"),
    ("denial_conclusion", r"(?:不是|并非)(?:梦|错觉|眼花|巧合)"),
    ("summary_conclusion", r"(?:现在|直到这时|这一刻)(?:他|她|人物)?(?:才)?(?:知道|明白|意识到).{0,18}(?:错|真相|原因)"),
    # These are narrower than a general because/so rule.  They catch the
    # narrator explaining a character's route or uncertainty in one neat
    # sentence, which is the shape exposed by the Zhuque sample, without
    # banning ordinary causal prose.
    ("author_rationale", r"(?:因为|所以|因此|毕竟).{0,28}(?:总不会|只能|必然|显然|应该)"),
    ("uncertainty_inventory", r"(?:他|她|人物|顾沉).{0,12}(?:不知道|不清楚|不确定).{0,42}(?:能不能|在什么地方|还有没有|怎么|是否)"),
)
_SIMILE_RE = re.compile(
    r"(?:好像|仿佛|如同|宛如|犹如|像)[^。！？!?\n，,]{0,18}"
)
_REPEATED_ACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("put_back_loop", r"(?:放回|放下|靠回|重新握起|重新拿起|收回).{0,80}(?:放回|放下|靠回|重新握起|重新拿起|收回)"),
)

# Repeating an injured character's state once can be natural.  Repeating two
# or more state groups in the same short scene is different: it restates the
# same information with a new simile instead of showing a changed condition.
# Keep these patterns broad enough to work across genres, but require multiple
# independent groups before raising a generation retry signal.
_STATE_ECHO_PATTERNS: tuple[tuple[str, str], ...] = (
    ("temperature_or_vitality", r"(?:沙地|地面|身体|体温|温度)[^。！？!?\n]{0,20}(?:抽走|流失|流干|漏出去|变冷|发凉|发冷|冰冷)"),
    ("strength_or_control", r"(?:没有|没(?:有)?|连)[^。！？!?\n]{0,14}(?:力气|力量|睁眼|抬头|回答|站起来|动弹)"),
    ("heartbeat_or_breath", r"(?:心跳|心脏|呼吸|喘气)[^。！？!?\n]{0,18}(?:慢|重|停|喘|顶回来|发慌|沉)"),
    ("injury_or_cultivation", r"(?:丹田|经脉|伤口|指甲|手指)[^。！？!?\n]{0,18}(?:碎|断|消散|流|疼|痛|空|麻|发白|干)"),
)
_MOTION_RE = re.compile(
    r"(?:往(?:前|里|下|上|西|那个方向)|走|挪|扶|喘(?:气)?|停(?:下来)?|继续|沿着|迈出|拨开|推开|爬|站起来|坐回)"
)
_SCENE_TURN_RE = re.compile(
    r"(?:听见|听到|看见|看到|发现|有人|脚步|岔路|选择|拦住|追上|掉下|折断|亮起|出现|打开|推开|猫|影子|声音)"
)

GENERATION_STYLE_PROTOCOL_VERSION = "generation-style-protocol-v4"

_GENERATION_PATHS: dict[str, dict[str, str]] = {
    "event_action_dialogue": {
        "label": "动作与对白推进",
        "instruction": (
            "先让一个正在发生的动作改变现场，再让人物用不完整的对白或声音回应；"
            "对话之后必须出现一个可见物件变化、位置变化或新的选择。"
        ),
    },
    "object_consequence": {
        "label": "物件与后果推进",
        "instruction": (
            "先写一个具体物件、痕迹或声音发生变化，让人物试探或处理它；"
            "不要解释异常的意义，直接写处理动作带来的新限制或新压力。"
        ),
    },
    "plain_factual": {
        "label": "朴素现场推进",
        "instruction": (
            "只用准确的动作、位置、触感、对白和结果推进现场；"
            "把情绪藏在人物做了什么、没做什么以及说到一半停在哪里。"
        ),
    },
}


def select_generation_style_path(
    chapter_number: int,
    scene_index: int,
    attempt: int,
) -> str:
    """Select a deliberate prose route for an independent scene candidate.

    The first candidate follows the chapter/scene rotation.  Repairs use
    different routes rather than asking the Provider to keep polishing the
    same sentence pattern.  This is generation-time diversification, not an
    invitation to change plot facts.
    """
    if attempt == 1:
        return "event_action_dialogue"
    if attempt >= 2:
        return "plain_factual"
    names = tuple(_GENERATION_PATHS)
    return names[max(0, int(chapter_number) + int(scene_index) - 2) % len(names)]


def render_generation_style_protocol(path: str = "event_action_dialogue") -> str:
    """Render the compact writer protocol placed at the end of the prompt."""
    selected = _GENERATION_PATHS.get(path) or _GENERATION_PATHS["event_action_dialogue"]
    return (
        f"【出稿前正文协议 {GENERATION_STYLE_PROTOCOL_VERSION}】\n"
        f"本轮写法：{selected['label']}。{selected['instruction']}\n"
        "硬基线：非对白不使用类比；旁白解释性总结为 0；把感觉直接写成物体实际的移动、声音、触感、位置或后果；"
        "不要替读者宣布人物已经得出的结论。\n"
        "同一身体状态、任务提示、地点信息或规则结果已经写清后，不要换一种比喻再次解释；只有状态发生变化时才补写新增后果。"
        "系统/规则提示只写首次出现的内容，后续只写新增字段、代价或人物行动。\n"
        "表达质地保持平、快、干：对白在有角色交锋的场景中约占三分之一，但不硬凑；"
        "心理解释为 0，身体动作和物件处理承担反应；允许拿错、找不到、被打断、答非所问和事情暂时没做完。\n"
        "每个自然段只承担一个现场落点：动作、对白、物件变化、人物反应或结果，"
        "不要求每段都完整解释因果；段落长短随现场变化，不要把所有段落写成同样长度。\n"
        "本章只保留一条主压力；辅助人物互动必须在同一场内改变主线的风险、资源、关系、位置或线索，"
        "不能为了放缓另开一段完整求助、闲聊或说明；没有主线变化就压缩成几句并直接触发下一步。\n"
        "移动、寻找或赶路最多连续两段没有新事件；每三段内必须出现路线选择、障碍、他人介入、线索变化或可见后果。"
        "没有新事件就压缩成一段，不要把走、停、喘、看、继续拆成行程记录。\n"
        "只学习下面示例的信息落地方式，不复制词句：\n"
        "门栓先动了一下。苏长庚把手收回来，侧身挡住楼梯口。\n"
        "‘谁在里面？’\n"
        "门后没有回答，门缝下却漫出一线水。纸包的边角湿了，里面露出黑色的一截。\n"
        "他没有再推门，把纸包交给身后的人：‘拿稳。’\n"
        "可用的现场反应是‘把册子往自己这边挪了挪’或‘蹲久了腿麻，撑着地站起来’，"
        "不要把它解释成紧张、害怕或意识到真相。\n"
        "出稿前只在内部检查：删掉非对白比喻和替读者下结论的句子；确认至少有一次具体选择改变现场；"
        "不要输出检查过程、规则、标题或说明。"
    )


def _remove_dialogue(text: str) -> str:
    return _DIALOGUE_RE.sub(" ", str(text or ""))


def _paragraphs(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"\n{2,}|\n", text) if item.strip()]


def _state_echo_metrics(text: str) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    repeated_occurrences = 0
    for kind, pattern in _STATE_ECHO_PATTERNS:
        matches = [match.group(0)[:80] for match in re.finditer(pattern, text)]
        if len(matches) >= 2:
            groups.append({"kind": kind, "count": len(matches), "evidence": matches[:3]})
            repeated_occurrences += len(matches) - 1
    return {
        "groups": groups,
        "group_count": len(groups),
        "repeated_occurrences": repeated_occurrences,
    }


def _procedural_motion_metrics(
    text: str,
    *,
    dialogue_count: int | None = None,
) -> dict[str, Any]:
    paragraphs = _paragraphs(text)
    compact = re.sub(r"\s+", "", text)
    motion_hits = len(_MOTION_RE.findall(text))
    turn_hits = len(_SCENE_TURN_RE.findall(text))
    if dialogue_count is None:
        dialogue_count = len(_DIALOGUE_RE.findall(text))
    first_turn_chars = len(compact)
    match = _SCENE_TURN_RE.search(text)
    if match:
        first_turn_chars = len(re.sub(r"\s+", "", text[: match.start()]))
    return {
        "paragraph_count": len(paragraphs),
        "motion_hits": motion_hits,
        "turn_hits": turn_hits,
        "dialogue_count": dialogue_count,
        "first_turn_chars": first_turn_chars,
    }


def _subject_opening_metrics(text: str) -> dict[str, Any]:
    paragraphs = _paragraphs(text)
    subject_count = sum(
        1
        for paragraph in paragraphs
        if re.sub(r"^[\s\"“”‘’「」『』（）(]+", "", paragraph).startswith(("他", "她"))
    )
    return {
        "subject": "他/她",
        "count": subject_count,
        "paragraph_count": len(paragraphs),
        "ratio": round(subject_count / len(paragraphs), 4) if paragraphs else 0.0,
    }


def inspect_generation_naturalness(text: Any) -> dict[str, Any]:
    """Return high-confidence prose risks for a single generation attempt."""
    source = str(text or "").strip()
    narrative = _remove_dialogue(source)
    compact = re.sub(r"\s+", "", narrative)
    size = len(compact)
    flags: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    explanation_hits: list[dict[str, str]] = []
    for label, pattern in _EXPLANATION_PATTERNS:
        for match in re.finditer(pattern, narrative):
            explanation_hits.append({"kind": label, "evidence": match.group(0)[:60]})
    if explanation_hits:
        flags.append({
            "code": "scene_explanatory_narration",
            "severity": "high",
            "message": "旁白替读者下结论或解释人物已经知道的意义；改用具体动作、物件后果或未完成的念头",
            "evidence": explanation_hits[:4],
        })

    similes = [match.group(0)[:60] for match in _SIMILE_RE.finditer(narrative)]
    # Treat genuinely dense image chains as a generation risk, not every pair
    # of natural comparisons as a hard failure.  A fixed threshold overfires
    # on longer scenes: three images in 900-1300 characters is not the same
    # problem as six images in a 600-character opening.  Scale the threshold
    # with scene size while keeping a floor so short scenes do not overfire.
    # A scene gets a soft warning as soon as it exceeds the density baseline,
    # but it only becomes a generation blocker after three additional images.
    # Six-to-nine context-bound comparisons in a 900–1100 character scene are
    # not by themselves an AI-writing defect; treating the soft band as a hard
    # retry made the real Provider fail on ordinary scene texture. Dense image
    # chains remain blocked, especially in shorter scenes.
    simile_limit = max(6, int(size / 160))
    hard_simile_limit = simile_limit + 3
    if size >= 500 and len(similes) > simile_limit:
        warnings.append({
            "code": "scene_metaphor_density_warning",
            "severity": "low",
            "message": "非对白类比略多；优先改成可观察的动作、物件或结果，但不阻断本场",
            "evidence": {
                "count": len(similes),
                "limit": simile_limit,
                "hard_limit": hard_simile_limit,
                "narrative_chars": size,
            },
        })
    if size >= 500 and len(similes) > hard_simile_limit:
        flags.append({
            "code": "scene_metaphor_density",
            "severity": "medium",
            "message": "非对白比喻密度过高；改成可观察的动作、物件或结果",
            "evidence": {
                "count": len(similes),
                "limit": simile_limit,
                "hard_limit": hard_simile_limit,
                "examples": similes[:4],
                "narrative_chars": size,
            },
        })

    repeated_actions: list[dict[str, str]] = []
    for label, pattern in _REPEATED_ACTION_PATTERNS:
        match = re.search(pattern, narrative)
        if match:
            repeated_actions.append({"kind": label, "evidence": match.group(0)[:100]})
    if repeated_actions:
        flags.append({
            "code": "scene_repeated_action_loop",
            "severity": "medium",
            "message": "同一物件或回身动作在短场景内重复，容易形成机械回环；保留一次，另一处改成新的选择或后果",
            "evidence": repeated_actions,
        })

    state_echo = _state_echo_metrics(narrative)
    # One repeated body state is allowed.  Two independent repeated groups
    # indicate that the candidate is explaining the same condition instead of
    # advancing it, especially in a scene that is already short and serial.
    if (
        state_echo["group_count"] >= 2
        and state_echo["repeated_occurrences"] >= 3
    ):
        flags.append({
            "code": "scene_state_echo",
            "severity": "high",
            "message": "同一身体状态、任务结果或规则信息被反复解释；保留一次，后续只写变化、代价或行动",
            "evidence": state_echo,
        })

    # Dialogue is removed from ``narrative`` for prose-only checks, so count
    # it from the original source. Otherwise every dialogue-heavy scene is
    # incorrectly reported as silent route logging.
    procedural_motion = _procedural_motion_metrics(
        narrative,
        dialogue_count=len(_DIALOGUE_RE.findall(source)),
    )
    motion_hits = int(procedural_motion["motion_hits"])
    turn_hits = int(procedural_motion["turn_hits"])
    if (
        procedural_motion["paragraph_count"] >= 7
        and motion_hits >= 8
        and procedural_motion["dialogue_count"] <= 1
        and motion_hits >= max(8, int(turn_hits * 1.4))
        and procedural_motion["first_turn_chars"] > min(420, max(160, int(size * 0.40)))
    ):
        flags.append({
            "code": "scene_procedural_motion",
            "severity": "high",
            "message": "移动/寻找段落像行程记录；压缩无事件移动，并提前落一个阻碍、线索、他人介入或选择代价",
            "evidence": procedural_motion,
        })

    subject_opening = _subject_opening_metrics(narrative)
    if (
        subject_opening["paragraph_count"] >= 8
        and subject_opening["count"] >= 5
        and subject_opening["ratio"] >= 0.55
        and motion_hits >= 8
        and procedural_motion["dialogue_count"] <= 1
    ):
        flags.append({
            "code": "scene_subject_opening",
            "severity": "medium",
            "message": "多个段落以同一主语起笔并连续记录动作；改从动作、物件、声音、环境后果或他人反应起笔",
            "evidence": subject_opening,
        })

    return {
        "schema_version": "generation-naturalness-v1",
        "passed": not flags,
        "narrative_chars": size,
        "explanation_count": len(explanation_hits),
        "metaphor_count": len(similes),
        "warnings": warnings,
        "repeated_action_count": len(repeated_actions),
        "state_echo": state_echo,
        "procedural_motion": procedural_motion,
        "subject_opening": subject_opening,
        "flags": flags,
    }
