"""Reader-facing chapter payoff contracts and evidence checks.

The contract is deliberately broader than "a fight every chapter".  A web
novel payoff can be a win, reveal, resource gain, relationship shift, escape or
rule exploit.  What matters is that the reader sees a choice, a consequence
and a new pressure, while the existing Novel Brain remains the only truth
store.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from app.v7.quality.payoff_strategy import choose_payoff_type


PAYOFF_SCHEMA_VERSION = "chapter-payoff-contract-v2"
PAYOFF_INTENSITY_LEVELS = ("small", "medium", "high", "peak")
PAYOFF_INTENSITY_SCORES = {"small": 1, "medium": 2, "high": 3, "peak": 4}
PAYOFF_INTENSITY_ALIASES = {
    "小": "small",
    "小爽": "small",
    "中": "medium",
    "中爽": "medium",
    "正常": "medium",
    "燃": "high",
    "大爽": "high",
    "高": "high",
    "爆燃": "peak",
    "超大": "peak",
    "高潮": "peak",
}
# P2-4 质量整改：反馈强度等级
# 和爽点强度对应，高强度爽点必须匹配高强度反馈
FEEDBACK_INTENSITY_LEVELS = ("small", "medium", "high", "peak")
FEEDBACK_INTENSITY_SCORES = {"small": 1, "medium": 2, "high": 3, "peak": 4}

# P2-4 质量整改：高强度反馈关键词
# 出现这些词说明反馈强度较高
HIGH_FEEDBACK_KEYWORDS = (
    "全场", "众人", "所有人", "鸦雀无声", "一片哗然", "震惊", "石化",
    "不敢相信", "难以置信", "目瞪口呆", "倒吸一口凉气", "哗然",
    "轰动", "震动", "震撼", "沸腾", "炸开了锅", "不敢置信",
)
PEAK_FEEDBACK_KEYWORDS = (
    "全场震惊", "全场鸦雀无声", "所有人都懵了", "一片死寂",
    "史无前例", "前所未有", "颠覆认知", "刷新认知",
)
PAYOFF_PHASES = ("pressure", "build", "burst", "feedback", "aftershock")
PAYOFF_PHASE_ALIASES = {
    "压制": "pressure",
    "压力": "pressure",
    "铺垫": "build",
    "蓄力": "build",
    "酝酿": "build",
    "爆发": "burst",
    "兑现": "burst",
    "反击": "burst",
    "反馈": "feedback",
    "反应": "feedback",
    "围观": "feedback",
    "余波": "aftershock",
    "代价": "aftershock",
    "新压力": "aftershock",
}
PAYOFF_FEEDBACK_TYPES = {
    "status_reversal",
    "money_or_resource",
    "opponent_reaction",
    "breakthrough",
    "combat_advantage",
    "hidden_strength",
    "rule_exploit",
    "system_reward",
    "ability_discovery",
    "industry_breakthrough",
    "career_progress",
}
PAYOFF_TYPES = {
    "status_reversal", "money_or_resource", "information_advantage", "relationship_shift",
    "career_progress", "industry_breakthrough", "opponent_reaction", "breakthrough",
    "combat_advantage", "resource_gain", "reveal", "survival", "hidden_strength",
    "rule_exploit", "system_reward", "ability_discovery", "sacrifice", "faction_shift",
    "family_survival", "reversal", "other",
}

# Providers and outline prompts frequently return reader-facing Chinese labels
# instead of the canonical enum values above.  Treating every unfamiliar label
# as ``other`` makes a valid early-chapter payoff fail the hard contract (for
# example, ``身份反转`` was being downgraded to ``other``).  Keep the canonical
# enum as the storage contract, but normalize common genre/platform wording at
# the boundary.  This is an alias map, not a banned-word list: novel prose and
# punctuation remain unrestricted.
PAYOFF_TYPE_ALIASES = {
    "身份反转": "status_reversal",
    "地位反转": "status_reversal",
    "逆袭": "status_reversal",
    "打脸": "status_reversal",
    "身份逆转": "status_reversal",
    "权力确立": "status_reversal",
    "权威确立": "status_reversal",
    "正式掌权": "status_reversal",
    "职位反转": "status_reversal",
    "反转": "reversal",
    "财富增长": "money_or_resource",
    "金钱资源": "money_or_resource",
    "资源获取": "resource_gain",
    "资源获得": "resource_gain",
    "信息优势": "information_advantage",
    "信息揭示": "reveal",
    "真相揭示": "reveal",
    "揭示": "reveal",
    "关系变化": "relationship_shift",
    "关系转变": "relationship_shift",
    "事业进展": "career_progress",
    "职业进展": "career_progress",
    "行业突破": "industry_breakthrough",
    "突破": "breakthrough",
    "境界突破": "breakthrough",
    "战斗优势": "combat_advantage",
    "战力优势": "combat_advantage",
    "击退": "combat_advantage",
    "战胜": "combat_advantage",
    "生存": "survival",
    "逃生": "survival",
    "隐藏实力": "hidden_strength",
    "规则利用": "rule_exploit",
    "规则漏洞": "rule_exploit",
    "系统奖励": "system_reward",
    "能力发现": "ability_discovery",
    "牺牲": "sacrifice",
    "势力变化": "faction_shift",
    "家人存活": "family_survival",
    "能力展示": "status_reversal",
    "决策反馈": "status_reversal",
    "掌控力": "status_reversal",
    "权威建立": "status_reversal",
    "获得认可": "relationship_shift",
    "态度转变": "relationship_shift",
    "事业推进": "career_progress",
    "方案通过": "career_progress",
    "其他": "other",
}


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _anchor_key(value: Any) -> str:
    """Compare evidence anchors without making punctuation a hard failure."""
    return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE)


_RESULT_ACTION_PREFIXES = (
    "发现", "得知", "看到", "看见", "注意到", "拿到", "获得", "找到", "确认",
    "意识到", "决定", "开始", "出现", "发生", "变成", "成为", "打开", "推开",
    "听见", "听到", "收到", "拿出", "交出", "得到", "失去", "暴露", "引来",
    "触发", "开启", "揭开", "揭示", "证明",
)


def _observable_result_terms(anchor: str) -> list[str]:
    """Extract conservative observable terms from a prose payoff anchor.

    A contract is planning language, while the chapter is natural prose. An
    anchor such as ``发现纸条，得知“借期已至”`` should remain verifiable when
    the chapter says ``瞥见门缝下多了一张纸`` and quotes ``借期已至``. This
    helper removes only common action prefixes and requires every remaining
    meaningful clause to be present; it is not a bag-of-words score.
    """
    raw_parts = re.split(r"[，。；：、,;:!?！？\s]+", str(anchor or ""))
    terms: list[str] = []
    for part in raw_parts:
        term = _anchor_key(part)
        if not term:
            continue
        for prefix in _RESULT_ACTION_PREFIXES:
            prefix_key = _anchor_key(prefix)
            if term.startswith(prefix_key) and len(term) > len(prefix_key):
                term = term[len(prefix_key):]
                break
        if len(term) >= 2 and term not in terms:
            terms.append(term)
    return terms


def _anchor_key_with_spans(value: Any) -> tuple[str, list[tuple[int, int]]]:
    """Return the normalized anchor key and its source-character spans.

    Evidence is produced before/alongside the final humanization pass. A
    semantic editor may change a few words while retaining a long, verbatim
    run from the scene. Keeping spans lets the validator report the actual
    text excerpt instead of accepting a reviewer paraphrase as if it were
    quoted from the chapter.
    """
    raw = str(value or "")
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    for index, char in enumerate(raw):
        if char == "_" or not char.isalnum():
            continue
        chars.append(char)
        spans.append((index, index + 1))
    return "".join(chars), spans


def _resolve_fuzzy_anchor(text: str, anchor: str) -> str:
    """Find a long contiguous excerpt when a safe rewrite changed wording.

    This is intentionally conservative. It does not accept a bag-of-words
    overlap: the shared portion must be contiguous, must be long enough to be
    meaningful, and must cover a substantial part of the proposed anchor.
    """
    anchor_key, _ = _anchor_key_with_spans(anchor)
    text_key, text_spans = _anchor_key_with_spans(text)
    if not anchor_key or not text_key:
        return ""

    match = SequenceMatcher(None, anchor_key, text_key, autojunk=False).find_longest_match(
        0, len(anchor_key), 0, len(text_key)
    )
    minimum = max(24, int(len(anchor_key) * 0.35))
    if match.size < minimum:
        return ""
    start = text_spans[match.b]
    end = text_spans[match.b + match.size - 1]
    return text[start[0] : end[1]].strip()


def _first(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(data.get(key))
        if value:
            return value
    return ""


def _normalize_payoff_intensity(value: Any, *, default: str = "small") -> str:
    raw = _text(value).lower()
    if raw in PAYOFF_INTENSITY_LEVELS:
        return raw
    return PAYOFF_INTENSITY_ALIASES.get(raw, default)


# P2-4 质量整改：推断反馈强度
def _infer_feedback_intensity(feedback_text: str, *, payoff_intensity: str = "small") -> str:
    """从反馈文本中推断反馈强度。

    基于关键词匹配和文本长度粗略估计，用于评分参考。
    高强度爽点应该匹配高强度反馈。
    """
    text = str(feedback_text or "").strip()
    if not text:
        return "small"

    text_lower = text.lower()
    score = 1  # 默认 small

    # 关键词匹配
    peak_count = sum(1 for kw in PEAK_FEEDBACK_KEYWORDS if kw in text_lower)
    if peak_count >= 1:
        score = max(score, 4)  # peak

    high_count = sum(1 for kw in HIGH_FEEDBACK_KEYWORDS if kw in text_lower)
    if high_count >= 3:
        score = max(score, 4)  # peak
    elif high_count >= 2:
        score = max(score, 3)  # high
    elif high_count >= 1:
        score = max(score, 2)  # medium

    # 文本长度：越长通常反馈越充分
    text_len = len(text)
    if text_len >= 200:
        score = max(score, 3)  # high
    elif text_len >= 100:
        score = max(score, 2)  # medium
    elif text_len >= 30:
        score = max(score, 1)  # small

    # 爽点强度和反馈强度的匹配：高强度爽点至少要有中等反馈
    payoff_score = PAYOFF_INTENSITY_SCORES.get(payoff_intensity, 1)
    if payoff_score >= 3:  # high 或 peak 爽点
        # 爽点强度高但反馈太弱，按爽点强度的下一档来算
        score = max(score, payoff_score - 1)

    # 映射回等级
    if score >= 4:
        return "peak"
    elif score >= 3:
        return "high"
    elif score >= 2:
        return "medium"
    else:
        return "small"


def _normalize_payoff_phases(value: Any) -> list[str]:
    values: list[Any]
    if isinstance(value, list):
        values = value
    elif isinstance(value, str):
        values = re.split(r"[,，、|>/→\s]+", value)
    else:
        values = []
    phases: list[str] = []
    for item in values:
        raw = _text(item).lower()
        phase = raw if raw in PAYOFF_PHASES else PAYOFF_PHASE_ALIASES.get(raw, "")
        if phase and phase not in phases:
            phases.append(phase)
    return phases


def _payoff_feedback(data: dict[str, Any]) -> str:
    """Return observable feedback without requiring a crowd scene.

    A payoff may land through a rival's reaction, a changed relationship, a
    resource/state change, or a rule consequence.  The field keeps the
    reader-facing requirement while avoiding a fixed "everyone is shocked"
    template in every chapter.
    """
    return _first(
        data,
        "payoff_feedback",
        "witness_reaction",
        "reaction",
        "external_reaction",
        "external_consequence",
        "aftermath",
    )


def _normalize_payoff_type(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    if raw in PAYOFF_TYPES:
        return raw
    return PAYOFF_TYPE_ALIASES.get(raw, "other")


def _infer_payoff_type(data: dict[str, Any]) -> str:
    """Infer a canonical type from payoff evidence when the enum is ``other``.

    Providers sometimes put the useful reader-facing label in the promise or
    visible-result field and emit ``other`` in the enum field.  Inference is
    intentionally limited to explicit commercial-narrative signals; when no
    signal is present we keep ``other`` so a required contract still fails
    truthfully instead of inventing a payoff.
    """
    signal = " ".join(
        _text(data.get(key), 600)
        for key in (
            "reader_promise", "reader_expectation", "promise", "visible_result",
            "result", "outcome", "payoff", "witness_reaction", "reaction",
            "active_choice", "choice", "decision", "action",
        )
    )
    evidence = data.get("payoff_evidence")
    if isinstance(evidence, list):
        signal += " " + " ".join(
            " ".join(
                _text(item.get(key), 300)
                for key in ("type", "payoff_type", "result", "visible_result", "reaction")
            )
            for item in evidence
            if isinstance(item, dict)
        )
    if not signal:
        return ""
    if any(token in signal for token in (
        "身份反转", "地位反转", "逆袭", "打脸", "成为新老板", "当上老板",
        "正式掌权", "权力确立", "权威确立", "站稳脚跟", "解雇", "被保安带走",
        "员工重新评估", "掌权", "收购公司", "买下公司", "最大股东", "原CEO被解职",
        "新老板", "展现新老板", "完成交接", "宣布审计", "内部审计", "身份确立", "权威",
        "展现掌控力", "掌控局面", "控制局面", "会议通过", "通过提案", "提案通过",
        "董事会同意", "高管态度转变", "态度转变", "开始配合", "开始支持", "获得认可",
        "得到认可", "赢得认可", "立威", "威信", "接管公司", "接手公司", "掌舵",
        "获得授权", "同意试行", "试行改革", "占据主动",
    )):
        return "status_reversal"
    if any(token in signal for token in (
        "关系缓和", "建立信任", "成为盟友", "转为合作", "公开支持", "获得支持",
    )):
        return "relationship_shift"
    if any(token in signal for token in (
        "方案通过", "项目推进", "拿下项目", "签约成功", "合作达成", "客户同意",
        "获得授权", "负责人", "完成交接",
    )):
        return "career_progress"
    if any(token in signal for token in ("境界突破", "实力突破", "突破", "晋级", "升级")):
        return "breakthrough"
    if any(token in signal for token in ("资源获取", "资源获得", "获得资源", "拿到资源", "财富增长", "拿到钱")):
        return "resource_gain"
    if any(token in signal for token in (
        "真相揭示", "信息揭示", "揭开真相", "发现线索", "发现异常", "注意到",
        "看见", "看到", "听见", "得知", "线索", "证据", "秘密", "异常",
        "真相", "消息", "信息优势",
    )):
        return "reveal"
    if any(token in signal for token in ("关系变化", "关系转变", "获得认可", "收服")):
        return "relationship_shift"
    if any(token in signal for token in ("活下来", "逃出生天", "成功逃脱", "生存")):
        return "survival"
    if any(token in signal for token in ("规则利用", "利用规则", "规则漏洞")):
        return "rule_exploit"
    if any(token in signal for token in ("系统奖励", "获得奖励")):
        return "system_reward"
    if any(token in signal for token in ("隐藏实力", "暴露实力")):
        return "hidden_strength"
    return ""


def normalize_payoff_contract(value: Any, *, chapter_number: int | None = None) -> dict[str, Any]:
    """Normalize provider/outline aliases into one stable contract."""
    data = value if isinstance(value, dict) else {}
    raw_type = _first(data, "payoff_type", "type", "kind")
    payoff_type = _normalize_payoff_type(raw_type)
    if payoff_type in {"", "other"}:
        payoff_type = _infer_payoff_type(data) or payoff_type
    feedback = _payoff_feedback(data)
    intensity = _normalize_payoff_intensity(
        _first(data, "payoff_intensity", "intensity", "level", "payoff_level")
    )
    return {
        "schema_version": str(data.get("schema_version") or PAYOFF_SCHEMA_VERSION),
        "chapter_number": int(data.get("chapter_number") or chapter_number or 0),
        "chapter_type": _first(data, "chapter_type", "chapter_mode") or "normal",
        "reader_promise": _first(data, "reader_promise", "reader_expectation", "promise"),
        "pressure": _first(data, "pressure", "conflict", "tension_target", "stakes"),
        "active_choice": _first(data, "active_choice", "choice", "decision", "action"),
        "payoff_type": payoff_type,
        "visible_result": _first(data, "visible_result", "result", "outcome", "payoff"),
        "witness_reaction": _first(data, "witness_reaction", "reaction", "external_reaction") or feedback,
        "payoff_feedback": feedback,
        "cost": _first(data, "cost", "cost_or_risk", "price", "consequence"),
        "next_pressure": _first(data, "next_pressure", "hook", "next_hook", "new_pressure"),
        "payoff_intensity": intensity,
        "payoff_arc": _normalize_payoff_phases(
            data.get("payoff_arc") or data.get("payoff_phases") or data.get("arc")
        ),
        "setup_refs": [
            _text(item, 240)
            for item in (data.get("setup_refs") or data.get("foreshadow_refs") or [])
            if _text(item, 240)
        ][:8],
        "text_anchor": _first(data, "text_anchor", "evidence_anchor", "anchor",),
        "level": _first(data, "level", "payoff_level") or "small",
        "source": _first(data, "source") or "chapter_plan",
    }


def build_payoff_contract(
    value: Any,
    *,
    chapter_number: int | None = None,
    profile: dict[str, Any] | None = None,
    recent_types: list[str] | None = None,
    chapter_function: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a contract from outline/plot fields without inventing facts."""
    data = value if isinstance(value, dict) else {}
    contract = normalize_payoff_contract(data, chapter_number=chapter_number)
    if isinstance(chapter_function, dict):
        contract["chapter_type"] = str(
            chapter_function.get("chapter_type")
            or chapter_function.get("chapter_mode")
            or contract.get("chapter_type")
            or "normal"
        ).strip().lower()
    # A provider/legacy plot brief may explicitly repeat a payoff type even
    # when the active strategy has exhausted that type's rotation window.
    # Repair the contract before prose generation starts.  This keeps the
    # rule a generation-time constraint and avoids spending a full chapter
    # generation only to reject an otherwise valid draft after the fact.
    if contract.get("payoff_type") and recent_types:
        profile = profile if isinstance(profile, dict) else {}
        variety = validate_payoff_variety(
            contract["payoff_type"],
            recent_types,
            profile=profile,
        )
        alternatives = list(variety.get("alternatives") or [])
        recent_tail = {
            str(item).strip()
            for item in (recent_types or [])[-int(variety.get("window") or 3):]
            if str(item).strip()
        }
        replacement = next(
            (item for item in alternatives if item not in recent_tail),
            alternatives[0] if alternatives else "",
        )
        if not variety.get("passed") and replacement:
            original_type = contract["payoff_type"]
            contract["payoff_type"] = replacement
            contract["payoff_type_source"] = "strategy_rotation_repair"
            contract["payoff_type_repaired_from"] = original_type
            contract["payoff_type_repair_reason"] = (
                f"{original_type} 已连续占满 {variety.get('window')} 章轮换窗口"
            )
    if not contract["payoff_type"]:
        profile = profile if isinstance(profile, dict) else {}
        contract["payoff_type"] = choose_payoff_type(
            profile.get("payoff_strategy") or {},
            chapter_number=int(chapter_number or 1),
            allowed_types=profile.get("payoff_types") or [],
            recent_types=recent_types or [],
        )
        contract["payoff_type_source"] = "strategy_rotation"
    if not _first(data, "payoff_intensity", "intensity", "level", "payoff_level"):
        policy = (profile or {}).get("payoff_policy") or {}
        early_limit = int(policy.get("early_chapters_need_payoff") or 0)
        contract["payoff_intensity"] = (
            str(policy.get("early_min_payoff_intensity") or "medium")
            if int(chapter_number or 0) <= early_limit
            else str(policy.get("default_payoff_intensity") or "small")
        )
        contract["intensity_source"] = "policy_floor"
    if not contract.get("payoff_arc"):
        # Existing plot briefs predate the five-phase field.  Carry their
        # behavior forward with an explicit planning template; this does not
        # claim that the prose already contains every phase.
        contract["payoff_arc"] = list(PAYOFF_PHASES)
        contract["arc_source"] = "policy_template"
    return contract


