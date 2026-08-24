"""Generation Engine - Sprint 2.

Real generation pipeline:
  context assembly -> scene planning (AI) -> serial scene generation with
  state handoffs -> generation-time scene retry -> advisory/fallback audit

No mocks, no placeholder text. Failures raise instead of returning fake success.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from ..brain.novel_brain import NovelBrain
from ..trace.tracer import ExecutionTracer
from ..events.event_bus import EventBus
from ...services.ai_runtime import (
    execution_key as build_execution_key,
    normalise_prompt_version,
    prompt_hash,
    record_async_execution,
)
from ...services.unified_gateway import UnifiedAIGateway
from ...services.text_quality import (
    chapter_mirror_stats,
    deduplicate_full_paragraphs,
    duplicate_paragraph_stats,
    normalize_and_validate_rewrite,
)
from ...services.chapter_payoff import (
    build_payoff_contract,
    repair_payoff_beat_structure,
    validate_payoff_contract,
    validate_payoff_variety,
    score_payoff_contract,
)
from ...services.content_policy import analyze_content_policy, content_generation_contract
from ...services.quality_profiles import (
    compile_quality_directive,
    quality_profile_metadata,
    reader_chapter_budget,
    select_quality_profile,
)
from ..quality.deai_metrics import analyze_deai_patterns
from ..quality.generation_naturalness import (
    _remove_dialogue,
    inspect_generation_naturalness,
    render_generation_style_protocol,
    select_generation_style_path,
)
from ..quality.novel_reviewer_reference import render_ai_flavor_guidance
from ..quality.opening_variation import (
    build_opening_history,
    inspect_opening,
    opening_prompt_block,
    select_opening_plan,
)
from ..quality.prose_generation import (
    apply_segment_replacements,
    build_generation_critic_report,
    feature_card_from_style_card,
    render_prose_feature_card,
    sanitise_style_card_for_prompt,
    validate_segment_replacement_payload,
)
from ..quality.readability_contract import (
    build_readability_plan,
    readability_plan_metadata,
    render_readability_plan,
)
from ..quality.writing_methodology import (
    build_writing_workflow_contract,
    render_writing_methodology_contract,
    validate_writing_workflow,
)
from ..quality.web_research import WebResearchService, render_web_research_guidance
from ...services.pov_quality import analyze_third_person_narrative, third_person_generation_contract

# P1-3 质量整改：导入质量门控灰度开关
from ..integration.quality import CHAPTER_MIRROR_HARD_GATE, PAYOFF_VARIETY_HARD_GATE

logger = logging.getLogger(__name__)

CHAPTER_STATE_TYPE = "chapter"
SCENE_SERIAL_GENERATION_VERSION = "2.35.0"
# Keep the canonical writer loop intentionally small.  Candidate fan-out and
# local prose surgery belong to explicit/manual tooling, not the production
# chapter path; nested retries made the writer see too many competing rules.
SCENE_SERIAL_MAX_ATTEMPTS = 2
# A pure expression repair may use one additional independent writing path;
# budget, truncation, POV and fact-contract failures never receive this
# extension.  This keeps the bounded loop from turning into open-ended
# polishing while giving the real Provider one fresh style route.
SCENE_STYLE_RETRY_MAX_ATTEMPTS = 3
# A complete final scene that misses the chapter ceiling by only a few
# characters deserves one final, smaller generation path. This is deliberately
# narrower than style/truncation retries: it never relaxes the 2200-3000 reader
# budget and never rescues a materially oversized scene.
SCENE_FINAL_BUDGET_RETRY_MAX_ATTEMPTS = 3
SCENE_FINAL_BUDGET_RETRY_RATIO = 0.62
SCENE_FINAL_BUDGET_RETRY_MAX_OVERFLOW_CHARS = 480
SCENE_HANDOFF_SCHEMA = "scene-handoff-v1"
# Platform limits are not reader targets.  The active quality profile now
# derives a reader-facing chapter budget before planning and prose generation.
# DeepSeek's initial completion allowance was too generous for the capped
# opening scene: a 480-character target with a 1.25 margin produced an 843-
# character candidate before the scene contract could stop it. Keep the first
# call close to the actual prose envelope; truncation retries have their own
# larger, evidence-based margins below.
SCENE_DEEPSEEK_TOKEN_CHAR_MARGIN = 1.05
SCENE_OPENAI_TOKEN_CHAR_MARGIN = 1.10
SCENE_DEEPSEEK_TRUNCATION_REPAIR_MARGIN = 1.35
SCENE_OPENAI_TRUNCATION_REPAIR_MARGIN = 1.20
SCENE_DEEPSEEK_FINAL_TRUNCATION_REPAIR_MARGIN = 1.70
SCENE_OPENAI_FINAL_TRUNCATION_REPAIR_MARGIN = 1.50
# The retry must have enough completion headroom to finish a Chinese scene.
# The prompt and hard character envelope perform the compression; an overly
# small token cap turns a valid pacing repair into provider truncation.
# A 1.10 retry produced 1,488 chars against a 1,146-char envelope, while 0.86
# produced 1,148 chars against a 1,099-char envelope without truncation.  Keep
# the complete 0.86 repair and allow small natural variance; large runaways
# still fail the generation contract.
SCENE_DEEPSEEK_OVERLONG_REPAIR_MARGIN = 0.86
SCENE_OPENAI_OVERLONG_REPAIR_MARGIN = 0.90
# If a scene was truncated before a complete-but-overlong retry arrived, the
# next compression still needs enough completion headroom to finish.  The
# ordinary overlong ratio is safe only when truncation was never observed.
SCENE_MIXED_TRUNCATION_OVERLONG_REPAIR_MARGIN = 1.10
SCENE_PROVIDER_TOKEN_CAP = 6000
SCENE_TARGET_MAX_RATIO = 1.30
SCENE_NATURAL_LENGTH_TOLERANCE = 1.13
# Keep a small rounding/paragraph variance allowance.  A 32-character
# boundary was rejecting otherwise natural scenes by a few dozen characters;
# chapter-level target reservation remains the hard ceiling.
SCENE_NATURAL_LENGTH_TOLERANCE_CHARS = 64
# A scene-level budget is a pacing guide, not a reason to rewrite a complete
# scene for a few trailing characters. The chapter envelope and future-scene
# reservation remain hard limits.
SCENE_NATURAL_LENGTH_SOFT_OVERFLOW_CHARS = 64
# A chapter-level boundary needs a little more room than one paragraph. This
# is still a bounded reader variance, not permission to approach the platform
# ceiling or pad a chapter after its result is complete.
CHAPTER_NATURAL_LENGTH_TOLERANCE_CHARS = 192
# A complete final scene may need a little more room for its consequence and
# hook. This is an absolute generation allowance only when no future scene is
# being starved; it is not a new chapter target or a platform ceiling.
# A complete closing scene may need room for its immediate consequence and
# hook even when an earlier scene consumed more of the chapter allocation than
# planned. Keep this materially below a platform-sized chapter: it is a
# bounded final-scene allowance, not permission to pad or continue indefinitely.
CHAPTER_FINAL_SCENE_NATURAL_VARIANCE_CHARS = 480


def chinese_word_count(text: str) -> int:
    """Count characters the way Chinese novel platforms do (whitespace ignored)."""
    if not text:
        return 0
    return len(re.sub(r"\s+", "", text))


def chapter_state_key(chapter_number: int) -> str:
    return f"chapter_{chapter_number:04d}"


def _chapter_title_key(value: Any) -> str:
    """Normalize a chapter title for duplicate detection."""
    text = re.sub(r"第\s*\d+\s*章", "", str(value or ""))
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _title_hint_fragment(value: Any) -> str:
    """Extract a short event-shaped title from a plot hint.

    A plot hint is often a sentence.  It must never be appended verbatim to a
    chapter title because that turns the title into a summary (for example
    ``江心岛迷雾·周衡在逃脱后，发现手机屏``).  Keep only the first event
    clause, remove narration scaffolding, and prefer a compact hook phrase.
    """
    text = str(value or "").strip()
    text = re.sub(r"^(?:本章|章末|钩子|结果)\s*[：:]?", "", text)
    text = re.split(r"[。！？；\n]", text, maxsplit=1)[0]
    comma_clauses = re.split(r"[，,]", text)
    if len(comma_clauses) > 1 and re.search(r"发现|收到|看见|听见|进入|遭遇|面对", comma_clauses[1]):
        text = comma_clauses[1]
    else:
        text = comma_clauses[0]
    text = re.sub(r"[“”\"'「」《》]", "", text)
    text = re.sub(
        r"^(?:[^，。！？：:]{0,10})?(?:发现|收到|看见|听见|进入|遭遇|面对|决定|开始)\s*",
        "",
        text,
    )
    text = re.sub(r"^(?:语音|消息|短信)(?:中|里)?(?:传来|出现|提示)\s*", "", text)
    text = re.sub(r"^(?:主角|人物|他|她)[的地]", "", text)
    text = text.replace("突然", "")
    text = re.sub(r"(?:的)?(?:声音|消息|警告)$", "", text)
    text = text.strip(" ：:，,、—-")
    if len(text) > 12:
        tail = re.split(r"的", text)[-1].strip(" ：:，,、—-")
        if 2 <= len(tail) <= 12:
            text = tail
    return text[:12].rstrip("的了着在与和或从向").strip(" ：:，,、—-")


def validate_tomato_chapter_title(value: Any) -> tuple[bool, str]:
    """Validate the reader-facing short-title contract used by Tomato novels."""
    text = re.sub(r"\s+", "", str(value or "")).strip()
    if not text:
        return False, "标题为空"
    if len(text) < 2:
        return False, "标题少于 2 个字"
    if len(text) > 12:
        return False, "标题超过 12 个字"
    if re.search(r"第\s*\d+\s*章|本章|这一章|读者将|读者会", text):
        return False, "标题包含章节或元叙述"
    if re.search(r"主角在|人物在|他在|她在|将要|正在", text):
        return False, "标题仍是剧情摘要句式"
    if len(text) >= 8 and re.search(r"发现|收到|看见|听见|进入|遭遇|面对|决定|然后|之后|以后|突然|屏幕", text):
        return False, "标题包含过长的动作摘要"
    if len(text) >= 9 and re.search(r"[，,。！？；;：:]", text):
        return False, "标题包含完整句子标点"
    return True, "ok"


def ensure_unique_chapter_title(
    title: Any,
    *,
    previous_titles: list[Any] | None = None,
    chapter_number: int = 0,
    hints: list[Any] | None = None,
) -> str:
    """Return a unique, short, reader-facing Tomato chapter title.

    The provider still supplies the creative title.  This deterministic guard
    is the final contract: it removes chapter-summary output, keeps titles in
    the 2-12 character range, and uses planned hooks only as compact fallback
    candidates when the provider repeats or violates the title shape.
    """
    def clean_title(value: Any) -> str:
        text = str(value or "").strip()
        text = re.sub(r"^第\s*\d+\s*章\s*[：:、\s]*", "", text)
        text = re.sub(
            r"^(?:本章|这一章|主角|人物|他|她)?"
            r"(?:在[^，。！？：:]{0,20})?"
            r"(?:发现|收到|看见|听见|进入|遭遇|面对|决定|开始)\s*",
            "",
            text,
        )
        text = re.sub(r"^(?:本章|这一章)(?:将|要)?\s*", "", text)
        text = re.sub(r"[（(][^）)]{0,20}[）)]$", "", text).strip()
        text = text.strip(" ：:，,、—-")
        if len(text) > 12:
            text = _title_hint_fragment(text)
        return text[:12].rstrip("的了着在与和或从向").strip(" ：:，,、—-")

    base = clean_title(title) or f"第{chapter_number}章"
    previous_keys = {
        _chapter_title_key(item)
        for item in (previous_titles or [])
        if _chapter_title_key(item)
    }
    is_valid, _ = validate_tomato_chapter_title(base)
    if is_valid and _chapter_title_key(base) not in previous_keys:
        return base[:40]

    for hint in hints or []:
        fragment = clean_title(_title_hint_fragment(hint))
        fragment_valid, _ = validate_tomato_chapter_title(fragment)
        if not fragment_valid or _chapter_title_key(fragment) in previous_keys:
            continue
        return fragment[:12]

    # Do not fall back to "第 N 章" or append a sentence.  These compact
    # reader-facing hooks keep the title contract valid even when the provider
    # returns no usable title or no distinct hint.
    for fallback in ("新线索", "新危机", "局势突变", "暗线启动", "反击开始"):
        if _chapter_title_key(fallback) not in previous_keys:
            return fallback
    return f"新局{chapter_number or ''}"[:12]


class AIGatewayError(RuntimeError):
    """Raised when the LLM call cannot be completed."""


class AIGatewayTruncatedError(AIGatewayError):
    """The provider stopped at its output limit before returning a full result."""


class BudgetAccountingError(AIGatewayError):
    """Raised when a successful provider call cannot be durably accounted for."""


def is_retryable_provider_failure(error: Any) -> bool:
    """Classify a provider failure that is safe to retry at the task layer.

    This is deliberately narrower than ``AIGatewayError``.  Missing keys,
    budget failures and ledger failures must stop immediately; a transient
    5xx/rate-limit/transport failure may be retried by the bounded Celery
    task.  Keeping the classification next to the gateway prevents the V7
    director and worker from making different decisions about the same error.
    """
    text = str(error or "").strip().lower()
    if not text:
        return False

    permanent_markers = (
        "api key",
        "not configured",
        "refusing to fabricate",
        "budget",
        "cost accounting",
        "accounting failed",
        "ledger",
    )
    if any(marker in text for marker in permanent_markers):
        return False

    transient_markers = (
        "server error",
        "service unavailable",
        "temporarily unavailable",
        "rate limit",
        "too many requests",
        "timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "connection error",
        "502",
        "503",
        "504",
        "429",
        "llm call failed after",
        "did not return parseable json",
        "json unusable",
        "usable confidence",
    )
    return any(marker in text for marker in transient_markers)


class ContextAssembler:
    """Assembles real generation context out of the Novel Brain."""

    def __init__(
        self,
        brain: NovelBrain,
        project_id: str | None = None,
        genre_id: str | None = None,
    ):
        self.brain = brain
        self.project_id = project_id
        self.genre_id = genre_id
        self._genre_cache: dict[str, Any] | None = None

    async def load_style_card(self) -> dict[str, Any]:
        """Load the V6 author/genre card used by the shared project scope."""
        if not self.project_id:
            return {}

        def _read() -> dict[str, Any]:
            from ...db import connect, decode

            conn = connect()
            try:
                row = conn.execute(
                    "SELECT author_card, genre_card FROM style_cards "
                    "WHERE project_id=%s ORDER BY updated_at DESC LIMIT 1",
                    (self.project_id,),
                ).fetchone()
                author_card = decode(row.get("author_card"), {}) if row else {}
                genre_card = decode(row.get("genre_card"), {}) if row else {}
                author_card = author_card if isinstance(author_card, dict) else {}
                genre_card = genre_card if isinstance(genre_card, dict) else {}
                if author_card or genre_card:
                    return {"author_card": author_card, "genre_card": genre_card}

                # A project can have a useful novel-level style summary before
                # the richer author-style learning table has been populated.
                # Do not silently discard it: an empty style card makes the
                # prose provider fall back to generic model cadence.
                novel = conn.execute(
                    "SELECT meta FROM contents WHERE id=%s AND type='novel' "
                    "AND is_deleted=FALSE",
                    (str(self.brain.novel_id),),
                ).fetchone()
                meta = decode(novel.get("meta"), {}) if novel else {}
                style_summary = str(meta.get("style") or "").strip() if isinstance(meta, dict) else ""
                if not style_summary:
                    return {}
                return {
                    "author_card": {
                        "style_summary": style_summary,
                        "source": "novel_meta.style",
                    },
                    "genre_card": {},
                }
            finally:
                conn.close()

        return await asyncio.to_thread(_read)

    async def load_genre_context(self) -> dict[str, Any]:
        """加载品类上下文（风格卡、知识、约束、Prompt模板等）。

        从品类库加载当前品类的规则、知识、风格卡、Prompt模板等信息，
        支持继承解析（子品类没有的自动用父品类的）。
        """
        if not self.genre_id:
            return {}

        # 缓存命中
        if self._genre_cache is not None:
            return self._genre_cache

        try:
            from ..services.genre_inheritance import (
                resolve_genre_rules,
                resolve_genre_knowledge,
                resolve_genre_prompts,
            )
            from ..db import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                # 解析规则（含继承）
                rules = await resolve_genre_rules(db, self.genre_id)

                # 解析知识（含继承）
                knowledge = await resolve_genre_knowledge(db, self.genre_id)

                # 解析 Prompt 模板（含继承）
                prompts = await resolve_genre_prompts(db, self.genre_id)

                # 提取风格卡
                style_card = {}
                for rule_key, rule_dict in rules.items():
                    if rule_dict.get("rule_type") == "style_card":
                        style_card = rule_dict.get("rule_value") or {}
                        break

                # 提取约束
                constraints = []
                for rule_key, rule_dict in rules.items():
                    if rule_dict.get("rule_type") in ("forbidden_words", "world_constraint"):
                        constraints.append({
                            "type": rule_dict.get("rule_type"),
                            "key": rule_dict.get("rule_key"),
                            "value": rule_dict.get("rule_value"),
                            "severity": rule_dict.get("severity"),
                            "description": rule_dict.get("description"),
                        })

                # 整理知识条目（按类型分组）
                knowledge_by_type: dict[str, list[dict[str, Any]]] = {}
                for item in knowledge:
                    ktype = item.get("knowledge_type") or "other"
                    if ktype not in knowledge_by_type:
                        knowledge_by_type[ktype] = []
                    knowledge_by_type[ktype].append({
                        "title": item.get("title"),
                        "content": item.get("content"),
                        "tags": item.get("tags") or [],
                        "priority": item.get("priority"),
                    })

                # 按优先级排序
                for ktype in knowledge_by_type:
                    knowledge_by_type[ktype].sort(
                        key=lambda x: x.get("priority", 0), reverse=True
                    )

                # 整理 Prompt 模板（按类型分组）
                prompts_by_type: dict[str, list[dict[str, Any]]] = {}
                for prompt_name, prompt_data in prompts.items():
                    ptype = prompt_data.get("prompt_type", "other")
                    if ptype not in prompts_by_type:
                        prompts_by_type[ptype] = []
                    prompts_by_type[ptype].append(prompt_data)

                # 提取 writer 类型的主 Prompt（用于章节生成注入）
                writer_prompt = None
                if "writer" in prompts_by_type and prompts_by_type["writer"]:
                    # 取第一个 writer Prompt 作为主写作 Prompt
                    writer_prompt = prompts_by_type["writer"][0]

                if not rules or not knowledge or not prompts or not writer_prompt:
                    raise AIGatewayError(
                        f"genre context is incomplete for configured genre_id={self.genre_id}"
                    )

                result = {
                    "genre_id": self.genre_id,
                    "style_card": style_card,
                    "constraints": constraints,
                    "knowledge": knowledge_by_type,
                    "prompts": prompts_by_type,
                    "writer_prompt": writer_prompt,  # 写作专用 Prompt
                    "total_rules": len(rules),
                    "total_knowledge": len(knowledge),
                    "total_prompts": len(prompts),
                }

                self._genre_cache = result
                return result

        except Exception as e:
            # A configured real genre is part of the generation contract.  A
            # silent empty-context fallback changes the book's category and
            # makes a successful-looking chapter impossible to audit.
            logger.error("加载品类上下文失败，停止生成: %s", type(e).__name__)
            if isinstance(e, AIGatewayError):
                raise
            raise AIGatewayError("genre context unavailable; generation stopped") from e

    async def load_previous_chapters(
        self,
        chapter_number: int,
        *,
        count: int = 2,
        include_rejected: bool = False,
    ) -> list[dict[str, Any]]:
        """Load accepted chapters; rejected drafts never become story context."""
        states = await self.brain.state.list_states(CHAPTER_STATE_TYPE, limit=200)
        chapters: list[dict[str, Any]] = []
        for s in states:
            value = s.get("value") or {}
            num = value.get("chapter_number")
            if (
                isinstance(num, int)
                and num < chapter_number
                and (include_rejected or value.get("passed_review") is not False)
            ):
                chapters.append(value)
        chapters.sort(key=lambda c: c.get("chapter_number", 0))
        return chapters[-count:] if count > 0 else chapters

    async def assemble_context(
        self,
        chapter_number: int,
        *,
        scene_type: str = "normal",
        token_budget: int = 5400,
        include_rejected: bool = False,
    ) -> dict[str, Any]:
        """Assemble layered context: state / goals / constraints / recap.

        Rejected drafts are excluded by default. Ordered diagnostic batches may
        opt in so a held chapter can still be measured for cross-chapter
        continuity; the rendered context labels that chapter as provisional.
        """
        overview = await self.brain.get_overview()

        # 长程连续性修复（2026-08-02）：此前 world/character/plot 各只取
        # 最新 30 条，50 章长程下早期关键设定（伏笔/设定细节）会被挤出
        # 上下文窗口，导致后段连贯性下滑（50 章测试 CH46-50 连贯性跌到
        # 78.8）。改为按需取全量：world/character 取 120 条、plot 取 100 条，
        # global 取全量。state 是压缩后的摘要（每条 ~200 字），120 条约
        # 2.4 万字 ≈ 6k tokens，仍在 8k 上下文预算内。
        character_states = await self.brain.state.list_states("character", limit=120)
        world_states = await self.brain.state.list_states("world", limit=120)
        plot_states = await self.brain.state.list_states("plot", limit=100)
        global_states = await self.brain.state.list_states("global", limit=100)

        goals = await self.brain.goals.list_goals(limit=50)
        active_goals = [
            g for g in goals if g.get("status") in ("in_progress", "pending")
        ]
        constraints = await self.brain.constraints.list_constraints(limit=50)
        style_card = await self.load_style_card()
        active_rules = await self.brain.rules.active_instructions(chapter_number=chapter_number)
        quality_learning = []
        quality_store = getattr(self.brain, "quality_learning", None)
        if quality_store is not None:
            quality_learning = await quality_store.active_recommendations(
                chapter_number=chapter_number,
                limit=2,  # P1-1 质量整改：quality_learning 从4条降到2条
            )

        # 加载品类上下文（第8层）
        genre_context = await self.load_genre_context()

        previous = await self.load_previous_chapters(
            chapter_number,
            count=3,
            include_rejected=include_rejected,
        )
        opening_history = build_opening_history(previous, limit=3)
        payoff_history_chapters = await self.load_previous_chapters(
            chapter_number,
            count=5,  # P1-1 质量整改：payoff_history 从20章降到5章
            include_rejected=include_rejected,
        )
        recap_parts: list[str] = []
        for prev in previous:
            summary = prev.get("summary") or ""
            if summary:
                provisional = "（待复核草稿）" if prev.get("passed_review") is False else ""
                recap_parts.append(
                    f"第{prev.get('chapter_number')}章{provisional}梗概：{summary}"
                )
        last_tail = ""
        previous_transition_contract: dict[str, Any] = {}
        recent_payoff_types: list[str] = []
        recent_payoff_history: list[dict[str, Any]] = []
        previous_workflow = (previous[-1].get("writing_workflow") or {}) if previous else {}
        previous_current_state = (
            previous_workflow.get("current_state")
            if isinstance(previous_workflow, dict)
            else {}
        ) or {}
        previous_delta = (previous[-1].get("transition_contract") or {}).get("state_delta") if previous else {}
        previous_delta = previous_delta if isinstance(previous_delta, dict) else {}
        legacy_known_facts = [
            item.get("summary") or item.get("key")
            for values in previous_delta.values()
            if isinstance(values, list)
            for item in values
            if isinstance(item, dict) and (item.get("summary") or item.get("key"))
        ][:12]
        if payoff_history_chapters:
            last_text = previous[-1].get("text") or ""
            # The old 400-character tail was too short to carry a scene's
            # actor/location/object state into the next chapter.  Keep a
            # durable transition contract as well as the literal tail.
            last_tail = last_text[-1200:] if last_text else ""
            previous_transition_contract = previous[-1].get("transition_contract") or {}
            if previous[-1].get("passed_review") is False:
                previous_transition_contract = {
                    **previous_transition_contract,
                    "provisional": True,
                    "warning": "上一章尚未通过质量复核；可承接事实，但不得把未确认状态当作真相写死。",
                }
            for previous_chapter in payoff_history_chapters:
                previous_contract = previous_chapter.get("payoff_contract") or {}
                payoff_type = str(previous_contract.get("payoff_type") or "").strip()
                if payoff_type:
                    recent_payoff_types.append(payoff_type)
                if payoff_type or previous_contract.get("payoff_intensity"):
                    # P1-1 质量整改：精简payoff_history，只保留类型和强度
                    recent_payoff_history.append({
                        "chapter_number": previous_chapter.get("chapter_number"),
                        "payoff_type": payoff_type,
                        "payoff_intensity": previous_contract.get("payoff_intensity") or "small",
                    })

        layers = {
            "story_state": {
                "total": overview.get("states", {}).get("total", 0),
                "pending_review": overview.get("states", {}).get("pending_review", 0),
            },
            "characters": [
                {"key": s["key"], "value": s["value"], "confidence": s["confidence"]}
                for s in character_states
            ],
            "world": [
                {"key": s["key"], "value": s["value"], "confidence": s["confidence"]}
                for s in world_states
            ],
            "plot": [
                {"key": s["key"], "value": s["value"], "confidence": s["confidence"]}
                for s in plot_states
            ],
            "global": [
                {"key": s["key"], "value": s["value"]} for s in global_states
            ],
            "active_goals": [
                {
                    "name": g.get("name"),
                    "description": g.get("description"),
                    "progress": g.get("progress") or 0.0,
                    "status": g.get("status"),
                    "target_chapter": g.get("target_chapter"),
                }
                for g in active_goals
            ],
            "constraints": [
                {
                    "name": c.get("name"),
                    "description": c.get("description"),
                    "severity": c.get("severity"),
                    "type": c.get("type"),
                }
                for c in constraints
            ],
            "recap": recap_parts,
            "previous_tail": last_tail,
            # Kept out of the rendered prompt; used only by the deterministic
            # mirror detector after the new chapter is generated.
            "previous_full_text": previous[-1].get("text") if previous else "",
            "previous_transition_contract": previous_transition_contract,
            # Carry the last accepted workflow's knowledge boundary forward;
            # a scene plan must not let a character react to a fact that was
            # not yet known at the start of this chapter.
            "current_time": previous_current_state.get("time"),
            "current_location": previous_current_state.get("location"),
            "known_facts": previous_current_state.get("knowledge") or legacy_known_facts,
            "objects": previous_current_state.get("objects") or [],
            "resources": previous_current_state.get("resources") or [],
            "relationships": previous_current_state.get("relationships") or [],
            "opening_history": opening_history,
            "recent_payoff_types": recent_payoff_types[-8:],
            "recent_payoff_history": recent_payoff_history[-5:],  # P1-1 质量整改：从20章降到5章
            "style_card": style_card,
            "active_rules": active_rules,
            "quality_learning": quality_learning,
            "genre": genre_context,  # 第8层：品类上下文
        }

        rendered = self.render(layers)
        # Rough token budgeting: ~1.6 chars per token for mixed zh/en text.
        max_chars = int(token_budget * 1.6)
        truncated = False
        if len(rendered) > max_chars:
            rendered = self._fit_context(layers, max_chars)
            truncated = True

        return {
            "chapter_number": chapter_number,
            "scene_type": scene_type,
            "token_budget": token_budget,
            "context_layers": layers,
            "rendered_context": rendered,
            "rendered_chars": len(rendered),
            "truncated": truncated,
            "previous_chapters": [p.get("chapter_number") for p in previous],
            "previous_titles": [p.get("title") for p in previous if p.get("title")],
        }

    @staticmethod
    def render(layers: dict[str, Any]) -> str:
        """Render context layers into a prompt-ready block."""
        blocks: list[str] = []

        def fmt_state(items: list[dict[str, Any]], title: str) -> None:
            if not items:
                return
            lines = []
            for it in items[:20]:
                value = it.get("value")
                if isinstance(value, dict):
                    text = value.get("summary") or value.get("description") or json.dumps(
                        value, ensure_ascii=False
                    )
                else:
                    text = str(value)
                lines.append(f"- {it['key']}: {text}")
            blocks.append(f"【{title}】\n" + "\n".join(lines))

        fmt_state(layers.get("characters", []), "人物状态")
        fmt_state(layers.get("world", []), "世界设定")
        fmt_state(layers.get("plot", []), "情节状态")

        goals = layers.get("active_goals", [])
        if goals:
            lines = [
                f"- {g.get('name')}（进度 {float(g.get('progress') or 0) * 100:.0f}%"
                + (f"，目标第{g['target_chapter']}章" if g.get("target_chapter") else "")
                + f"）：{g.get('description') or ''}"
                for g in goals[:15]
            ]
            blocks.append("【当前故事目标】\n" + "\n".join(lines))

        constraints = layers.get("constraints", [])
        if constraints:
            lines = [
                f"- [{c.get('severity')}] {c.get('name')}：{c.get('description') or ''}"
                for c in constraints[:20]
            ]
            blocks.append("【必须遵守的约束】\n" + "\n".join(lines))

        transition = layers.get("previous_transition_contract")
        if transition:
            blocks.append(
                "【上一章交接契约（必须优先遵守）】\n"
                + json.dumps(transition, ensure_ascii=False, separators=(",", ":"))
            )

        style_card = layers.get("style_card")
        if style_card:
            prompt_style_card = sanitise_style_card_for_prompt(style_card)
            blocks.append(
                "【V6作者风格卡（只约束表达，不改变剧情事实）】\n"
                + json.dumps(prompt_style_card, ensure_ascii=False, separators=(",", ":"))
            )

        # 品类风格卡与约束（第8层）
        genre = layers.get("genre") or {}
        if genre:
            genre_style = genre.get("style_card") or {}
            if genre_style:
                blocks.append(
                    "【品类风格卡（品类专属写作风格）】\n"
                    + json.dumps(
                        sanitise_style_card_for_prompt(genre_style),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )

            genre_constraints = genre.get("constraints") or []
            if genre_constraints:
                lines = []
                for c in genre_constraints[:10]:
                    desc = c.get("description") or c.get("key") or ""
                    severity = c.get("severity", "info")
                    lines.append(f"- [{severity}] {desc}")
                blocks.append("【品类约束（必须遵守）】\n" + "\n".join(lines))

            genre_knowledge = genre.get("knowledge") or {}
            if genre_knowledge:
                # 只渲染最重要的知识（参考类和世界观类）
                important_types = ["reference", "world_setting", "character"]
                knowledge_lines = []
                for ktype in important_types:
                    items = genre_knowledge.get(ktype, [])
                    if items:
                        knowledge_lines.append(f"## {ktype}")
                        for item in items[:3]:  # 每种类型最多3条
                            title = item.get("title", "")
                            content = item.get("content", "")[:200]  # 截断长内容
                            knowledge_lines.append(f"- {title}: {content}...")
                if knowledge_lines:
                    blocks.append("【品类知识库（写作参考）】\n" + "\n".join(knowledge_lines))

        active_rules = layers.get("active_rules") or []
        if active_rules:
            blocks.append(
                "【已验证的低风险写作规则（只处理表达，不改变剧情）】\n"
                + "\n".join(
                    f"- {item.get('instruction') or item.get('code')}"
                    for item in active_rules[:12]
                )
            )

        web_research = layers.get("web_research") or {}
        research_guidance = render_web_research_guidance(web_research)
        if research_guidance:
            blocks.append("【实时网感灵感卡（仅供原创灵感）】\n" + research_guidance)

        quality_learning = layers.get("quality_learning") or []
        if quality_learning:
            blocks.append(
                "【本项目正负样本学习提示（达到灰度门槛后才会出现）】\n"
                + "\n".join(
                    f"- {item.get('instruction')}（样本{item.get('sample_count', 0)}，"
                    f"正向率{float(item.get('positive_rate') or 0) * 100:.0f}%）"
                    for item in quality_learning[:2]  # P1-1 质量整改：从4条降到2条
                    if isinstance(item, dict) and item.get("instruction")
                )
            )

        recap = layers.get("recap", [])
        if recap:
            blocks.append("【前情提要】\n" + "\n".join(recap))

        tail = layers.get("previous_tail")
        if tail:
            blocks.append("【上一章结尾原文（用于承接）】\n" + tail)

        opening_history = layers.get("opening_history") or []
        if opening_history:
            blocks.append(
                "【最近章节开场类型（只用于避免模板重复）】\n"
                + "、".join(
                    f"第{item.get('chapter_number')}章:{item.get('mode')}"
                    for item in opening_history[-3:]
                )
            )

        return "\n\n".join(blocks) if blocks else "（暂无历史上下文，这是故事的开端）"

    @classmethod
    def _fit_context(cls, layers: dict[str, Any], max_chars: int) -> str:
        """Fit context without dropping the cross-chapter anchors.

        State inventories are compressible; the previous tail, hand-off
        contract, constraints and recap are not.  The previous implementation
        sliced the fully-rendered prompt from character zero, which commonly
        removed exactly those anchors.
        """
        anchor_layers = {
            "active_goals": layers.get("active_goals", []),
            "constraints": layers.get("constraints", []),
            "recap": layers.get("recap", []),
            "previous_transition_contract": layers.get("previous_transition_contract", {}),
            "previous_tail": layers.get("previous_tail", ""),
            "style_card": layers.get("style_card", {}),
            "active_rules": layers.get("active_rules", []),
            "quality_learning": layers.get("quality_learning", []),
            "web_research": layers.get("web_research", {}),
        }
        anchor = cls.render(anchor_layers)
        state_blob = cls.render(
            {
                "characters": layers.get("characters", [])[:40],
                "world": layers.get("world", [])[:40],
                "plot": layers.get("plot", [])[:40],
            }
        )
        if len(anchor) >= max_chars:
            # Keep the literal tail and contract even if a pathological story
            # has an enormous constraint list.
            tail = str(layers.get("previous_tail") or "")[-1200:]
            contract = json.dumps(
                layers.get("previous_transition_contract") or {},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            required = "【上一章交接契约】\n" + contract + "\n\n【上一章结尾原文】\n" + tail
            return required[-max_chars:]
        available = max_chars - len(anchor) - 2
        if available <= 0:
            return anchor
        if len(state_blob) > available:
            state_blob = state_blob[:available] + "\n【设定状态其余条目见 Novel Brain，禁止据此改写已确认事实】"
        return state_blob + "\n\n" + anchor


class SceneDirector:
    """Plans the chapter beat sheet with a real AI call."""

    def __init__(self, brain: NovelBrain, gateway: "AIGateway"):
        self.brain = brain
        self.gateway = gateway

    @staticmethod
    def validate_scene_plan_contract(
        plan: Any,
        *,
        target_word_count: int,
    ) -> dict[str, Any]:
        """Fail closed when the planner does not return a usable beat sheet.

        The writer can produce fluent prose from a malformed plan, but it
        cannot reliably preserve a payoff arc or chapter objective in that
        case.  Treating ``beats: []`` as a valid plan was one of the ways a
        weak provider response reached the prose stage.
        """
        if not isinstance(plan, dict):
            raise AIGatewayError("scene plan contract invalid: expected an object")
        title = str(plan.get("chapter_title") or "").strip()
        if not title:
            raise AIGatewayError("scene plan contract invalid: chapter_title is empty")
        beats = plan.get("beats")
        if not isinstance(beats, list) or not 4 <= len(beats) <= 6:
            raise AIGatewayError(
                "scene plan contract invalid: beats must contain 4-6 items"
            )
        phases: set[str] = set()
        beat_errors: list[str] = []
        for index, beat in enumerate(beats, start=1):
            if not isinstance(beat, dict):
                beat_errors.append(f"beat_{index}_not_object")
                continue
            if not str(beat.get("name") or "").strip():
                beat_errors.append(f"beat_{index}_name_missing")
            if not str(beat.get("content") or "").strip():
                beat_errors.append(f"beat_{index}_content_missing")
            try:
                words = int(beat.get("target_words") or 0)
            except (TypeError, ValueError):
                words = 0
            if words <= 0:
                beat_errors.append(f"beat_{index}_target_words_invalid")
            if target_word_count >= 1800:
                scene_card = beat.get("scene_card") or beat.get("scene")
                if not isinstance(scene_card, dict):
                    beat_errors.append(f"beat_{index}_scene_card_missing")
                else:
                    required_scene_fields = (
                        "location",
                        "time",
                        "goal",
                        "obstacle",
                        "choice",
                        "turn",
                        "state_change",
                        "knowledge_boundary",
                        "handoff",
                        "trigger",
                        "causal_link",
                    )
                    missing_scene_fields = [
                        field
                        for field in required_scene_fields
                        if not str(scene_card.get(field) or "").strip()
                    ]
                    if missing_scene_fields:
                        beat_errors.append(
                            f"beat_{index}_scene_card_fields_missing:"
                            + ",".join(missing_scene_fields[:4])
                        )
            phase_values = beat.get("payoff_phases")
            if not isinstance(phase_values, list):
                phase_values = [beat.get("payoff_phase")]
            phases.update(
                str(value).strip().lower()
                for value in phase_values
                if str(value or "").strip()
            )
        if beat_errors:
            raise AIGatewayError(
                "scene plan contract invalid: " + ", ".join(beat_errors[:8])
            )
        required_phases = {"pressure", "build", "burst", "feedback", "aftershock"}
        missing_phases = sorted(required_phases - phases)
        if missing_phases:
            raise AIGatewayError(
                "scene plan contract invalid: missing payoff phases "
                + ", ".join(missing_phases)
            )
        if target_word_count >= 1800:
            pacing_text = str(plan.get("pacing") or "")
            if re.search(r"中段[^。！？!?]{0,20}(?:放缓|舒缓|缓冲|放松)", pacing_text):
                raise AIGatewayError(
                    "scene plan contract invalid: pacing_interlude_without_mainline_turn"
                )
        chapter_type = str(plan.get("chapter_type") or "").strip().lower()
        if chapter_type not in {"normal", "aftermath", "relationship", "suspense"}:
            raise AIGatewayError(
                "scene plan contract invalid: chapter_type is unsupported"
            )
        planned_words = sum(
            int(beat.get("target_words") or 0)
            for beat in beats
            if isinstance(beat, dict)
        )
        target = max(1, int(target_word_count or 0))
        if planned_words < int(target * 0.35) or planned_words > int(target * 2.0):
            raise AIGatewayError(
                "scene plan contract invalid: beat targets are outside the safe range"
            )
        return {
            "passed": True,
            "beat_count": len(beats),
            "planned_words": planned_words,
            "payoff_phases": sorted(phases),
        }

    @staticmethod
    def _ensure_prose_texture_plan(
        plan: dict[str, Any],
        *,
        chapter_number: int,
    ) -> dict[str, Any]:
        """Carry an executable prose texture into the writer prompt.

        A planner may omit the optional texture object even when the chapter
        contract is valid.  Leaving that omission silent makes the prose call
        fall back to a generic, highly regular model cadence.  Fill only the
        writing instructions that are safe to derive from the chapter shape;
        this is not an author imitation or a post-write rewrite.
        """
        source = plan.get("prose_texture_plan")
        texture = dict(source) if isinstance(source, dict) else {}
        modes = (
            {
                "narrator_bias": "限知只写视角人物当下能确认的细节，原因只露出一半，先让动作留下问题。",
                "sensory_anchor": "手边物件和触感：让一个具体物件在选择前后发生可见变化。",
                "subtext": "人物不正面回答关键问题，先改拿物件、避开视线或把话说到一半。",
                "rhythm": "连续动作使用短句，观察和犹豫允许长句；转折后不追加解释性总结。",
            },
            {
                "narrator_bias": "限知只跟随人物当前的误判与修正，不替读者提前宣布真相。",
                "sensory_anchor": "一个反复出现但会改变意义的声音或光线，随现场结果变化。",
                "subtext": "人物用看似日常的话掩住真实目的，让对话和动作互相打架。",
                "rhythm": "对白短、动作断、信息隔段落露出；允许一处回答落空，不用整齐排比补齐。",
            },
            {
                "narrator_bias": "只挑人物会在此刻注意到的偏差，不做全景式环境清单。",
                "sensory_anchor": "一个有磨损、重量或温度的物件，作为人物判断的依据而非装饰。",
                "subtext": "人物口头答应，身体却先做出保留、试探或准备退路的动作。",
                "rhythm": "信息密集处打断句群，结果出现后留一点余波，不把每段写成完整闭环。",
            },
            {
                "narrator_bias": "限知跟着人物最在意的风险走，暂不解释与当前选择无关的设定。",
                "sensory_anchor": "现场留下的一处具体痕迹，前后两次出现时含义必须发生变化。",
                "subtext": "人物通过反问、错开话题或临时改动作表达拒绝，不由旁白代说情绪。",
                "rhythm": "起笔方式跟随现场而变，不按动作/环境/对白固定轮换；高潮处收短，余波处放慢。",
            },
        )[max(0, chapter_number - 1) % 4]
        defaults = {
            "information_delivery": "动作、对白、物件和他人反馈交替承载信息，解释延后到人物需要作选择时",
            "voice_anchor": "人物先做选择再说话；不同人物的避答、用词和反应不能互换",
            **modes,
        }
        normalised = {
            key: str(texture.get(key) or defaults[key]).strip()[:320]
            for key in defaults
        }
        normalised["source"] = "provider" if source else "deterministic_texture_scheduler"
        plan["prose_texture_plan"] = normalised
        return plan

    @staticmethod
    def _repair_generation_phase_labels(plan: Any) -> dict[str, Any] | None:
        """Repair only a provable phase-label omission before prose generation.

        A real Provider can return a coherent five-beat story while omitting
        one enum label.  The previous flow paid for a second Provider repair
        and still failed when that label was omitted again.  This helper does
        not invent plot facts or prose: it adds ``aftershock`` only when the
        final beat already has a concrete content/hook/next-pressure anchor.
        Any other missing phase remains fail-closed and still requires a real
        Provider repair.
        """
        if not isinstance(plan, dict) or not isinstance(plan.get("beats"), list):
            return None
        required = {"pressure", "build", "burst", "feedback", "aftershock"}
        phases: set[str] = set()
        for beat in plan["beats"]:
            if not isinstance(beat, dict):
                return None
            values = beat.get("payoff_phases")
            if not isinstance(values, list):
                values = [beat.get("payoff_phase")]
            phases.update(
                str(value).strip().lower()
                for value in values
                if str(value or "").strip()
            )
        missing = required - phases
        if missing != {"aftershock"}:
            return None
        final_beat = plan["beats"][-1]
        if not isinstance(final_beat, dict) or not str(final_beat.get("content") or "").strip():
            return None
        payoff_contract = plan.get("payoff_contract") or {}
        chapter_contract = plan.get("chapter_contract") or {}
        has_next_anchor = any(
            str(value or "").strip()
            for value in (
                plan.get("hook"),
                payoff_contract.get("next_pressure") if isinstance(payoff_contract, dict) else "",
                chapter_contract.get("next_inevitable_event")
                if isinstance(chapter_contract, dict)
                else "",
                (final_beat.get("scene_card") or {}).get("handoff")
                if isinstance(final_beat.get("scene_card"), dict)
                else "",
            )
        )
        if not has_next_anchor:
            return None
        repaired = dict(plan)
        repaired["beats"] = [dict(beat) for beat in plan["beats"]]
        final_copy = repaired["beats"][-1]
        phase_values = final_copy.get("payoff_phases")
        if not isinstance(phase_values, list):
            phase_values = [final_copy.get("payoff_phase")] if final_copy.get("payoff_phase") else []
        if "aftershock" not in phase_values:
            phase_values.append("aftershock")
        final_copy["payoff_phases"] = phase_values
        repaired["generation_phase_repair"] = {
            "applied": ["aftershock"],
            "reason": "final beat already carries a concrete chapter-end hook or next-pressure anchor",
        }
        return repaired

    async def _repair_payoff_strength_before_prose(
        self,
        plan: dict[str, Any],
        *,
        chapter_number: int,
        quality_profile: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Repair an underpowered payoff plan before paying for prose.

        The previous flow only checked the early-chapter payoff floor after
        the full chapter had been generated.  A provider could therefore
        return a coherent but merely ``high`` plan for a profile requiring
        ``peak`` and the run would waste a prose call before being rejected.
        This bounded real-provider repair changes only the plan's payoff
        contract and beat execution.  It never upgrades a label locally: the
        repaired contract must still contain the visible result, feedback,
        active choice and five-phase beat structure before prose begins.
        """
        profile = quality_profile if isinstance(quality_profile, dict) else {}
        source_contract = plan.get("payoff_contract")
        # A legacy/fixture plan without an explicit payoff contract is still
        # allowed to reach the existing chapter-level contract gate.  The
        # pre-generation repair is reserved for a real plan that declared a
        # payoff but undershot the active floor; otherwise structural-plan
        # tests and legacy plot briefs would unexpectedly spend a second AI
        # call for fields they never attempted to provide.
        if not isinstance(source_contract, dict) or not source_contract:
            return dict(plan), {}
        contract = build_payoff_contract(
            source_contract,
            chapter_number=chapter_number,
            profile=profile,
            recent_types=[],
            chapter_function=plan,
        )
        validation = validate_payoff_contract(
            contract,
            profile=profile,
            required=True,
            chapter_function=plan,
        )
        plan = dict(plan)
        plan["payoff_contract"] = contract
        plan["payoff_validation"] = validation
        if validation.get("strength_passed", True):
            return plan, {}
        if self.gateway is None:
            raise AIGatewayError(
                "payoff plan is below the generation floor and repair gateway is not configured"
            )

        repair_prompt = (
            "下面是一个尚未进入正文生成的章节场景计划。它的爽点契约没有达到当前题材/平台的生成前最低档位。"
            "请只输出 JSON，不要输出正文、解释或评价。\n\n"
            "修复目标：把可见的事件兑现、人物主动选择、外部反馈和章末新压力写成同一条可执行动作链；"
            "不是把 payoff_intensity 字段从 high 改成 peak。爆发必须在 beats 中有具体动作和结果，"
            "反馈必须落到对手、组织、资源、规则或现场状态的可见变化，余波必须改变下一步选择。"
            "只能使用当前计划已经提供的剧情事实、人物、地点、规则和物件，不得新增世界观、角色、能力、奖励或事件；"
            "如果现有事实确实无法形成最低档位，返回 repairable=false，禁止硬填 peak。\n\n"
            f"章节号：{chapter_number}\n"
            f"当前最低爽点档位：{validation.get('minimum_intensity') or '未指定'}\n"
            f"当前校验问题：{json.dumps(validation.get('strength_issues') or [], ensure_ascii=False)}\n"
            "返回格式："
            "{\"repairable\":true,\"payoff_contract\":{\"reader_promise\":\"...\","
            "\"pressure\":\"...\",\"active_choice\":\"...\",\"payoff_type\":\"...\","
            "\"visible_result\":\"...\",\"payoff_feedback\":\"...\",\"payoff_intensity\":\"peak\","
            "\"payoff_arc\":[\"pressure\",\"build\",\"burst\",\"feedback\",\"aftershock\"],"
            "\"cost\":\"...\",\"next_pressure\":\"...\"},"
            "\"beats\":[...]}。beats 必须保留 4-6 个，保留原有节拍事实并补齐可见动作；每个 beat 必须包含原计划要求的 scene_card。\n\n"
            f"当前爽点契约：{json.dumps(contract, ensure_ascii=False)}\n"
            f"当前节拍：{json.dumps(plan.get('beats') or [], ensure_ascii=False)}\n"
            f"当前章节目标/冲突/钩子：{json.dumps({key: plan.get(key) for key in ('scene_goal', 'conflict', 'hook', 'reader_promise')}, ensure_ascii=False)}"
        )
        repaired = await self.gateway.generate_json(
            repair_prompt,
            system_prompt=(
                "你是严格的商业网文场景计划修复器，只输出合法 JSON。"
                "不得用改标签冒充提升爽点，不得新增当前计划没有的剧情事实。"
            ),
            max_tokens=3600,
            temperature=0.2,
            prompt_name="v7.generation.payoff_plan.repair",
            prompt_version="1.0.0",
        )
        usage = dict(repaired.get("usage") or {})
        data = repaired.get("data")
        if not isinstance(data, dict) or data.get("repairable") is False:
            reason = str((data or {}).get("reason") or "Provider refused payoff repair") if isinstance(data, dict) else "invalid repair response"
            raise AIGatewayError(f"payoff plan repair failed: {reason}")
        repaired_contract = build_payoff_contract(
            data.get("payoff_contract") or {},
            chapter_number=chapter_number,
            profile=profile,
            recent_types=[],
            chapter_function=plan,
        )
        repaired_beats = data.get("beats")
        if not isinstance(repaired_beats, list):
            raise AIGatewayError("payoff plan repair failed: beats are missing")
        candidate = dict(plan)
        candidate["payoff_contract"] = repaired_contract
        candidate["beats"] = repaired_beats
        self.validate_scene_plan_contract(candidate, target_word_count=int(plan.get("target_word_count") or 3000))
        repaired_validation = validate_payoff_contract(
            repaired_contract,
            profile=profile,
            required=True,
            chapter_function=candidate,
        )
        if not repaired_validation.get("passed") or not repaired_validation.get("strength_passed"):
            raise AIGatewayError(
                "payoff plan repair failed: repaired contract still misses "
                + ";".join(repaired_validation.get("missing") or repaired_validation.get("strength_issues") or ["required strength"])
            )
        candidate["payoff_validation"] = repaired_validation
        candidate["generation_payoff_repair"] = {
            "applied": True,
            "source": "real_provider_pre_generation_repair",
            "before": validation,
            "after": repaired_validation,
        }
        return candidate, usage

    @staticmethod
    def _adopt_plot_brief(
        chapter_number: int,
        target_word_count: int,
        plot_brief: dict[str, Any] | None,
        previous_titles: list[Any] | None = None,
    ) -> dict[str, Any] | None:
        """Turn a plot-engine brief into a beat sheet, or None if unusable."""
        if not plot_brief:
            return None
        beats = plot_brief.get("suggested_beats") or []
        if len(beats) < 4:
            return None

        normalised: list[dict[str, Any]] = []
        for beat in beats:
            if not isinstance(beat, dict) or not beat.get("name"):
                return None
            normalised.append({
                "name": str(beat.get("name"))[:80],
                "purpose": str(beat.get("purpose") or beat.get("emotion") or "")[:120],
                "content": str(beat.get("content") or "")[:600],
                "emotion": beat.get("emotion"),
                "target_words": int(beat.get("target_words") or 0),
                # PlotEngine emits these labels as part of the commercial
                # beat contract. Preserve them when adopting the brief;
                # dropping them here made the downstream writer infer a
                # different arc and could erase the build phase.
                "payoff_phase": beat.get("payoff_phase"),
                "payoff_phases": beat.get("payoff_phases"),
                "scene_card": beat.get("scene_card") or beat.get("scene") or {},
            })

        planned = sum(b["target_words"] for b in normalised)
        if planned <= 0:
            share = target_word_count // len(normalised)
            for b in normalised:
                b["target_words"] = share
        elif (
            target_word_count >= 1800
            and abs(planned - target_word_count) > target_word_count * 0.10
        ):
            # Rescale rather than discard: the shape is useful, but a provider
            # plan that drifts materially from the reader budget must not let
            # early scenes consume the final scene's space.  Deliberately
            # short synthetic targets stay untouched for focused unit tests.
            factor = target_word_count / planned
            for b in normalised:
                b["target_words"] = max(200, int(b["target_words"] * factor))

        objectives = plot_brief.get("must_accomplish") or []
        chapter_title = ensure_unique_chapter_title(
            plot_brief.get("chapter_title_hint") or f"第{chapter_number}章",
            previous_titles=previous_titles,
            chapter_number=chapter_number,
            hints=[
                plot_brief.get("hook"),
                (plot_brief.get("payoff_contract") or {}).get("visible_result"),
                *[beat.get("name") for beat in normalised],
            ],
        )
        return {
            "chapter_number": chapter_number,
            "chapter_title": chapter_title,
            "scene_goal": plot_brief.get("tension_target")
            or (objectives[0] if objectives else ""),
            "beats": normalised,
            "pov_character": plot_brief.get("pov_character"),
            "pov_policy": "third_person_narrative",
            "pacing": plot_brief.get("pacing_advice"),
            "conflict": plot_brief.get("tension_target"),
            "hook": plot_brief.get("hook"),
            "reader_promise": plot_brief.get("reader_promise"),
            "reader_experience_plan": plot_brief.get("reader_experience_plan") or {},
            "prose_texture_plan": plot_brief.get("prose_texture_plan") or {},
            "payoff_contract": plot_brief.get("payoff_contract") or {},
            "chapter_contract": plot_brief.get("chapter_contract") or {},
            "causal_ledger": plot_brief.get("causal_ledger") or [],
            "state_delta": plot_brief.get("state_delta") or {},
            "writing_workflow": plot_brief.get("writing_workflow") or {},
            "chapter_type": plot_brief.get("chapter_type") or plot_brief.get("chapter_mode") or "normal",
            "emotional_target": plot_brief.get("emotional_target"),
            "opening_anchor": plot_brief.get("opening_anchor"),
            "risks": plot_brief.get("risks") or [],
            "must_accomplish": objectives,
            "target_word_count": target_word_count,
            "source": "plot_engine_brief",
            "_usage": {"tokens_input": 0, "tokens_output": 0, "cost": 0.0, "model": None},
        }

    async def plan_scene(
        self,
        chapter_number: int,
        context: dict[str, Any],
        *,
        outline: str | None = None,
        target_word_count: int = 3000,
        plot_brief: dict[str, Any] | None = None,
        quality_profile: dict[str, Any] | None = None,
        previous_titles: list[Any] | None = None,
        opening_plan: dict[str, Any] | None = None,
        readability_plan: dict[str, Any] | None = None,
        writing_workflow: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Produce a beat sheet. Returns dict including `_usage` for accounting.

        When the Story Director already obtained a usable beat sheet from the
        plot engine's assessment pass, it is adopted directly instead of paying
        for a second planning call that could contradict the first.
        """
        context_layers = context.get("context_layers") or {}
        writing_workflow = writing_workflow or context_layers.get("writing_workflow") or {}
        chapter_type_hint = (plot_brief or {}).get("chapter_type") if isinstance(plot_brief, dict) else None
        effective_opening_plan = opening_plan or select_opening_plan(
            chapter_number,
            chapter_type=chapter_type_hint,
            previous_history=context_layers.get("opening_history") or [],
            plot_brief=plot_brief,
        )
        readability_plan = readability_plan or build_readability_plan(
            chapter_number,
            chapter_type=chapter_type_hint,
            plot_brief=plot_brief,
            quality_profile=quality_profile,
            opening_plan=effective_opening_plan,
            style_card=context_layers.get("style_card") or {},
            recent_history=context_layers.get("readability_history") or [],
        )

        adopted = self._adopt_plot_brief(
            chapter_number,
            target_word_count,
            plot_brief,
            previous_titles=previous_titles,
        )
        if adopted is not None:
            try:
                self.validate_scene_plan_contract(
                    adopted,
                    target_word_count=target_word_count,
                )
            except AIGatewayError:
                # Plot assessment and scene planning are separate Provider
                # contracts. If the assessment has usable story content but
                # incomplete commercial phase labels, let the scene planner
                # rebuild a complete beat sheet instead of failing the whole
                # chapter before the repair-capable planning path runs.
                adopted = None
            if adopted is not None:
                adopted["opening_plan"] = effective_opening_plan
                adopted["readability_plan"] = readability_plan
                adopted["writing_workflow"] = writing_workflow
                adopted, payoff_usage = await self._repair_payoff_strength_before_prose(
                    adopted,
                    chapter_number=chapter_number,
                    quality_profile=quality_profile,
                )
                adopted_usage = dict(adopted.get("_usage") or {})
                for key in ("tokens_input", "tokens_output"):
                    adopted_usage[key] = int(adopted_usage.get(key) or 0) + int(payoff_usage.get(key) or 0)
                adopted_usage["cost"] = float(adopted_usage.get("cost") or 0.0) + float(payoff_usage.get("cost") or 0.0)
                adopted_usage["model"] = payoff_usage.get("model") or adopted_usage.get("model")
                adopted["_usage"] = adopted_usage
                return adopted

        brief_block = ""
        if plot_brief:
            objectives = plot_brief.get("must_accomplish") or []
            if objectives:
                brief_block = (
                    "\n【结构编辑给出的本章目标】\n"
                    + "\n".join(f"- {o}" for o in objectives[:6])
                    + f"\n张力目标：{plot_brief.get('tension_target') or '未指定'}"
                    + f"\n节奏建议：{plot_brief.get('pacing_advice') or '未指定'}\n"
                )

        quality_directive = compile_quality_directive(
            quality_profile,
            chapter_number=chapter_number,
            chapter_function=plot_brief or {},
            payoff_contract=(plot_brief or {}).get("payoff_contract") if plot_brief else None,
            active_rules=(context.get("context_layers") or {}).get("active_rules") or [],
            opening_plan=effective_opening_plan,
            readability_plan=readability_plan,
        )
        payoff_policy = (quality_profile or {}).get("payoff_policy") or {}
        early_payoff_limit = int(payoff_policy.get("early_chapters_need_payoff") or 0)
        early_payoff_floor = str(
            payoff_policy.get("early_min_payoff_intensity") or "medium"
        )
        payoff_generation_floor = (
            f"【生成前爽点硬约束】本章属于前{early_payoff_limit}章，"
            f"爽点契约和 beats 必须实际执行不低于 {early_payoff_floor} 档的可见兑现；"
            "不能只把 payoff_intensity 字段改成高档，必须在爆发、反馈和余波中写出具体动作与后果。"
            if early_payoff_limit and chapter_number <= early_payoff_limit
            else ""
        )

        # 章节标题番茄化要求（最高优先级）
        title_tomato_requirement = """
【最高优先级：章节标题必须番茄化！】
chapter_title 是本章最重要的门面，必须让读者一眼就想点进去！

✅ 番茄标题五大范式（必须从中选一种）：
1. 冲突型：把最激烈的冲突直接甩在脸上
   例：《你也配叫天才？》《他敢动手？》《全场死寂》《打脸了》
2. 悬念型：抛出一个问题，让读者忍不住想点进去看答案
   例：《门后是什么》《谁来了？》《她居然敢？》《这是什么操作？》
3. 反转型：前后反差巨大，制造强烈期待
   例：《废物竟是大佬》《刚被休就改嫁》《穷小子是神豪》
4. 数字型：用具体数字制造冲击力和真实感
   例：《重生三年，三个崽都是大佬》《签到1095天，我无敌了》
5. 口语型：用大白话、感叹句、反问句，像聊天一样有代入感
   例：《什么？寡妇她又暴富了！》《绝了！这都能翻盘？》

✅ 按题材分类的正面案例（必须参考你写的题材选）：
- 重生年代类：《重生八零，我把前夫踹了》《带着空间回六零》《重生九零：肥妻逆袭》
- 种田基建类：《逃荒路上，我靠囤货养活全村》《开荒第一年，就富甲一方》《寡妇带崽，种田暴富》
- 系统流类：《签到三年，我成了全球大佬》《系统觉醒，百倍返还！》《每天一个随机大礼包》
- 都市脑洞类：《我有一座冒险屋》《我的 cells 都在进化》《全球高武》
- 玄幻升级类：《废柴逆袭，一剑斩神》《开局获得混沌体》《我在异界当帝师》

❌ 绝对禁止的烂标题（写了就是不合格！）：
- 文艺抽象型：《重生之墙》《乱世基建手札》《苏晚的古代生活》《岁月静好》
- 平淡无味型：《初遇》《成长》《转折》《新的开始》《往事》《远方》
- 散文标题型：《雨夜来客》《春风十里》《那年夏天》《时光荏苒》
- 用"之"字连接的：《XX之X》《XX传奇》《XX札记》《XX随笔》
- 抽象概念型：《命运》《抉择》《救赎》《觉醒》《蜕变》

⚠️ 标题硬性要求：
1. 必须 2-12 字，短平快，不能太长
2. 必须包含爽点暗示或冲突钩子，不能平淡
3. 必须让读者一眼就知道这章有好戏看
4. 绝对不能文艺、不能抽象、不能像散文
5. 禁止使用"之墙""手札""札记""随笔""传奇"等文艺词汇
6. 禁止用抽象概念做标题（命运、抉择、救赎等）

【记住】标题是点击率的关键！番茄读者只看标题决定点不点！
═════════════════════════════════════════════════════════════
"""

        # 上一章结尾状态（用于衔接）
        context_layers = context.get("context_layers") or {}
        previous_transition = context_layers.get("previous_transition_contract") or {}
        previous_tail = context_layers.get("previous_tail") or ""
        transition_block = ""
        if previous_tail or previous_transition:
            end_state = previous_transition.get("end_state") or {}
            open_threads = previous_transition.get("open_threads") or []
            next_bridge = previous_transition.get("next_chapter_bridge") or previous_tail[-600:]

            threads_text = ""
            if open_threads:
                threads_text = "\n未解决的线索：\n" + "\n".join(
                    f"- {t.get('summary', '')}" for t in open_threads[:5]
                )

            transition_block = (
                f"【第二优先级：上一章结尾状态 - 必须从这里继续，不得跳跃！】\n"
                f"═════════════════════════════════════════════════════════════\n"
                f"上一章结尾的最后内容（必须直接承接，不得跳场）：\n"
                f"「{next_bridge}」\n"
                f"\n"
                f"上一章梗概：{end_state.get('summary', '')}\n"
                f"{threads_text}\n"
                f"\n"
                f"【硬性要求】本章开头必须直接承接上一章结尾的动作、地点和人物状态！\n"
                f"除非正文明确写出过渡，否则不得突然切换场景、时间或人物位置！\n"
                f"═════════════════════════════════════════════════════════════\n\n"
            )

        opening_block = opening_prompt_block(effective_opening_plan)
        prompt = (
            f"【最高优先级：核心设定与硬性约束 - 必须严格遵守，不得擅自修改！】\n"
            f"═════════════════════════════════════════════════════════════\n"
            f"以下是本书的核心设定，是所有剧情的基础，绝对不能违反！\n"
            f"1. 必须严格遵守以下设定，不得擅自修改任何人物、世界观、金手指等核心要素\n"
            f"2. 所有剧情必须在设定框架内展开，不得出现设定矛盾\n"
            f"3. 如果发现设定有疑问，以本设定为准，不得自行发挥\n"
            f"\n"
            f"{outline or '（无额外设定要求）'}\n"
            f"\n"
            f"【再次强调】以上设定是硬性约束，违反任何一条都属于严重错误！\n"
            f"═════════════════════════════════════════════════════════════\n\n"
            f"{transition_block}"
            f"你是小说的场景导演。请为第 {chapter_number} 章设计场景结构。\n\n"
            f"{context.get('rendered_context', '')}\n\n"
            f"{brief_block}\n"
            f"目标字数：{target_word_count} 字。\n\n"
            f"{title_tomato_requirement}\n"
            f"{render_readability_plan(readability_plan)}\n\n"
            f"{render_writing_methodology_contract(writing_workflow)}\n\n"
            f"【网文质量策略】\n{quality_directive}\n\n"
            f"{payoff_generation_floor}\n\n"
            f"{opening_block}\n\n"
            "请只输出 JSON，格式：\n"
            "{\n"
            '  "chapter_title": "本章标题",\n'
            '  "scene_goal": "本章要达成的叙事目的",\n'
            '  "chapter_type": "normal|aftermath|relationship|suspense",\n'
            '  "beats": [{"name":"节拍名","purpose":"作用","content":"要写什么",'
            '"emotion":"情绪","target_words":800,"payoff_phase":"pressure|build|burst|feedback|aftershock",'
            '"scene_card":{"location":"地点","time":"时间","characters":["在场人物"],'
            '"goal":"本场目标","obstacle":"本场阻碍","choice":"人物选择",'
            '"turn":"本场转折","state_change":"明确状态变化",'
            '"knowledge_boundary":"人物此时能知道/不能知道什么",'
            '"handoff":"下一场可直接承接的落点",'
            '"trigger":"触发本场关键事件的可见前提",'
            '"causal_link":"前因如何在正文中导致本场结果"}}],\n'
            '  "pov_character": "视角人物",\n'
            '  "pov_policy": "third_person_narrative",\n'
            '  "pacing": "slow|medium|fast",\n'
            '  "conflict": "本章核心冲突",\n'
            '  "hook": "章末钩子",\n'
            '  "reader_promise": "读者在本章应获得的情绪/信息承诺",\n'
            '  "emotional_target": "开场情绪 -> 中段转折 -> 章末情绪",\n'
            '  "reader_experience_plan": {"reader_emotion":"本章读者情绪","information_to_feel":"读者要现场感受到的变化","scene_payoff":"本章兑现","avoid":["同构写法"]},\n'
            '  "prose_texture_plan": {"information_delivery":"动作/对白/物件/反馈","rhythm":"句段节奏","voice_anchor":"人物声音抓手"},\n'
            '  "opening_mode": "action|dialogue|object|external_event|environment|body_sensation",\n'
            '  "opening_anchor": "与上一章尾部衔接的具体动作/地点/未决问题",\n'
            '  "payoff_contract": {"reader_promise":"读者本章要等什么","pressure":"当前压力",'
            '"active_choice":"主角主动选择","payoff_type":"兑现类型",'
            '"visible_result":"正文中必须出现的可见结果","payoff_feedback":"对手/组织/资源/规则/旁观者的可见反馈",'
            '"payoff_intensity":"small|medium|high|peak","payoff_arc":["pressure","build","burst","feedback","aftershock"],'
            '"witness_reaction":"可选的具体他人反应","cost":"代价或余波",'
            '"next_pressure":"章末新增压力","setup_refs":[]},\n'
            '  "payoff_phases": ["pressure", "build", "burst", "feedback", "aftershock"],\n'
            '  "chapter_contract": {"core_problem":"本章核心问题","observable_payoff":"可见兑现","cost":"代价/余波","next_inevitable_event":"下一必然压力"},\n'
            '  "causal_ledger": [{"event":"事件","trigger":"可见前提","knower":"知情边界",'
            '"motive":"为什么现在","visible_link":"读者如何看出前因导致结果",'
            '"cost":"代价","next_effect":"下一影响"}],\n'
            '  "state_delta": {"changed":["变化"],"unchanged":["不变"]},\n'
            '  "confidence": 0.85\n'
            "}\n"
            "beats 数量 4-6 个，各 beat 的 target_words 之和应接近目标字数；每个 beat 必须增加 payoff_phase 或 payoff_phases，"
            "严格覆盖 pressure/build/burst/feedback/aftershock 五个阶段，允许一个 beat 承担两个阶段；"
            "至少有一个 beat 明确写 build（压制后的试探、准备、取舍或蓄力），不能把连续的压力描述冒充 build；"
            "最后一个 beat 必须明确写出 aftershock，并让 content、scene_card.handoff 或 payoff_contract.next_pressure 之一落到具体的章末后果/下一压力。"
            "每个 beat 都必须提供 scene_card：明确地点、时间、在场人物、目标、阻碍、选择、转折、状态变化、"
            "知情边界和下一场承接点；这些字段服务于连续写作，不要写成泛泛的剧情摘要。"
            "因果/动机是硬要求：每个 beat 的 content 和 scene_card.choice 都必须回答人物为什么在此刻行动；"
            "如果重返上一章或前面场景出现过的地点、门、物件或线索，必须写清触发信息→当前目标→主动决定，"
            "禁止只用‘鬼使神差’、‘下意识’、‘不知为何’或‘心血来潮’充当唯一动机。"
            "每个关键异常、开门、封印松动、袭击、修炼变化或新能力都必须写 trigger 和 causal_link："
            "先给读者一个可见前提/征兆，再让人物判断并作出选择，最后让结果明确反馈；"
            "不能让门、敌人、法器或规则无前提突然出现，也不能把碑文、幻象、梦境或他人话语里的数字/年代"
            "直接改写成主角自己的经历，必须保留文字的说话者和归属。"
            "重大袭击、对抗或爆发后的下一 beat，必须先写一个可见的即时后果（伤势、资源损失、环境变化、旁观者反应、"
            "敌我状态变化或短暂喘息）再进入长段解释/师徒对话；不能从战斗结果直接跳到讲设定。"
            "正常章节的中段不得把放缓、舒缓、缓冲当作独立节奏目标；辅助人物求助、闲聊或知识展示必须直接改变主线的风险、资源、关系、位置或线索，"
            "否则压缩为主线动作中的几句，不得占用一个完整节拍。"
            "chapter_type 必须从 normal、aftermath、relationship、suspense 中选择；"
            "输出必须紧凑：每个 beat 的 name/purpose/content/emotion 各不超过 80 字，"
            "causal_ledger 每列不超过 60 字，列表只写本章真正发生的 4-6 个事件；不要重复字段或附加解释。"
            "causal_ledger 的 knower 必须写清具体人物/群体及其知情时点；上一章未确认的事实不得让人物预先知晓。"
            "禁止写成剧情摘要、操作说明或元叙述，禁止出现‘第X章’、‘本章’、"
            "‘主角在……发现……’、‘读者将……’等模板。"
        )
        result = await self.gateway.generate_json(
            prompt,
            system_prompt=(
                "你是资深小说结构编辑，只输出严格合法的 JSON。"
                + third_person_generation_contract()
                + content_generation_contract(quality_profile)
            ),
            # This contract contains the causal ledger, payoff phases and
            # readability plan.  2k completion tokens truncated real DeepSeek
            # plans mid-JSON during long runs; keep the first attempt large
            # enough and let AIGateway's bounded length retry handle outliers.
            max_tokens=4200,
            temperature=0.6,
            prompt_name="v7.generation.scene_plan",
            prompt_version="1.8.0",
        )
        plan = self._repair_generation_phase_labels(result["data"]) or result["data"]
        usage = dict(result.get("usage") or {})
        try:
            self.validate_scene_plan_contract(
                plan,
                target_word_count=target_word_count,
            )
        except AIGatewayError as contract_error:
            # Provider JSON can be syntactically valid while omitting one of
            # the commercial beat phases. Give the same real Provider one
            # bounded repair opportunity; never invent a phase locally or let
            # a malformed plan reach the writer.
            repair_prompt = (
                "下面的场景计划 JSON 没有通过结构契约校验。请只修复结构并输出完整 JSON，"
                "不要写解释，不要改动已经存在的剧情事实。必须保留 4-6 个 beats，"
                "每个 beat 有 name、content、target_words，并通过 payoff_phase 或 payoff_phases"
                "完整覆盖 pressure、build、burst、feedback、aftershock 五阶段；每个 beat 必须有"
                "scene_card，写清地点、时间、人物、目标、阻碍、选择、转折、状态变化、知情边界和承接点；"
                "每个 beat 必须写清人物此刻为什么行动；重返已有地点/物件时必须写触发信息、当前目标和主动决定，"
                "不得用‘鬼使神差’、‘下意识’或‘不知为何’代替动机；重大冲突后先写即时后果或喘息，"
                "再进入解释性对话，不能从结果直接跳到讲设定；"
                "正常章节中段不得以放缓/缓冲为独立目标，辅助互动必须直接改变主线并在本场落到可见结果；"
                "关键异常、开门、封印松动、袭击、修炼变化或新能力必须补充可见前提和因果连接；"
                "碑文、幻象、梦境或他人话语中的数字/年代属于原说话者，不得改成主角自己的经历；"
                "chapter_type 必须是 normal、aftermath、relationship、suspense 之一。\n"
                f"校验错误：{contract_error}\n"
                f"原始计划：{json.dumps(plan, ensure_ascii=False)}\n"
                "只输出修复后的计划 JSON。"
            )
            repaired = await self.gateway.generate_json(
                repair_prompt,
                system_prompt="你是严格的场景计划校对员，只输出合法 JSON。",
                max_tokens=3600,
                temperature=0.0,
                prompt_name="v7.generation.scene_plan.repair",
                prompt_version="1.3.0",
            )
            repair_usage = repaired.get("usage") or {}
            usage = {
                "tokens_input": int(usage.get("tokens_input") or 0)
                + int(repair_usage.get("tokens_input") or 0),
                "tokens_output": int(usage.get("tokens_output") or 0)
                + int(repair_usage.get("tokens_output") or 0),
                "cost": float(usage.get("cost") or 0.0)
                + float(repair_usage.get("cost") or 0.0),
                "model": repair_usage.get("model") or usage.get("model"),
            }
            plan = self._repair_generation_phase_labels(repaired["data"]) or repaired["data"]
            try:
                self.validate_scene_plan_contract(
                    plan,
                    target_word_count=target_word_count,
                )
            except AIGatewayError:
                # Keep the failure closed, but repair a phase omission at the
                # planning boundary when the existing payoff helper can add a
                # bounded structural beat.  This changes no prose or story
                # fact; it gives the real writer an explicit feedback/retort
                # slot instead of paying for repeated provider retries that
                # omit the same enum again.
                phase_repair = repair_payoff_beat_structure(plan.get("beats"))
                if not phase_repair.get("after", {}).get("passed"):
                    raise
                plan = dict(plan)
                plan["beats"] = phase_repair["beats"]
                plan["generation_phase_repair"] = {
                    "applied": list(phase_repair.get("repaired_phases") or []),
                    "source": "deterministic_pre_generation_structure_repair",
                    "before": phase_repair.get("before") or {},
                    "after": phase_repair.get("after") or {},
                }
                self.validate_scene_plan_contract(
                    plan,
                    target_word_count=target_word_count,
                )
        # The planner can choose the focal character, never the product-wide
        # narrative mode.  This prevents a learned/project POV field from
        # silently re-enabling first-person prose.
        plan["pov_policy"] = "third_person_narrative"
        plan["chapter_title"] = ensure_unique_chapter_title(
            plan.get("chapter_title") or f"第{chapter_number}章",
            previous_titles=previous_titles,
            chapter_number=chapter_number,
            hints=[plan.get("hook"), plan.get("scene_goal"), plan.get("conflict")],
        )
        plan["chapter_number"] = chapter_number
        plan["chapter_type"] = str(plan.get("chapter_type") or "normal").strip().lower()
        plan = self._ensure_prose_texture_plan(plan, chapter_number=chapter_number)
        if plan["chapter_type"] != str(readability_plan.get("chapter_type") or "normal"):
            readability_plan = build_readability_plan(
                chapter_number,
                chapter_type=plan["chapter_type"],
                plot_brief={
                    **(plot_brief or {}),
                    "chapter_type": plan["chapter_type"],
                },
                quality_profile=quality_profile,
                opening_plan=effective_opening_plan,
                style_card=context_layers.get("style_card") or {},
                recent_history=context_layers.get("readability_history") or [],
            )
        # The provider may describe the scene, but the application-owned
        # scheduler is the authority for opening diversity.
        plan["opening_plan"] = effective_opening_plan
        plan["readability_plan"] = readability_plan
        plan["target_word_count"] = target_word_count
        self.validate_scene_plan_contract(plan, target_word_count=target_word_count)
        plan, payoff_usage = await self._repair_payoff_strength_before_prose(
            plan,
            chapter_number=chapter_number,
            quality_profile=quality_profile,
        )
        for key in ("tokens_input", "tokens_output"):
            usage[key] = int(usage.get(key) or 0) + int(payoff_usage.get(key) or 0)
        usage["cost"] = float(usage.get("cost") or 0.0) + float(payoff_usage.get("cost") or 0.0)
        usage["model"] = payoff_usage.get("model") or usage.get("model")
        plan["_usage"] = usage
        return plan


class DeAIPipeline:
    """Rule pre-clean plus a real semantic final-humanize pass.

    Regexes can remove a few visible clichés, but they cannot fix cadence,
    voice or the over-explained feeling the quality report identified.  The
    final pass therefore goes through the same real V7 provider gateway as the
    rest of generation.  Provider failure is explicit; the rule pass is not a
    fake success fallback.
    """

    # Layer 1: AI 腔套话
    CLICHE_PATTERNS: list[tuple[str, str]] = [
        (r"值得一提的是[，,]?", ""),
        (r"总而言之[，,]?", ""),
        (r"综上所述[，,]?", ""),
        (r"毫无疑问[，,]?", ""),
        (r"不得不说[，,]?", ""),
        (r"众所周知[，,]?", ""),
        (r"在这个[^，。]{0,8}的世界里[，,]?", ""),
        (r"这一切的一切[，,]?", "这一切"),
        (r"仿佛整个世界都", "像是"),
        (r"心中五味杂陈", "心里说不清是什么滋味"),
        (r"嘴角勾起一抹[^，。]{0,4}的弧度", "嘴角动了动"),
        (r"眼中闪过一丝", "眼里掠过"),
        (r"深深地吸了一口气", "吸了口气"),
        (r"缓缓地", "缓缓"),
        (r"轻轻地", "轻轻"),
        (r"默默地", "默默"),
    ]

    # Layer 5: 半角标点 -> 全角
    PUNCT_MAP = {
        ",": "，",
        ";": "；",
        ":": "：",
        "?": "？",
        "!": "！",
    }
    # Only medium/high signals justify a billable whole-chapter semantic
    # rewrite.  Low-severity observations remain in the metrics and review
    # report, but they are not proof that the prose needs a provider pass.
    # In particular, a deliberate repeated phrase or a small amount of
    # ellipsis is common in web fiction and should not erase the author's
    # voice merely because a detector noticed it.
    # P0-1 质量整改：恢复去AI味语义重写，把之前性能优化删掉的flag加回来
    SEMANTIC_REWRITE_FLAGS = {
        "dash_density",
        "ellipsis_density",          # P0-1: 恢复省略号密度检测
        "uniform_cadence",
        "repeated_paragraph_opening",
        "ai_phrase",
        "repeated_phrase",           # P0-1: 恢复重复短语检测
        "repeated_tic",
        "structural_ai_smell",
    }
    # Low-severity observations remain auditable metrics, but must not trigger
    # a billable whole-chapter provider rewrite on their own.
    SEMANTIC_REWRITE_SEVERITIES = {"medium", "high"}
    DETERMINISTIC_HARD_FLAGS = {
        "dash_density",
        "uniform_cadence",
        "repeated_paragraph_opening",
        "duplicate_paragraph",
        "ai_phrase",
        "repeated_tic",
        "structural_ai_smell",
    }

    def __init__(self, gateway: "AIGateway | None" = None):
        self.gateway = gateway

    @classmethod
    def _deterministic_fallback_gate(
        cls,
        metrics: dict[str, Any],
        message: str,
    ) -> dict[str, Any]:
        """Decide whether a safe rule-only result can stand in for semantic rewrite.

        A malformed/oversized provider candidate is a failed quality step even
        when the deterministic pass produced readable text.  Keeping that text
        is useful for diagnosis and retry, but it must never become a passed
        chapter merely because the fallback looks clean.
        """
        blocking_flags = [
            str(flag.get("code"))
            for flag in (metrics.get("flags") or [])
            if isinstance(flag, dict)
            and str(flag.get("code") or "") in cls.DETERMINISTIC_HARD_FLAGS
            and str(flag.get("severity") or "").lower() in {"medium", "high"}
        ]
        risk_score = int(metrics.get("risk_score") or 0)
        passed = False
        gate = {
            "passed": passed,
            "mode": "deterministic_fallback",
            "message": message,
            "warning": message,
            "risk_score": risk_score,
        }
        gate["code"] = "rewrite_candidate_rejected" if blocking_flags else "semantic_rewrite_unavailable"
        if not passed:
            gate["blocking_flags"] = sorted(set(blocking_flags))
        return gate

    @staticmethod
    def _paragraph_opening_guidance(metrics: dict[str, Any] | None) -> str:
        """Turn a repeated-opening signal into a concrete rewrite instruction."""
        opening = (metrics or {}).get("repeated_paragraph_opening") or {}
        ratio = float(opening.get("ratio") or 0.0)
        if ratio < 0.30 or not opening.get("opening"):
            return ""
        return (
            "【段首结构修复】检测到段落开头重复："
            f"‘{opening.get('opening')}’约占 {ratio:.1%}。"
            "定稿必须保留全部事件、人物和自然分段，把一部分段落改为从动作、环境、物件、对白或他人反应起笔，"
            "将同一人名段首尽量降到全章约四分之一以内；不能删段，也不能把人名全部粗暴替换成‘他/她’。"
        )

    @staticmethod
    def _tic_guidance(metrics: dict[str, Any] | None) -> str:
        """Turn repeated action/response tics into a bounded rewrite instruction."""
        tic = (metrics or {}).get("tic_metrics") or {}
        repeated = bool(tic.get("repeated"))
        phrase = str(tic.get("dominant") or "").strip()
        count = int(tic.get("dominant_count") or 0)
        if not repeated or not phrase:
            return ""
        return (
            "【动作短语去模板】检测到动作/反应短语"
            f"‘{phrase}’出现约 {count} 次。保留必要的沉默或反应，"
            "但至少改写其中一处为具体的视线、手部动作、停顿后的决定、声音变化或环境后果；"
            "不得删掉事件，也不要把它机械替换成另一个单一口头禅。"
        )

    @staticmethod
    def _deterministic_paragraph_opening_repair(
        text: str,
        opening: str,
        *,
        max_ratio: float = 0.25,
    ) -> tuple[str, dict[str, Any]]:
        """Safely reduce repeated named-character paragraph openings.

        A targeted Provider repair is preferred because it can choose a more
        natural action or object lead.  If that small request returns an empty
        or malformed result, however, the whole chapter must not be rejected
        when a lossless local repair is possible.  This fallback only changes
        a few leading occurrences of a specific two-character name to the
        neutral third-person pronoun ``他``; it never merges, deletes, or
        invents paragraphs and it skips quoted dialogue openings.
        """
        source = str(text or "")
        opening = str(opening or "").strip()
        evidence: dict[str, Any] = {
            "mode": "deterministic",
            "opening": opening,
            "applied": False,
            "replaced_count": 0,
            "before_ratio": 0.0,
            "after_ratio": 0.0,
            "reason": "",
        }
        if len(opening) != 2 or opening[0] in "他她它我你":
            evidence["reason"] = "generic_or_unsupported_opening"
            return source, evidence

        segments = re.split(r"(\n+)", source)
        paragraph_indexes = [
            index
            for index in range(0, len(segments), 2)
            if segments[index].strip()
        ]
        total = len(paragraph_indexes)
        if total < 12:
            evidence["reason"] = "too_few_paragraphs"
            return source, evidence

        quote_starts = ('"', "“", "”", "‘", "’", "「", "」", "『", "』")
        matches: list[int] = []
        for index in paragraph_indexes:
            paragraph = segments[index]
            leading = paragraph[: len(paragraph) - len(paragraph.lstrip())]
            body = paragraph[len(leading) :]
            if body.startswith(quote_starts):
                continue
            if body.startswith(opening) and len(body) > len(opening):
                matches.append(index)

        before_count = len(matches)
        evidence["before_count"] = before_count
        evidence["before_ratio"] = round(before_count / total, 4) if total else 0.0
        allowed = max(1, int(total * max_ratio))
        required = max(0, before_count - allowed)
        if required <= 0:
            evidence["after_ratio"] = evidence["before_ratio"]
            evidence["reason"] = "already_within_target"
            return source, evidence

        # Keep the first named opening for clarity and spread replacements
        # through the chapter instead of changing one contiguous block.
        candidates = matches[1:]
        selected_positions: list[int] = []
        if required >= len(candidates):
            selected_positions = list(range(len(candidates)))
        elif required == 1:
            selected_positions = [len(candidates) // 2]
        else:
            for step in range(required):
                position = round(step * (len(candidates) - 1) / (required - 1))
                if position not in selected_positions:
                    selected_positions.append(position)
        selected = {candidates[position] for position in selected_positions}

        replaced = 0
        for index in selected:
            paragraph = segments[index]
            leading = paragraph[: len(paragraph) - len(paragraph.lstrip())]
            body = paragraph[len(leading) :]
            if not body.startswith(opening) or len(body) <= len(opening):
                continue
            segments[index] = leading + "他" + body[len(opening) :]
            replaced += 1

        repaired = "".join(segments)
        after_metrics = analyze_deai_patterns(repaired)
        after_opening = after_metrics.get("repeated_paragraph_opening") or {}
        after_ratio = float(after_opening.get("ratio") or 0.0)
        evidence.update({
            "applied": replaced > 0 and after_ratio < 0.30,
            "replaced_count": replaced,
            "after_count": after_opening.get("count", 0),
            "after_ratio": round(after_ratio, 4),
            "reason": "ok" if replaced > 0 and after_ratio < 0.30 else "target_not_reached",
        })
        return repaired if evidence["applied"] else source, evidence

    async def repair_opening(
        self,
        text: str,
        *,
        chapter_number: int = 1,
        opening_plan: dict[str, Any] | None = None,
        source_facts: str = "",
        quality_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Repair a failed opening contract before the final de-AI pass.

        The repair is intentionally prefix-only: the provider returns one
        replacement paragraph and the application preserves the rest of the
        generated chapter byte-for-byte.  This gives a real model repair
        opportunity without allowing an opening fix to rewrite plot facts or
        silently become a second chapter draft.
        """
        if self.gateway is None:
            raise AIGatewayError("opening repair gateway is not configured")
        plan = opening_plan if isinstance(opening_plan, dict) else {}
        requested = str(plan.get("mode") or "action").strip().lower()
        recent = [str(item) for item in (plan.get("forbidden_recent_modes") or [])]
        paragraphs = text.replace("\r\n", "\n").split("\n\n")
        first_paragraph = paragraphs[0].strip() if paragraphs else ""
        if not first_paragraph:
            raise AIGatewayError("opening repair source paragraph is empty")

        prompt = (
            "只修复正文第一段的开场方式，输出 JSON，不要解释。"
            f"本章指定开场类型：{requested}；最近三章禁止重复：{recent or ['无']}。"
            "第一段必须从指定类型直接起笔，并在前300字内出现具体压力、异常、目标或选择；"
            "禁止身体部位+疼痛/发闷/一阵袭来/‘像有人’作为默认开头。"
            "必须保留原段中的人物、地点、时间、物件、事件、因果和信息，不得新增剧情、删事实或写总结。"
            "只返回一个完整段落字段 opening_text，不能返回正文全文。\n\n"
            f"【上一章/设定事实】\n{source_facts or '（按原段事实，不补写未知信息）'}\n\n"
            f"【原第一段】\n{first_paragraph}\n\n"
            'JSON 格式：{"opening_text":"修复后的第一段"}'
        )
        result = await self.gateway.generate_json(
            prompt,
            system_prompt=(
                "你是严格的中文网文开场编辑，只输出合法 JSON。"
                + third_person_generation_contract()
                + content_generation_contract(quality_profile)
            ),
            max_tokens=min(2200, max(1100, int(len(first_paragraph) * 1.4))),
            temperature=0.45,
            prompt_name="v7.generation.opening_repair",
            prompt_version="1.0.0",
        )
        candidate = str((result.get("data") or {}).get("opening_text") or "").strip()
        if not candidate or "\n\n" in candidate:
            raise AIGatewayError("opening repair did not return exactly one paragraph")
        if len(candidate) < max(20, int(len(first_paragraph) * 0.25)):
            raise AIGatewayError("opening repair candidate is too short")
        if len(candidate) > max(700, int(len(first_paragraph) * 1.8)):
            raise AIGatewayError("opening repair candidate expanded the source paragraph")

        repaired = "\n\n".join([candidate, *paragraphs[1:]])
        duplicate_stats = duplicate_paragraph_stats(repaired)
        if float(duplicate_stats.get("duplicate_ratio") or 0.0) >= 0.01:
            raise AIGatewayError("opening repair candidate contains repeated full paragraphs")
        gate = inspect_opening(
            repaired,
            requested_mode=requested,
            chapter_number=chapter_number,
            recent_modes=recent,
        )
        if not gate.get("passed"):
            raise AIGatewayError(
                "opening repair still failed: "
                + ";".join(str(item.get("code")) for item in gate.get("flags") or [])
            )
        return {
            "processed_text": repaired,
            "opening": gate,
            "quality_gate": {"passed": True, "mode": "provider_prefix_repair"},
            "layers_applied": [{
                "layer": "provider_opening_contract_repair",
                "changes": 1,
                "applied": True,
                "requested_mode": requested,
                "observed_mode": gate.get("observed_mode"),
            }],
            "usage": result.get("usage") or {},
        }

    async def process(
        self,
        text: str,
        *,
        source_facts: str = "",
        forbidden_changes: str = "",
        quality_retry_feedback: str = "",
        style_profile: str = "",
        quality_profile: dict[str, Any] | None = None,
        payoff_contract: dict[str, Any] | None = None,
        safe_deduplicate: bool = False,
        active_rules: list[Any] | None = None,
        force_semantic_rewrite: bool = False,
        readability_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run rule pre-clean and semantic humanization.

        The semantic output must retain nearly all source material and
        paragraph structure.  If it does not, fail the generation instead of
        silently returning the weaker heuristic result.
        """
        original = text
        before_metrics = analyze_deai_patterns(text, profile=quality_profile)
        layers: list[dict[str, Any]] = []

        text, n = self._layer_cliches(text)
        layers.append({"layer": "cliche_removal", "changes": n})

        text, n = self._layer_dashes(text)
        layers.append({"layer": "dash_ellipsis_normalize", "changes": n})

        text, n = self._layer_parallel(text)
        layers.append({"layer": "parallel_structure_break", "changes": n})

        text, n = self._layer_repetition(text)
        layers.append({"layer": "repetition_reduction", "changes": n})

        text, n = self._layer_punctuation(text)
        layers.append({"layer": "punctuation_normalize", "changes": n})

        text, n = self._layer_paragraph(text)
        layers.append({"layer": "paragraph_rhythm", "changes": n})

        text, n = self._layer_trailing_moral(text)
        layers.append({"layer": "trailing_moral_removal", "changes": n})

        # Provider retries occasionally append an exact second copy of the
        # paragraph stream.  In the canonical generation path this repair is
        # safe because it removes only exact full-paragraph duplicates and
        # keeps the first occurrence; the evidence is retained for audit.
        dedup_evidence: dict[str, Any] = {}
        if safe_deduplicate:
            text, dedup_evidence = deduplicate_full_paragraphs(text)
            removed = int(dedup_evidence.get("removed_paragraphs") or 0)
            layers.append({
                "layer": "exact_duplicate_paragraph_repair",
                "changes": removed,
                "applied": bool(removed),
                "evidence": dedup_evidence,
            })

        # Do not send every chapter through a whole-chapter semantic rewrite.
        # A clean chapter should keep its original voice; the provider pass is
        # reserved for measured risks that deterministic rules cannot safely
        # repair.  This also makes a missing provider an explicit problem only
        # when semantic humanization is actually needed.
        rule_metrics = analyze_deai_patterns(text, profile=quality_profile)
        duplicate_flags = [
            flag
            for flag in rule_metrics.get("flags") or []
            if isinstance(flag, dict) and flag.get("code") == "duplicate_paragraph"
        ]
        semantic_flags = [
            flag
            for flag in rule_metrics.get("flags") or []
            if isinstance(flag, dict)
            and flag.get("code") in self.SEMANTIC_REWRITE_FLAGS
            and str(flag.get("severity") or "").lower()
            in self.SEMANTIC_REWRITE_SEVERITIES
        ]
        semantic_trigger_flags = sorted(
            {
                str(flag.get("code"))
                for flag in semantic_flags
                if flag.get("code")
            }
        )
        if force_semantic_rewrite and not duplicate_flags and not semantic_flags:
            semantic_trigger_flags = ["forced_local_repair"]
        if duplicate_flags or (not semantic_flags and not force_semantic_rewrite):
            quality_gate: dict[str, Any] = {
                "passed": not duplicate_flags,
                "mode": "deterministic_only",
            }
            if duplicate_flags:
                quality_gate.update(
                    {
                        "code": "duplicate_paragraph",
                        "message": str(duplicate_flags[0].get("message") or "正文存在完整段落重复"),
                    }
                )
            layers.append(
                {
                    "layer": "semantic_final_humanize",
                    "changes": 0,
                    "applied": False,
                    "reason": (
                        "duplicate paragraph detected; semantic rewrite is blocked"
                        if duplicate_flags
                        else "rule-cleaned text is below semantic rewrite trigger"
                    ),
                }
            )
            return {
                "original_text": original,
                "processed_text": text,
                "layers_applied": layers,
                "total_changes": sum(item["changes"] for item in layers),
                "original_chars": chinese_word_count(original),
                "processed_chars": chinese_word_count(text),
                "semantic_humanize": False,
                "humanize_changes": [],
                "ai_patterns_removed": [],
                "metrics": {
                    "before": before_metrics,
                    "after": rule_metrics,
                    "risk_delta": before_metrics["risk_score"] - rule_metrics["risk_score"],
                    "duplicate_repair": dedup_evidence,
                    "semantic_trigger_flags": semantic_trigger_flags,
                },
                "quality_gate": quality_gate,
                "usage": {},
            }

        if self.gateway is None:
            raise AIGatewayError("semantic final_humanize gateway is not configured")
        payoff_contract = payoff_contract if isinstance(payoff_contract, dict) else {}
        protected_payoff_fields = (
            ("主动选择", "active_choice"),
            ("爆发结果", "visible_result"),
            ("可见反馈", "payoff_feedback"),
            ("章末压力", "next_pressure"),
            ("关键证据锚点", "text_anchor"),
        )
        protected_payoff_lines = [
            f"- {label}：{payoff_contract.get(key)}"
            for label, key in protected_payoff_fields
            if payoff_contract.get(key)
        ]
        protected_payoff_block = "\n".join(protected_payoff_lines) or "（本章没有单独提供可保护锚点，以正文事实为准）"
        humanize_prompt = (
            "请对下面这章小说执行最终人文化定稿。只改表达，不改事件、人物、"
            "因果、物品状态或对话信息；保留具体细节和自然分段。按四层检查："
            "结构层打散‘提出观点-解释-总结’的重复段式，场景转换用动作/对白承接，"
            "章末保留悬念而不是总结；句法层减少正式连接词、对称排比和过度完整的解释；"
            "词语层删除高频套话、翻译腔和空泛形容；人物层保留角色口吻与对白潜台词，"
            "用动作和细节承载情绪，不把情绪标签直接说满。不要把全文修成同一种‘干净、完整、均匀’的句子，"
            "允许符合人物口吻的短句、碎片句、停顿和省略主语；让段落有长短落差，段首轮换动作、物件、声音、对白、"
            "环境后果和人物反应，但不要按固定顺序机械轮换。原文自然的地方少改，"
            "这不是同义词替换：需要在不改变事实的前提下重组部分句群和段落内部节奏，"
            "让不同段落的叙述密度、停顿、人物反应和信息露出产生自然波动；"
            "不要为了制造波动故意加入错别字、冷僻词、病句或刻意不自然的表达，也不要伪造所谓‘人工痕迹’。"
            "不得摘要、缩写、新增剧情或机械删成电报句。标点不设禁用清单；"
            "保留有语义必要的破折号、省略号和分号，只处理整章高密度、连续重复或模板化使用。\n\n"
            f"{third_person_generation_contract()}\n"
            f"{content_generation_contract(quality_profile)}\n\n"
            f"【不可变事实】\n{source_facts or '（无额外事实）'}\n\n"
            f"【禁止改动】\n{forbidden_changes or '情节、人物、时间线、设定与对白信息'}\n\n"
            f"【作者文风卡】\n{style_profile or '（暂无作者文风卡）'}\n\n"
            f"【上次质量反馈】\n{quality_retry_feedback or '（首次定稿）'}\n\n"
            f"【本章质量策略】\n{compile_quality_directive(quality_profile, payoff_contract=payoff_contract, active_rules=active_rules, readability_plan=readability_plan)}\n\n"
            f"{render_readability_plan(readability_plan, compact=True) if readability_plan else ''}\n\n"
            f"【AI味候选词库指导】\n{render_ai_flavor_guidance(quality_profile)}\n\n"
            # P1-2 质量整改：调整爽点保护锚点的描述
            # 明确告诉模型：保护的是事实和因果，表达方式可以完全彻底重写
            "【爽点保护锚点】以下内容是本章爽点的核心事实与因果链，必须完整保留：\n"
            "- 保护范围：事件、人物动作、结果、对手反应、资源变化、规则后果、新的压力\n"
            "- 可以做的：表达方式可以完全彻底重写，换说法、换语序、换描写角度都可以\n"
            "- 不能做的：删除、弱化成抽象总结、改成原文没有的事件、降低爽感强度\n"
            "注意：锚点保护的是'发生了什么'和'为什么发生'，不是'怎么说的'；\n"
            "去AI味重写时请大胆改写表达方式，不要只微调措辞导致AI腔残留。\n"
            f"{protected_payoff_block}\n\n"
            "【爽点保真】保留并强化本章已经写出的压制、主动选择、爆发结果、可见反馈和余波；"
            "去 AI 味只改表达，不得把强反馈改成平铺直叙，不得删掉对手态度、资源变化、规则后果或新的压力；"
            "不得为了制造爽点新增原文没有的事件。\n"
            f"{self._paragraph_opening_guidance(rule_metrics)}\n\n"
            f"{self._tic_guidance(rule_metrics)}\n\n"
            f"【原文】\n{text}\n\n"
            "【长度约束】输出必须保留原文全部事件与细节，字符数尽量与原文一致，"
            "不得扩写；与原文相比不得增加超过 10%，不得摘要或新增剧情。\n"
            "输出 JSON：{\"humanized_text\":\"完整正文\","
            "\"changes\":[\"改动说明\"],\"ai_patterns_removed\":[\"消除的痕迹\"]}"
        )
        pre_humanized_text = text
        try:
            ai = await self.gateway.generate_json(
                humanize_prompt,
                system_prompt=(
                    "你是严格的真人网文责任编辑，只输出合法 JSON。"
                    + third_person_generation_contract()
                    + content_generation_contract(quality_profile)
                ),
                max_tokens=max(2400, min(5200, int(chinese_word_count(text) * 1.18))),
                temperature=0.65,
                prompt_name="bootstrap.final_humanize",
                prompt_version="1.4.0",
            )
        except AIGatewayError as exc:
            # A malformed/truncated semantic rewrite is a quality failure, not
            # an HTTP 500.  Keep the deterministic rule-cleaned text intact and
            # surface a blocking flag so StoryDirector's bounded rework loop
            # can retry the whole chapter or persist needs_review truthfully.
            logger.warning(
                "V7 final_humanize provider output invalid; retaining rule-cleaned text (%s)",
                type(exc).__name__,
            )
            after_metrics = analyze_deai_patterns(pre_humanized_text, profile=quality_profile)
            fallback_message = "语义去 AI 改写返回了无效 JSON，已保留规则清洗稿"
            quality_gate = self._deterministic_fallback_gate(
                after_metrics,
                fallback_message,
            )
            if not quality_gate["passed"]:
                after_metrics.setdefault("flags", []).append({
                    "code": "rewrite_candidate_rejected",
                    "severity": "high",
                    "message": fallback_message,
                })
            layers.append({
                "layer": "semantic_final_humanize",
                "changes": 0,
                "applied": False,
                "reason": "provider_invalid_json",
            })
            return {
                "original_text": original,
                "processed_text": pre_humanized_text,
                "layers_applied": layers,
                "total_changes": sum(item["changes"] for item in layers),
                "original_chars": chinese_word_count(original),
                "processed_chars": chinese_word_count(pre_humanized_text),
                "semantic_humanize": False,
                "humanize_changes": [],
                "ai_patterns_removed": [],
                "metrics": {
                    "before": before_metrics,
                    "after": after_metrics,
                    "risk_delta": before_metrics["risk_score"] - after_metrics["risk_score"],
                    "duplicate_repair": dedup_evidence,
                    "semantic_trigger_flags": semantic_trigger_flags,
                },
                "quality_gate": quality_gate,
                "usage": {},
            }

        payload = ai.get("data") or {}
        humanized = str(payload.get("humanized_text") or "").strip()
        try:
            # The provider may accidentally collapse JSON newlines.  Reflow
            # only at sentence boundaries; reject actual content loss.
            text, shape = normalize_and_validate_rewrite(
                pre_humanized_text,
                humanized,
                minimum_chars=50,
            )
            duplicate_stats = duplicate_paragraph_stats(text)
            if float(duplicate_stats.get("duplicate_ratio") or 0.0) >= 0.01:
                raise ValueError(
                    "rewrite candidate contains repeated full paragraphs: "
                    f"ratio={duplicate_stats.get('duplicate_ratio')}"
                )
        except ValueError as exc:
            message = str(exc)
            if not (
                "changed chapter length outside safe range" in message
                or "repeated full paragraphs" in message
            ):
                raise AIGatewayError(f"final_humanize {message}") from exc
            # Preserve the rule-cleaned source, but carry a blocking evidence
            # flag into ReviewEngine.  The director may retry; if retries are
            # exhausted this chapter becomes needs_review, never reviewed.
            logger.warning("V7 final_humanize candidate rejected: %s", message)
            text = pre_humanized_text
            after_metrics = analyze_deai_patterns(text, profile=quality_profile)
            quality_gate = self._deterministic_fallback_gate(
                after_metrics,
                message,
            )
            if not quality_gate["passed"]:
                after_metrics.setdefault("flags", []).append({
                    "code": "rewrite_candidate_rejected",
                    "severity": "high",
                    "message": message,
                })
            layers.append({
                "layer": "semantic_final_humanize",
                "changes": 0,
                "applied": False,
                "reason": message,
            })
            return {
                "original_text": original,
                "processed_text": text,
                "layers_applied": layers,
                "total_changes": sum(item["changes"] for item in layers),
                "original_chars": chinese_word_count(original),
                "processed_chars": chinese_word_count(text),
                "semantic_humanize": False,
                "humanize_changes": [],
                "ai_patterns_removed": [],
                "metrics": {
                    "before": before_metrics,
                    "after": after_metrics,
                    "risk_delta": before_metrics["risk_score"] - after_metrics["risk_score"],
                    "duplicate_repair": dedup_evidence,
                    "semantic_trigger_flags": semantic_trigger_flags,
                },
                "quality_gate": quality_gate,
                "usage": ai.get("usage") or {},
            }
        text, punctuation_changes = self._layer_dashes(text)
        if punctuation_changes:
            layers.append({
                "layer": "post_humanize_dash_density_repair",
                "changes": punctuation_changes,
                "applied": True,
            })
        after_metrics = analyze_deai_patterns(text, profile=quality_profile)
        combined_usage = dict(ai.get("usage") or {})
        opening_repair_gate: dict[str, Any] = {"passed": True, "mode": "not_needed"}
        opening_repair_usage: dict[str, Any] = {}
        opening_risk = after_metrics.get("repeated_paragraph_opening") or {}
        if float(opening_risk.get("ratio") or 0.0) >= 0.30:
            opening_repair_prompt = (
                "只修复下面正文的段落起笔重复，不改剧情。必须保留全部事件、人物、地点、时间、因果、对白、信息和段落数量；"
                "不删段、不合并段、不新增剧情，不把整章改成摘要。"
                f"当前约 {float(opening_risk.get('ratio') or 0.0):.1%} 的段落以‘{opening_risk.get('opening')}’开头，"
                "请只改动部分段落的前几个字，让它们自然地从动作、环境、物件、对白或他人反应起笔，"
                "目标是把同一人名段首降到全章约四分之一以内；不要把人名全部机械换成‘他/她’，保持第三人称限知清晰。"
                "输出完整正文 JSON，不要输出解释。\n\n"
                f"【正文】\n{text}"
            )
            try:
                opening_ai = await self.gateway.generate_json(
                    opening_repair_prompt,
                    system_prompt=(
                        "你是严格的中文网文段落编辑，只做局部起笔修复，只输出合法 JSON。"
                        + third_person_generation_contract()
                    ),
                    max_tokens=max(1800, min(4800, int(chinese_word_count(text) * 1.08))),
                    temperature=0.25,
                    prompt_name="bootstrap.final_humanize_opening_repair",
                    prompt_version="1.1.0",
                )
                opening_candidate = str((opening_ai.get("data") or {}).get("humanized_text") or "").strip()
                opening_candidate, opening_shape = normalize_and_validate_rewrite(
                    text,
                    opening_candidate,
                    minimum_chars=50,
                )
                opening_duplicates = duplicate_paragraph_stats(opening_candidate)
                if float(opening_duplicates.get("duplicate_ratio") or 0.0) >= 0.01:
                    raise ValueError("opening repair candidate contains repeated full paragraphs")
                opening_after = analyze_deai_patterns(opening_candidate, profile=quality_profile)
                opening_after_risk = opening_after.get("repeated_paragraph_opening") or {}
                opening_repair_usage = opening_ai.get("usage") or {}
                for key in ("tokens_input", "tokens_output", "cost"):
                    primary = combined_usage.get(key) or 0
                    extra = opening_repair_usage.get(key) or 0
                    combined_usage[key] = primary + extra
                combined_usage["model"] = opening_repair_usage.get("model") or combined_usage.get("model")
                if float(opening_after_risk.get("ratio") or 0.0) >= 0.30:
                    raise ValueError(
                        "opening repair did not reduce repeated paragraph opening below the hard gate"
                    )
                text = opening_candidate
                after_metrics = opening_after
                opening_repair_gate = {
                    "passed": True,
                    "mode": "targeted",
                    "before_ratio": opening_risk.get("ratio"),
                    "after_ratio": opening_after_risk.get("ratio"),
                    "shape": opening_shape,
                }
                layers.append({
                    "layer": "targeted_paragraph_opening_repair",
                    "changes": 1,
                    "applied": True,
                    "before_ratio": opening_risk.get("ratio"),
                    "after_ratio": opening_after_risk.get("ratio"),
                    "usage": opening_repair_usage,
                })
            except (AIGatewayError, ValueError) as exc:
                fallback_text, fallback_evidence = self._deterministic_paragraph_opening_repair(
                    text,
                    str(opening_risk.get("opening") or ""),
                )
                if fallback_evidence.get("applied"):
                    text = fallback_text
                    after_metrics = analyze_deai_patterns(text, profile=quality_profile)
                    opening_repair_gate = {
                        **fallback_evidence,
                        "passed": False,
                        "mode": "deterministic_fallback",
                        "code": "opening_repair_provider_failed",
                        "provider_error": str(exc),
                    }
                else:
                    opening_repair_gate = {
                        "passed": False,
                        "mode": "targeted",
                        "code": "repeated_paragraph_opening",
                        "message": str(exc),
                        "before_ratio": opening_risk.get("ratio"),
                        "fallback": fallback_evidence,
                    }
                layers.append({
                    "layer": "targeted_paragraph_opening_repair",
                    "changes": 0,
                    "applied": False,
                    "reason": str(exc),
                    "usage": opening_repair_usage,
                })
                if fallback_evidence.get("applied"):
                    layers.append({
                        "layer": "deterministic_paragraph_opening_repair",
                        "changes": int(fallback_evidence.get("replaced_count") or 0),
                        "applied": True,
                        "evidence": fallback_evidence,
                    })
        structural_smell = after_metrics.get("structural_ai_smell") or {}
        structural_flags = [
            flag for flag in after_metrics.get("flags") or []
            if isinstance(flag, dict) and flag.get("code") == "structural_ai_smell"
        ]
        if structural_flags:
            structural_evidence = {
                "message": structural_flags[0].get("message") or "模式级 AI 味门禁未通过",
                "evidence": structural_smell,
            }
            if opening_repair_gate.get("passed", True):
                opening_repair_gate = {
                    **opening_repair_gate,
                    "passed": False,
                    "code": "structural_ai_smell",
                    **structural_evidence,
                }
            else:
                # Preserve the first concrete failure (for example a provider
                # rejection) while keeping the independent structural signal
                # visible for the next bounded retry and audit trail.
                opening_repair_gate = {
                    **opening_repair_gate,
                    "structural_ai_smell": structural_evidence,
                }
            layers.append({
                "layer": "structural_ai_smell_gate",
                "changes": 0,
                "applied": False,
                "reason": "语义改写后仍有多项独立结构信号未消除",
                "evidence": structural_flags[0].get("evidence") or {},
            })
        layers.append(
            {
                "layer": "semantic_final_humanize",
                "changes": len(payload.get("changes") or []),
                "patterns_removed": payload.get("ai_patterns_removed") or [],
                "shape": shape,
                "usage": combined_usage,
            }
        )

        total = sum(item["changes"] for item in layers)
        return {
            "original_text": original,
            "processed_text": text,
            "layers_applied": layers,
            "total_changes": total,
            "original_chars": chinese_word_count(original),
            "processed_chars": chinese_word_count(text),
            "semantic_humanize": True,
            "humanize_changes": payload.get("changes") or [],
            "ai_patterns_removed": payload.get("ai_patterns_removed") or [],
            "metrics": {
                "before": before_metrics,
                "after": after_metrics,
                "risk_delta": before_metrics["risk_score"] - after_metrics["risk_score"],
                "duplicate_repair": dedup_evidence,
                "semantic_trigger_flags": semantic_trigger_flags,
            },
            "quality_gate": opening_repair_gate,
            "usage": combined_usage,
        }

    def _layer_cliches(self, text: str) -> tuple[str, int]:
        count = 0
        for pattern, repl in self.CLICHE_PATTERNS:
            text, n = re.subn(pattern, repl, text)
            count += n
        return text, count

    def _layer_dashes(self, text: str) -> tuple[str, int]:
        count = 0
        text, n = re.subn(r"-{2,}", "——", text)
        count += n
        text, n = re.subn(r"\.{3,}", "……", text)
        count += n
        text, n = re.subn(r"。{2,}", "。", text)
        count += n
        text, n = re.subn(r"(——){2,}", "——", text)
        count += n
        # Keep punctuation expressive, but do not let a chapter become a
        # repeated interruption template. Only chapters long enough to make a
        # density signal meaningful are touched; the first natural dashes are
        # preserved and only excess occurrences become commas.
        compact_size = len(re.sub(r"\s+", "", text))
        if compact_size >= 600:
            dash_limit = max(3, int(compact_size / 1000 * 5))
            seen = 0

            def replace_excess(match: re.Match[str]) -> str:
                nonlocal count, seen
                seen += 1
                if seen <= dash_limit:
                    return match.group(0)
                count += 1
                return "，"

            text = re.sub(r"——|—", replace_excess, text)
        return text, count

    PRONOUNS = "他她它我你"

    def _layer_parallel(self, text: str) -> tuple[str, int]:
        """Merge 3 consecutive short clauses that all open with the same pronoun.

        "他知道A。他知道B。他知道C。" -> "他知道A，知道B，也知道C。"
        Only a leading single-character pronoun is dropped, so no word can be
        broken apart.
        """
        count = 0
        out_paras: list[str] = []
        for para in text.split("\n"):
            clauses = [c for c in re.split(r"(?<=[。！？])", para) if c]
            i = 0
            while i + 2 < len(clauses):
                window = [clauses[i], clauses[i + 1], clauses[i + 2]]
                stripped = [c.strip() for c in window]
                head = stripped[0][:1] if stripped[0] else ""
                if (
                    head in self.PRONOUNS
                    and all(s.startswith(head) for s in stripped)
                    and all(4 < len(s) <= 30 for s in stripped)
                    and all(s[1:2] not in "们的" for s in stripped)
                ):
                    first = re.sub(r"[。！？]$", "，", stripped[0])
                    second = re.sub(r"[。！？]$", "，", stripped[1][1:])
                    third = "也" + stripped[2][1:]
                    clauses[i : i + 3] = [first + second + third]
                    count += 1
                    i += 1
                else:
                    i += 1
            out_paras.append("".join(clauses))
        return "\n".join(out_paras), count

    # Intensifiers the model likes to stutter on.
    STUTTER_WORDS = ("非常", "十分", "真的", "突然", "忽然", "渐渐", "慢慢")

    def _layer_repetition(self, text: str) -> tuple[str, int]:
        """Collapse artefact repetitions without touching legitimate ABAB forms."""
        count = 0
        # 3+ repeats of the same 2-4 char unit is always an artefact.
        text, n = re.subn(r"([\u4e00-\u9fa5]{2,4})\1{2,}", r"\1", text)
        count += n
        # Doubled intensifiers ("非常非常") are AI stutter, not style.
        for word in self.STUTTER_WORDS:
            text, n = re.subn(f"(?:{word}){{2,}}", word, text)
            count += n
        text, n = re.subn(r"的{2,}", "的", text)
        count += n
        return text, count

    def _layer_punctuation(self, text: str) -> tuple[str, int]:
        """Convert half-width punctuation only when it sits in Chinese context."""
        count = 0
        chars = list(text)

        def is_cjk(ch: str) -> bool:
            return bool(ch) and "\u4e00" <= ch <= "\u9fa5"

        for idx, ch in enumerate(chars):
            if ch not in self.PUNCT_MAP:
                continue
            prev_ch = chars[idx - 1] if idx > 0 else ""
            next_ch = chars[idx + 1] if idx + 1 < len(chars) else ""
            if is_cjk(prev_ch) or is_cjk(next_ch):
                chars[idx] = self.PUNCT_MAP[ch]
                count += 1

        text = "".join(chars)
        text, n = re.subn(r"[ \t]+(?=[\u4e00-\u9fa5])", "", text)
        count += n
        return text, count

    def _layer_paragraph(self, text: str) -> tuple[str, int]:
        """Split paragraphs longer than 220 chars at a sentence boundary."""
        count = 0
        out: list[str] = []
        for para in text.split("\n"):
            stripped = para.strip()
            if len(stripped) <= 220:
                out.append(para)
                continue
            sentences = re.findall(r"[^。！？]*[。！？]", stripped) or [stripped]
            buf = ""
            pieces: list[str] = []
            for s in sentences:
                if len(buf) + len(s) > 180 and buf:
                    pieces.append(buf)
                    buf = s
                else:
                    buf += s
            if buf:
                pieces.append(buf)
            if len(pieces) > 1:
                count += len(pieces) - 1
            out.extend(pieces)
        return "\n".join(out), count

    def _layer_trailing_moral(self, text: str) -> tuple[str, int]:
        """Drop the AI habit of closing with a summarising moral sentence."""
        count = 0
        paras = [p for p in text.split("\n")]
        while paras and not paras[-1].strip():
            paras.pop()
        if paras:
            last = paras[-1].strip()
            if len(last) < 90 and re.search(
                r"(或许|也许|然而)?[^。]*(明白了|懂得了|意味着|注定|命运的齿轮|故事才刚刚开始)[^。]*。$",
                last,
            ) and re.search(r"(懂得了|明白了|命运的齿轮|故事才刚刚开始)", last):
                paras.pop()
                count = 1
        return "\n".join(paras), count


class AIGateway:
    """Async LLM gateway with retry. Raises AIGatewayError instead of faking success."""

    INPUT_PRICE_PER_M = 1.0   # CNY / 1M tokens
    OUTPUT_PRICE_PER_M = 2.0  # CNY / 1M tokens

    def __init__(
        self,
        tracer: ExecutionTracer | None = None,
        *,
        db: AsyncSession | None = None,
        novel_id: uuid.UUID | None = None,
        project_id: str | None = None,
        provider_config: dict[str, str] | None = None,
    ):
        self.tracer = tracer
        self.db = db
        self.novel_id = novel_id
        self.project_id = project_id
        provider_config = provider_config or {}
        configured_model = str(provider_config.get("model") or "")
        inferred_provider = (
            "openai" if configured_model.startswith("gpt-5.6") else "deepseek"
        )
        self.provider = provider_config.get("provider") or inferred_provider
        self.api_key = provider_config.get("api_key") or os.getenv(
            f"{self.provider.upper()}_API_KEY", ""
        )
        self.base_url = provider_config.get("base_url") or self._default_base_url(self.provider)
        self.default_model = configured_model or os.getenv(
            f"{self.provider.upper()}_MODEL", "deepseek-chat"
        )
        self.timeout = float(os.getenv("V7_AI_TIMEOUT", "180"))
        self.max_retries = int(os.getenv("V7_AI_MAX_RETRIES", "3"))
        self._route_resolved = False

    @staticmethod
    def _default_base_url(provider: str) -> str:
        return {
            "deepseek": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            "openai": os.getenv("OPENAI_API_URL", "https://api.openai.com/v1"),
            "claude": os.getenv("CLAUDE_API_URL", "https://api.anthropic.com/v1/messages"),
            "gemini": os.getenv("GEMINI_API_URL", "https://generativelanguage.googleapis.com"),
        }.get(provider, os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"))

    @staticmethod
    def _route_candidates(prompt_name: str | None) -> list[str]:
        """Map V7 prompt names to the editable ``model_routes`` contract."""
        prompt_name = str(prompt_name or "").strip()
        aliases = {
            "v7.review.": "review_7dim",
            # The editor keeps the existing model-route task keys so deployed
            # provider/model configuration remains reusable, but the call
            # itself is owned by the V7 async gateway and is recorded under a
            # V7 prompt name/provenance identity.
            "v7.editor.polish": "editor_polish",
            "v7.editor.rewrite": "editor_rewrite",
            "v7.editor.continue": "editor_continue",
            "v7.editor.deai": "editor_deai",
            "v7.editor.expand": "editor_expand",
            "v7.editor.condense": "editor_condense",
            "v7.generation.chapter": "gen_next_chapter",
            "v7.generation.continuation": "gen_next_chapter",
            "v7.generation.scene_plan": "write_chapter_draft",
            # Scene prose and handoffs are part of the canonical chapter
            # writer.  Resolve them through the same editable route so a
            # real OpenAI-compatible model (for example GPT-5.6 Luna) can be
            # selected without a second generation path.
            "v7.generation.scene": "gen_next_chapter",
            "v7.generation.scene_handoff": "gen_next_chapter",
            "v7.generation.final_humanize": "final_humanize",
            "v7.generation.chapter_repair": "write_polish",
            "v7.memory.": "extract_story_facts",
            "v7.plot.": "generate_story_arc",
        }
        result = [prompt_name] if prompt_name else []
        for prefix, task_type in aliases.items():
            if prompt_name == prefix or prompt_name.startswith(prefix):
                result.append(task_type)
        return list(dict.fromkeys(result))

    async def _resolve_model_route(self, prompt_name: str | None) -> None:
        """Resolve the actual provider/model from ``model_routes`` once."""
        if self._route_resolved or self.db is None or not hasattr(self.db, "execute"):
            return
        self._route_resolved = True
        for task_type in self._route_candidates(prompt_name):
            try:
                result = await self.db.execute(
                    sql_text(
                        "SELECT provider, model, params FROM model_routes "
                        "WHERE task_type = :task_type AND is_active = TRUE LIMIT 1"
                    ),
                    {"task_type": task_type},
                )
                route = result.mappings().first()
            except Exception:
                # A missing route table or a unit double must not fabricate a
                # result.  The subsequent credential check remains fail-closed.
                return
            if not route:
                continue
            provider = str(route.get("provider") or self.provider)
            self.provider = provider
            self.default_model = str(route.get("model") or self.default_model)
            self.api_key = self.api_key or os.getenv(f"{provider.upper()}_API_KEY", "")
            self.base_url = self._default_base_url(provider)
            return

    async def _assert_budget(self, prompt: str, max_tokens: int) -> None:
        """Block before the provider call when a hard V7 budget is exceeded."""
        if self.db is None or self.novel_id is None:
            return
        from ..cost.cost_manager import BudgetExceededError, CostBudgetManager

        estimated_input = max(1, len(prompt) // 4)
        estimated_tokens = estimated_input + max(1, max_tokens)
        estimated_cost = (
            estimated_input / 1_000_000 * self.INPUT_PRICE_PER_M
            + max(1, max_tokens) / 1_000_000 * self.OUTPUT_PRICE_PER_M
        )
        try:
            await CostBudgetManager(self.db, self.novel_id).assert_within_budget(
                estimated_cost_cny=estimated_cost,
                estimated_tokens=estimated_tokens,
            )
        except BudgetExceededError as exc:
            raise AIGatewayError(str(exc)) from exc

    async def _record_budget_spend(self, usage: dict[str, Any], prompt_name: str | None) -> None:
        """Record provider-reported spend exactly once after a successful call."""
        if self.db is None or self.novel_id is None:
            return
        from ..cost.cost_manager import CostBudgetManager

        run_id = getattr(self.tracer, "_current_run", None) if self.tracer else None
        try:
            await CostBudgetManager(self.db, self.novel_id).record_cost(
                cost_cny=float(usage.get("cost") or 0.0),
                tokens=int(usage.get("tokens_input") or 0) + int(usage.get("tokens_output") or 0),
                run_id=run_id,
                source="v7_ai_gateway",
                description=f"V7 provider call: {prompt_name or 'unattributed'}",
            )
        except Exception as exc:
            # Do not retry the provider after a successful response if the
            # ledger write failed: that would create a second billable call.
            raise BudgetAccountingError(
                f"provider succeeded but V7 cost accounting failed: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _as_uuid(value: Any) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError):
            return None

    async def _record_shared_provenance(
        self,
        *,
        prompt: str,
        system_prompt: str,
        history: list[dict[str, str]] | None,
        result: dict[str, Any],
        prompt_name: str | None,
        prompt_version: str | None,
        json_mode: bool,
        attempt: int,
        logical_mutation_id: str,
        started: float,
    ) -> None:
        """Close PromptVersionManager + shared ledger in one V7 transaction."""
        if self.db is None or self.novel_id is None:
            return
        # Unit doubles used for provider/budget tests intentionally do not
        # pretend to be an AsyncSession.  Production sessions expose execute,
        # add and flush; only those sessions may claim durable provenance.
        if not all(hasattr(self.db, attr) for attr in ("execute", "flush")):
            return

        effective_name = (prompt_name or "v7.unattributed").strip() or "v7.unattributed"
        effective_version = normalise_prompt_version(prompt_version)
        run_id = getattr(self.tracer, "_current_run", None) if self.tracer else None
        step_stack = getattr(self.tracer, "_step_stack", []) if self.tracer else []
        step_id = step_stack[-1] if step_stack else None
        exact_hash = prompt_hash(
            prompt,
            system_prompt=system_prompt,
            history=history,
        )
        metadata = {
            "gateway_version": "v7",
            "provider": str(result.get("provider") or self.provider),
            "runtime_managed_prompt": True,
            "rendered_prompt_hash": exact_hash,
            "json_mode": json_mode,
            "attempt": attempt,
            "logical_mutation_id": logical_mutation_id,
        }

        from ..prompt.prompt_manager import PromptVersionManager

        manager = PromptVersionManager(self.db, self.novel_id)
        await manager.record_runtime_execution(
            effective_name,
            version_label=effective_version,
            rendered_prompt=prompt,
            model=str(result.get("model") or self.default_model),
            input_variables={
                "system_prompt": system_prompt,
                "history": history or [],
                "rendered_prompt_hash": exact_hash,
                "json_mode": json_mode,
            },
            output_raw=result.get("text"),
            tokens_input=int(result.get("tokens_input") or 0),
            tokens_output=int(result.get("tokens_output") or 0),
            cost=float(result.get("cost") or 0.0),
            duration_seconds=max(0.0, time.perf_counter() - started),
            status="success",
            run_id=self._as_uuid(run_id),
            step_id=self._as_uuid(step_id),
            novel_id=self.novel_id,
            validation_passed=True,
            extra_metadata=metadata,
        )
        await record_async_execution(
            self.db,
            execution_key=build_execution_key(
                "v7",
                scope=str(self.novel_id),
                client_mutation_id=logical_mutation_id,
                attempt=attempt,
            ),
            gateway_version="v7",
            project_id=self.project_id,
            novel_id=str(self.novel_id),
            run_id=str(run_id) if run_id else None,
            step_id=str(step_id) if step_id else None,
            task_type=effective_name,
            prompt_name=effective_name,
            prompt_version=effective_version,
            rendered_prompt=prompt,
            prompt_hash_value=exact_hash,
            provider=str(result.get("provider") or self.provider),
            model=str(result.get("model") or self.default_model),
            status="succeeded",
            prompt_tokens=int(result.get("tokens_input") or 0),
            completion_tokens=int(result.get("tokens_output") or 0),
            cost_cny=float(result.get("cost") or 0.0),
            latency_ms=int(max(0.0, time.perf_counter() - started) * 1000),
            client_mutation_id=logical_mutation_id,
            metadata=metadata,
        )

    async def _record_failed_provenance(
        self,
        *,
        prompt: str,
        system_prompt: str,
        history: list[dict[str, str]] | None,
        prompt_name: str | None,
        prompt_version: str | None,
        json_mode: bool,
        attempt: int,
        logical_mutation_id: str,
        model_name: str,
        error: Exception,
    ) -> None:
        """Best-effort record of a non-billable failed provider attempt."""
        if self.db is None or self.novel_id is None:
            return
        if not all(hasattr(self.db, attr) for attr in ("execute", "flush")):
            return
        effective_name = (prompt_name or "v7.unattributed").strip() or "v7.unattributed"
        effective_version = normalise_prompt_version(prompt_version)
        run_id = getattr(self.tracer, "_current_run", None) if self.tracer else None
        step_stack = getattr(self.tracer, "_step_stack", []) if self.tracer else []
        step_id = step_stack[-1] if step_stack else None
        exact_hash = prompt_hash(prompt, system_prompt=system_prompt, history=history)
        metadata = {
            "gateway_version": "v7",
            "provider": self.provider,
            "runtime_managed_prompt": True,
            "rendered_prompt_hash": exact_hash,
            "json_mode": json_mode,
            "attempt": attempt,
            "failure": True,
        }

        from ..prompt.prompt_manager import PromptVersionManager

        manager = PromptVersionManager(self.db, self.novel_id)
        await manager.record_runtime_execution(
            effective_name,
            version_label=effective_version,
            rendered_prompt=prompt,
            model=model_name,
            input_variables={
                "system_prompt": system_prompt,
                "history": history or [],
                "rendered_prompt_hash": exact_hash,
                "json_mode": json_mode,
            },
            output_raw=None,
            tokens_input=0,
            tokens_output=0,
            cost=0.0,
            status="failed",
            error_message=str(error)[:2000],
            run_id=self._as_uuid(run_id),
            step_id=self._as_uuid(step_id),
            novel_id=self.novel_id,
            extra_metadata=metadata,
        )
        await record_async_execution(
            self.db,
            execution_key=build_execution_key(
                "v7",
                scope=str(self.novel_id),
                client_mutation_id=logical_mutation_id,
                attempt=attempt,
            ),
            gateway_version="v7",
            project_id=self.project_id,
            novel_id=str(self.novel_id),
            run_id=str(run_id) if run_id else None,
            step_id=str(step_id) if step_id else None,
            task_type=effective_name,
            prompt_name=effective_name,
            prompt_version=effective_version,
            rendered_prompt=prompt,
            prompt_hash_value=exact_hash,
            provider=self.provider,
            model=model_name,
            status="failed",
            error=str(error)[:2000],
            client_mutation_id=logical_mutation_id,
            metadata=metadata,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def generate(
        self,
        prompt: str,
        *,
        system_prompt: str = "你是一个专业的中文小说创作助手。",
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4000,
        json_mode: bool = False,
        history: list[dict[str, str]] | None = None,
        prompt_name: str | None = None,
        prompt_version: str | None = None,
        client_mutation_id: str | None = None,
        task_type: str | None = None,
        reject_truncated: bool = True,
        expand_on_truncation: bool = True,
    ) -> dict[str, Any]:
        """Call the LLM. Raises AIGatewayError after all retries fail."""
        await self._resolve_model_route(task_type or prompt_name)
        if not self.api_key:
            raise AIGatewayError(
                f"{self.provider.upper()}_API_KEY is not configured; refusing to fabricate output"
            )

        base_max_tokens = max(1, int(max_tokens or 1))
        request_max_tokens = base_max_tokens
        await self._assert_budget(prompt, request_max_tokens)

        model = model or self.default_model

        last_error: Exception | None = None
        logical_mutation_id = client_mutation_id or uuid.uuid4().hex
        for attempt in range(1, self.max_retries + 1):
            started = time.perf_counter()
            try:
                # Total-duration hard cap: httpx's ``timeout`` only guards the
                # idle gap between two socket reads, so a slow-but-chatty LLM
                # stream can hang the chapter forever (observed: DeepSeek slow
                # window left generate_chapter stuck >30min with no timeout
                # firing). wrap the whole request in asyncio.wait_for so the
                # call can never exceed ``self.timeout`` wall-clock seconds.
                async def _one_request() -> dict[str, Any]:
                    response = await UnifiedAIGateway(
                        provider=self.provider,
                        api_key=self.api_key,
                        base_url=self.base_url,
                        model=model,
                        timeout=self.timeout,
                    ).complete_async(
                        prompt,
                        system_prompt=system_prompt,
                        history=history,
                        temperature=temperature,
                        max_tokens=request_max_tokens,
                        json_mode=json_mode,
                        # Keep the existing test seam while the transport is
                        # now owned by the shared V6/V7 gateway.
                        client_factory=httpx.AsyncClient,
                    )
                    return {
                        "content": response.content,
                        "prompt_tokens": response.prompt_tokens,
                        "completion_tokens": response.completion_tokens,
                        "finish_reason": response.finish_reason,
                    }

                result = await asyncio.wait_for(_one_request(), timeout=self.timeout)

                content = result["content"]
                tokens_input = int(result.get("prompt_tokens", 0))
                tokens_output = int(result.get("completion_tokens", 0))
                cost = (
                    tokens_input / 1_000_000 * self.INPUT_PRICE_PER_M
                    + tokens_output / 1_000_000 * self.OUTPUT_PRICE_PER_M
                )
                if not content or not content.strip():
                    raise AIGatewayError("LLM returned empty content")

                result_payload = {
                    "text": content,
                    "provider": self.provider,
                    "model": model,
                    "tokens_input": tokens_input,
                    "tokens_output": tokens_output,
                    "cost": cost,
                    "finish_reason": result.get("finish_reason", "stop"),
                    "attempts": attempt,
                    "prompt_name": prompt_name,
                    "prompt_version": prompt_version,
                }
                if (
                    reject_truncated
                    and str(result_payload.get("finish_reason") or "stop") == "length"
                ):
                    # A chapter ending mid-sentence/quote is not a usable
                    # completion. Charge the real provider response, then
                    # retry with a bounded larger budget; never return the
                    # truncated prefix as if it were a successful draft.
                    await self._record_budget_spend(result_payload, prompt_name)
                    raise AIGatewayTruncatedError(
                        "LLM output was truncated at the provider token limit "
                        f"(max_tokens={request_max_tokens})"
                    )
                await self._record_budget_spend(result_payload, prompt_name)
                try:
                    await self._record_shared_provenance(
                        prompt=prompt,
                        system_prompt=system_prompt,
                        history=history,
                        result=result_payload,
                        prompt_name=prompt_name,
                        prompt_version=prompt_version,
                        json_mode=json_mode,
                        attempt=attempt,
                        logical_mutation_id=logical_mutation_id,
                        started=started,
                    )
                except Exception as exc:
                    # Never retry a billable provider response after either
                    # provenance or the shared ledger failed to close.
                    raise BudgetAccountingError(
                        f"provider succeeded but V7 provenance accounting failed: {type(exc).__name__}"
                    ) from exc
                return result_payload
            except Exception as exc:  # noqa: BLE001 - retried and re-raised below
                last_error = exc
                if isinstance(exc, BudgetAccountingError):
                    break
                if isinstance(exc, AIGatewayTruncatedError):
                    if not expand_on_truncation:
                        try:
                            await self._record_failed_provenance(
                                prompt=prompt,
                                system_prompt=system_prompt,
                                history=history,
                                prompt_name=prompt_name,
                                prompt_version=prompt_version,
                                json_mode=json_mode,
                                attempt=attempt,
                                logical_mutation_id=logical_mutation_id,
                                model_name=model,
                                error=exc,
                            )
                        except Exception:
                            logger.warning(
                                "V7 bounded scene truncation provenance could not be persisted",
                                exc_info=True,
                            )
                        return {**result_payload, "truncated": True}
                    next_limit = min(
                        6000,
                        max(request_max_tokens + 600, int(request_max_tokens * 1.5)),
                    )
                    if next_limit > request_max_tokens:
                        request_max_tokens = next_limit
                        await self._assert_budget(prompt, request_max_tokens)
                if attempt == self.max_retries:
                    try:
                        await self._record_failed_provenance(
                            prompt=prompt,
                            system_prompt=system_prompt,
                            history=history,
                            prompt_name=prompt_name,
                            prompt_version=prompt_version,
                            json_mode=json_mode,
                            attempt=attempt,
                            logical_mutation_id=logical_mutation_id,
                            model_name=model,
                            error=exc,
                        )
                    except Exception:
                        logger.warning(
                            "V7 failed provider provenance could not be persisted",
                            exc_info=True,
                        )
                if attempt < self.max_retries:
                    await asyncio.sleep(min(2 ** attempt, 8))

        if isinstance(last_error, AIGatewayTruncatedError) and not expand_on_truncation:
            raise last_error
        raise AIGatewayError(
            f"LLM call failed after {self.max_retries} attempts: {last_error}"
        ) from last_error

    async def generate_json(
        self,
        prompt: str,
        *,
        system_prompt: str = "你是一个严谨的助手，只输出合法 JSON。",
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        prompt_name: str | None = None,
        prompt_version: str | None = None,
        client_mutation_id: str | None = None,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        """Generate and parse a JSON object. Returns {"data":..., "usage":...}."""
        usage_total = {
            "tokens_input": 0,
            "tokens_output": 0,
            "cost": 0.0,
            "model": None,
            "provider": None,
        }
        last_text = ""
        last_finish_reason = "unknown"
        for attempt in range(2):
            compact_retry = attempt > 0
            retry_max_tokens = max_tokens
            if compact_retry and last_finish_reason == "length":
                # A valid JSON contract is more important than preserving the
                # first, truncated attempt.  Give the same real provider one
                # bounded larger response budget; never synthesize the
                # missing tail locally.
                retry_max_tokens = min(6000, max(max_tokens, int(max_tokens * 1.5)))
            result = await self.generate(
                prompt if attempt == 0 else (
                    prompt
                    + "\n\n上一次输出不是可用的完整 JSON，请严格只输出一个完整 JSON 对象，不要解释、不要代码块标记。"
                    + "所有字符串值保持精炼；数组只保留契约必需项；不要重复字段，不要输出契约之外的长篇说明。"
                ),
                system_prompt=system_prompt,
                model=model,
                temperature=temperature,
                max_tokens=retry_max_tokens,
                json_mode=True,
                reject_truncated=False,
                prompt_name=prompt_name,
                prompt_version=prompt_version,
                client_mutation_id=client_mutation_id,
                task_type=task_type,
            )
            usage_total["tokens_input"] += result["tokens_input"]
            usage_total["tokens_output"] += result["tokens_output"]
            usage_total["cost"] += result["cost"]
            usage_total["model"] = result["model"]
            usage_total["provider"] = result.get("provider")
            last_text = result["text"]
            last_finish_reason = str(result.get("finish_reason") or "unknown")

            data = self._parse_json(last_text)
            if data is not None:
                return {"data": data, "usage": usage_total, "raw": last_text}

        raise AIGatewayError(
            "LLM did not return parseable JSON after 2 attempts "
            f"(finish_reason={last_finish_reason}): {last_text[:200]}"
        )

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
            candidate = re.sub(r"\s*```$", "", candidate)
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
        return None


class GenerationEngine:
    """Main generation orchestrator: context -> scene plan -> AI text -> de-AI."""

    def __init__(
        self,
        db: AsyncSession,
        novel_id: uuid.UUID,
        brain: NovelBrain,
        tracer: ExecutionTracer,
        event_bus: EventBus,
        project_id: str | None = None,
        provider_config: dict[str, str] | None = None,
        quality_profile: dict[str, Any] | None = None,
        genre_id: str | None = None,
    ):
        self.db = db
        self.novel_id = novel_id
        self.brain = brain
        self.tracer = tracer
        self.event_bus = event_bus
        self.project_id = project_id
        self.provider_config = provider_config or {}
        self.quality_profile = quality_profile or select_quality_profile()
        self.genre_id = genre_id

        self.ai_gateway = AIGateway(
            tracer,
            db=db,
            novel_id=novel_id,
            project_id=project_id,
            provider_config=self.provider_config,
        )
        self.context_assembler = ContextAssembler(brain, project_id, genre_id)
        self.scene_director = SceneDirector(brain, self.ai_gateway)
        self.deai_pipeline = DeAIPipeline(self.ai_gateway)

    @staticmethod
    def _normalise_scene_cards(
        scene_plan: dict[str, Any],
        *,
        target_word_count: int,
    ) -> list[dict[str, Any]]:
        """Turn planner beats into explicit scene contracts.

        The old writer received a beat list and was asked to infer all scene
        boundaries while writing the whole chapter.  That makes continuity a
        best-effort property of one long completion.  A scene card is the
        smallest unit that can carry a goal, obstacle, choice, turn and state
        change into the next real Provider call.
        """
        beats = scene_plan.get("beats") or []
        if not isinstance(beats, list) or not beats:
            raise AIGatewayError("scene serial contract invalid: no scene beats")
        cards: list[dict[str, Any]] = []
        planned_words = sum(
            int(beat.get("target_words") or 0)
            for beat in beats
            if isinstance(beat, dict)
        )
        if planned_words <= 0:
            planned_words = max(1, int(target_word_count or 1))
        scene_scale = 1.0
        if (
            target_word_count >= 1800
            and abs(planned_words - target_word_count) > target_word_count * 0.10
        ):
            scene_scale = target_word_count / planned_words
        opening_excess = 0
        for index, raw in enumerate(beats, start=1):
            if not isinstance(raw, dict):
                raise AIGatewayError(f"scene serial contract invalid: scene_{index}_not_object")
            scene_card = raw.get("scene_card") or raw.get("scene") or {}
            if not isinstance(scene_card, dict):
                scene_card = {}
            target_words = int(raw.get("target_words") or 0)
            if target_words <= 0:
                target_words = max(200, int(target_word_count * 0.8 / len(beats)))
            elif scene_scale != 1.0:
                target_words = max(200, int(target_words * scene_scale))
            opening_constraint = ""
            if index == 1:
                # The opening is where the previous real-provider run lost
                # pacing: a 600-word routine beat delayed the first anomaly.
                # Keep this as a generation contract instead of repairing a
                # finished chapter with a detector-oriented rewrite.
                if target_word_count >= 1800:
                    opening_cap = max(400, min(420, int(target_word_count * 0.14)))
                    opening_excess = max(0, target_words - opening_cap)
                    target_words = opening_cap
                opening_constraint = (
                    "前两句必须出现正在发生的动作、具体异常或明确目标；"
                    "前120字内必须出现会改变人物判断、位置、关系、资源或风险的具体阻碍/发现；"
                    "日常交代最多一个短段，不能用扫地、环境、回忆或设定连续铺满开头；"
                    "前420字内必须让异常造成可见后果、主动选择或新的风险，不能只停留在‘似乎不对’；"
                    "第一场结尾必须留下可见的动作、发现、选择或压力变化。"
                )
            elif index == 2:
                opening_constraint = (
                    "前两句继续上一场的动作或后果；前半场必须把上一场的异常转成新的选择、代价或风险，"
                    "不得重新解释背景。"
                )
            cards.append({
                "scene_index": index,
                "name": str(raw.get("name") or f"场景{index}")[:80],
                "purpose": str(raw.get("purpose") or "")[:160],
                "content": str(raw.get("content") or "")[:800],
                "emotion": str(raw.get("emotion") or "")[:80],
                "target_words": target_words,
                "payoff_phase": raw.get("payoff_phase"),
                "payoff_phases": raw.get("payoff_phases") or [],
                "location": str(scene_card.get("location") or raw.get("location") or "")[:120],
                "time": str(scene_card.get("time") or raw.get("time") or "")[:120],
                "characters": [
                    str(item)[:80]
                    for item in (scene_card.get("characters") or raw.get("characters") or [])
                    if str(item).strip()
                ][:8],
                "goal": str(scene_card.get("goal") or raw.get("goal") or raw.get("purpose") or "")[:240],
                "obstacle": str(scene_card.get("obstacle") or raw.get("obstacle") or "")[:240],
                "choice": str(scene_card.get("choice") or raw.get("choice") or "")[:240],
                "turn": str(scene_card.get("turn") or raw.get("turn") or "")[:240],
                "state_change": str(
                    scene_card.get("state_change") or raw.get("state_change") or ""
                )[:240],
                "knowledge_boundary": str(
                    scene_card.get("knowledge_boundary") or raw.get("knowledge_boundary") or ""
                )[:240],
                "handoff": str(scene_card.get("handoff") or raw.get("handoff") or "")[:240],
                "trigger": str(
                    scene_card.get("trigger") or raw.get("trigger") or ""
                )[:240],
                "causal_link": str(
                    scene_card.get("causal_link") or raw.get("causal_link") or ""
                )[:240],
                "opening_constraint": opening_constraint,
            })
        if opening_excess and len(cards) > 1:
            # Preserve the planner's total scale while moving time away from
            # routine setup and into the consequential scenes that follow.
            later_cards = cards[1:]
            later_words = sum(card["target_words"] for card in later_cards) or len(later_cards)
            distributed = 0
            for offset, card in enumerate(later_cards):
                if offset == len(later_cards) - 1:
                    share = opening_excess - distributed
                else:
                    share = round(opening_excess * card["target_words"] / later_words)
                    distributed += share
                card["target_words"] += share
        if target_word_count >= 1800 and len(cards) > 3:
            # Provider scene calls need enough room to finish a complete
            # event. Four-to-six tiny beats make a real model write past the
            # beat envelope, then leave the final beat with an unusable
            # remainder. Keep the opening card intact and coalesce the
            # middle beats until the reader-budget chapter has at most three
            # serial scenes: opening, escalation, consequence/hook.
            while len(cards) > 3:
                pair_index = min(
                    range(1, len(cards) - 1),
                    key=lambda index: cards[index]["target_words"]
                    + cards[index + 1]["target_words"],
                )
                left = cards[pair_index]
                right = cards[pair_index + 1]
                merged = dict(left)
                merged["name"] = f"{left['name']} / {right['name']}"[:80]
                for key, limit in (
                    ("purpose", 160),
                    ("content", 800),
                    ("emotion", 80),
                    ("goal", 240),
                    ("obstacle", 240),
                    ("choice", 240),
                    ("turn", 240),
                    ("state_change", 240),
                    ("knowledge_boundary", 240),
                    ("handoff", 240),
                    ("trigger", 240),
                    ("causal_link", 240),
                ):
                    values = [str(left.get(key) or "").strip(), str(right.get(key) or "").strip()]
                    merged[key] = "；".join(value for value in values if value)[:limit]
                merged["target_words"] = left["target_words"] + right["target_words"]
                phases = []
                for phase in (
                    list(left.get("payoff_phases") or [])
                    + ([left.get("payoff_phase")] if left.get("payoff_phase") else [])
                    + list(right.get("payoff_phases") or [])
                    + ([right.get("payoff_phase")] if right.get("payoff_phase") else [])
                ):
                    if phase and phase not in phases:
                        phases.append(phase)
                merged["payoff_phases"] = phases
                merged["payoff_phase"] = right.get("payoff_phase") or left.get("payoff_phase")
                merged["location"] = right.get("location") or left.get("location") or ""
                merged["time"] = right.get("time") or left.get("time") or ""
                merged["characters"] = list(dict.fromkeys(
                    list(left.get("characters") or []) + list(right.get("characters") or [])
                ))[:8]
                merged["opening_constraint"] = left.get("opening_constraint") or ""
                cards[pair_index : pair_index + 2] = [merged]
            for index, card in enumerate(cards, start=1):
                card["scene_index"] = index
                if index == 2:
                    card["opening_constraint"] = (
                        "前两句继续上一场的动作或后果；前半场必须把上一场的异常转成新的选择、代价或风险，"
                        "不得重新解释背景。"
                    )
                elif index > 2:
                    card["opening_constraint"] = ""
        if target_word_count >= 1800:
            planned_total = sum(card["target_words"] for card in cards)
            excess = max(0, planned_total - target_word_count)
            if excess and len(cards) > 1:
                GenerationEngine._rebalance_future_scene_targets(
                    cards,
                    future_start=1,
                    excess_chars=excess,
                )
        # Keep the plan's declared scale visible to the writer, but do not
        # silently change the chapter scale merely to satisfy an opening cap.
        planned_words = sum(card["target_words"] for card in cards) or 1
        for card in cards:
            card["target_share"] = round(card["target_words"] / planned_words, 4)
        return cards

    @staticmethod
    def _scene_length_bounds(
        scene_card: dict[str, Any],
        *,
        scene_index: int,
    ) -> tuple[int, int]:
        """Return the write-time length contract for one scene.

        Length is a pacing control, not a post-write truncation rule.  The
        opening target is capped separately. A scene may breathe, but it must
        still end near its planned beat so a routine setup scene cannot consume
        the chapter's pacing budget and leave review to discover the problem.
        """
        target_words = max(1, int(scene_card.get("target_words") or 300))
        minimum = max(120, int(target_words * 0.45))
        maximum = max(minimum + 100, int(target_words * SCENE_TARGET_MAX_RATIO))
        return minimum, maximum

    @staticmethod
    def _scene_allowed_max_chars(scene_card: dict[str, Any], *, scene_index: int) -> int:
        """Allow a small natural-language variance without accepting padding."""
        _minimum, planned_maximum = GenerationEngine._scene_length_bounds(
            scene_card,
            scene_index=scene_index,
        )
        return max(
            planned_maximum,
            int(planned_maximum * SCENE_NATURAL_LENGTH_TOLERANCE)
            + SCENE_NATURAL_LENGTH_TOLERANCE_CHARS,
        )

    def _scene_generation_max_tokens(
        self,
        scene_card: dict[str, Any],
        *,
        scene_index: int,
        max_scene_chars: int | None = None,
        token_margin: float | None = None,
    ) -> int:
        """Give the Provider enough completion headroom for a scene.

        ``chinese_word_count`` is a character budget, while the old scene
        call had a fixed 700-token floor.  That floor was larger than the
        short scene contracts, so DeepSeek naturally returned a 600-700
        character scene and the application rejected it as overlong.  Token
        headroom is intentionally larger than the character envelope because
        Chinese completion tokens are not a character limit.  The chapter-
        level contract remains responsible for the platform-wide limit.
        """
        _minimum, nominal_maximum = self._scene_length_bounds(
            scene_card,
            scene_index=scene_index,
        )
        maximum = max(
            _minimum,
            int(max_scene_chars) if max_scene_chars is not None else nominal_maximum,
        )
        provider = str(getattr(self.ai_gateway, "provider", "") or "").lower()
        margin = token_margin if token_margin is not None else (
            SCENE_OPENAI_TOKEN_CHAR_MARGIN
            if provider == "openai"
            else SCENE_DEEPSEEK_TOKEN_CHAR_MARGIN
        )
        # The previous fixed 1600/2400-token ceilings made the bounded
        # truncation retry ineffective for larger scenes: both the first call
        # and its repair could be silently sent below the character-derived
        # completion budget.  Keep the character envelope as the hard pacing
        # contract, but let the real provider finish a large scene naturally;
        # 6000 remains the gateway's global completion safety ceiling.
        # A repair margin below 1.0 is intentional: an overlong candidate
        # needs a genuinely smaller completion ceiling. The prompt still
        # carries the character envelope, while this token ceiling prevents a
        # Provider that ignores the prose instruction from returning the same
        # long scene three times. The retry loop handles a rare truncation by
        # making one bounded upward adjustment instead of jumping back to the
        # initial expansion margin.
        return max(
            240,
            min(
                SCENE_PROVIDER_TOKEN_CAP,
                int(maximum * margin),
            ),
        )

    @staticmethod
    def _scene_exceeds_chapter_budget(
        *,
        accepted_chars: int,
        candidate_chars: int,
        future_minimum_chars: int,
        chapter_max_chars: int,
    ) -> bool:
        """Keep a candidate from consuming the next scenes' hard minimum."""
        return (
            accepted_chars + candidate_chars + future_minimum_chars
            > chapter_max_chars
        )

    @staticmethod
    def _should_shrink_retry_envelope(
        *,
        previous_issue_codes: set[str],
        attempt: int,
        compression_mode: bool,
    ) -> bool:
        """Shrink only a complete over-budget candidate, never a truncation.

        A Provider truncation is missing prose, so its partial character count
        cannot prove that the scene is overlong.  Treating it as an overlong
        candidate can shrink the next completion envelope until the Provider
        is guaranteed to truncate again.
        """
        return bool(
            previous_issue_codes.intersection({
                "scene_chapter_budget_overrun",
                "scene_reader_budget_overrun",
            })
            and "scene_provider_truncated" not in previous_issue_codes
            # The normal scene contract allows two attempts (initial write
            # plus one repair).  Shrink the repair envelope on that first
            # retry; waiting for attempt 2 made this branch unreachable for
            # ordinary over-budget scenes and sent the Provider the same
            # effective budget twice.
            and attempt >= 1
            and not compression_mode
        )

    @staticmethod
    def _can_extend_truncation_retry(
        *,
        previous_issue_codes: set[str],
        attempt: int,
        max_attempts: int,
    ) -> bool:
        """Allow one final completion attempt after two truncations only."""
        return bool(
            max_attempts == SCENE_SERIAL_MAX_ATTEMPTS
            and attempt == SCENE_SERIAL_MAX_ATTEMPTS - 1
            and "scene_provider_truncated" in previous_issue_codes
        )

    @staticmethod
    def _can_extend_style_retry(
        *,
        previous_issue_codes: set[str],
        attempt: int,
        max_attempts: int,
    ) -> bool:
        """Allow one fresh writing path for expression-only failures."""
        return bool(
            max_attempts == SCENE_SERIAL_MAX_ATTEMPTS
            and attempt == SCENE_SERIAL_MAX_ATTEMPTS - 1
            and GenerationEngine._is_style_only_retry(previous_issue_codes)
        )

    @staticmethod
    def _can_extend_final_budget_retry(
        *,
        previous_issue_codes: set[str],
        attempt: int,
        max_attempts: int,
        projected_chars: int,
        chapter_max_chars: int,
        future_minimum_chars: int,
        future_target_chars: int,
    ) -> bool:
        """Allow one tight rewrite for a near-boundary final scene only."""
        overflow = projected_chars - chapter_max_chars
        return bool(
            max_attempts == SCENE_SERIAL_MAX_ATTEMPTS
            and attempt == SCENE_SERIAL_MAX_ATTEMPTS - 1
            and previous_issue_codes == {"scene_chapter_budget_overrun"}
            and future_minimum_chars == 0
            and future_target_chars == 0
            and 0 < overflow <= SCENE_FINAL_BUDGET_RETRY_MAX_OVERFLOW_CHARS
        )

    @staticmethod
    def _is_style_only_retry(previous_issue_codes: set[str]) -> bool:
        """Return whether a retry can be regenerated from contracts alone."""
        return bool(previous_issue_codes) and previous_issue_codes.issubset({
            "dash_density",
            "ai_phrase",
            "uniform_cadence",
            "repeated_paragraph_opening",
            "repeated_tic",
            "structural_ai_smell",
            "scene_explanatory_narration",
            "scene_metaphor_density",
            "scene_repeated_action_loop",
            "scene_state_echo",
            "scene_procedural_motion",
            "scene_subject_opening",
            "scene_opening_contract",
        })

    @staticmethod
    def _safe_scene_retry_evidence(issue: dict[str, Any]) -> dict[str, Any] | None:
        """Keep failed prose examples out of the next Writer prompt.

        The old retry feedback serialised detector evidence verbatim.  That
        included the exact bad similes and conclusion phrases emitted by the
        previous candidate, which gave the next Provider a highly salient
        phrase bank to imitate.  A generation critic should communicate the
        defect category and its size, not replay the defective prose.
        """
        code = str(issue.get("code") or "")
        evidence = issue.get("evidence")
        if code == "scene_metaphor_density":
            return {
                "count": int(evidence.get("count") or 0)
                if isinstance(evidence, dict)
                else None,
                "baseline": "non_dialogue_simile_density",
            }
        if code == "scene_explanatory_narration":
            kinds = []
            if isinstance(evidence, list):
                kinds = sorted({
                    str(item.get("kind") or "")
                    for item in evidence
                    if isinstance(item, dict) and item.get("kind")
                })
            return {"kinds": kinds, "baseline": "narrator_conclusion_zero"}
        if code == "scene_repeated_action_loop":
            kinds = []
            if isinstance(evidence, list):
                kinds = sorted({
                    str(item.get("kind") or "")
                    for item in evidence
                    if isinstance(item, dict) and item.get("kind")
                })
            return {"kinds": kinds, "baseline": "new_choice_or_consequence"}
        if code == "scene_state_echo":
            if not isinstance(evidence, dict):
                return None
            groups = evidence.get("groups") or []
            return {
                "groups": [
                    str(item.get("kind") or "")
                    for item in groups[:5]
                    if isinstance(item, dict) and item.get("kind")
                ],
                "repeated_occurrences": evidence.get("repeated_occurrences"),
                "baseline": "state_delta_only",
            }
        if code == "scene_procedural_motion":
            if not isinstance(evidence, dict):
                return None
            return {
                "paragraph_count": evidence.get("paragraph_count"),
                "motion_hits": evidence.get("motion_hits"),
                "turn_hits": evidence.get("turn_hits"),
                "dialogue_count": evidence.get("dialogue_count"),
                "first_turn_chars": evidence.get("first_turn_chars"),
                "baseline": "event_before_route_log",
            }
        if code == "scene_subject_opening":
            if not isinstance(evidence, dict):
                return None
            return {
                "subject": evidence.get("subject"),
                "count": evidence.get("count"),
                "paragraph_count": evidence.get("paragraph_count"),
                "ratio": evidence.get("ratio"),
                "baseline": "varied_paragraph_leads",
            }
        if code == "structural_ai_smell":
            if not isinstance(evidence, dict):
                return None
            dimensions = evidence.get("failed_dimensions") or []
            names = [
                str(item.get("name") or "")
                for item in dimensions
                if isinstance(item, dict) and item.get("name")
            ]
            return {
                "overall_score": evidence.get("overall_score"),
                "failed_dimensions": names[:5],
            }
        if code == "repeated_paragraph_opening":
            if not isinstance(evidence, dict):
                return None
            return {
                "opening": str(evidence.get("opening") or "")[:2],
                "count": evidence.get("count"),
                "paragraph_count": evidence.get("paragraph_count"),
            }
        return None

    @staticmethod
    def _final_scene_budget_variance_allowed(
        *,
        projected_chars: int,
        chapter_max_chars: int,
        future_minimum_chars: int,
        future_target_chars: int,
    ) -> bool:
        """Keep the reader-facing chapter budget as the only hard ceiling."""
        # A complete final scene is not an exemption from the chapter budget.
        # The previous variance let a 3000-character chapter reach 3672
        # characters and reported it as a warning, which directly conflicted
        # with the reader-budget contract.
        return False

    @staticmethod
    def _rebalance_future_scene_targets(
        cards: list[dict[str, Any]],
        *,
        future_start: int,
        excess_chars: int,
    ) -> bool:
        """Move natural length variance into future scene allocations.

        A Provider can finish a current event above its planned share while
        the chapter still has room. Shrinking a later target is preferable to
        rewriting a coherent scene, provided every future scene keeps its
        generation minimum. Return False when the future cards have no slack.
        """
        if excess_chars <= 0:
            return True
        future_cards = cards[future_start:]
        capacity = []
        for card in future_cards:
            target = max(1, int(card.get("target_words") or 1))
            minimum = max(120, int(target * 0.45))
            capacity.append((card, max(0, target - minimum)))
        if sum(slack for _card, slack in capacity) < excess_chars:
            return False
        remaining = excess_chars
        for card, slack in reversed(capacity):
            reduction = min(slack, remaining)
            card["target_words"] = max(
                1,
                int(card.get("target_words") or 1) - reduction,
            )
            remaining -= reduction
            if remaining <= 0:
                break
        planned_words = sum(int(card.get("target_words") or 0) for card in cards) or 1
        for card in cards:
            card["target_share"] = round(card["target_words"] / planned_words, 4)
        return remaining <= 0

    @staticmethod
    def _scene_naturalness_flags(
        text: str,
        *,
        accepted_text: str = "",
    ) -> list[dict[str, Any]]:
        """Return only generation-time defects that justify one scene retry.

        This is deliberately narrower than the post-write audit.  It catches
        provider leakage, duplicate paragraphs and strong template signals at
        the scene boundary; it does not rewrite prose or optimise a detector
        score after the chapter is complete.
        """
        candidate = str(text or "").strip()
        if not candidate:
            return [{"code": "scene_empty", "message": "Provider returned an empty scene"}]
        flags: list[dict[str, Any]] = []
        metrics = analyze_deai_patterns(candidate)
        generation_naturalness = inspect_generation_naturalness(candidate)
        retry_codes = {
            "dash_density",
            "ai_phrase",
            "uniform_cadence",
            "repeated_paragraph_opening",
            "repeated_tic",
            "structural_ai_smell",
        }
        for flag in metrics.get("flags") or []:
            if (
                isinstance(flag, dict)
                and flag.get("code") in retry_codes
                and str(flag.get("severity") or "").lower() in {"medium", "high"}
            ):
                if flag.get("code") == "dash_density":
                    # deai_metrics is intentionally chapter-wide and counts
                    # dialogue punctuation too. Generation should only retry
                    # when the narrative itself is dash-heavy; a quoted
                    # interruption is a valid local voice choice.
                    narrative = _remove_dialogue(candidate)
                    narrative_compact = re.sub(r"\s+", "", narrative)
                    narrative_dash_count = len(re.findall(r"——|--|—", narrative))
                    narrative_density = (
                        narrative_dash_count / len(narrative_compact) * 1000
                        if narrative_compact
                        else 0.0
                    )
                    if narrative_density <= 5:
                        continue
                flags.append(flag)
        for flag in generation_naturalness.get("flags") or []:
            if (
                isinstance(flag, dict)
                and str(flag.get("severity") or "").lower() in {"medium", "high"}
            ):
                flags.append(flag)
        # Chapter-scale detection deliberately needs twelve paragraphs to
        # avoid false positives. A serial scene is often shorter, so catch a
        # clearly repetitive named opening before it contaminates the next
        # scene. Pronoun-led paragraphs are excluded for the same reason as
        # the chapter metric: ordinary third-person web prose can reuse them.
        paragraphs = [
            item.strip()
            for item in re.split(r"\n{2,}|\n", candidate)
            if item.strip()
        ]
        candidate_openings: dict[str, int] = {}
        for paragraph in paragraphs:
            first = re.sub(r"^[\s\"“”‘’「」『』（(]+", "", paragraph)
            opening = first[:2]
            if not opening or opening[:1] in "他她它我你":
                continue
            candidate_openings[opening] = candidate_openings.get(opening, 0) + 1
        candidate_opening, candidate_opening_count = max(
            candidate_openings.items(),
            key=lambda item: item[1],
            default=("", 0),
        )
        candidate_opening_ratio = (
            candidate_opening_count / len(paragraphs) if paragraphs else 0.0
        )
        if len(paragraphs) >= 8:
            opening, count = candidate_opening, candidate_opening_count
            ratio = candidate_opening_ratio
            if count >= 4 and ratio >= 0.45 and not any(
                item.get("code") == "repeated_paragraph_opening"
                for item in flags
                if isinstance(item, dict)
            ):
                flags.append({
                    "code": "repeated_paragraph_opening",
                    "severity": "medium",
                    "message": (
                        f"本场 {opening} 字段首占 {ratio:.1%}，短场景中已形成机械起笔；"
                        "必须改用动作、物件、环境、对白或他人反应起笔"
                    ),
                    "evidence": {
                        "opening": opening,
                        "count": count,
                        "ratio": round(ratio, 4),
                        "paragraph_count": len(paragraphs),
                    },
                })
        # A single scene often has fewer than twelve paragraphs, which is the
        # minimum sample size for the chapter-level repeated-opening metric.
        # Evaluate the accepted scene chain plus this candidate as a bounded
        # generation-time signal, but only carry this one flag forward when
        # the candidate increases the risk.  Other chapter-scale metrics stay
        # in the post-write report and do not cause a scene retry here.
        if accepted_text.strip():
            before_opening = (
                analyze_deai_patterns(accepted_text).get("repeated_paragraph_opening")
                or {}
            )
            after_opening = (
                analyze_deai_patterns(f"{accepted_text}\n\n{candidate}")
                .get("repeated_paragraph_opening")
                or {}
            )
            # Do not blame a short scene merely for joining an already
            # dominant chapter-level opening. The scene itself must repeat
            # the same named opening before this cross-scene signal can cause
            # a generation retry. This keeps serial continuity from turning
            # one natural handoff paragraph into a false hard failure.
            candidate_repeats_after_opening = (
                candidate_opening
                and candidate_opening == str(after_opening.get("opening") or "")
                and candidate_opening_count >= 3
                and candidate_opening_ratio >= 0.45
            )
            if (
                float(after_opening.get("ratio") or 0.0) >= 0.30
                and candidate_repeats_after_opening
                and (
                    str(after_opening.get("opening") or "")
                    != str(before_opening.get("opening") or "")
                    or int(after_opening.get("count") or 0)
                    > int(before_opening.get("count") or 0)
                )
                and not any(
                    item.get("code") == "repeated_paragraph_opening"
                    for item in flags
                    if isinstance(item, dict)
                )
            ):
                flags.append({
                    "code": "repeated_paragraph_opening",
                    "severity": "medium",
                    "message": (
                        f"{after_opening.get('opening')}字开头段落在已写场景链中占比偏高，"
                        "本场需要改用动作、物件、环境、对白或他人反应起笔"
                    ),
                    "evidence": after_opening,
                })
        duplicate_stats = duplicate_paragraph_stats(
            f"{accepted_text}\n\n{candidate}" if accepted_text else candidate
        )
        if float(duplicate_stats.get("duplicate_ratio") or 0.0) >= 0.01:
            flags.append({
                "code": "scene_duplicate_paragraph",
                "severity": "high",
                "message": "场景与已接受正文存在完整段落重复",
                "evidence": duplicate_stats,
            })
        if re.search(r"(?:根据大纲|场景目标|场景卡|接下来写|读者将|本章需要)", candidate):
            flags.append({
                "code": "scene_meta_leakage",
                "severity": "high",
                "message": "场景正文泄露了写作工程说明",
            })
        return flags

    async def _generate_structured_plain_scene_candidate(
        self,
        *,
        chapter_number: int,
        scene_index: int,
        context: dict[str, Any],
        scene_card: dict[str, Any],
        previous_scene_tail: str,
        current_state: dict[str, Any],
        previous_handoffs: list[dict[str, Any]],
        min_scene_chars: int,
        max_scene_chars: int,
    ) -> dict[str, Any]:
        """Generate one plain scene through a structured, low-variance path.

        A whole-scene prose prompt lets the Provider spend its first tokens on
        atmosphere and then decorate every turn with the same image pattern.
        This fallback asks for paragraph-sized factual beats instead.  The
        Provider still writes every sentence; the application only validates
        the JSON shape and joins the returned paragraphs.  It is used once
        after a naturalness failure, not as a post-write humanizer.
        """
        layers = context.get("context_layers") or {}
        facts = {
            "current_time": layers.get("current_time") or current_state.get("time") or "",
            "current_location": layers.get("current_location") or current_state.get("location") or "",
            "known_facts": current_state.get("known_facts") or [],
            "open_threads": current_state.get("open_threads") or [],
        }
        prompt = (
            "只写一个连续的中文网文场景，输出严格 JSON，不要 Markdown、解释或写作说明。"
            "这是一次现场写作，不是提纲。先让人物做事，信息在动作、对白、物件变化和可感知结果中出现；"
            "情绪通过人物做了什么、没做什么来表现，不替人物解释心理。"
            "每个段落只落到一个具体现场点，允许答非所问、拿错、找不到、被打断，事情可以暂时没做完。"
            "语言保持平、快、干，不做修辞联想，不用抽象总结，不为了完整而补齐因果。"
            f"\n第{chapter_number}章第{scene_index}场，正文长度控制在 {min_scene_chars}-{max_scene_chars} 字。"
            f"\n【本场契约】{json.dumps(scene_card, ensure_ascii=False)}"
            f"\n【已确认事实】{json.dumps(facts, ensure_ascii=False)}"
            f"\n【上一场末尾】{previous_scene_tail or '本章开端，直接从本场动作起笔。'}"
            f"\n【前场交接】{json.dumps(previous_handoffs[-2:], ensure_ascii=False)}"
            "\n返回：{\"paragraphs\":[\"正文段落1\",\"正文段落2\",\"正文段落3\"]}。"
            "paragraphs 必须是 4-9 个非空字符串；每个字符串只能是一段，不得含换行；"
            "必须完成一次目标、阻碍、选择和现场结果，然后停在下一步压力上。"
        )
        usage = {"tokens_input": 0, "tokens_output": 0, "cost": 0.0, "model": None}
        try:
            result = await self.ai_gateway.generate_json(
                prompt,
                system_prompt=(
                    "你是中文网文的现场写作者，只返回合法 JSON。"
                    "正文必须具体、克制、可继续阅读；不要写作者判断，不要解释写作方法。"
                ),
                max_tokens=min(SCENE_PROVIDER_TOKEN_CAP, max(900, int(max_scene_chars * 1.20))),
                temperature=0.28,
                prompt_name="v7.generation.scene.structured_plain",
                prompt_version="1.0.0",
            )
            usage.update(result.get("usage") or {})
        except Exception as exc:
            return {
                "accepted": False,
                "reason": f"structured_plain_provider_error:{type(exc).__name__}",
                "usage": usage,
            }

        data = result.get("data") if isinstance(result, dict) else None
        paragraphs = data.get("paragraphs") if isinstance(data, dict) else None
        if not isinstance(paragraphs, list) or not 4 <= len(paragraphs) <= 9:
            return {
                "accepted": False,
                "reason": "structured_plain_invalid_paragraphs",
                "usage": usage,
            }
        cleaned = [str(item or "").strip() for item in paragraphs]
        if any(not item or "\n" in item or "\r" in item for item in cleaned):
            return {
                "accepted": False,
                "reason": "structured_plain_invalid_text",
                "usage": usage,
            }
        candidate = "\n\n".join(cleaned)
        candidate_chars = chinese_word_count(candidate)
        naturalness = inspect_generation_naturalness(candidate)
        if not min_scene_chars <= candidate_chars <= max_scene_chars or not naturalness.get("passed"):
            return {
                "accepted": False,
                "reason": "structured_plain_quality_rejected",
                "candidate_chars": candidate_chars,
                "naturalness": naturalness,
                "usage": usage,
            }
        return {
            "accepted": True,
            "text": candidate,
            "candidate_chars": candidate_chars,
            "naturalness": naturalness,
            "usage": usage,
            "provider_result": {
                "text": candidate,
                "provider": getattr(self.ai_gateway, "provider", ""),
                "model": usage.get("model"),
                "tokens_input": usage.get("tokens_input", 0),
                "tokens_output": usage.get("tokens_output", 0),
                "cost": usage.get("cost", 0.0),
                "finish_reason": "stop",
                "truncated": False,
            },
        }

    @staticmethod
    def _local_segment_issue_codes(issues: list[dict[str, Any]]) -> set[str]:
        """Return issues that can be repaired without rewriting the scene."""
        return {
            str(item.get("code") or "")
            for item in issues
            if isinstance(item, dict)
        }.intersection({
            "dash_density",
            "ai_phrase",
            "repeated_paragraph_opening",
            "repeated_tic",
            "scene_meta_leakage",
        })

    @staticmethod
    def _segment_candidate_score(
        candidate: str,
        baseline: str,
        *,
        feature_card: dict[str, Any] | None = None,
    ) -> tuple[tuple[int, int, float], dict[str, Any]]:
        """Score a local candidate by unresolved internal risks only.

        The score is a selection aid, not an external AI-detector result.  A
        candidate that grows or raises the internal risk score is rejected;
        local replacements therefore cannot silently make a clean segment
        worse while chasing a different style signal.
        """
        candidate_metrics = analyze_deai_patterns(candidate)
        baseline_metrics = analyze_deai_patterns(baseline)
        candidate_flags = {
            str(item.get("code"))
            for item in (candidate_metrics.get("flags") or [])
            if isinstance(item, dict)
            and str(item.get("severity") or "").lower() in {"medium", "high"}
        }
        baseline_flags = {
            str(item.get("code"))
            for item in (baseline_metrics.get("flags") or [])
            if isinstance(item, dict)
            and str(item.get("severity") or "").lower() in {"medium", "high"}
        }
        candidate_local_flags = {
            code
            for segment in (build_generation_critic_report(candidate, candidate_metrics).get("segments") or [])
            for code in (segment.get("risk_codes") or [])
        }
        baseline_local_flags = {
            code
            for segment in (build_generation_critic_report(baseline, baseline_metrics).get("segments") or [])
            for code in (segment.get("risk_codes") or [])
        }
        candidate_flags.update(candidate_local_flags)
        baseline_flags.update(baseline_local_flags)
        size_ratio = len(re.sub(r"\s+", "", candidate)) / max(
            1,
            len(re.sub(r"\s+", "", baseline)),
        )
        risk_delta = int(candidate_metrics.get("risk_score") or 0) - int(
            baseline_metrics.get("risk_score") or 0
        )
        rejected = (
            risk_delta > 0
            or size_ratio < 0.72
            or size_ratio > 1.30
            or float((candidate_metrics.get("duplicate_paragraphs") or {}).get("duplicate_ratio") or 0.0) > 0.01
        )
        unresolved = len(candidate_flags)
        # A feature card is only a tie-breaker. It never overrides the
        # no-regression guard or causes a fixed target cadence.
        texture_distance = 0.0
        profile = (feature_card or {}).get("positive_profile") or {}
        if profile:
            candidate_mean = float(candidate_metrics.get("sentence_length_mean") or 0.0)
            target_mean = float(profile.get("sentence_length_p50") or 0.0)
            if target_mean:
                texture_distance = abs(candidate_mean - target_mean) / target_mean
        key = (
            999 if rejected else unresolved,
            999 if rejected else int(candidate_metrics.get("risk_score") or 0),
            999.0 if rejected else round(texture_distance + abs(1.0 - size_ratio) * 0.15, 4),
        )
        return key, {
            "risk_score": int(candidate_metrics.get("risk_score") or 0),
            "baseline_risk_score": int(baseline_metrics.get("risk_score") or 0),
            "risk_delta": risk_delta,
            "unresolved_flags": sorted(candidate_flags),
            "baseline_flags": sorted(baseline_flags),
            "size_ratio": round(size_ratio, 4),
            "rejected": rejected,
            "metrics": candidate_metrics,
        }

    async def _generate_best_local_segment_candidate(
        self,
        *,
        chapter_number: int,
        scene_index: int,
        context: dict[str, Any],
        scene_card: dict[str, Any],
        source_text: str,
        metrics: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Generate three independent local variants for high-risk paragraphs.

        The rest of the scene is immutable.  This is intentionally bounded to
        one generation-time pass and uses the internal critic only to locate
        candidate paragraphs; the Writer never sees a Zhuque target or score.
        """
        issue_codes = self._local_segment_issue_codes(issues)
        if not issue_codes:
            return {"text": source_text, "accepted": False, "reason": "no_local_issue"}
        critic = build_generation_critic_report(source_text, metrics)
        segments = critic.get("segments") or []
        if not segments:
            return {"text": source_text, "accepted": False, "reason": "no_local_segments"}
        expected_indexes = {
            int(item["paragraph_index"])
            for item in segments
            if isinstance(item, dict) and str(item.get("paragraph_index")).isdigit()
        }
        if not expected_indexes:
            return {"text": source_text, "accepted": False, "reason": "no_valid_segments"}

        paragraphs = [item.strip() for item in re.split(r"\n{2,}|\n", source_text) if item.strip()]
        feature_card = feature_card_from_style_card(
            (context.get("context_layers") or {}).get("style_card") or {}
        )
        feature_block = render_prose_feature_card(feature_card)
        guidance = {
            "repeated_paragraph_opening": "不要让段落都从姓名起笔；优先从正在发生的动作、物件、声音、环境或他人反应起笔。",
            "explicit_explanation": "删除替读者总结意义的句子，把结论改成一个可见动作、选择或后果。",
            "dash_density": "叙述优先使用句号、逗号和具体动作承接；只有对白确有打断时才保留破折号。",
            "repeated_tic": "保留必要反应，但把至少一处模板动作换成具体视线、手部动作、停顿后的决定或现场后果。",
            "scene_meta_leakage": "删除写作工程说明，只保留人物正在经历的现场。",
        }
        segment_payload = []
        for item in segments:
            index = int(item["paragraph_index"])
            segment_payload.append({
                "paragraph_index": index,
                "text": paragraphs[index],
                "before": paragraphs[index - 1][-180:] if index > 0 else "",
                "after": paragraphs[index + 1][:180] if index + 1 < len(paragraphs) else "",
                "repair_goal": "；".join(
                    guidance.get(code, "保持自然、具体、可读")
                    for code in item.get("risk_codes") or []
                ),
            })
        strategies = (
            "偏动作推进：让现场行为先落地",
            "偏人物即时反应：保留误判、避答或临时决定",
            "偏物件与环境反馈：让具体痕迹承担信息",
        )
        usage = {"tokens_input": 0, "tokens_output": 0, "cost": 0.0, "model": None}
        candidates: list[dict[str, Any]] = []
        for strategy in strategies:
            prompt = (
                "只做局部正文修订，不要重写整场。下面列出的段落是需要处理的局部片段，"
                "未列出的段落必须保持不变。保留所有人物、事实、因果、时间顺序和信息边界，"
                "不要新增剧情，不要删掉段落，不要把文字改成标准模板。\n"
                f"本场契约：{json.dumps(scene_card, ensure_ascii=False)}\n"
                f"本次组织方式：{strategy}\n"
                f"{feature_block}\n"
                "自然叙事原则：事件先于解释；人物可以答非所问、误判或停在半句话；"
                "不要总结主题，不要为了显得像真人加入错别字、无意义动作或怪癖。\n"
                f"需要修订的局部段落：{json.dumps(segment_payload, ensure_ascii=False)}\n"
                "只输出 JSON：{\"replacements\":[{\"paragraph_index\":整数,\"text\":\"完整替换段落\"}]}。"
                "replacements 必须覆盖全部列出的 paragraph_index，每个 text 只能是一段，不能含换行。"
            )
            result = await self.ai_gateway.generate_json(
                prompt,
                system_prompt=(
                    "你是中文网文的局部表达编辑，只返回严格 JSON。"
                    "只处理给定段落，不输出解释，不改写未列出的内容。"
                ),
                max_tokens=min(1800, max(420, len(json.dumps(segment_payload, ensure_ascii=False)) // 2)),
                temperature=0.72 + (0.05 if "即时" in strategy else 0.0),
                prompt_name="v7.generation.segment_candidates",
                prompt_version="1.0.0",
            )
            call_usage = result.get("usage") or {}
            usage["tokens_input"] += int(call_usage.get("tokens_input") or 0)
            usage["tokens_output"] += int(call_usage.get("tokens_output") or 0)
            usage["cost"] += float(call_usage.get("cost") or 0.0)
            usage["model"] = call_usage.get("model") or usage["model"]
            replacements = validate_segment_replacement_payload(
                result.get("data"),
                expected_indexes,
            )
            if replacements is None:
                continue
            candidate_text = apply_segment_replacements(source_text, replacements)
            if not candidate_text:
                continue
            key, evidence = self._segment_candidate_score(
                candidate_text,
                source_text,
                feature_card=feature_card,
            )
            candidates.append({
                "strategy": strategy,
                "text": candidate_text,
                "key": key,
                "evidence": evidence,
            })
        _baseline_key, baseline_evidence = self._segment_candidate_score(
            source_text,
            source_text,
            feature_card=feature_card,
        )
        if not candidates:
            return {
                "text": source_text,
                "accepted": False,
                "reason": "no_valid_candidate",
                "candidate_count": 0,
                "locked_paragraph_count": len(critic.get("locked_paragraph_indexes") or []),
                "usage": usage,
            }
        candidates.sort(key=lambda item: item["key"])
        winner = candidates[0]
        winner_flags = set(winner["evidence"].get("unresolved_flags") or [])
        baseline_flags = set(baseline_evidence.get("unresolved_flags") or [])
        accepted = (
            not winner["evidence"].get("rejected")
            and winner["evidence"].get("risk_score", 0) <= baseline_evidence.get("risk_score", 0)
            and (winner_flags < baseline_flags or winner["evidence"].get("risk_delta", 0) < 0)
        )
        return {
            "text": winner["text"] if accepted else source_text,
            "accepted": accepted,
            "reason": "best_of_3" if accepted else "all_candidates_rejected_by_no_regression",
            "candidate_count": len(candidates),
            "locked_paragraph_count": len(critic.get("locked_paragraph_indexes") or []),
            "risk_segment_count": len(segments),
            "baseline": baseline_evidence,
            "winner": winner["evidence"],
            "winner_strategy": winner["strategy"],
            "usage": usage,
        }

    @staticmethod
    def _merge_scene_state(
        current: dict[str, Any],
        handoff: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge a Provider-produced scene checkpoint without inventing facts."""
        merged = dict(current or {})
        for key in ("time", "location", "next_bridge"):
            value = handoff.get(key)
            if isinstance(value, str) and value.strip():
                merged[key] = value.strip()
        for key in ("known_facts", "open_threads", "state_changes", "continuity_warnings"):
            values = handoff.get(key)
            if isinstance(values, list):
                merged[key] = [item for item in values if str(item).strip()]
        return merged

    async def _extract_scene_handoff(
        self,
        *,
        chapter_number: int,
        scene_index: int,
        scene_card: dict[str, Any],
        scene_text: str,
        current_state: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Ask the real Provider for the next-scene state checkpoint.

        This is not a prose audit.  It is a compact write-time transaction:
        the next scene cannot rely on facts that were not returned in this
        checkpoint, and an unusable checkpoint stops generation instead of
        silently falling back to guessed state.
        """
        checkpoint = await self.ai_gateway.generate_json(
            (
                "请把刚写完的小说场景压缩成下一场景可执行的状态交接 JSON。"
                "只记录正文明确发生或明确知道的事实，不推测作者意图，不补写正文。"
                "next_bridge 必须是下一场景可以直接接住的动作、地点、人物状态或未决问题。\n"
                f"第{chapter_number}章，第{scene_index}场。\n"
                f"场景卡：{json.dumps(scene_card, ensure_ascii=False)}\n"
                f"写入前状态：{json.dumps(current_state, ensure_ascii=False)}\n"
                f"场景正文：\n{scene_text}\n\n"
                "只输出 JSON："
                '{"time":"当前明确时间", "location":"当前明确地点", '
                '"known_facts":["人物现在明确知道的事实"], '
                '"state_changes":["本场真正改变的状态"], '
                '"open_threads":["仍未解决且可继续的线索"], '
                '"continuity_warnings":["下一场不可违反的限制"], '
                '"next_bridge":"下一场直接承接点"}'
            ),
            system_prompt="你是连续剧小说的状态编辑，只输出严格合法 JSON，不写解释。",
            max_tokens=900,
            temperature=0.15,
            prompt_name="v7.generation.scene_handoff",
            prompt_version=SCENE_SERIAL_GENERATION_VERSION,
        )
        data = checkpoint.get("data") if isinstance(checkpoint, dict) else None
        if not isinstance(data, dict):
            raise AIGatewayError("scene handoff contract invalid: expected an object")
        for key in ("known_facts", "state_changes", "open_threads", "continuity_warnings"):
            if not isinstance(data.get(key), list):
                raise AIGatewayError(f"scene handoff contract invalid: {key} must be a list")
        if not str(data.get("next_bridge") or "").strip():
            raise AIGatewayError("scene handoff contract invalid: next_bridge is empty")
        data["schema_version"] = SCENE_HANDOFF_SCHEMA
        return data, dict(checkpoint.get("usage") or {})

    def _build_scene_generation_prompt(
        self,
        *,
        chapter_number: int,
        context: dict[str, Any],
        scene_plan: dict[str, Any],
        scene_card: dict[str, Any],
        scene_index: int,
        scene_count: int,
        previous_scene_tail: str,
        current_state: dict[str, Any],
        previous_handoffs: list[dict[str, Any]],
        retry_feedback: str = "",
        max_scene_chars: int | None = None,
        generation_path: str = "event_action_dialogue",
    ) -> str:
        context_layers = context.get("context_layers") or {}
        style_card = context_layers.get("style_card") or {}
        prompt_style_card = sanitise_style_card_for_prompt(style_card)
        style_block = json.dumps(
            prompt_style_card,
            ensure_ascii=False,
            separators=(",", ":"),
        )[:4200]
        feature_card = feature_card_from_style_card(style_card)
        feature_block = render_prose_feature_card(feature_card)
        handoff_block = json.dumps(previous_handoffs[-3:], ensure_ascii=False)[:3600]
        progress_block = json.dumps(current_state, ensure_ascii=False)[:3600]
        opening_plan = scene_plan.get("opening_plan") or context_layers.get("opening_plan") or {}
        opening_mode_block = (
            f"{opening_prompt_block(opening_plan)}\n"
            if scene_index == 1
            else ""
        )
        prose_texture_plan = scene_plan.get("prose_texture_plan") or {}
        prose_texture_block = json.dumps(
            prose_texture_plan,
            ensure_ascii=False,
            separators=(",", ":"),
        )[:2600]
        chapter_contract = scene_plan.get("chapter_contract") or {}
        payoff_contract = scene_plan.get("payoff_contract") or {}
        causal_ledger = scene_plan.get("causal_ledger") or []
        causal_contract_block = ""
        if chapter_contract or payoff_contract or causal_ledger:
            causal_contract_json = json.dumps(
                {
                    "chapter_contract": chapter_contract,
                    "payoff_contract": payoff_contract,
                    "causal_ledger": causal_ledger[:6],
                },
                ensure_ascii=False,
            )[:5200]
            causal_contract_block = (
                "【本章因果闭环与代价】\n"
                f"{causal_contract_json}\n"
                "本场若触发契约中的选择、突破、指点、开启、使用能力或其他关键动作，"
                "必须在本场或紧接下一场写出完整链条：触发动作→当场可见/可感知反馈→人物确认这就是代价或规则后果→新的压力。"
                "代价不能只在旁白中与结果并列宣布；至少落到一个具体物件、身体变化、规则变化、他人反应或资源损失，"
                "并让人物行为或对白对这个反馈作出反应。\n"
            )
        opening_instruction = ""
        if scene_index == 1:
            if previous_scene_tail or (context.get("context_layers") or {}).get("previous_tail"):
                opening_instruction = (
                    "这是本章第一场，开头必须从上一章结尾的动作、地点、物件或未决问题接住；"
                    "不要重新介绍世界观，不要把上一章复述一遍。"
                )
            else:
                opening_instruction = (
                    "这是故事开端，没有可供复述的上一章；直接从当前人物正在做的事和眼前问题起笔，"
                    "不要先写世界观沿革、职业史或泛泛环境介绍。"
                )
            opening_instruction += (
                "这是生成期节奏硬约束：前两句必须有动作、具体异常或明确目标；"
                "前120字内必须出现会改变判断、位置、关系、资源或风险的具体事件；"
                "日常铺垫最多一个短段，背景信息必须藏进动作、对白或物件，不能连续用日常拖慢开局；"
                "异常不能只作为奇观或氛围，前420字内必须让人物感知一个具体威胁、禁忌、代价、"
                "被发现风险或会改变选择的后果，并通过动作、对白、物件或身体反应落地。"
            )
        else:
            opening_instruction = (
                "这是本章后续场景，第一段必须接住上一场最后的动作、视线、声音、地点或选择；"
                "不允许用‘与此同时’、‘另一边’或空泛时间跳跃把状态抹掉。"
            )
            opening_instruction += (
                "跨场景桥接是正文硬要求：如果本场地点、时间或在场人物与上一场不同，"
                "不能把 scene_card.handoff 当成已经发生；必须先写离开、到达、等待、寻找、携带物件，"
                "或途中听见/看见的具体线索，让上一场的 state_change 在动作中落地。"
                "上一场若以失败、受伤、争执或重大信息结束，新危机或新解释前必须先出现可见余波、"
                "短暂喘息或主动决定；不得从‘决定去某地’直接跳到‘已经在某地遭遇袭击’。"
            )
            if scene_index == 2:
                opening_instruction += "前半场必须把上一场落点转成新的选择、代价或风险。"
        retry_block = f"\n【上次场景未通过，必须在本次生成中修复】\n{retry_feedback}\n" if retry_feedback else ""
        paragraph_opening_contract = (
            "【自然段首编排（硬结构，生成期执行，不要输出清单）】"
            "本场如果自然形成 8 段或以上，每连续 8 段中同一个两字姓名最多只能作为 2 段的首词，"
            "至少要分散使用动作、物件、声音、环境变化、对白或他人反应起笔；"
            "相邻段落不得用同一个两字姓名起笔，同一姓名全场最多出现在四分之一的段首。"
            "需要写主角时，优先先写正在发生的动作、视线、手中物件或现场后果，再在句中带出姓名；"
            "输出前自行检查段首分布；不要为了达标硬塞无关描写，也不要把所有姓名机械替换成‘他/她’，"
            "段落必须仍然服务于本场目标和压力。"
        )
        natural_generation_contract = (
            "【真人可读正文协议（生成期执行）】"
            "本场只围绕一个正在发生的问题推进：让一个具体动作或对白改变现场，再写人物如何应对，"
            "完成一次选择和可见结果后就收束，不要在同一场并列讲设定、回忆、人物履历和主题。"
            "信息不要一次交代完；允许人物漏听、误判、答非所问、把话说到一半改口，"
            "但每个停顿都必须留下现场压力或下一步选择。"
            "旁白不替读者解释刚刚发生的事情，不写‘这意味着/这说明/他意识到/他心里清楚/不是梦或错觉’式结论；"
            "把结论落到物件位置、动作停顿、资源损失、他人反应或新的限制。"
            "非对白本轮不使用类比，优先写具体颜色、声音、触感、位置和动作；"
            "不要把感觉改写成形容性联想，也不要用重复拿起、放回、转身来填充犹豫。"
            "对话不承担完整说明任务：角色可以避答、打断、说错重点，信息通过说话目的和现场反应逐步露出。"
            "段落长短随压力变化，避免每段都完整走完‘现象→判断→解释→总结’，也不要为了制造人工痕迹加入无关闲笔。"
            "同一身体状态、任务提示、地点信息或规则结果已经写清后，不要换一种表达再次解释；系统/规则只写新增变化。"
            "移动、寻找或赶路最多连续两段没有新事件；第三段前必须出现阻碍、线索、他人介入、路线选择代价或可见后果，"
            "否则压缩移动并收束，不要把走、停、喘、看、继续拆成行程记录。"
        )
        minimum, nominal_maximum = self._scene_length_bounds(scene_card, scene_index=scene_index)
        allowed_maximum = self._scene_allowed_max_chars(scene_card, scene_index=scene_index)
        maximum = max(
            minimum,
            int(max_scene_chars) if max_scene_chars is not None else nominal_maximum,
        )
        if max_scene_chars is not None:
            allowed_maximum = min(allowed_maximum, int(max_scene_chars))
        requested_target = max(1, int(scene_card.get("target_words") or maximum))
        effective_target = max(minimum, min(requested_target, maximum))
        opening_constraint = str(scene_card.get("opening_constraint") or "").strip()
        contract_block = f"\n【本场开头硬约束】\n{opening_constraint}" if opening_constraint else ""
        return (
            "你正在连续写一部长篇中文网络小说，当前任务只写一个场景，不写整章摘要。"
            "直接输出自然正文，不要标题、JSON、解释、场景编号、写作说明或 Markdown。\n"
            f"第{chapter_number}章《{scene_plan.get('chapter_title') or ''}》，"
            f"第{scene_index}/{scene_count}场。\n"
            f"【全书硬事实与前情】\n{context.get('rendered_context') or '无'}\n\n"
            f"【作者风格约束】\n{style_block or '暂无已确认样本；不要伪造固定口癖。'}\n\n"
            f"{feature_block}\n\n"
            f"【本章叙述质地与人物声音（生成前执行）】\n{prose_texture_block or '缺失；先按限知、潜台词和信息延后原则组织正文。'}\n\n"
            f"【本场契约】\n{json.dumps(scene_card, ensure_ascii=False)}\n\n"
            f"【上一场末尾原文】\n{previous_scene_tail or '本章开端，承接全书上一章结尾。'}\n\n"
            f"【已确认的写入状态】\n{progress_block}\n\n"
            f"【前面场景的状态交接】\n{handoff_block or '无'}\n\n"
            f"{opening_mode_block}"
            f"{opening_instruction}\n"
            f"{contract_block}\n"
            f"{causal_contract_block}"
            f"{natural_generation_contract}\n"
            f"{paragraph_opening_contract}\n"
            "本场必须把‘目标→阻碍→人物选择→可见结果/代价’写成现场发生的动作，"
            "让信息从对白、动作、物件、感官和他人反应中自然露出；不要把因果解释成提纲。"
            "跨场景桥接硬要求：如果本场 scene_card.location/time/characters 与上一场交接不同，"
            "必须把 handoff 转译成正文里的离开、到达、等待、寻找、携带物件或途中线索；"
            "不得把‘决定去某地’当作已经到达，也不得用‘与此同时’、‘另一边’或空泛时间跳跃遮蔽移动。"
            "如果上一场发生失败、受伤、争执或重大信息，本场的新危机/新解释前先写可见余波、短暂喘息或主动选择，"
            "让 trigger 从上一场的 state_change 或 handoff 落地。"
            "因果连续性硬要求：人物重返已经出现过的地点、门、物件或线索时，必须先写触发信息、当前目标和主动决定；"
            "不得只用‘鬼使神差’、‘下意识’、‘不知为何’或‘心血来潮’带过。重大袭击、对抗或爆发结束后，"
            "先写一个可见的即时后果或短暂喘息（伤势、资源损失、环境变化、旁观者反应或敌我状态变化），"
            "再进入长段解释、师徒对话或规则说明；不得从战斗结果直接跳到讲设定。"
            "关键异常、开门、封印松动、袭击、修炼变化或新能力必须先写可见前提/征兆，再写人物判断和主动选择，"
            "最后落到读者可见的结果或反馈；如果场景卡没有前提，先在正文补出一个来自已确认事实的具体触发，"
            "不能让门、敌人、法器或规则无前提突然出现。碑文、幻象、梦境或他人话语里的数字/年代属于原说话者，"
            "不得把它改写成主角自己的经历。"
            "人物只能使用已确认的知识，不能让旁观者替作者总结情绪。句子长短、段落长度和起笔方式要有真实变化，"
            "不要把每个段落都写成‘现象→判断→解释→总结’的完整闭环；允许信息暂不解释、动作被打断、人物答非所问、"
            "一句话说到一半改口，或只露出半个结果，但必须服务于人物目标、现场压力和读者追读。"
            "对白要符合具体人物此刻的说话目的，少用整齐的排比、万能反应和抽象总结；每个主要人物至少保留一处不直说的潜台词，"
            "通过停顿、避答、反问、错拿物件或临时改动作露出，而不是由旁白解释。"
            "优先使用本场 prose_texture_plan 指定的限知叙述偏向、感官/物件锚点和人物潜台词；"
            "不要平均分配信息，不要连续使用形容性联想或同一组情绪词，也不要故意加入错别字、病句或无关怪癖伪造人工痕迹。"
            "叙述保持第三人称；门上字、碑文、账册、书信、纸条等直接文字若出现‘我/吾/我们’，"
            "必须用中文引号标明直接文字，不能裸写成叙述者的第一人称。"
            "段落起笔要自然轮换：同一个两字人名不能连续占据多个段首，也不能在本场段落中占多数；"
            "可根据现场需要从动作、物件、声音、环境、对白或他人反应起笔，但不要为了轮换硬塞无关描写，"
            "更不能把人名全部替换成‘他/她’。"
            "破折号只在对白中确有停顿、打断或转折时使用；叙述优先用句号、逗号、动作和声音承接，"
            "不要连续用破折号制造整齐节奏，也不要为了避开检测把正常对白改得生硬。"
            "除非全书硬事实、已确认状态或本场契约明确给出，不要自行补写具体年份、持续年数、伤势来源、功法层级或系统规则；"
            "缺少依据时用动作和现场结果呈现，不要用精确数字制造伪连续性。"
            "本场结束时留下明确的动作、发现、选择或压力，给下一场一个能直接接住的落点；"
            "不要为了达到字数重复冲突。\n"
            f"本场建议约写 {effective_target} 字，参考节拍范围 {minimum}-{nominal_maximum} 字；"
            f"本次生成期本场可用章节剩余额度上限为 {maximum} 字，正文不得超过这个剩余额度；"
            "这是章节预算的生成期硬边界，不是要求把本场写满。"
            "如果完整事件在额度内已经完成，立即收束；优先删掉重复反应、背景解释和无关描写，"
            "不得为了凑章节字数补写日常、环境、回忆或重复反应。"
            f"\n\n{render_generation_style_protocol(generation_path)}"
            f"{retry_block}"
        )

    async def _generate_scene_sequence(
        self,
        *,
        chapter_number: int,
        context: dict[str, Any],
        scene_plan: dict[str, Any],
        target_word_count: int,
        chapter_max_chars: int,
        chapter_reader_max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Generate a chapter as a serial chain of real Provider scene calls."""
        cards = self._normalise_scene_cards(scene_plan, target_word_count=target_word_count)
        current_state = {
            "time": (context.get("context_layers") or {}).get("current_time") or "",
            "location": (context.get("context_layers") or {}).get("current_location") or "",
            "known_facts": list((context.get("context_layers") or {}).get("known_facts") or []),
            "open_threads": list(
                ((context.get("context_layers") or {}).get("previous_transition_contract") or {}).get(
                    "open_threads"
                )
                or []
            ),
        }
        scene_texts: list[str] = []
        scene_outputs: list[dict[str, Any]] = []
        handoffs: list[dict[str, Any]] = []
        usage = {"tokens_input": 0, "tokens_output": 0, "cost": 0.0, "model": None}
        # The chapter budget is the only hard reader-facing limit. Future beat
        # targets guide allocation, but a scene may naturally take more or
        # less space; the scheduler reallocates slack after a complete scene.
        planning_max_chars = min(
            chapter_max_chars,
            int(chapter_reader_max_chars or chapter_max_chars),
        )

        def add_call_usage(call_usage: dict[str, Any]) -> None:
            usage["tokens_input"] += int(call_usage.get("tokens_input") or 0)
            usage["tokens_output"] += int(call_usage.get("tokens_output") or 0)
            usage["cost"] += float(call_usage.get("cost") or 0.0)
            usage["model"] = call_usage.get("model") or usage["model"]

        for index, card in enumerate(cards, start=1):
            accepted_chars = chinese_word_count("\n\n".join(scene_texts))
            minimum_scene_chars, nominal_max_scene_chars = self._scene_length_bounds(
                card,
                scene_index=index,
            )
            future_minimum_chars = sum(
                self._scene_length_bounds(future_card, scene_index=future_index)[0]
                for future_index, future_card in enumerate(cards[index:], start=index + 1)
            )
            future_target_chars = sum(
                int(future_card.get("target_words") or 0)
                for future_card in cards[index:]
            )
            remaining_scene_budget = chapter_max_chars - accepted_chars - future_minimum_chars
            if remaining_scene_budget < minimum_scene_chars:
                raise AIGatewayError(
                    "scene serial chapter budget exhausted before the next scene: "
                    f"scene={index}, accepted_chars={accepted_chars}, "
                    f"chapter_max_chars={chapter_max_chars}"
                )
            remaining_planned_budget = (
                planning_max_chars - accepted_chars - future_target_chars
            )
            if target_word_count >= 1800 and future_target_chars:
                # If the planner leaves the current beat less than its normal
                # completion envelope, move only slack from later beats before
                # calling the Provider. This preserves a complete current
                # event and keeps the chapter inside the reader budget; it is
                # preferable to forcing a valid scene into a truncation-sized
                # envelope and asking a later audit to repair the damage.
                required_reallocation = max(
                    0,
                    nominal_max_scene_chars - remaining_planned_budget,
                )
                if required_reallocation and self._rebalance_future_scene_targets(
                    cards,
                    future_start=index,
                    excess_chars=required_reallocation,
                ):
                    future_minimum_chars = sum(
                        self._scene_length_bounds(future_card, scene_index=future_index)[0]
                        for future_index, future_card in enumerate(cards[index:], start=index + 1)
                    )
                    remaining_scene_budget = (
                        chapter_max_chars - accepted_chars - future_minimum_chars
                    )
                    future_target_chars = sum(
                        int(future_card.get("target_words") or 0)
                        for future_card in cards[index:]
                    )
                    remaining_planned_budget = (
                        planning_max_chars - accepted_chars - future_target_chars
                    )
            # The reader target is a planning signal, not another chapter
            # ceiling.  A scene may consume more of that target when the
            # chapter hard budget and future scenes' minimum expression space
            # still remain available.  Only the chapter budget below can
            # block generation.
            # Do not turn a beat's suggested size into a hard scene ceiling.
            # Use a generous Provider completion capacity for token budgeting;
            # the generated text is judged only against the chapter budget.
            soft_provider_capacity = max(
                nominal_max_scene_chars,
                int(self._scene_allowed_max_chars(card, scene_index=index) * 1.35),
            )
            scene_max_chars = min(remaining_scene_budget, soft_provider_capacity)
            feedback = ""
            accepted_scene = ""
            scene_metrics: dict[str, Any] = {}
            scene_critic: dict[str, Any] = {}
            scene_candidate_selection: dict[str, Any] = {}
            scene_warnings: list[dict[str, Any]] = []
            previous_issue_codes: set[str] = set()
            compression_mode = False
            truncation_seen = False
            final_scene_variance = False
            attempts_used = 0
            candidate = ""
            min_scene_chars, _planned_max_scene_chars = self._scene_length_bounds(
                card,
                scene_index=index,
            )
            pacing_max_scene_chars = self._scene_allowed_max_chars(
                card,
                scene_index=index,
            )
            # Keep ordinary quality repairs at the base retry limit. A
            # Provider truncation is different: the candidate is incomplete,
            # so the already-defined final truncation margin needs one extra
            # bounded attempt after the normal repair also truncates.
            max_scene_attempts = SCENE_SERIAL_MAX_ATTEMPTS
            attempt = 0
            while attempt < max_scene_attempts:
                attempt_warnings: list[dict[str, Any]] = []
                provider = str(getattr(self.ai_gateway, "provider", "") or "").lower()
                naturalness_retry = bool(
                    attempt
                    and previous_issue_codes
                    and self._is_style_only_retry(previous_issue_codes)
                )
                final_budget_retry = bool(
                    attempt >= SCENE_SERIAL_MAX_ATTEMPTS
                    and previous_issue_codes == {"scene_chapter_budget_overrun"}
                    and future_minimum_chars == 0
                    and future_target_chars == 0
                )
                overlong_margin = (
                    SCENE_OPENAI_OVERLONG_REPAIR_MARGIN
                    if provider == "openai"
                    else SCENE_DEEPSEEK_OVERLONG_REPAIR_MARGIN
                )
                if final_budget_retry:
                    repair_margin = 0.72 if provider != "openai" else 0.76
                elif attempt == 0:
                    repair_margin = None
                elif compression_mode:
                    if truncation_seen:
                        repair_margin = SCENE_MIXED_TRUNCATION_OVERLONG_REPAIR_MARGIN
                    elif previous_issue_codes.intersection({
                        "scene_explanatory_narration",
                        "scene_metaphor_density",
                        "scene_repeated_action_loop",
                        "structural_ai_smell",
                        "repeated_paragraph_opening",
                        "uniform_cadence",
                        "repeated_tic",
                        "dash_density",
                        "ai_phrase",
                        "scene_state_echo",
                        "scene_procedural_motion",
                        "scene_subject_opening",
                    }):
                        # A style repair still needs a complete Provider
                        # response.  The old compression branch saw the
                        # added overlong flag and immediately applied the
                        # 0.86 token ratio, which could truncate a valid
                        # naturalness candidate before the handoff was
                        # written.  Keep completion headroom here; the
                        # character envelope remains the hard length gate.
                        repair_margin = 1.15
                    elif "scene_provider_truncated" in previous_issue_codes:
                        # Keep a compressed contract after a truncation; do
                        # not use the normal truncation expansion, which can
                        # recreate the original overlong candidate.
                        repair_margin = min(
                            0.98,
                            overlong_margin + 0.10 * attempt,
                        )
                    else:
                        # Keep the same compact ratio for repeated overlong
                        # attempts. Shrinking it again compounds with the
                        # candidate-size cap below and can make a short final
                        # scene truncate before its handoff is complete.
                        repair_margin = overlong_margin
                elif "scene_provider_truncated" in previous_issue_codes:
                    repair_margin = (
                        (
                            SCENE_OPENAI_FINAL_TRUNCATION_REPAIR_MARGIN
                            if provider == "openai"
                            else SCENE_DEEPSEEK_FINAL_TRUNCATION_REPAIR_MARGIN
                        )
                        if attempt >= 2
                        else (
                            SCENE_OPENAI_TRUNCATION_REPAIR_MARGIN
                            if provider == "openai"
                            else SCENE_DEEPSEEK_TRUNCATION_REPAIR_MARGIN
                        )
                    )
                elif naturalness_retry:
                    # Naturalness repairs must still be complete prose.  The
                    # old overlong margin (0.86) was intended for a known
                    # over-budget candidate, but it also applied to a style
                    # retry and cut DeepSeek below the space needed to finish
                    # the scene.  Keep the reader character envelope as the
                    # hard limit; only lower the token ceiling after an
                    # actual length failure.
                    repair_margin = 1.15
                else:
                    repair_margin = (
                        SCENE_OPENAI_OVERLONG_REPAIR_MARGIN
                        if provider == "openai"
                        else SCENE_DEEPSEEK_OVERLONG_REPAIR_MARGIN
                    )
                attempt_max_scene_chars = scene_max_chars
                if "scene_overlong" in previous_issue_codes:
                    # The first retry used a token margin but still exposed
                    # the wider chapter remainder to the Provider. Tighten
                    # both contracts on the next call so the model receives
                    # an actual shorter scene budget instead of a warning.
                    attempt_max_scene_chars = min(
                        scene_max_chars,
                        pacing_max_scene_chars,
                    )
                if (
                    candidate
                    and compression_mode
                    and "scene_provider_truncated" not in previous_issue_codes
                ):
                    # Use the previous concrete size as feedback. This is a
                    # generation-time compression contract, not a text slice:
                    # the next Provider call must write a shorter complete
                    # scene with the same causal result.
                    previous_candidate_chars = chinese_word_count(candidate)
                    attempt_max_scene_chars = min(
                        attempt_max_scene_chars,
                        max(
                            min_scene_chars,
                            int(previous_candidate_chars * 0.80),
                        ),
                    )
                if self._should_shrink_retry_envelope(
                    previous_issue_codes=previous_issue_codes,
                    attempt=attempt,
                    compression_mode=compression_mode,
                ):
                    # A complete over-budget candidate needs a smaller
                    # character envelope instead of another full-size retry.
                    # Truncated candidates are explicitly excluded above.
                    attempt_max_scene_chars = max(
                        min_scene_chars,
                        int(attempt_max_scene_chars * 0.82),
                    )
                if final_budget_retry:
                    # The previous complete candidate was only a few
                    # characters over the chapter ceiling. Give the Provider
                    # a materially smaller envelope and require a complete
                    # event, rather than slicing the accepted prose after the
                    # fact or widening the reader budget.
                    attempt_max_scene_chars = max(
                        min_scene_chars,
                        int(remaining_scene_budget * SCENE_FINAL_BUDGET_RETRY_RATIO),
                    )
                scene_token_limit = self._scene_generation_max_tokens(
                    card,
                    scene_index=index,
                    max_scene_chars=attempt_max_scene_chars,
                    token_margin=repair_margin,
                )
                style_only_retry = bool(
                    attempt
                    and candidate
                    and self._is_style_only_retry(previous_issue_codes)
                )
                scene_prompt = self._build_scene_generation_prompt(
                    chapter_number=chapter_number,
                    context=context,
                    scene_plan=scene_plan,
                    scene_card=card,
                    scene_index=index,
                    scene_count=len(cards),
                    previous_scene_tail=scene_texts[-1][-1200:] if scene_texts else (
                        (context.get("context_layers") or {}).get("previous_tail") or ""
                    ),
                    current_state=current_state,
                    previous_handoffs=handoffs,
                    retry_feedback=feedback,
                    max_scene_chars=attempt_max_scene_chars,
                    generation_path=(
                        "plain_factual"
                        if naturalness_retry
                        else select_generation_style_path(
                            chapter_number,
                            index,
                            attempt,
                        )
                    ),
                )
                scene_system_prompt = (
                    "你是稳定写作同一本长篇小说的中文网文作者。只输出本场正文，"
                    "不输出任何工程说明；优先保证人物声音、动作因果、现场感和自然节奏。"
                    + third_person_generation_contract()
                    + content_generation_contract(self.quality_profile)
                )
                if attempt and candidate:
                    if "scene_opening_contract" in previous_issue_codes:
                        opening_plan = scene_plan.get("opening_plan") or {}
                        opening_mode = opening_plan.get("mode") or "object"
                        opening_label = opening_plan.get("label") or opening_mode
                        metaphor_rule = (
                            "非对白中的‘像、好像、仿佛、如同、宛如、犹如’必须为0处；"
                            if "scene_metaphor_density" in previous_issue_codes
                            else ""
                        )
                        scene_system_prompt = (
                            "你是中文网文的生成期开场修复编辑。必须依据本场 scene_card、已确认状态和"
                            "上一场交接点，从头重新写出完整正文；事件、人物、因果和本场结果必须保留。"
                            f"本次指定开场类型为‘{opening_label}’（{opening_mode}），首句必须直接让指定的"
                            "物件异常、外部事件、对白冲突或环境变化先发生，不能先写人物扫、走、站、抬手等动作。"
                            f"{metaphor_rule}只输出正文，不输出说明。"
                            + third_person_generation_contract()
                            + content_generation_contract(self.quality_profile)
                        )
                    elif "scene_metaphor_density" in previous_issue_codes:
                        scene_system_prompt = (
                            "你是中文网文的生成期字面现场编辑。必须依据本场 scene_card、已确认状态和"
                            "上一场交接点，从头重写出完整正文；保留事件、人物、因果和本场结果，"
                            "同时删除重复动作、重复环境和解释性总结；不能从上一版末尾续写，不能照抄句式。"
                            "本次非对白中的‘像、好像、仿佛、如同、宛如、犹如’必须为0处，"
                            "声音、光线、触感和情绪全部直接写实际变化；如果本场也超出章节剩余预算，"
                            "优先删掉重复反应和无关描写，但不能删掉目标、阻碍、选择或结果。只输出正文。"
                            + third_person_generation_contract()
                            + content_generation_contract(self.quality_profile)
                        )
                    elif style_only_retry:
                        if "scene_state_echo" in previous_issue_codes:
                            route = (
                                "上一版重复解释同一身体状态、任务提示或规则结果；本次只保留第一次完整呈现，"
                                "后续只写发生变化的症状、代价、物件后果或人物行动。系统/规则提示只保留新增字段，"
                                "不得换一种比喻、换一种身体感受或再写一遍相同任务。"
                            )
                        elif "scene_procedural_motion" in previous_issue_codes:
                            route = (
                                "上一版像赶路记录；压缩没有事件的走、停、喘、看和继续，"
                                "保留必要的路径信息，并在移动段前半段落下一个来自本场事实的阻碍、线索变化、"
                                "他人介入或带代价的路线选择。没有新变化就收束，不得用环境和身体感受补长度。"
                            )
                        elif "scene_subject_opening" in previous_issue_codes:
                            route = (
                                "上一版多个段落都从同一主语起笔；重新组织段落落点，"
                                "从正在发生的动作、手边物件、声音、环境后果、对白或他人反应起笔，"
                                "主语自然放进句中，不能只把人名机械替换成‘他/她’。"
                            )
                        elif "repeated_paragraph_opening" in previous_issue_codes:
                            route = (
                                "本轮专门重排段落起笔：上一版同一姓名占据过多段首；"
                                "重写时禁止让该姓名出现在连续段首，也不要让它作为超过四分之一段落的前两字；"
                                "优先从正在发生的动作、手边物件、声音、环境后果、对白或他人反应起笔，"
                                "姓名放进句子中自然带出。不能只把姓名替换成‘他/她’，不能删段或把多个段落合并，"
                                "必须保持本场事实、因果和结果。"
                            )
                        elif "scene_metaphor_density" in previous_issue_codes:
                            route = (
                                "本轮改用朴素现场推进：非对白中的‘像、好像、仿佛、如同、"
                                "宛如、犹如’必须为0处；声音、光线、触感和情绪都直接写实际变化，"
                                "不用任何联想替代现场。"
                            )
                        elif attempt == 1:
                            route = (
                                "本轮改用动作与对白推进：先让人物做事或说半句话，"
                                "用现场阻碍改变动作，不用旁白解释。"
                            )
                        else:
                            route = (
                                "本轮改用物件与后果推进：只保留一个可观察异常，"
                                "让人物试错或作出选择，不用比喻和总结句串联。"
                            )
                        scene_system_prompt = (
                            "你是中文网文的生成期表达修复编辑。必须依据本场 scene_card、已确认状态和"
                            "上一场交接点，从头重新写出完整正文；不要复制上一版的句式、段首、动作顺序或段落结构，"
                            "不要从上一版末尾续写。事件、人物和因果必须保留，表达必须重新组织；"
                            f"{route}只输出正文，不输出说明。"
                            + third_person_generation_contract()
                            + content_generation_contract(self.quality_profile)
                        )
                    else:
                        scene_system_prompt = (
                            "你是中文网文的生成期压缩编辑。当前不是自由扩写，必须在保留人物、"
                            "事实顺序、动作因果和本场结果的前提下，完整重写上一版候选正文；"
                            "删除重复反应、重复环境、解释性总结和无关支线，不能从末尾续写，"
                            "不能把超长正文原样复制。只输出压缩后的完整正文，不输出说明。"
                            + third_person_generation_contract()
                            + content_generation_contract(self.quality_profile)
                        )
                # One writer call per attempt.  The previous implementation
                # could call a structured candidate, then a full rewrite, and
                # then local Best-of-3 replacements in the same scene.  That
                # inflated cost and made the final prose path hard to reason
                # about without improving the real Provider result reliably.
                result = await self.ai_gateway.generate(
                    scene_prompt,
                    system_prompt=scene_system_prompt,
                    max_tokens=scene_token_limit,
                    temperature=(0.38 if style_only_retry else 0.58) if attempt else 0.62,
                    prompt_name="v7.generation.scene" if attempt == 0 else "v7.generation.scene.repair",
                    prompt_version=SCENE_SERIAL_GENERATION_VERSION,
                    expand_on_truncation=False,
                )
                add_call_usage(result)
                truncated = bool(result.get("truncated")) or str(
                    result.get("finish_reason") or ""
                ).lower() == "length"
                if truncated:
                    truncation_seen = True
                candidate = str(result.get("text") or "").strip()
                scene_metrics = analyze_deai_patterns(candidate)
                issues = []
                if not truncated and index == 1:
                    opening_plan = scene_plan.get("opening_plan") or (
                        context.get("context_layers") or {}
                    ).get("opening_plan") or {}
                    opening_gate = inspect_opening(
                        candidate,
                        # The scheduler is a writing hint, not a second
                        # lexical classifier gate.  Requiring an exact mode
                        # here rejected concrete openings that did not match
                        # the narrow regex, even when the prose contract and
                        # downstream review could evaluate them normally.
                        requested_mode=None,
                        chapter_number=chapter_number,
                        recent_modes=opening_plan.get("forbidden_recent_modes") or [],
                    )
                    for opening_failure in opening_gate.get("flags") or []:
                        issues.append({
                            "code": "scene_opening_contract",
                            "severity": "high",
                            "message": (
                                "首场开场类型未执行，必须在生成期按指定类型完整重写："
                                f"{opening_failure.get('message')}"
                            ),
                            "evidence": opening_failure.get("evidence"),
                        })
                if not truncated:
                    scene_pov_metrics = analyze_third_person_narrative(candidate)
                    if not scene_pov_metrics.get("passed"):
                        issues.append({
                            "code": "scene_first_person_narrative",
                            "severity": "high",
                            "message": (
                                "本场叙述泄漏第一人称，必须在生成期修复；"
                                "碑文、账册、字条等直接文字必须用中文引号标明，不能裸写在叙述句中。"
                            ),
                            "evidence": scene_pov_metrics,
                        })
                if truncated:
                    issues.append({
                        "code": "scene_provider_truncated",
                        "severity": "high",
                        "message": (
                            f"Provider 在本场 {scene_token_limit} token 上限截断；"
                            f"本次场景字符预算为 {attempt_max_scene_chars}；"
                            "必须在同一字符预算内重写并收束；重试可以增加 token 完成空间，"
                            "但不能扩写事件或扩大读者预算"
                        ),
                        "evidence": {
                            "attempt": attempt + 1,
                            "token_limit": scene_token_limit,
                            "max_scene_chars": attempt_max_scene_chars,
                            "candidate_chars": chinese_word_count(candidate),
                        },
                    })
                else:
                    issues.extend(self._scene_naturalness_flags(
                        candidate,
                        accepted_text="\n\n".join(scene_texts),
                    ))
                if not truncated and candidate:
                    # Keep the critic as observable evidence only.  It no
                    # longer starts a second nested generation tree inside a
                    # scene; the single bounded rewrite above is the only
                    # canonical repair path.
                    scene_critic = build_generation_critic_report(candidate, scene_metrics)
                candidate_word_count = chinese_word_count(candidate)
                projected_chapter_chars = accepted_chars + candidate_word_count
                # A truncated completion is an incomplete candidate, not an
                # overlong scene.  Measuring its partial text against pacing
                # and chapter budgets can switch the retry state into
                # compression mode and shrink the next token limit below what
                # is needed to finish the same scene.  Only complete Provider
                # output is eligible for length and pacing validation.
                if not truncated:
                    if self._scene_exceeds_chapter_budget(
                        accepted_chars=accepted_chars,
                        candidate_chars=candidate_word_count,
                        future_minimum_chars=future_minimum_chars,
                        chapter_max_chars=chapter_max_chars,
                    ):
                        issues.append({
                            "code": "scene_chapter_budget_overrun",
                            "severity": "high",
                            "message": (
                                f"本场候选会使章节达到 {projected_chapter_chars} 字，"
                                f"并挤占后续场景最低 {future_minimum_chars} 字；"
                                f"章节上限为 {chapter_max_chars} 字，必须在生成期收束本场"
                            ),
                        })
                    if (
                        target_word_count >= 1800
                        and accepted_chars + candidate_word_count + future_target_chars
                        > planning_max_chars
                    ):
                        attempt_warnings.append({
                            "code": "scene_reader_budget_variance",
                            "severity": "low",
                            "message": (
                                f"本场候选及后续目标合计 "
                                f"{accepted_chars + candidate_word_count + future_target_chars} 字，"
                                f"高于读者目标预算 {planning_max_chars} 字；章节总预算仍是唯一硬上限"
                            ),
                        })
                    if candidate_word_count < min_scene_chars:
                        issues.append({
                            "code": "scene_too_short",
                            "severity": "high",
                            "message": f"场景只有 {candidate_word_count} 字，至少需要 {min_scene_chars} 字",
                        })
                    if candidate_word_count > pacing_max_scene_chars:
                        attempt_warnings.append({
                            "code": "scene_natural_length_variance",
                            "severity": "low",
                            "message": (
                                f"场景有 {candidate_word_count} 字，高于节拍建议 "
                                f"{pacing_max_scene_chars} 字；章节预算仍是唯一硬限制"
                            ),
                            "word_count": candidate_word_count,
                            "max_scene_chars": pacing_max_scene_chars,
                        })
                if (
                    any(
                        isinstance(item, dict)
                        and item.get("code") == "scene_reader_budget_overrun"
                        for item in issues
                    )
                    and not truncated
                ):
                    excess = (
                        accepted_chars
                        + candidate_word_count
                        + future_target_chars
                        - planning_max_chars
                    )
                    if self._rebalance_future_scene_targets(
                        cards,
                        future_start=index,
                        excess_chars=excess,
                    ):
                        issues = [
                            item
                            for item in issues
                            if item.get("code") != "scene_reader_budget_overrun"
                        ]
                budget_issue_codes = {
                    "scene_chapter_budget_overrun",
                    "scene_reader_budget_overrun",
                }
                if (
                    not truncated
                    and issues
                    and all(
                        isinstance(item, dict)
                        and item.get("code") in budget_issue_codes
                        for item in issues
                    )
                    and self._final_scene_budget_variance_allowed(
                        projected_chars=projected_chapter_chars,
                        chapter_max_chars=chapter_max_chars,
                        future_minimum_chars=future_minimum_chars,
                        future_target_chars=future_target_chars,
                    )
                ):
                    final_scene_variance = True
                    attempt_warnings.append({
                        "code": "final_scene_natural_length_variance",
                        "severity": "low",
                        "message": (
                            f"完整最后场景使章节达到 {projected_chapter_chars} 字，"
                            f"略超生成预算 {chapter_max_chars} 字；未挤占后续场景，"
                            "保留结局并记录长度警告"
                        ),
                        "projected_chars": projected_chapter_chars,
                        "chapter_max_chars": chapter_max_chars,
                    })
                    issues = []
                if (
                    not truncated
                    and any(
                        isinstance(item, dict)
                        and item.get("code") in {
                            "scene_overlong",
                            "scene_chapter_budget_overrun",
                            "scene_reader_budget_overrun",
                        }
                        for item in issues
                    )
                ):
                    compression_mode = True
                if not issues:
                    accepted_scene = candidate
                    scene_warnings = attempt_warnings
                    attempts_used = attempt + 1
                    break
                previous_issue_codes = {
                    str(item.get("code") or "")
                    for item in issues
                    if isinstance(item, dict)
                }
                can_retry = attempt < max_scene_attempts - 1
                if self._can_extend_truncation_retry(
                    previous_issue_codes=previous_issue_codes,
                    attempt=attempt,
                    max_attempts=max_scene_attempts,
                ):
                    max_scene_attempts += 1
                    can_retry = True
                elif self._can_extend_final_budget_retry(
                    previous_issue_codes=previous_issue_codes,
                    attempt=attempt,
                    max_attempts=max_scene_attempts,
                    projected_chars=projected_chapter_chars,
                    chapter_max_chars=chapter_max_chars,
                    future_minimum_chars=future_minimum_chars,
                    future_target_chars=future_target_chars,
                ):
                    max_scene_attempts = SCENE_FINAL_BUDGET_RETRY_MAX_ATTEMPTS
                    can_retry = True
                elif self._can_extend_style_retry(
                    previous_issue_codes=previous_issue_codes,
                    attempt=attempt,
                    max_attempts=max_scene_attempts,
                ):
                    max_scene_attempts = SCENE_STYLE_RETRY_MAX_ATTEMPTS
                    can_retry = True
                if can_retry:
                    feedback = "；".join(
                        str(item.get("message") or item.get("code"))
                        + (
                            "；风险摘要："
                            + json.dumps(
                                self._safe_scene_retry_evidence(item),
                                ensure_ascii=False,
                            )
                            if self._safe_scene_retry_evidence(item)
                            else ""
                        )
                        for item in issues[:5]
                    )
                    if any(
                        isinstance(item, dict)
                        and item.get("code") == "repeated_paragraph_opening"
                        for item in issues
                    ):
                        opening_evidence = scene_metrics.get("repeated_paragraph_opening") or {}
                        opening_name = str(opening_evidence.get("opening") or "").strip()
                        paragraph_count = len(
                            [
                                item
                                for item in re.split(r"\n{2,}|\n", candidate)
                                if item.strip()
                            ]
                        )
                        max_opening_count = max(1, int(paragraph_count * 0.25))
                        feedback += (
                            "\n段首修复硬要求：本次完整重写必须重新安排段落结构；"
                            f"上一版共约 {paragraph_count} 段，‘{opening_name}’有 {int(opening_evidence.get('count') or 0)} 段首命中，"
                            f"本次最多允许 {max_opening_count} 段以该姓名起笔。"
                            "每连续 8 段最多 2 段用同一姓名起笔，其他段落从动作、物件、声音、环境、对白或他人反应起笔；"
                            "不要只把姓名替换成‘他/她’，不要删掉事件、合并段落或照抄上一版。"
                        )
                    if any(
                        isinstance(item, dict)
                        and item.get("code") == "scene_opening_contract"
                        for item in issues
                    ):
                        opening_plan = scene_plan.get("opening_plan") or {}
                        feedback += (
                            "\n开场类型修复硬要求：这是本章第一场，必须从指定类型直接起笔；"
                            f"指定类型为‘{opening_plan.get('label') or opening_plan.get('mode') or '动作'}’。"
                            "本次完整重写首段，不得以身体部位、疼痛、发闷、轰鸣或模糊联想起笔；"
                            "必须让指定的物件、动作、对白、外部事件或环境变化先推动人物行动，"
                            "保留本场事实和因果，不要把原文首句换个同义词。"
                        )
                    if any(
                        isinstance(item, dict)
                        and item.get("code") == "scene_explanatory_narration"
                        for item in issues
                    ):
                        feedback += (
                            "\n解释腔修复硬要求：删除旁白替读者下结论的句子，不能只换成同义词；"
                            "不要写‘这意味着/这说明/他意识到/他心里清楚/不是梦或错觉’式结论，"
                            "把信息改成一个具体物件变化、人物临时决定、答非所问或尚未完成的念头；"
                            "事件发生后不要立刻解释它的意义。"
                        )
                    if any(
                        isinstance(item, dict)
                        and item.get("code") == "scene_metaphor_density"
                        for item in issues
                    ):
                        metaphor_issue = next(
                            (
                                item
                                for item in issues
                                if isinstance(item, dict)
                                and item.get("code") == "scene_metaphor_density"
                            ),
                            {},
                        )
                        metaphor_evidence = metaphor_issue.get("evidence") or {}
                        metaphor_count = metaphor_evidence.get("count") or "多处"
                        feedback += (
                            f"\n类比修复硬要求：上一版非对白检测到 {metaphor_count} 处类比；"
                            "本次完整重写必须为0处。非对白不得出现‘像、好像、仿佛、如同、宛如、犹如’，"
                            "全部改成可观察的颜色、位置、触感、声音、动作或后果；"
                            "生成前逐句检查，不要用同义联想替代类比。"
                        )
                    if any(
                        isinstance(item, dict)
                        and item.get("code") == "scene_repeated_action_loop"
                        for item in issues
                    ):
                        feedback += (
                            "\n动作回环修复硬要求：不要重复‘放回/拿起/转身/回头’来表示犹豫；"
                            "保留一次动作，另一处必须改成新的信息、阻碍、选择或可见后果，不能原地重述。"
                        )
                    if any(
                        isinstance(item, dict)
                        and item.get("code") == "dash_density"
                        for item in issues
                    ):
                        feedback += (
                            "\n破折号生成期修复硬要求：叙述中的破折号全部改用句号、逗号、冒号或具体动作承接；"
                            "只有对白确有打断、停顿或转折时才保留极少量破折号；首段不得用破折号制造模板节奏，"
                            "不要把正常对白改成生硬短句，也不要用连续破折号代替人物动作。"
                        )
                    if any(
                        isinstance(item, dict)
                        and item.get("code") == "scene_first_person_narrative"
                        for item in issues
                    ):
                        feedback += (
                            "\n第三人称修复硬要求：叙述者只能使用‘他/她/人物姓名’等第三人称；"
                            "如果门上字、碑文、账册、书信或纸条本身出现‘我/吾/我们’，"
                            "必须把整段直接文字放进中文引号，或改成第三人称转述，"
                            "不能让第一人称裸露在叙述句中。保留事实，不删掉信息。"
                        )
                    if candidate:
                        structural_compression = previous_issue_codes.intersection({
                            "scene_overlong",
                            "scene_chapter_budget_overrun",
                            "scene_reader_budget_overrun",
                        })
                        style_only_retry = self._is_style_only_retry(previous_issue_codes)
                        if structural_compression:
                            feedback += (
                                "\n上一版候选已超出本场或本章预算，本次禁止复述、照抄或从其末尾续写；"
                                "请依据本场契约、已确认状态和上一场交接点从头重写，先完成目标→阻碍→选择→结果，"
                                "达到结果立即收束，严格服从本次较短的有效字数预算。"
                            )
                        elif style_only_retry:
                            feedback += (
                                "\n上一版只用于定位表达缺陷，不要复制其句式、段首、动作顺序或段落结构；"
                                "请依据 scene_card 和上次诊断证据从头重写，保留事实与因果，不要续写。"
                            )
                        else:
                            feedback += (
                                "\n上一版候选正文（可能未完或含局部问题，只用于本次生成期重写；"
                                "不要从末尾续写，不要原样复制；请完整重写并收束到本场预算内）：\n"
                                f"{candidate[:6000]}"
                            )
                    attempt += 1
                    continue
                raise AIGatewayError(
                    f"scene {index} failed generation contract after bounded retry: "
                    + "; ".join(
                        (
                            f"{item.get('code')}[{item.get('word_count')}字>"
                            f"{item.get('max_scene_chars')}字]"
                        )
                        if isinstance(item, dict) and item.get("code") == "scene_overlong"
                        else (
                            (
                                f"{item.get('code')}[accepted={accepted_chars},"
                                f"candidate={candidate_word_count},future_min={future_minimum_chars},"
                                f"chapter_max={chapter_max_chars}]"
                            )
                            if isinstance(item, dict) and item.get("code") == "scene_chapter_budget_overrun"
                            else (
                                (
                                    f"{item.get('code')}[accepted={accepted_chars},"
                                    f"candidate={candidate_word_count},future_target={future_target_chars},"
                                    f"planning_max={planning_max_chars}]"
                                )
                                if isinstance(item, dict) and item.get("code") == "scene_reader_budget_overrun"
                                else (
                                    f"{item.get('code')}[token_limit={scene_token_limit},"
                                    f"max_chars={attempt_max_scene_chars},"
                                    f"attempt={item.get('evidence', {}).get('attempt') if isinstance(item.get('evidence'), dict) else '?'},"
                                    f"candidate={item.get('evidence', {}).get('candidate_chars') if isinstance(item.get('evidence'), dict) else '?'}]"
                                    if isinstance(item, dict) and item.get("code") == "scene_provider_truncated"
                                    else (
                                        f"{item.get('code')}[evidence="
                                        f"{json.dumps(item.get('evidence'), ensure_ascii=False)}]"
                                        if item.get("evidence")
                                        else str(item.get("code"))
                                    )
                                )
                            )
                        )
                        for item in issues
                    )
                )

            handoff, handoff_usage = await self._extract_scene_handoff(
                chapter_number=chapter_number,
                scene_index=index,
                scene_card=card,
                scene_text=accepted_scene,
                current_state=current_state,
            )
            add_call_usage(handoff_usage)
            handoffs.append(handoff)
            current_state = self._merge_scene_state(current_state, handoff)
            scene_texts.append(accepted_scene)
            scene_outputs.append({
                "scene_index": index,
                "name": card["name"],
                "target_words": card["target_words"],
                "word_count": chinese_word_count(accepted_scene),
                "min_scene_chars": self._scene_length_bounds(card, scene_index=index)[0],
                "max_scene_chars": self._scene_length_bounds(card, scene_index=index)[1],
                "chapter_budget_max_chars": scene_max_chars,
                "max_provider_tokens": self._scene_generation_max_tokens(
                    card,
                    scene_index=index,
                    max_scene_chars=scene_max_chars,
                ),
                "attempts": attempts_used or (2 if feedback else 1),
                "generation_warnings": scene_warnings,
                "naturalness_metrics": scene_metrics,
                "generation_critic": scene_critic,
                "segment_candidate_selection": scene_candidate_selection,
                "handoff": handoff,
            })

        return {
            "text": "\n\n".join(scene_texts).strip(),
            "scene_cards": cards,
            "scene_outputs": scene_outputs,
            "scene_handoffs": handoffs,
            "scene_state": current_state,
            "final_scene_natural_variance": final_scene_variance,
            "usage": usage,
            "generation_mode": "scene_serial",
            "generation_version": SCENE_SERIAL_GENERATION_VERSION,
        }

    async def generate_chapter(
        self,
        chapter_number: int,
        *,
        prompt: str | None = None,
        outline: str | None = None,
        target_word_count: int = 3000,
        max_continuations: int = 1,
        plot_brief: dict[str, Any] | None = None,
        writing_workflow: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate one chapter with a soft length target and hard quality gates.

        Length is a planning signal, not a reason to append unlimited prose.
        One continuation is allowed only when the first draft is materially
        short; every continuation is checked before it can be appended.
        """
        if not hasattr(self, "quality_profile"):
            self.quality_profile = select_quality_profile()
        reader_budget = reader_chapter_budget(
            self.quality_profile,
            requested_target=target_word_count,
        )
        target_word_count = int(reader_budget["target_word_count"])
        usage = {"tokens_input": 0, "tokens_output": 0, "cost": 0.0, "model": None}
        minimum_chapter_chars = int(reader_budget["minimum_chars"])
        maximum_chapter_chars = int(reader_budget["maximum_chars"])
        # Scene targets remain soft pacing allocations, but the reader-facing
        # chapter budget is the hard generation ceiling. Do not accept a
        # complete final scene merely because it fits an extra variance band.
        generation_hard_max_chars = maximum_chapter_chars
        generation_absolute_max_chars = maximum_chapter_chars
        # The canonical path is scene-serial, but this legacy token value is
        # still part of the returned provenance and repair contract.
        generation_max_tokens = max(
            900,
            min(3200, int(maximum_chapter_chars * 0.58)),
        )

        def add_usage(step_ctx: Any, u: dict[str, Any]) -> None:
            usage["tokens_input"] += u.get("tokens_input", 0)
            usage["tokens_output"] += u.get("tokens_output", 0)
            usage["cost"] += u.get("cost", 0.0)
            usage["model"] = u.get("model") or usage["model"]
            if step_ctx is not None:
                step_ctx.set_output(
                    tokens_input=u.get("tokens_input", 0),
                    tokens_output=u.get("tokens_output", 0),
                    cost=u.get("cost", 0.0),
                    model=u.get("model"),
                )

        # Step: assemble context
        async with self.tracer.trace_step(
            "generation.assemble_context",
            "context_assembly",
            input_summary=f"Assemble context for chapter {chapter_number}",
        ) as step:
            context = await self.context_assembler.assemble_context(
                chapter_number,
                include_rejected=bool(getattr(self, "include_rejected_context", False)),
            )
            step.set_output(
                f"context {context['rendered_chars']} chars, "
                f"prev={context['previous_chapters']}",
                data={
                    "rendered_chars": context["rendered_chars"],
                    "truncated": context["truncated"],
                    "previous_chapters": context["previous_chapters"],
                },
            )

        # Step: plan scene (real AI)
        async with self.tracer.trace_step(
            "generation.web_research",
            "web_research",
            input_summary=f"Collect live web-novel inspiration for chapter {chapter_number}",
        ) as step:
            research = await WebResearchService(
                # Some deterministic unit harnesses construct the engine via
                # __new__ and intentionally omit database identity.  The
                # disabled research path never reads it; preserve that test
                # seam without weakening required live research.
                novel_id=getattr(self, "novel_id", uuid.UUID(int=0)),
                event_bus=self.event_bus,
                ai_gateway=self.ai_gateway,
            ).collect(
                chapter_number=chapter_number,
                quality_profile=self.quality_profile,
                plot_brief=plot_brief,
                outline=outline or prompt,
            )
            context["context_layers"]["web_research"] = research
            # Keep lightweight deterministic harnesses compatible; production
            # ContextAssembler always exposes both methods.
            if hasattr(self.context_assembler, "render"):
                context["rendered_context"] = self.context_assembler.render(context["context_layers"])
                max_chars = int(context.get("token_budget", 5400) * 1.6)
                context["truncated"] = len(context["rendered_context"]) > max_chars
                if context["truncated"] and hasattr(self.context_assembler, "_fit_context"):
                    context["rendered_context"] = self.context_assembler._fit_context(
                        context["context_layers"], max_chars
                    )
                context["rendered_chars"] = len(context["rendered_context"])
            add_usage(step, research.get("usage") or {})
            step.set_output(
                f"{research.get('status')} cards={len(research.get('cards') or [])}",
                data={
                    "status": research.get("status"),
                    "cache_status": research.get("cache_status"),
                    "card_count": len(research.get("cards") or []),
                    "source_count": len(research.get("sources") or []),
                },
            )

        seed_workflow = writing_workflow or (plot_brief or {}).get("writing_workflow") or {}
        methodology_required = bool(seed_workflow or plot_brief)
        seed_contract = seed_workflow.get("chapter_contract") if isinstance(seed_workflow, dict) else {}
        workflow_input = {
            **(plot_brief or {}),
            "chapter_contract": seed_contract or (plot_brief or {}).get("chapter_contract") or {},
            "causal_ledger": (seed_workflow.get("causal_ledger") if isinstance(seed_workflow, dict) else None)
            or (plot_brief or {}).get("causal_ledger") or [],
            "state_delta": (seed_workflow.get("state_delta") if isinstance(seed_workflow, dict) else None)
            or (plot_brief or {}).get("state_delta") or {},
        }
        writing_workflow = build_writing_workflow_contract(
            chapter_number,
            context_layers=context.get("context_layers") or {},
            plot_brief=workflow_input,
        )
        context.setdefault("context_layers", {})["writing_workflow"] = writing_workflow

        opening_plan = select_opening_plan(
            chapter_number,
            chapter_type=(plot_brief or {}).get("chapter_type") if isinstance(plot_brief, dict) else None,
            previous_history=(context.get("context_layers") or {}).get("opening_history") or [],
            plot_brief=plot_brief,
        )
        context.setdefault("context_layers", {})["opening_plan"] = opening_plan
        readability_plan = build_readability_plan(
            chapter_number,
            chapter_type=(plot_brief or {}).get("chapter_type") if isinstance(plot_brief, dict) else None,
            plot_brief=plot_brief,
            quality_profile=self.quality_profile,
            opening_plan=opening_plan,
            style_card=(context.get("context_layers") or {}).get("style_card") or {},
            recent_history=(context.get("context_layers") or {}).get("readability_history") or [],
        )
        context.setdefault("context_layers", {})["readability_plan"] = readability_plan

        # Step: plan scene (real AI)
        async with self.tracer.trace_step(
            "generation.plan_scene",
            "scene_planning",
            input_summary="Plan scene structure with AI",
        ) as step:
            scene_plan = await self.scene_director.plan_scene(
                chapter_number,
                context,
                outline=outline or prompt,
                target_word_count=target_word_count,
                plot_brief=plot_brief,
                quality_profile=self.quality_profile,
                previous_titles=context.get("previous_titles") or [],
                opening_plan=opening_plan,
                readability_plan=readability_plan,
                writing_workflow=writing_workflow,
            )
            add_usage(step, scene_plan.pop("_usage", {}))
            step.set_output(
                f"{len(scene_plan.get('beats', []))} beats: "
                f"{scene_plan.get('chapter_title')}",
                data={"scene_plan": scene_plan},
                confidence=float(scene_plan.get("confidence", 0.8) or 0.8),
            )

        scene_plan["recent_payoff_types"] = list(
            (context.get("context_layers") or {}).get("recent_payoff_types") or []
        )[-8:]
        scene_plan["recent_payoff_history"] = list(
            (context.get("context_layers") or {}).get("recent_payoff_history") or []
        )[-20:]
        writing_workflow = build_writing_workflow_contract(
            chapter_number,
            context_layers=context.get("context_layers") or {},
            plot_brief=workflow_input,
            scene_plan=scene_plan,
        )
        scene_plan["writing_workflow"] = writing_workflow
        context.setdefault("context_layers", {})["writing_workflow"] = writing_workflow

        # The payoff contract is a generation input, not only a review field.
        # Normalise it before the writer and humanizer see the chapter so a
        # missing/weak plan cannot be hidden by a later prose score.
        payoff_contract_required = True
        payoff_contract = build_payoff_contract(
            scene_plan.get("payoff_contract") or {},
            chapter_number=chapter_number,
            profile=self.quality_profile,
            recent_types=(context.get("context_layers") or {}).get("recent_payoff_types") or [],
            chapter_function=scene_plan,
        )
        scene_plan["payoff_contract"] = payoff_contract
        payoff_validation = validate_payoff_contract(
            payoff_contract,
            profile=self.quality_profile,
            required=payoff_contract_required,
            chapter_function=scene_plan,
        )
        payoff_variety = validate_payoff_variety(
            payoff_contract.get("payoff_type"),
            scene_plan.get("recent_payoff_types") or [],
            profile=self.quality_profile,
        )
        payoff_beat_repair = repair_payoff_beat_structure(scene_plan.get("beats") or [])
        scene_plan["beats"] = payoff_beat_repair["beats"]
        payoff_beat_validation = payoff_beat_repair["after"]
        scene_plan["payoff_validation"] = payoff_validation
        scene_plan["payoff_variety"] = payoff_variety
        scene_plan["payoff_beat_validation"] = payoff_beat_validation
        scene_plan["payoff_beat_repair"] = payoff_beat_repair

        # Step: serial scene generation (the primary quality control)
        async with self.tracer.trace_step(
            "generation.ai_generate",
            "ai_generation",
            input_summary=f"Generate {len(scene_plan.get('beats') or [])} linked scenes near {target_word_count} chars with AI",
        ) as step:
            serial_result = await self._generate_scene_sequence(
                chapter_number=chapter_number,
                context=context,
                scene_plan=scene_plan,
                target_word_count=target_word_count,
                chapter_max_chars=generation_hard_max_chars,
                chapter_reader_max_chars=maximum_chapter_chars,
            )
            add_usage(step, serial_result.get("usage") or {})
            text = str(serial_result.get("text") or "").strip()
            scene_handoffs = serial_result.get("scene_handoffs") or []
            scene_outputs = serial_result.get("scene_outputs") or []
            scene_state = serial_result.get("scene_state") or {}
            raw_pov_metrics = analyze_third_person_narrative(text)
            raw_content_policy = analyze_content_policy(text, self.quality_profile)
            opening_gate = inspect_opening(
                text,
                requested_mode=(scene_plan.get("opening_plan") or {}).get("mode"),
                chapter_number=chapter_number,
                recent_modes=(scene_plan.get("opening_plan") or {}).get("forbidden_recent_modes") or [],
            )
            preflight_failures: list[dict[str, Any]] = []
            if not raw_pov_metrics["passed"]:
                preflight_failures.append({
                    "code": "third_person_narrative_required",
                    "severity": "high",
                    "message": "生成初稿的叙述部分出现第一人称；对白/短信中的第一人称不计入。",
                    "evidence": raw_pov_metrics,
                })
            if not raw_content_policy["passed"]:
                preflight_failures.extend(raw_content_policy.get("failures") or [])
            for opening_failure in opening_gate.get("flags") or []:
                preflight_failures.append({
                    **opening_failure,
                    "message": f"开场质量门禁：{opening_failure.get('message')}",
                })
            continuations = 0
            continuation_failures: list[dict[str, Any]] = []
            raw_count = chinese_word_count(text)
            continuation_limit = 0
            step.set_output(
                f"{raw_count} chars, {len(scene_outputs)} serial scenes",
                data={
                    "raw_word_count": raw_count,
                    "continuations": continuations,
                    "continuation_limit": continuation_limit,
                    "continuation_failures": continuation_failures,
                    "scene_outputs": scene_outputs,
                    "scene_state": scene_state,
                },
            )

        # Step: de-AI pipeline (real transformations).  If the raw draft
        # already fails a generation-first contract, do not pay for semantic
        # humanization; return the draft for one bounded generation rework.
        async with self.tracer.trace_step(
            "generation.deai_process",
            "deai_processing",
            input_summary="Record generation-first naturalness metrics; use rewrite only as fallback",
        ) as step:
            context_layers = context.get("context_layers") or {}
            opening_repair_result: dict[str, Any] | None = None
            opening_failure_codes = {
                str(item.get("code") or "")
                for item in preflight_failures
                if str(item.get("code") or "").startswith("opening_")
            }
            non_opening_failures = [
                item for item in preflight_failures
                if not str(item.get("code") or "").startswith("opening_")
            ]
            if opening_failure_codes and not non_opening_failures:
                # Opening diversity is a repairable prose contract. Give the
                # real provider one prefix-only repair before treating the
                # chapter as unusable; hard POV/content failures still fail
                # closed and never get hidden by this path.
                try:
                    opening_repair_result = await self.deai_pipeline.repair_opening(
                        text,
                        chapter_number=chapter_number,
                        opening_plan=scene_plan.get("opening_plan") or {},
                        source_facts=json.dumps(
                            {
                                "previous_transition_contract": context_layers.get(
                                    "previous_transition_contract"
                                ),
                                "previous_tail": context_layers.get("previous_tail"),
                                "known_facts": context_layers.get("known_facts") or [],
                            },
                            ensure_ascii=False,
                        ),
                        quality_profile=self.quality_profile,
                    )
                    text = opening_repair_result["processed_text"]
                    opening_gate = opening_repair_result["opening"]
                    raw_pov_metrics = analyze_third_person_narrative(text)
                    raw_content_policy = analyze_content_policy(text, self.quality_profile)
                    preflight_failures = []
                    if not raw_pov_metrics["passed"]:
                        preflight_failures.append({
                            "code": "third_person_narrative_required",
                            "severity": "high",
                            "message": "开场修复后的正文叙述部分出现第一人称；对白/短信中的第一人称不计入。",
                            "evidence": raw_pov_metrics,
                        })
                    if not raw_content_policy["passed"]:
                        preflight_failures.extend(raw_content_policy.get("failures") or [])
                    step.set_output(
                        "provider opening repair passed",
                        data={"opening_repair": opening_repair_result.get("opening")},
                    )
                    add_usage(step, opening_repair_result.get("usage") or {})
                except (AIGatewayError, ValueError) as exc:
                    preflight_failures = [
                        *non_opening_failures,
                        {
                            "code": "opening_repair_failed",
                            "severity": "high",
                            "message": f"开场契约修复失败：{type(exc).__name__}",
                        },
                    ]
            if preflight_failures:
                before_metrics = analyze_deai_patterns(text, profile=self.quality_profile)
                deai_result = {
                    "processed_text": text,
                    "layers_applied": [],
                    "total_changes": 0,
                    "semantic_humanize": False,
                    "humanize_changes": [],
                    "ai_patterns_removed": [],
                    "metrics": {
                        "before": before_metrics,
                        "after": before_metrics,
                        "pov": raw_pov_metrics,
                        "content_policy": raw_content_policy,
                        "opening": opening_gate,
                    },
                    "quality_gate": {
                        "passed": False,
                        "code": "generation_preflight_failed",
                        "skipped": True,
                        "reason": "generation_preflight_failed; semantic humanization skipped",
                    },
                    "usage": {},
                }
            else:
                # Scene serial generation is the primary naturalness control.
                # Keep the post-write pipeline as an explicit fallback only:
                # it must not rewrite every accepted chapter and erase the
                # voice that was established scene by scene.  The metrics stay
                # observable for the audit layer, but they are not a detector
                # score and do not silently trigger a full-chapter rewrite.
                before_metrics = analyze_deai_patterns(text, profile=self.quality_profile)
                deai_result = {
                    "processed_text": text,
                    "layers_applied": [],
                    "total_changes": 0,
                    "semantic_humanize": False,
                    "humanize_changes": [],
                    "ai_patterns_removed": [],
                    "metrics": {
                        "before": before_metrics,
                        "after": before_metrics,
                        "mode": "generation_first",
                        "post_write_audit": "advisory_unless_fallback_requested",
                    },
                    "quality_gate": {
                        "passed": True,
                        "mode": "generation_first",
                        "post_write_audit": "fallback_only",
                    },
                    "usage": {},
                }
            if opening_repair_result:
                deai_result["layers_applied"] = [
                    *(opening_repair_result.get("layers_applied") or []),
                    *(deai_result.get("layers_applied") or []),
                ]
                deai_result["total_changes"] = int(deai_result.get("total_changes") or 0) + int(
                    sum(item.get("changes") or 0 for item in opening_repair_result.get("layers_applied") or [])
                )
                deai_result.setdefault("metrics", {})["opening_repair"] = opening_repair_result.get("opening") or {}
            deai_result.setdefault("metrics", {})["pov_preflight"] = raw_pov_metrics
            deai_result.setdefault("metrics", {})["content_policy_preflight"] = raw_content_policy
            add_usage(step, deai_result.get("usage") or {})
            step.set_output(
                f"{deai_result['total_changes']} edits across "
                f"{len(deai_result['layers_applied'])} layers",
                data={"layers": deai_result["layers_applied"]},
            )

        final_text = deai_result["processed_text"]
        word_count = chinese_word_count(final_text)
        generation_failures = [*preflight_failures, *continuation_failures]
        generation_warnings: list[dict[str, Any]] = []
        methodology_validation = validate_writing_workflow(writing_workflow)
        if methodology_required and not methodology_validation.get("passed"):
            generation_failures.append({
                "code": "writing_methodology_contract_incomplete",
                "severity": "high",
                "message": "生成前因果契约未补齐，不能把缺失因果的正文标记为可用",
                "missing": methodology_validation.get("missing") or [],
            })
        mirror_stats = chapter_mirror_stats(
            final_text,
            previous_text=str(context_layers.get("previous_full_text") or ""),
        )
        # 正文镜像是重复章/平行版本的硬门禁；不能让它进入完成状态。
        if not mirror_stats.get("passed") and CHAPTER_MIRROR_HARD_GATE:
            generation_failures.append({
                "code": "chapter_mirror",
                "severity": "high",
                "message": "正文与自身前后半段或上一章高度镜像，不能进入完成状态",
                "evidence": mirror_stats,
            })
        if word_count < minimum_chapter_chars:
            generation_failures.append(
                {
                    "code": "chapter_too_short",
                    "severity": "high",
                    "message": (
                        f"最终正文 {word_count} 字，低于本章最低生成阈值 "
                        f"{minimum_chapter_chars} 字"
                    ),
                }
            )
        if word_count > generation_absolute_max_chars:
            generation_failures.append(
                {
                    "code": "chapter_too_long",
                    "severity": "high",
                    "message": (
                        f"最终正文 {word_count} 字，超过本章最大生成阈值 "
                        f"{generation_absolute_max_chars} 字"
                    ),
                }
            )
        elif word_count > generation_hard_max_chars:
            if serial_result.get("final_scene_natural_variance"):
                generation_warnings.append({
                    "code": "chapter_final_scene_natural_length_variance",
                    "message": (
                        f"最终正文 {word_count} 字，超过常规生成阈值 "
                        f"{generation_hard_max_chars} 字，但最后场景完整且未挤占后续场景，"
                        f"仍处于绝对上限 {generation_absolute_max_chars} 字内"
                    ),
                })
            else:
                generation_failures.append(
                    {
                        "code": "chapter_too_long",
                        "severity": "high",
                        "message": (
                            f"最终正文 {word_count} 字，超过本章常规生成阈值 "
                            f"{generation_hard_max_chars} 字且没有最后场景自然波动证据"
                        ),
                    }
                )
        elif word_count > maximum_chapter_chars:
            generation_warnings.append({
                "code": "chapter_natural_length_variance",
                "message": (
                    f"最终正文 {word_count} 字，超过读者预算 {maximum_chapter_chars} 字，"
                    f"但处于允许的自然波动范围 {generation_hard_max_chars} 字内"
                ),
            })
        deai_gate = deai_result.get("quality_gate") or {}
        if deai_gate.get("passed") is False:
            generation_failures.append(
                {
                    "code": str(deai_gate.get("code") or "deai_quality_gate_failed"),
                    "severity": "high",
                    "message": str(
                        deai_gate.get("message") or "去 AI 味候选未通过安全校验"
                    ),
                }
            )
        final_pov_metrics = analyze_third_person_narrative(final_text)
        final_content_policy = analyze_content_policy(final_text, self.quality_profile)
        final_opening_gate = inspect_opening(
            final_text,
            requested_mode=(scene_plan.get("opening_plan") or {}).get("mode"),
            chapter_number=chapter_number,
            recent_modes=(scene_plan.get("opening_plan") or {}).get("forbidden_recent_modes") or [],
        )
        for opening_failure in final_opening_gate.get("flags") or []:
            if not any(item.get("code") == opening_failure.get("code") for item in generation_failures):
                generation_failures.append({
                    **opening_failure,
                    "message": f"最终开场质量门禁：{opening_failure.get('message')}",
                })
        if not final_pov_metrics["passed"] and not any(
            item.get("code") == "third_person_narrative_required" for item in generation_failures
        ):
            generation_failures.append({
                "code": "third_person_narrative_required",
                "severity": "high",
                "message": "最终正文的叙述部分出现第一人称；对白/短信中的第一人称不计入。",
                "evidence": final_pov_metrics,
            })
        for policy_failure in final_content_policy.get("failures") or []:
            if not any(
                item.get("code") == policy_failure.get("code") for item in generation_failures
            ):
                generation_failures.append(policy_failure)
        deai_result.setdefault("metrics", {})["after_pov"] = final_pov_metrics
        deai_result.setdefault("metrics", {})["after_content_policy"] = final_content_policy
        if payoff_contract and not payoff_validation.get("passed"):
            generation_failures.append({
                "code": "payoff_contract_missing",
                "severity": "high",
                "message": "本章爽点契约缺少必要字段：" + "、".join(payoff_validation.get("missing") or []),
            })
        if payoff_contract_required and not payoff_validation.get("strength_passed", True):
            generation_failures.append({
                "code": "payoff_strength_insufficient",
                "severity": "high",
                "message": "本章爽点强度或可见反馈不足：" + "；".join(payoff_validation.get("strength_issues") or []),
                "evidence": payoff_validation,
            })
        if payoff_contract_required and not payoff_beat_validation.get("passed"):
            generation_failures.append({
                "code": "payoff_beat_structure_missing",
                "severity": "high",
                "message": "爽点节拍没有覆盖：" + "、".join(payoff_beat_validation.get("missing_phases") or []),
                "evidence": payoff_beat_validation,
            })
        # 爽点类型轮换仍保留为可观测 warning，只有完整占满轮换窗口时
        # 才由配置开关升级为 hard gate。
        if payoff_contract_required and not payoff_variety.get("passed") and PAYOFF_VARIETY_HARD_GATE:
            generation_failures.append({
                "code": "payoff_type_repetition",
                "severity": "high",
                "message": payoff_variety.get("issue") or "本章爽点类型重复度过高",
                "evidence": payoff_variety,
            })
        payoff_score = score_payoff_contract(
            payoff_contract,
            profile=self.quality_profile,
            text=final_text,
            recent_types=scene_plan.get("recent_payoff_types") or [],
            recent_history=scene_plan.get("recent_payoff_history") or [],
        )
        chapter_title_value = scene_plan.get("chapter_title") or ""
        chapter_title_passed, chapter_title_reason = validate_tomato_chapter_title(chapter_title_value)
        if not chapter_title_passed:
            generation_failures.append({
                "code": "tomato_chapter_title_invalid",
                "severity": "high",
                "message": chapter_title_reason,
                "title": chapter_title_value,
            })
        generation_quality = {
            "schema_version": "generation-quality-v1",
            "generation_mode": "scene_serial",
            "generation_version": SCENE_SERIAL_GENERATION_VERSION,
            "passed": not generation_failures,
            "minimum_chars": minimum_chapter_chars,
            "maximum_chars": maximum_chapter_chars,
            "generation_hard_max_chars": generation_hard_max_chars,
            "generation_absolute_max_chars": generation_absolute_max_chars,
            "warnings": generation_warnings,
            "reader_chapter_budget": reader_budget,
            "failures": generation_failures,
            "continuations": continuations,
            "continuation_limit": continuation_limit,
            "pov_metrics": final_pov_metrics,
            "content_policy": final_content_policy,
            "opening": final_opening_gate,
            "payoff_validation": payoff_validation,
            "payoff_beat_validation": payoff_beat_validation,
            "payoff_beat_repair": payoff_beat_repair,
            "payoff_variety": payoff_variety,
            "payoff_score": payoff_score,
            "chapter_mirror": mirror_stats,
            "readability_plan": readability_plan_metadata(
                scene_plan.get("readability_plan") or readability_plan
            ),
            "writing_methodology": {
                "status": writing_workflow.get("status"),
                "validation": methodology_validation,
                "methodology_version": writing_workflow.get("methodology_version"),
            },
            "scene_serial": {
                "scene_count": len(scene_outputs),
                "handoff_count": len(scene_handoffs),
                "state": scene_state,
                "warnings": [
                    warning
                    for scene in scene_outputs
                    for warning in (scene.get("generation_warnings") or [])
                ],
                "post_write_audit": "fallback_only",
            },
            "quality_profile": quality_profile_metadata(self.quality_profile),
            "chapter_title": {
                "value": chapter_title_value,
                "passed": chapter_title_passed,
                "reason": chapter_title_reason,
                "max_chars": 12,
                "style": "tomato_reader_hook",
            },
                "web_research": {
                "status": (context.get("context_layers") or {}).get("web_research", {}).get("status", "disabled"),
                "cache_status": (context.get("context_layers") or {}).get("web_research", {}).get("cache_status"),
                "card_count": len((context.get("context_layers") or {}).get("web_research", {}).get("cards") or []),
                "source_count": len((context.get("context_layers") or {}).get("web_research", {}).get("sources") or []),
            },
        }

        await self.event_bus.publish(
            "generation_completed",
            f"Chapter {chapter_number} generation completed",
            "generation",
            source="generation_engine",
            event_data={
                "chapter_number": chapter_number,
                "word_count": word_count,
                "tokens": usage["tokens_input"] + usage["tokens_output"],
                "cost": usage["cost"],
                "deai_changes": deai_result["total_changes"],
                "generation_mode": "scene_serial",
                "scene_count": len(scene_outputs),
            },
        )

        return {
            "chapter_number": chapter_number,
            "title": scene_plan.get("chapter_title") or f"第{chapter_number}章",
            "text": final_text,
            "word_count": word_count,
            "target_word_count": target_word_count,
            "meets_target": word_count >= target_word_count,
            "generation_quality": generation_quality,
            "context": {
                "rendered_chars": context["rendered_chars"],
                "previous_chapters": context["previous_chapters"],
                "previous_titles": context.get("previous_titles") or [],
                "previous_tail": context["context_layers"].get("previous_tail", ""),
                "previous_transition_contract": context["context_layers"].get(
                    "previous_transition_contract", {}
                ),
                "genre_id": (context["context_layers"].get("genre") or {}).get("genre_id") or getattr(self, "genre_id", None),
                "genre": context["context_layers"].get("genre", {}),
                "constraints": context["context_layers"].get("constraints", []),
                "style_card": context["context_layers"].get("style_card", {}),
                "active_rules": context["context_layers"].get("active_rules", []),
                "recent_payoff_types": context["context_layers"].get("recent_payoff_types", []),
                "recent_payoff_history": context["context_layers"].get("recent_payoff_history", []),
                "web_research": context["context_layers"].get("web_research", {}),
                "opening_history": context["context_layers"].get("opening_history", []),
                "opening_plan": scene_plan.get("opening_plan") or {},
                "readability_plan": readability_plan_metadata(
                    scene_plan.get("readability_plan") or readability_plan
                ),
                "writing_workflow": writing_workflow,
                "scene_state": scene_state,
                "scene_handoffs": scene_handoffs,
            },
            "scene_plan": scene_plan,
            "scene_serial": {
                "generation_mode": "scene_serial",
                "generation_version": SCENE_SERIAL_GENERATION_VERSION,
                "scene_outputs": scene_outputs,
                "scene_handoffs": scene_handoffs,
                "final_state": scene_state,
            },
            "payoff_contract": payoff_contract,
            "payoff_validation": payoff_validation,
            "payoff_variety": payoff_variety,
            "payoff_score": payoff_score,
            "chapter_mirror": mirror_stats,
            "pov_metrics": final_pov_metrics,
            "content_policy": final_content_policy,
            "opening_quality": final_opening_gate,
            "readability_plan": readability_plan_metadata(
                scene_plan.get("readability_plan") or readability_plan
            ),
            "writing_workflow": writing_workflow,
            "title_quality": generation_quality.get("chapter_title"),
            "quality_profile": quality_profile_metadata(self.quality_profile),
            "deai": {
                "layers_applied": deai_result["layers_applied"],
                "total_changes": deai_result["total_changes"],
                "provider_humanization_required": True,
                "provider_humanization_performed": bool(deai_result.get("semantic_humanize")),
                "generation_first": True,
                "post_write_audit": "fallback_only",
                "external_detector_verification": "not_verified",
                "semantic_humanize": deai_result.get("semantic_humanize", False),
                "humanize_changes": deai_result.get("humanize_changes", []),
                "ai_patterns_removed": deai_result.get("ai_patterns_removed", []),
                "metrics": deai_result.get("metrics", {}),
                "quality_gate": deai_result.get("quality_gate") or {
                    "passed": False,
                    "code": "deai_quality_gate_missing",
                    "message": "去 AI 味质量门禁缺失，结果未验证",
                },
            },
            "usage": usage,
        }

    async def repair_local_quality(
        self,
        generation: dict[str, Any],
        *,
        feedback: str,
    ) -> dict[str, Any]:
        """Repair a prose-only defect without regenerating the whole scene.

        StoryDirector uses this only when every failed gate is a local prose
        contract (POV, content policy or deterministic de-AI signal).  The
        Provider still has to return a lossless rewrite through
        ``DeAIPipeline``; length, paragraph shape, POV, content policy and
        the existing quality gate are recomputed before the repaired draft is
        reviewed again.  Structural failures never enter this shortcut.
        """
        context = generation.get("context") or {}
        style_card = context.get("style_card") or {}
        active_rules = context.get("active_rules") or []
        deai_result = await self.deai_pipeline.process(
            generation.get("text") or "",
            source_facts=json.dumps(
                {
                    "previous_transition_contract": context.get(
                        "previous_transition_contract"
                    ),
                    "previous_tail": context.get("previous_tail"),
                },
                ensure_ascii=False,
            ),
            forbidden_changes=json.dumps(
                context.get("constraints") or [], ensure_ascii=False
            ),
            quality_retry_feedback=feedback,
            style_profile=json.dumps(
                {**style_card, "active_rules": active_rules},
                ensure_ascii=False,
            ),
            quality_profile=self.quality_profile,
            payoff_contract=generation.get("payoff_contract") or {},
            safe_deduplicate=True,
            active_rules=active_rules,
            force_semantic_rewrite=True,
            readability_plan=(
                (generation.get("scene_plan") or {}).get("readability_plan")
                or context.get("readability_plan")
            ),
        )

        final_text = deai_result.get("processed_text") or generation.get("text") or ""
        word_count = chinese_word_count(final_text)
        previous_quality = generation.get("generation_quality") or {}
        minimum_chars = int(previous_quality.get("minimum_chars") or 600)
        fallback_budget = reader_chapter_budget(
            getattr(self, "quality_profile", None),
            requested_target=int(generation.get("target_word_count") or 3000),
        )
        maximum_chars = int(
            previous_quality.get("maximum_chars")
            or fallback_budget["maximum_chars"]
        )
        failures: list[dict[str, Any]] = []
        if word_count < minimum_chars:
            failures.append({
                "code": "chapter_too_short",
                "severity": "high",
                "message": f"修复后正文 {word_count} 字，低于最低阈值 {minimum_chars} 字",
            })
        if word_count > maximum_chars:
            failures.append({
                "code": "chapter_too_long",
                "severity": "high",
                "message": f"修复后正文 {word_count} 字，超过最大阈值 {maximum_chars} 字",
            })
        quality_gate = deai_result.get("quality_gate") or {
            "passed": False,
            "code": "deai_quality_gate_missing",
            "message": "去 AI 味质量门禁缺失，结果未验证",
        }
        if quality_gate.get("passed") is False:
            failures.append({
                "code": str(quality_gate.get("code") or "deai_quality_gate_failed"),
                "severity": "high",
                "message": str(quality_gate.get("message") or "去 AI 味修复未通过安全校验"),
            })
        pov_metrics = analyze_third_person_narrative(final_text)
        content_policy = analyze_content_policy(final_text, self.quality_profile)
        if not pov_metrics.get("passed"):
            failures.append({
                "code": "third_person_narrative_required",
                "severity": "high",
                "message": "修复后叙述部分仍出现第一人称；对白/短信中的第一人称不计入。",
                "evidence": pov_metrics,
            })
        failures.extend(content_policy.get("failures") or [])

        generation_quality = {
            **previous_quality,
            "schema_version": "generation-quality-v1",
            "passed": not failures,
            "minimum_chars": minimum_chars,
            "maximum_chars": maximum_chars,
            "failures": failures,
            "pov_metrics": pov_metrics,
            "content_policy": content_policy,
        }
        usage = dict(generation.get("usage") or {})
        repair_usage = deai_result.get("usage") or {}
        for key in ("tokens_input", "tokens_output", "cost"):
            usage[key] = (usage.get(key) or 0) + (repair_usage.get(key) or 0)
        usage["model"] = repair_usage.get("model") or usage.get("model")

        return {
            **generation,
            "text": final_text,
            "word_count": word_count,
            "meets_target": word_count >= int(generation.get("target_word_count") or 0),
            "generation_quality": generation_quality,
            "pov_metrics": pov_metrics,
            "content_policy": content_policy,
            "deai": {
                "layers_applied": deai_result.get("layers_applied") or [],
                "total_changes": deai_result.get("total_changes") or 0,
                "semantic_humanize": deai_result.get("semantic_humanize", False),
                "humanize_changes": deai_result.get("humanize_changes") or [],
                "ai_patterns_removed": deai_result.get("ai_patterns_removed") or [],
                "metrics": deai_result.get("metrics") or {},
                "quality_gate": quality_gate,
            },
            "usage": usage,
        }

    def _build_generation_prompt(
        self,
        chapter_number: int,
        context: dict[str, Any],
        scene_plan: dict[str, Any],
        outline: str | None,
        target_word_count: int,
    ) -> str:
        beats = scene_plan.get("beats") or []
        quality_profile = getattr(self, "quality_profile", None) or select_quality_profile()
        reader_budget = reader_chapter_budget(
            quality_profile,
            requested_target=target_word_count,
        )
        effective_target = int(reader_budget["target_word_count"])
        quality_directive = compile_quality_directive(
            quality_profile,
            chapter_number=chapter_number,
            chapter_function=scene_plan,
            payoff_contract=scene_plan.get("payoff_contract") or None,
            active_rules=(context.get("context_layers") or {}).get("active_rules") or [],
            opening_plan=scene_plan.get("opening_plan") or (context.get("context_layers") or {}).get("opening_plan"),
            readability_plan=scene_plan.get("readability_plan")
            or (context.get("context_layers") or {}).get("readability_plan"),
        )
        readability_plan = scene_plan.get("readability_plan") or (
            context.get("context_layers") or {}
        ).get("readability_plan")
        reader_experience_plan = scene_plan.get("reader_experience_plan") or {}
        prose_texture_plan = scene_plan.get("prose_texture_plan") or {}
        planner_readability_block = ""
        if reader_experience_plan or prose_texture_plan:
            planner_readability_block = (
                "【场景导演补充的读者体验与表达重点】\n"
                + json.dumps(
                    {
                        "reader_experience": reader_experience_plan,
                        "prose_texture": prose_texture_plan,
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
        opening_block = opening_prompt_block(
            scene_plan.get("opening_plan") or (context.get("context_layers") or {}).get("opening_plan")
        )
        beat_lines = "\n".join(
            f"{i + 1}. {b.get('name')}（约{b.get('target_words', 0)}字，情绪：{b.get('emotion','')}）："
            f"{b.get('content', '')}"
            for i, b in enumerate(beats)
        )

        # 品类写作 Prompt 注入
        genre_writer_prompt = ""
        genre_context = (context.get("context_layers") or {}).get("genre") or {}
        writer_prompt_data = genre_context.get("writer_prompt")
        if writer_prompt_data and writer_prompt_data.get("content"):
            genre_writer_prompt = (
                f"【品类写作指导 - {writer_prompt_data.get('prompt_name', '通用')} v{writer_prompt_data.get('version', '1.0')}】\n"
                f"{writer_prompt_data['content']}\n\n"
            )

        # 上一章结尾状态（用于衔接）
        context_layers = context.get("context_layers") or {}
        writing_workflow = scene_plan.get("writing_workflow") or context_layers.get("writing_workflow") or {}
        research_guidance = render_web_research_guidance(context_layers.get("web_research"))
        research_prompt = f"【实时网感灵感卡】\n{research_guidance}\n\n" if research_guidance else ""
        previous_transition = context_layers.get("previous_transition_contract") or {}
        previous_tail = context_layers.get("previous_tail") or ""
        transition_block = ""
        if previous_tail or previous_transition:
            end_state = previous_transition.get("end_state") or {}
            open_threads = previous_transition.get("open_threads") or []
            next_bridge = previous_transition.get("next_chapter_bridge") or previous_tail[-600:]

            threads_text = ""
            if open_threads:
                threads_text = "\n未解决的线索：\n" + "\n".join(
                    f"- {t.get('summary', '')}" for t in open_threads[:5]
                )

            transition_block = (
                f"【第二优先级：上一章结尾状态 - 必须从这里继续，不得跳跃！】\n"
                f"═════════════════════════════════════════════════════════════\n"
                f"上一章结尾的最后内容（必须直接承接，不得跳场）：\n"
                f"「{next_bridge}」\n"
                f"\n"
                f"上一章梗概：{end_state.get('summary', '')}\n"
                f"{threads_text}\n"
                f"\n"
                f"【硬性要求】本章开头必须直接承接上一章结尾的动作、地点和人物状态！\n"
                f"除非正文明确写出过渡，否则不得突然切换场景、时间或人物位置！\n"
                f"═════════════════════════════════════════════════════════════\n\n"
            )

        return (
            f"【最高优先级：核心设定与硬性约束 - 必须严格遵守，不得擅自修改！】\n"
            f"═════════════════════════════════════════════════════════════\n"
            f"以下是本书的核心设定，是所有剧情的基础，绝对不能违反！\n"
            f"1. 必须严格遵守以下设定，不得擅自修改任何人物、世界观、金手指等核心要素\n"
            f"2. 所有剧情必须在设定框架内展开，不得出现设定矛盾\n"
            f"3. 如果发现设定有疑问，以本设定为准，不得自行发挥\n"
            f"\n"
            f"{outline or '（无额外设定要求）'}\n"
            f"\n"
            f"【再次强调】以上设定是硬性约束，违反任何一条都属于严重错误！\n"
            f"═════════════════════════════════════════════════════════════\n\n"
            f"{transition_block}"
            f"{context.get('rendered_context', '')}\n\n"
            f"====================\n"
            f"现在写第 {chapter_number} 章：《{scene_plan.get('chapter_title')}》\n"
            f"视角人物：{scene_plan.get('pov_character', '主角')}\n"
            "叙述模式：第三人称限知；对白/短信中的‘我’可以保留，叙述句中的‘我’不可以出现。\n"
            f"核心冲突：{scene_plan.get('conflict', '')}\n"
            f"节奏：{scene_plan.get('pacing', 'medium')}\n"
            f"章节功能类型：{scene_plan.get('chapter_type') or scene_plan.get('chapter_mode') or 'normal'}\n"
            f"读者承诺：{scene_plan.get('reader_promise', '')}\n"
            f"情绪曲线：{scene_plan.get('emotional_target', '')}\n"
            f"开场接续锚点：{scene_plan.get('opening_anchor', '')}\n"
            f"章末钩子：{scene_plan.get('hook', '')}\n\n"
            f"{render_readability_plan(readability_plan)}\n\n"
            f"{render_writing_methodology_contract(writing_workflow)}\n\n"
            f"{planner_readability_block}"
            f"{genre_writer_prompt}"
            f"{research_prompt}"
            f"【网文质量策略】\n{quality_directive}\n\n"
            f"{opening_block}\n\n"
            f"【AI味候选词库指导】\n{render_ai_flavor_guidance(quality_profile)}\n\n"
            f"【本章爽点契约】\n{json.dumps(scene_plan.get('payoff_contract') or {}, ensure_ascii=False)}\n\n"
            "【连续性硬门禁】上一章结尾、交接契约和本章第一场必须处于同一"
            "时间线/地点/人物状态；除非正文明确给出过渡，不得跳场。\n"
            f"节拍安排：\n{beat_lines}\n\n"
            f"请写出约 {effective_target} 个汉字的完整章节正文；"
            f"读者推荐预算为 {reader_budget['recommended_range'][0]}-{reader_budget['recommended_range'][1]} 字，"
            f"本次生成硬范围为 {reader_budget['minimum_chars']}-{reader_budget['maximum_chars']} 字；"
            "完成节拍和章尾钩子后立即收束，不要为了凑字数继续解释或重复描写。"
            f"必须与前情提要和已有设定保持一致，不得与【必须遵守的约束】冲突。"
            "节拍完整性是硬要求：节拍表列出的关键过桥、交易、对抗、修炼或设定揭示，必须在正文中写出可见的动作过程、阻碍和结果，"
            "不能从‘准备’直接跳到‘成功’，也不能只用一句‘事情顺利完成’带过；首次出现的能力、印记、规则或代价，"
            "要通过一个动作、对白、感官反应或具体后果交代来源，避免百科式解释。"
            "爽点完整性是硬要求：按‘压制→蓄力→爆发→反馈→余波’推进，允许把相邻阶段合并但不能缺失；"
            "爆发必须由主角的主动选择和已有依据造成，反馈必须落到对手、组织、资源、规则或旁观者的可见变化，"
            "余波必须留下代价、身份变化或新的压力。不要把每章写成同一种打脸，不要用‘众人震惊’替代具体反应。"
            "P1-6 质量整改：主角主动性硬要求——"
            "主角必须主动发起至少一次行动，不能全程被动应对；主动装逼是核心爽点类型之一，"
            "包括主动展示实力、主动打脸、主动布局、主动挑事、主动揭短等；"
            "每3章至少有一次主角主动装逼/主动搞事的情节，避免主角一直被动挨打被动反应。"
            "正文质量要求：开头两段直接承接上一章的动作、地点或未决问题，不要重新讲背景；"
            "每个场景都要完成‘目标→阻碍→选择→代价/结果’，在篇幅允许时推进局部变化、"
            "信息揭示或情绪转折；可参考每约 800-1200 字出现一次局部变化，但只能作为节奏检查，"
            "不得按固定字数机械插入；"
            "转折前给读者可见的动作、线索或异常，高潮后留下具体余波；"
            "人物只能使用自己已经获得的信息，能力、物品、时间和地点必须有来源；"
            "句式长短要有变化，避免连续段落用同一主语和同一收束方式；"
            "同一个两字人名作为段落开头尽量不超过全章约四分之一，交替从动作、场景、物件、对白或他人反应起笔，"
            "同时保持第三人称限知清晰，不能把人名全换成‘他/她’来制造另一种重复。"
            "章末必须把钩子落实为动作、发现或新的选择，不得用总结/说教代替；"
            "情绪要有起伏，避免每段都用同一种‘提出问题-解释-总结’结构；"
            "不要为了‘去AI味’禁用任何单个词或标点，判断标准是整章分布、语境和阅读体验。"
        )

    @staticmethod
    def _build_continuation_prompt(
        text: str,
        scene_plan: dict[str, Any],
        missing: int,
        *,
        quality_profile: dict[str, Any] | None = None,
        readability_plan: dict[str, Any] | None = None,
    ) -> str:
        tail = text[-1600:]
        beats = scene_plan.get("beats") or []
        remaining = "、".join(b.get("name", "") for b in beats[-2:]) if beats else ""
        payoff = scene_plan.get("payoff_contract") or {}
        return (
            f"{third_person_generation_contract()}\n"
            f"{content_generation_contract(quality_profile)}\n\n"
            f"{render_readability_plan(readability_plan, compact=True) if readability_plan else ''}\n\n"
            f"{render_writing_methodology_contract(scene_plan.get('writing_workflow') or {})}\n\n"
            f"以下是本章已写好的结尾部分：\n\n{tail}\n\n"
            f"请只补足剩余节拍，目标补写约 {missing} 个汉字，"
            f"完成剩余节拍（{remaining}）并以钩子收束："
            f"{scene_plan.get('hook', '')}。保持上一段的时间线、地点、人物状态和情绪，"
            f"爽点余波与新压力必须落地（可见反馈：{payoff.get('payoff_feedback') or '未指定'}；"
            f"下一压力：{payoff.get('next_pressure') or scene_plan.get('hook', '')}），"
            "如果节拍和钩子已经完成就立即停止，不要为了凑字数继续解释；"
            "不要重新开场、不要重复上文，不要写任何说明；补写段落也要变化起笔，"
            "不要机械重复同一人名或同一动作开头，也不要用大量‘他/她’替换。"
        )