def validate_payoff_beat_structure(beats: Any) -> dict[str, Any]:
    """Validate the compressed five-phase payoff arc used by the writer.

    Four beats are enough when one beat carries two phases.  The validator
    therefore checks phase coverage, not a fixed number of beats.  Missing
    phase labels are inferred only for legacy plans and are reported as such;
    new plans are prompted to emit them explicitly.
    """
    items = [item for item in (beats or []) if isinstance(item, dict)]
    covered: list[str] = []
    inferred = False
    evidence: list[dict[str, Any]] = []
    for index, beat in enumerate(items):
        raw_phases = beat.get("payoff_phases") or beat.get("payoff_phase") or beat.get("phase")
        phases = _normalize_payoff_phases(raw_phases)
        if not phases:
            text = " ".join(_text(beat.get(key), 300) for key in ("name", "purpose", "content", "emotion"))
            for phase, keywords in {
                "pressure": ("压", "危", "风险", "阻碍", "逼"),
                "build": ("铺", "蓄", "准备", "试探", "积"),
                "burst": ("爆", "反击", "兑现", "击败", "突破", "拿下"),
                "feedback": ("反应", "震", "态度", "围观", "承认", "让步"),
                "aftershock": ("余波", "代价", "后果", "新压", "钩子"),
            }.items():
                if any(keyword in text for keyword in keywords):
                    phases.append(phase)
        if not phases and items:
            # Legacy plans commonly have four beats without phase metadata.
            # Positional inference keeps old plans readable but does not hide
            # the fact in the returned evidence.
            inferred = True
            if len(items) == 4:
                positional = (
                    ("pressure",),
                    ("build",),
                    ("burst",),
                    ("feedback", "aftershock"),
                )
            else:
                positional = tuple((phase,) for phase in PAYOFF_PHASES)
            phases = list(positional[min(index, len(positional) - 1)])
        for phase in phases:
            if phase not in covered:
                covered.append(phase)
        evidence.append({"beat_index": index, "phases": phases})
    missing = [phase for phase in PAYOFF_PHASES if phase not in covered]
    passed = bool(items) and not missing
    return {
        "schema_version": PAYOFF_SCHEMA_VERSION,
        "passed": passed,
        "required_phases": list(PAYOFF_PHASES),
        "covered_phases": covered,
        "missing_phases": missing,
        "inferred": inferred,
        "evidence": evidence,
    }


def repair_payoff_beat_structure(beats: Any) -> dict[str, Any]:
    """Repair missing payoff phases before prose generation.

    Providers occasionally return a plausible beat sheet while omitting the
    explicit aftershock/new-pressure phase. Treating that omission only as a
    post-generation failure wastes a full prose request and makes the prompt
    contract weaker than the quality gate. Add the missing phase to the final
    feedback beat when possible; otherwise append a structural instruction.
    The repair is deterministic, included in the returned plan, and revalidated.
    """
    items = [dict(item) for item in (beats or []) if isinstance(item, dict)]
    before = validate_payoff_beat_structure(items)
    if before.get("passed"):
        return {
            "beats": items,
            "repaired": False,
            "repaired_phases": [],
            "before": before,
            "after": before,
        }

    repaired_phases: list[str] = []
    missing = list(before.get("missing_phases") or [])
    phase_copy = {
        "pressure": ("压力落地", "让当前风险在场景中逼近，不能只写背景说明。"),
        "build": ("蓄力与选择", "写出主角依据已有信息做出具体选择，并承担准备或代价。"),
        "burst": ("结果兑现", "把本章核心行动写成可见结果，而不是用旁白跳过。"),
        "feedback": ("外部反馈", "让对手、组织、资源、规则或旁观者出现可观察变化。"),
        "aftershock": ("余波与新压", "写出结果立刻造成的后果、代价或新压力，并把章末钩子落到动作上。"),
    }

    # The final beat normally owns feedback; attaching the aftershock there
    # preserves the outline's pacing and avoids manufacturing a sixth beat.
    if items and "aftershock" in missing:
        last = items[-1]
        existing = _normalize_payoff_phases(
            last.get("payoff_phases") or last.get("payoff_phase") or last.get("phase")
        )
        if existing:
            last["payoff_phases"] = list(dict.fromkeys(existing + ["aftershock"]))
            last.pop("payoff_phase", None)
            last["name"] = f"{_text(last.get('name') or '结果反馈', 80)}与余波"
            purpose = _text(last.get("purpose") or last.get("content"), 300)
            last["purpose"] = f"{purpose}；{phase_copy['aftershock'][1]}"
            repaired_phases.append("aftershock")
            missing.remove("aftershock")

    # Any other missing phase, or a plan with no usable beats, receives one
    # explicit structural instruction. This is prompt repair, not invented plot.
    for phase in missing:
        name, purpose = phase_copy.get(phase, (phase, "补足本阶段的可见行动与后果。"))
        items.append({
            "name": name,
            "purpose": purpose,
            "content": purpose,
            "emotion": "推进",
            "target_words": 300,
            "payoff_phases": [phase],
            # This is a structural instruction card, not invented story
            # state. It gives the writer a causal slot to fill from the
            # already-confirmed neighboring beats instead of allowing a
            # synthetic phase to reach prose generation without continuity
            # constraints.
            "scene_card": {
                "location": "沿用上一节已确认地点或其明确承接地点",
                "time": "承接上一节已确认时间",
                "characters": [],
                "goal": f"把{phase_copy.get(phase, (phase, ''))[0]}写成现场动作",
                "obstacle": purpose,
                "choice": "人物依据已确认信息作出当前选择",
                "turn": "本阶段产生一个可见转折",
                "state_change": "本阶段造成一项可观察状态变化",
                "knowledge_boundary": "只使用前面已确认的事实，不补写未知设定",
                "handoff": "把本阶段结果直接交给下一节或章末压力",
                "trigger": "上一阶段已经发生的结果",
                "causal_link": "上一阶段结果触发本阶段行动并产生可见反馈",
            },
            "source": "pre_generation_payoff_repair",
        })
        repaired_phases.append(phase)

    after = validate_payoff_beat_structure(items)
    return {
        "beats": items,
        "repaired": bool(repaired_phases),
        "repaired_phases": repaired_phases,
        "before": before,
        "after": after,
    }


def validate_payoff_contract(
    value: Any,
    *,
    profile: dict[str, Any] | None = None,
    required: bool = False,
    chapter_function: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the minimum commercial-narrative contract.

    ``required=False`` is used for legacy V6 records.  New V7 generation passes
    ``required=True`` once a quality profile is attached, so old data remains
    readable while new chapters cannot silently omit the reader promise.
    """
    contract = normalize_payoff_contract(value)
    profile = profile if isinstance(profile, dict) else {}
    strategy = profile.get("payoff_strategy") or {}
    chapter_function = chapter_function if isinstance(chapter_function, dict) else {}
    if not chapter_function and contract.get("chapter_type"):
        chapter_function = {"chapter_type": contract.get("chapter_type")}
    chapter_mode = str(
        chapter_function.get("chapter_type")
        or chapter_function.get("chapter_mode")
        or "normal"
    ).strip().lower()
    mode_policy = (strategy.get("chapter_modes") or {}).get(chapter_mode) or {}
    active_choice_required = bool(mode_policy.get("active_choice_required", True))
    hard_fields = ("reader_promise", "pressure", "visible_result", "next_pressure")
    if active_choice_required:
        hard_fields = (*hard_fields[:2], "active_choice", *hard_fields[2:])
    missing = [key for key in hard_fields if not contract.get(key)]
    soft_missing = [key for key in ("cost", "payoff_feedback") if not contract.get(key)]
    policy = profile.get("payoff_policy") or {}
    raw_type = _first(
        value if isinstance(value, dict) else {},
        "payoff_type",
        "type",
        "kind",
    )
    explicit_other = raw_type.strip().lower() in {"other", "其他"}
    if required and int(contract.get("chapter_number") or 0) <= int(policy.get("early_chapters_need_payoff") or 0):
        # ``other`` is a real enum value, not a word ban. Accept it when the
        # provider explicitly selected it and the complete reader contract is
        # present. Missing/unknown labels still fail, while the inference path
        # above upgrades recognizable Chinese evidence.
        if contract.get("payoff_type") == "" or (
            contract.get("payoff_type") == "other" and not explicit_other
        ):
            missing.append("payoff_type")
    issues = [f"爽点契约缺少 {key}" for key in missing]
    warnings = [f"爽点契约建议补充 {key}" for key in soft_missing]
    intensity = contract.get("payoff_intensity") or "small"
    intensity_score = PAYOFF_INTENSITY_SCORES.get(intensity, 1)
    min_intensity = str(
        policy.get("early_min_payoff_intensity")
        if int(contract.get("chapter_number") or 0) <= int(policy.get("early_chapters_need_payoff") or 0)
        else policy.get("default_payoff_intensity") or "small"
    )
    strength_issues: list[str] = []
    if required and intensity_score < PAYOFF_INTENSITY_SCORES.get(min_intensity, 1):
        strength_issues.append(f"爽点强度低于{min_intensity}档")
    feedback_types = set(policy.get("feedback_required_types") or PAYOFF_FEEDBACK_TYPES)
    feedback_required = bool(
        mode_policy.get("visible_feedback_required", policy.get("feedback_required", False))
    )
    if required and (
        feedback_required
        or contract.get("payoff_type") in feedback_types
        or intensity in {"high", "peak"}
    ) and not contract.get("payoff_feedback"):
        strength_issues.append("缺少可见反馈（可为对手/组织/资源/规则后果，不要求固定围观群众）")
    if required and not contract.get("payoff_arc"):
        strength_issues.append("未声明压制-蓄力-爆发-反馈-余波的爽点结构")
    return {
        "schema_version": PAYOFF_SCHEMA_VERSION,
        "passed": not missing if required else True,
        "required": required,
        "missing": missing,
        "warnings": warnings,
        "issues": issues,
        "strength_passed": not strength_issues,
        "strength_issues": strength_issues,
        "intensity": intensity,
        "intensity_score": intensity_score,
        "minimum_intensity": min_intensity,
        "chapter_mode": chapter_mode,
        "active_choice_required": active_choice_required,
        "visible_feedback_required": feedback_required,
        "contract": contract,
    }


def validate_payoff_evidence(
    text: str,
    evidence: Any,
    *,
    required: bool = False,
) -> dict[str, Any]:
    """Check reviewer-provided payoff evidence against the actual chapter."""
    items = evidence if isinstance(evidence, list) else []
    checked: list[dict[str, Any]] = []
    invalid: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            invalid.append(f"evidence[{index}] 不是对象")
            continue
        anchor = _text(item.get("anchor") or item.get("text_anchor"), 240)
        result = _text(item.get("result") or item.get("visible_result"), 300)
        exact_match = bool(anchor) and anchor in str(text or "")
        normalized_match = (
            not exact_match
            and len(_anchor_key(anchor)) >= 6
            and _anchor_key(anchor) in _anchor_key(text)
        )
        fuzzy_anchor = ""
        if anchor and not (exact_match or normalized_match):
            fuzzy_anchor = _resolve_fuzzy_anchor(str(text or ""), anchor)
        if not anchor or not (exact_match or normalized_match or fuzzy_anchor):
            invalid.append(f"evidence[{index}] 缺少正文中可定位的原文锚点")
            continue
        if not result:
            invalid.append(f"evidence[{index}] 缺少可见结果")
            continue
        resolved_anchor = fuzzy_anchor or anchor
        checked.append({
            "type": _text(item.get("type") or item.get("payoff_type")) or "other",
            "anchor": resolved_anchor,
            "result": result,
            "source_anchor": anchor if fuzzy_anchor else None,
            "match_mode": (
                "exact"
                if exact_match
                else "punctuation_normalized"
                if normalized_match
                else "fuzzy_contiguous"
            ),
        })
    # A provider may append a second illustrative evidence item whose anchor
    # is not verbatim, even though another item is exactly locatable.  Keep the
    # invalid items in the report for diagnosis, but require at least one real
    # anchor instead of rejecting an otherwise verifiable chapter wholesale.
    passed = bool(checked) if required else not invalid
    return {
        "schema_version": PAYOFF_SCHEMA_VERSION,
        "passed": passed,
        "required": required,
        "checked": checked,
        "invalid": invalid,
    }


def validate_payoff_variety(
    payoff_type: str,
    recent_types: list[str] | None = None,
    *,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep repeated payoff types from becoming a chapter template.

    A single repeated type is a warning because a legitimate scene arc may
    need it twice.  The gate only fails when the same type occupies the whole
    configured rotation window and an alternative exists for the profile.
    """
    profile = profile if isinstance(profile, dict) else {}
    strategy = profile.get("payoff_strategy") or {}
    current = str(payoff_type or "").strip()
    recent = [str(item).strip() for item in (recent_types or []) if str(item).strip()]
    window = max(2, int(strategy.get("no_repeat_window") or 3))
    tail = recent[-window:]
    cycle = [str(item) for item in strategy.get("type_cycle") or [] if str(item)]
    alternatives = [item for item in cycle if item != current]
    repeated = bool(current and current in tail)
    streak = 0
    for item in reversed(recent):
        if item != current:
            break
        streak += 1
    blocked = bool(current and streak >= window and alternatives)
    return {
        "schema_version": PAYOFF_SCHEMA_VERSION,
        "passed": not blocked,
        "payoff_type": current,
        "recent_types": recent[-8:],
        "window": window,
        "repeated": repeated,
        "streak": streak,
        "alternatives": alternatives[:6],
        "warning": "本章爽点类型与近期重复，优先换用策略轮换类型" if repeated else "",
        "issue": "同一爽点类型已连续占满轮换窗口，需重新规划" if blocked else "",
    }


def score_payoff_contract(
    value: Any,
    *,
    profile: dict[str, Any] | None = None,
    text: str = "",
    recent_types: list[str] | None = None,
    recent_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Produce an explainable reader-payoff score from contract evidence.

    This is intentionally deterministic.  It measures whether the chapter
    plan made a real promise and supplied an observable result; it does not
    pretend to measure literary value or replace a blind review.
    """
    contract = normalize_payoff_contract(value)
    source_text = str(text or "")
    result_anchor = str(contract.get("visible_result") or "").strip()
    feedback = str(contract.get("payoff_feedback") or "").strip()
    next_pressure = str(contract.get("next_pressure") or "").strip()
    result_key = _anchor_key(result_anchor)
    source_key = _anchor_key(source_text)
    result_terms = _observable_result_terms(result_anchor)
    result_visible = bool(result_anchor) and (
        result_anchor in source_text
        or (
            len(result_key) >= 8
            and result_key in source_key
        )
        or (
            len(result_terms) >= 2
            and all(term in source_key for term in result_terms)
        )
    )
    variety = validate_payoff_variety(
        contract.get("payoff_type"),
        recent_types,
        profile=profile,
    )
    strategy = profile.get("payoff_strategy") or {}
    history = [
        item for item in (recent_history or [])
        if isinstance(item, dict) and item.get("chapter_number") is not None
    ]
    history.append({
        "chapter_number": contract.get("chapter_number"),
        "payoff_type": contract.get("payoff_type"),
        "payoff_intensity": contract.get("payoff_intensity") or "small",
    })
    intensity_values = [
        PAYOFF_INTENSITY_SCORES.get(str(item.get("payoff_intensity") or "small"), 1)
        for item in history
    ]
    type_values = [
        str(item.get("payoff_type") or "").strip()
        for item in history
        if str(item.get("payoff_type") or "").strip()
    ]
    max_low_streak = max(1, int((profile.get("payoff_policy") or {}).get("max_low_payoff_streak") or 2))
    five_chapter_values = intensity_values[-5:]
    five_chapter_streak = 0
    five_chapter_max_streak = 0
    for item in five_chapter_values:
        if item <= 1:
            five_chapter_streak += 1
            five_chapter_max_streak = max(five_chapter_max_streak, five_chapter_streak)
        else:
            five_chapter_streak = 0
    five_chapter_ready = len(five_chapter_values) >= 5
    five_chapter_score = (
        100
        if five_chapter_ready and five_chapter_max_streak <= max_low_streak
        else 65
        if five_chapter_ready
        else 50
    )
    if five_chapter_ready and len(set(five_chapter_values)) >= 2:
        five_chapter_score = min(100, five_chapter_score + 10)
    twenty_chapter_values = intensity_values[-20:]
    twenty_chapter_types = type_values[-20:]
    twenty_chapter_ready = len(twenty_chapter_values) >= 20
    twenty_type_diversity = (
        len(set(twenty_chapter_types)) / len(twenty_chapter_types)
        if twenty_chapter_types
        else 0.0
    )
    twenty_chapter_score = (
        round(min(100, 60 + twenty_type_diversity * 40))
        if twenty_chapter_ready
        else 50
    )
    if twenty_chapter_ready and max(five_chapter_values or [1]) <= 1:
        twenty_chapter_score = max(0, twenty_chapter_score - 20)
    dimensions = {
        "expectation_fulfillment": 100 if contract.get("reader_promise") and result_anchor else 0,
        "protagonist_agency": 100 if contract.get("active_choice") else 0,
        "result_visibility": 100 if result_visible else (55 if result_anchor else 0),
        "feedback_effectiveness": 100 if feedback else 0,
        # P2-4 质量整改：增加反馈强度评分
        "feedback_intensity": round(
            FEEDBACK_INTENSITY_SCORES.get(
                _infer_feedback_intensity(feedback, payoff_intensity=contract.get("payoff_intensity") or "small"),
                1
            ) / 4 * 100
        ),
        "payoff_intensity": round(
            # P1-6: 没有主动选择的爽点评分直接降一档
            max(1, PAYOFF_INTENSITY_SCORES.get(str(contract.get("payoff_intensity") or "small"), 1) - (0 if contract.get("active_choice") else 1)) / 4 * 100
        ),
        "hook_strength": 100 if next_pressure else 0,
        "payoff_variety": 65 if variety.get("repeated") else 100,
        "five_chapter_curve": five_chapter_score,
        "twenty_chapter_distribution": twenty_chapter_score,
    }
    # P2-4 质量整改：增加反馈强度权重，调整其他维度权重
    # 反馈强度是爽感的重要组成部分，高强度爽点必须匹配高强度反馈
    weights = {
        "expectation_fulfillment": 0.12,    # P0-4: 0.15 → 0.12
        "protagonist_agency": 0.18,         # P1-6: 0.15 → 0.18（主角主动性是爽感核心）
        "result_visibility": 0.12,          # P0-4: 0.15 → 0.12
        "feedback_effectiveness": 0.08,     # P2-4: 0.12 → 0.08（反馈强度单独评分）
        "feedback_intensity": 0.08,         # P2-4: 新增（反馈强度，匹配爽点强度）
        "payoff_intensity": 0.20,           # P0-4: 0.10 → 0.20（提到最高权重之一）
        "hook_strength": 0.12,              # P0-4: 0.14 → 0.12
        "payoff_variety": 0.03,             # P2-4: 0.04 → 0.03（降低多样性权重）
        "five_chapter_curve": 0.05,         # P2-4: 0.07 → 0.05（降低短期曲线权重）
        "twenty_chapter_distribution": 0.02, # P2-4: 0.03 → 0.02（降低长期分布权重）
    }
    score = round(sum(dimensions[key] * weights[key] for key in dimensions), 1)
    # 番茄爽文加码：爽点评分通过标准从75→80
    return {
        "schema_version": PAYOFF_SCHEMA_VERSION,
        "score": score,
        "passed": score >= 80,              # 加码：75 → 80
        "source": "deterministic_contract",
        "dimensions": dimensions,
        "weights": weights,
        "evidence": {
            "reader_promise": bool(contract.get("reader_promise")),
            "active_choice": bool(contract.get("active_choice")),
            "visible_result": result_visible,
            "payoff_feedback": bool(feedback),
            "next_pressure": bool(next_pressure),
            "payoff_type": contract.get("payoff_type") or "",
            "five_chapter_curve": {
                "ready": five_chapter_ready,
                "sample_count": len(five_chapter_values),
                "required": 5,
                "max_low_payoff_streak": five_chapter_max_streak,
                "allowed_low_payoff_streak": max_low_streak,
            },
            "twenty_chapter_distribution": {
                "ready": twenty_chapter_ready,
                "sample_count": len(twenty_chapter_values),
                "required": 20,
                "type_diversity": round(twenty_type_diversity, 3),
                "types": sorted(set(twenty_chapter_types)),
                "strategy": strategy.get("strategy_id"),
            },
        },
        "variety": variety,
        "warnings": [
            message
            for message in (
                "正文中未定位到契约声明的可见结果" if result_anchor and not result_visible else "",
                "本章爽点类型与近期重复" if variety.get("repeated") else "",
                "爽点契约综合分低于 70" if score < 70 else "",
                "五章爽点曲线样本不足，暂不判定" if not five_chapter_ready else "",
                "二十章爽点分布样本不足，暂不判定" if not twenty_chapter_ready else "",
            )
            if message
        ],
    }


def evaluate_payoff_schedule(
    chapters: list[dict[str, Any]] | None,
    *,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate consecutive low-payoff chapters without hard-coding char counts."""
    chapters = [item for item in (chapters or []) if isinstance(item, dict)]
    policy = (profile or {}).get("payoff_policy") or {}
    max_streak = max(1, int(policy.get("max_low_payoff_streak") or 2))
    streak = 0
    issues: list[str] = []
    for item in chapters:
        contract = normalize_payoff_contract(item.get("payoff_contract") or item)
        feedback_types = set(policy.get("feedback_required_types") or PAYOFF_FEEDBACK_TYPES)
        has_payoff = (
            bool(contract.get("visible_result"))
            and contract.get("payoff_type") not in {""}
            and (
                bool(contract.get("payoff_feedback"))
                or contract.get("payoff_type") not in feedback_types
            )
        )
        streak = 0 if has_payoff else streak + 1
        if streak > max_streak:
            issues.append(f"第{contract.get('chapter_number') or '?'}章前后连续 {streak} 章缺少可见兑现")
    return {
        "schema_version": PAYOFF_SCHEMA_VERSION,
        "passed": not issues,
        "max_low_payoff_streak": max_streak,
        "issues": issues,
        "sampled": len(chapters),
    }
