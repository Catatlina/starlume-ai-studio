"""Celery tasks — workflow execution and scheduled jobs.

V3 Bootstrap: 4-stage professional architecture
  Stage 1 - Planning: Idea → MarketFit → StoryPattern → CoreGameplay →
                       WorldArchitecture → CharacterSystem → ConflictMap
  Stage 2 - Blueprint: VolumePlan → StoryArc → ChapterOutlineBatch → SceneBeatSheet
  Stage 3 - Writing:   ChapterDraft → SelfReview → Polish → LengthCheck → FactReconcile
  Stage 4 - Finalization: Humanize → ConsistencyCheck(7-dim) → ContinuityAudit

Features:
  - Context window management (write-before-search + write-after-reconcile, up to 100 chapters)
  - Chapter idempotency (ON CONFLICT with generation_key)
  - Budget tracking per chapter/node
  - Event ledger (record_event from fusion_deep_workflow)
  - Checkpoint support (resume from any failed node)
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any

from celery.exceptions import Retry

from app.db import connect, encode, new_id, row_to_dict
from app.gateway import (BudgetExceeded, OutputValidationError, ProviderError, complete,
                         validate_task_output, _request_api_key, _request_api_base_url, _request_model)
from app.core.context_budget import cap_context_tokens
from app.core.byok import resolve_byok_key, stash_byok_key
from app.core.concurrency import acquire_ai_slot, release_ai_slot
from app.services.novel_export import extract_body_text
from app.services.text_quality import duplicate_paragraph_stats, normalize_and_validate_rewrite
from app.services.prompt_compiler import (select_strategies, compile_strategy_directive,
                                           compile_prompt, skill_hints_for_strategies,
                                           SKILL_GENERATE_CONFLICT, SKILL_GENERATE_HOOK)
from app.services.quality_profiles import (
    compile_quality_directive,
    profile_from_context,
    quality_profile_metadata,
)
from app.v7.quality.opening_variation import (
    build_opening_history,
    inspect_opening,
    opening_prompt_block,
    select_opening_plan,
)
from app.v7.generation.generation_engine import (
    CHAPTER_SINGLE_PASS_GENERATION_VERSION,
    is_retryable_provider_failure,
)
from app.services.planning_contract import (
    creative_bible_section_defects,
    creative_bible_strategy_section_defects,
    mechanic_innovation_defects,
    mechanic_contract_guidance,
    mechanic_families_for_idea,
    validate_longform_contract,
    validate_chapter_outline_user_contract,
    validate_volume_plan_contract,
)

from .celery_app import celery_app

# ── 4-stage bootstrap node definitions ──────────────────────────────────────

BOOTSTRAP_STAGES = {
    "planning": {
        "label": "规划阶段",
        "nodes": [
            ("plan_idea",              "agent", "StoryArchitect", "创意展开",         "plan_idea"),
            ("plan_market_fit",        "agent", "StoryArchitect", "市场匹配分析",     "plan_market_fit"),
            ("plan_story_pattern",     "agent", "StoryArchitect", "故事模式识别",     "plan_story_pattern"),
            ("plan_core_gameplay",     "agent", "StoryArchitect", "核心玩法/爽点",    "plan_core_gameplay"),
            ("plan_world_architecture","agent", "StoryArchitect", "世界观架构",       "plan_world_architecture"),
            ("plan_character_system",  "agent", "Character",      "人物系统设计",     "plan_character_system"),
            ("plan_conflict_map",      "agent", "StoryArchitect", "冲突图谱",         "plan_conflict_map"),
        ],
    },
    "blueprint": {
        "label": "蓝图阶段",
        "nodes": [
            ("blueprint_volume_plan",     "agent", "StoryArchitect", "分卷规划",          "blueprint_volume_plan"),
            ("generate_story_arc",        "agent", "StoryArchitect", "故事弧生成",        "generate_story_arc"),
            ("blueprint_chapter_outline", "agent", "StoryArchitect", "逐章细纲",          "blueprint_chapter_outline"),
            ("blueprint_scene_beat",      "agent", "StoryArchitect", "场景节拍表",        "blueprint_scene_beat"),
        ],
    },
    "writing": {
        "label": "写作阶段",
        "nodes": [
            ("write_chapter_draft",    "agent", "Writer",    "章节初稿",          "write_chapter_draft"),
            ("write_self_review",      "agent", "Writer",    "自我审阅",          "write_self_review"),
            ("write_polish",           "agent", "Writer",    "润色打磨",          "write_polish"),
            ("write_length_check",     "agent", "Reviewer",  "篇幅检查",          "write_length_check"),
            ("write_fact_reconcile",   "agent", "Reviewer",  "事实核对",          "write_fact_reconcile"),
        ],
    },
    "finalization": {
        "label": "最终化阶段",
        "nodes": [
            ("final_humanize",          "agent", "Writer",    "去AI味人文化",      "final_humanize"),
            ("final_consistency_check", "agent", "Reviewer",  "七维一致性检查",    "final_consistency_check"),
            ("final_continuity_audit",  "agent", "Reviewer",  "连续性审计",        "final_continuity_audit"),
        ],
    },
}

# Human confirmation node sits between planning and blueprint
HUMAN_NODE = ("human_confirm_title", "human", None, "选定书名", None)

# Flattened list preserving stage order (used by create_run for node seeding)
BOOTSTRAP_NODES: list[tuple[str, str, str | None, str, str | None]] = []
for _stage_key, _stage_def in BOOTSTRAP_STAGES.items():
    if _stage_key == "planning":
        BOOTSTRAP_NODES.extend(list(_stage_def["nodes"]))
        BOOTSTRAP_NODES.append(HUMAN_NODE)
    else:
        BOOTSTRAP_NODES.extend(list(_stage_def["nodes"]))

# Node key → stage lookup
NODE_STAGE: dict[str, str] = {}
for stage_key, stage_def in BOOTSTRAP_STAGES.items():
    for node_key, *_ in stage_def["nodes"]:
        NODE_STAGE[node_key] = stage_key
NODE_STAGE["human_confirm_title"] = "human"

# ── Budget defaults (CNY) ───────────────────────────────────────────────────

# Default budget per chapter (all 5 writing nodes combined)
DEFAULT_CHAPTER_BUDGET_CNY = 0.50

# ── Hard AI gates (code-level, NOT prompt suggestions) ──
# 每章非空白字符（中文字数）下限；低于则进入重写循环，用尽后标记「待人工重写」。
MIN_CHAPTER_CHARS = int(os.getenv("MIN_CHAPTER_CHARS", "2000"))
# 七维评分阈值；低于则进入重写循环，用尽后标记「待人工重写」。
REVIEW_SCORE_THRESHOLD = float(os.getenv("CHAPTER_QUALITY_THRESHOLD", "80"))
# 低于阈值时最多重写的次数（共 max_rewrites+1 轮评审）。
MAX_CHAPTER_REWRITES = int(os.getenv("CHAPTER_MAX_REWRITES", "3"))

# Per-node budget allocation (planning ≈ blueprint < writing < finalization)
NODE_BUDGET_MULTIPLIERS: dict[str, float] = {
    # Planning: cheaper, broad strokes
    "plan_idea": 0.5, "plan_market_fit": 0.5, "plan_story_pattern": 0.5,
    "plan_core_gameplay": 0.5, "plan_world_architecture": 0.8,
    "plan_character_system": 0.8, "plan_conflict_map": 0.8,
    # Blueprint: structured output
    "blueprint_volume_plan": 0.5, "blueprint_chapter_outline": 1.0,
    "blueprint_scene_beat": 0.8,
    # Writing: heavy generation
    "write_chapter_draft": 2.0, "write_self_review": 0.5,
    "write_polish": 0.5, "write_length_check": 0.3,
    "write_fact_reconcile": 0.5,
    # Finalization: thorough checking
    "final_consistency_check": 0.5, "final_continuity_audit": 0.5,
    "final_humanize": 1.0,
}

# ── Chapter idempotency key format ──────────────────────────────────────────

def _chapter_idempotency_key(novel_id: str, chapter_seq: int) -> str:
    return f"novel:{novel_id}:chapter:{chapter_seq}:bootstrap:v2"


_NON_NARRATIVE_MARKERS = (
    "本章将深入探讨",
    "在润色过程中",
    "首先需要明确章节",
    "目标读者",
    "逻辑结构",
    "提升阅读流畅性",
    "删除冗余表述",
    "替换模糊词汇",
    "去AI味是",
    "具体改动可以",
    "输出JSON",
    "处理后的完整正文",
    "本文将",
)


def _chapter_paragraphs_from_text(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").replace("\r\n", "\n").split("\n") if line.strip()]


def _chapter_doc_from_paragraphs(paragraphs: list[str]) -> dict[str, Any]:
    return {"type": "doc", "content": [{"type": "paragraph", "text": p} for p in paragraphs]}


def _looks_like_non_narrative_text(text: str) -> bool:
    compact = str(text or "")
    hits = sum(1 for marker in _NON_NARRATIVE_MARKERS if marker in compact)
    return hits >= 2


def _assert_story_revision_quality(
    *,
    task_type: str,
    before_text: str,
    after_paragraphs: list[str],
    min_ratio: float = 0.65,
) -> None:
    after_text = "\n".join(after_paragraphs).strip()
    before_chars = len(str(before_text or "").strip())
    after_chars = len(after_text)
    before_paragraph_count = len(_chapter_paragraphs_from_text(before_text))
    if not after_text:
        raise OutputValidationError(f"{task_type} returned empty chapter text")
    if _looks_like_non_narrative_text(after_text):
        raise OutputValidationError(f"{task_type} returned non-narrative instructional text")
    if before_chars >= 200 and after_chars < int(before_chars * min_ratio):
        raise OutputValidationError(
            f"{task_type} shortened chapter too much: {after_chars}/{before_chars} chars"
        )
    # Polishing may legitimately merge adjacent paragraphs.  Use a proportional
    # structural floor instead of requiring almost the same paragraph count; the
    # character-ratio gate above remains the primary protection against deletion.
    min_paragraph_count = math.ceil(before_paragraph_count * 0.60)
    if before_paragraph_count >= 6 and len(after_paragraphs) < min_paragraph_count:
        raise OutputValidationError(
            f"{task_type} dropped too many paragraphs: {len(after_paragraphs)}/{before_paragraph_count}"
        )


def _assert_min_chapter_length(task_type: str, text: str) -> None:
    from app.services.text_metrics import count_content_chars
    chars = count_content_chars(text)
    if chars < MIN_CHAPTER_CHARS:
        raise OutputValidationError(f"{task_type} chapter too short: {chars}/{MIN_CHAPTER_CHARS} chars")


# V3 Chapter Function (§5): pacing gate reads the function_type sequence across
# the whole outline and flags monotonous runs (water-filling risk). Deterministic
# — not an LLM call — so it is reliable and cheap to run on every chapter.
CHAPTER_FUNCTION_TYPES = {
    "开篇吸引", "信息展示", "人物成长", "关系推进", "冲突升级",
    "爽点释放", "伏笔埋设", "伏笔回收", "转折", "高潮",
}
# A run of this many identical consecutive function_type is a real pacing problem.
CHAPTER_FUNCTION_MONOTONY_THRESHOLD = 5


def _check_chapter_function_pacing(outlines: Any) -> dict[str, Any]:
    """Return a {status, issues} check for the chapter-function rhythm.

    Only considers outlines that actually carry a function_type, so books whose
    outlines were produced before V3 (or via the expand_outline path) degrade
    gracefully instead of being penalised.
    """
    seq: list[str] = []
    if isinstance(outlines, list):
        for o in outlines:
            if isinstance(o, dict):
                ft = str(o.get("function_type", "")).strip()
                if ft:
                    seq.append(ft)
    if not seq:
        return {"status": "pass", "issues": [], "sampled": 0}
    longest_run = run = 1
    run_type = seq[0]
    for i in range(1, len(seq)):
        if seq[i] == seq[i - 1]:
            run += 1
            if run > longest_run:
                longest_run = run
                run_type = seq[i]
        else:
            run = 1
    if longest_run >= CHAPTER_FUNCTION_MONOTONY_THRESHOLD:
        return {
            "status": "fail",
            "issues": [
                f"连续 {longest_run} 章 function_type 重复（{run_type}），疑似节奏单调/水字"
            ],
            "sampled": len(seq),
            "longest_run": longest_run,
        }
    return {"status": "pass", "issues": [], "sampled": len(seq), "longest_run": longest_run}


# V3 Novel DNA (§3.2): self-consistency check — a forbidden deviation must not
# contradict the positioning/promise. Deterministic; runs at plan_idea time and
# the result is stored so audit_plan_fidelity / Reviewer can surface it.
def _check_novel_dna_consistency(dna: Any) -> dict[str, Any]:
    if not isinstance(dna, dict):
        return {"status": "pass", "issues": [], "checked": False}
    positioning = str(dna.get("commercial_positioning", ""))
    promise = str(dna.get("story_promise", ""))
    haystack = (positioning + " " + promise).lower()
    deviations = dna.get("forbidden_deviations") or []
    issues: list[str] = []
    if isinstance(deviations, list):
        for dev in deviations:
            token = str(dev).replace("禁止", "").strip().lower()
            if token and token in haystack:
                issues.append(f"禁止偏离「{dev}」与商业定位/故事承诺自相矛盾")
    status = "fail" if issues else "pass"
    return {"status": status, "issues": issues, "checked": True}


# V3 Story Arc (§4): deterministic coverage/drift check. A chapter that falls
# inside an active arc's declared chapter_range must involve at least one of the
# arc's participants; total non-overlap is a warning (not a hard block), so the
# writer is nudged back onto the arc without killing the run. Books without arcs
# degrade gracefully (pass).
def _check_story_arc_coverage(arcs: Any, chapter_seq: int, outline_participants: Any) -> dict[str, Any]:
    if not isinstance(arcs, list) or not arcs or not chapter_seq:
        return {"status": "pass", "issues": [], "sampled": 0, "covered": False}
    issues: list[str] = []
    covered = False
    for arc in arcs:
        if not isinstance(arc, dict):
            continue
        rng = arc.get("chapter_range") or []
        if len(rng) == 2 and rng[0] <= chapter_seq <= rng[1]:
            covered = True
            arc_parts = {str(p).strip() for p in (arc.get("participants") or []) if str(p).strip()}
            chap_parts = {str(p).strip() for p in (outline_participants or []) if str(p).strip()}
            if arc_parts and not (arc_parts & chap_parts):
                issues.append(
                    f"第 {chapter_seq} 章落在弧线「{arc.get('name', '')}」区间 {rng} 内，"
                    f"但本章参与者与弧线参与者无交集，疑似弧线被忽略"
                )
    if issues:
        return {"status": "warning", "issues": issues, "sampled": len(arcs), "covered": covered}
    return {"status": "pass", "issues": [], "sampled": len(arcs), "covered": covered}


# V3 Strategy Library (§6): build the Writer directive + skill hints for the
# current chapter. Reads the active strategies, matches them against the
# chapter's seq + function_type, and compiles a Chinese directive + the Writer
# skill hints it triggers. Both degrade to "" / [] when nothing matches, so the
# Writer prompt is never blocked.
def _strategy_directive_for_chapter(run_context: dict) -> tuple[str, list[str]]:
    seq = int(run_context.get("_chapter_seq") or run_context.get("chapter_seq") or 1)
    outlines = run_context.get("chapter_outlines") or []
    function_type = ""
    chapter_outline: dict[str, Any] = {}
    if isinstance(outlines, list):
        for o in outlines:
            if isinstance(o, dict) and int(o.get("seq") or 0) == seq:
                function_type = str(o.get("function_type", ""))
                chapter_outline = o
                break
    db = connect()
    rows = db.execute(
        "SELECT name, category, stages, applicable_conditions, description "
        "FROM strategy WHERE status = 'active'"
    ).fetchall()
    db.close()
    strats = [dict(r) for r in rows]
    matched = select_strategies(strats, seq, function_type)
    hints = skill_hints_for_strategies(matched)
    compiled = compile_prompt(
        "",
        strategy_directive=compile_strategy_directive(matched),
        novel_dna=run_context.get("novel_dna"),
        chapter_function=chapter_outline,
    )
    return compiled, hints


def _quality_directive_for_chapter(run_context: dict) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Compile the selected platform/genre policy without creating a second truth store."""
    seq = int(run_context.get("_chapter_seq") or run_context.get("chapter_seq") or 1)
    chapter_outline = run_context.get("_chapter_outline") or {}
    if not isinstance(chapter_outline, dict):
        chapter_outline = {}
    profile = profile_from_context(run_context)
    contract = chapter_outline.get("payoff_contract") if isinstance(chapter_outline, dict) else None
    opening_plan = select_opening_plan(
        seq,
        chapter_type=chapter_outline.get("chapter_type") if isinstance(chapter_outline, dict) else None,
        previous_history=run_context.get("_opening_history") or [],
        plot_brief=chapter_outline,
    )
    directive = compile_quality_directive(
        profile,
        chapter_number=seq,
        chapter_function=chapter_outline,
        payoff_contract=contract if isinstance(contract, dict) else None,
        active_rules=run_context.get("active_rules") or [],
        opening_plan=opening_plan,
    )
    return directive, quality_profile_metadata(profile), contract if isinstance(contract, dict) else {}


def _opening_contract_for_chapter(run_context: dict[str, Any]) -> str:
    """Shared opening contract for the compatibility/bootstrap writer."""
    seq = int(run_context.get("_chapter_seq") or run_context.get("chapter_seq") or 1)
    outline = run_context.get("_chapter_outline") or {}
    outline = outline if isinstance(outline, dict) else {}
    plan = select_opening_plan(
        seq,
        chapter_type=outline.get("chapter_type") or outline.get("function_type"),
        previous_history=run_context.get("_opening_history") or [],
        plot_brief=outline,
    )
    return opening_prompt_block(plan)


def _opening_history_for_novel(novel_id: str, before_chapter: int | None = None) -> list[dict[str, Any]]:
    """Load recent persisted prose for legacy worker routes.

    The canonical V7 brain already supplies this history. This small adapter
    keeps direct Celery/editor calls on the same global scheduler without
    adding a second source of story facts.
    """
    db = connect()
    try:
        try:
            rows = db.execute(
                """SELECT seq, body FROM contents
                   WHERE parent_id=%s AND type='chapter' AND is_deleted=FALSE
                   AND status='reviewed'
                   ORDER BY seq DESC LIMIT 5""",
                (novel_id,),
            ).fetchall()
        except Exception:
            # History is a quality input, not a reason to turn an otherwise
            # explicit generation failure into a database-shaped exception.
            rows = []
    finally:
        db.close()
    chapters = []
    for row in reversed(rows):
        seq = int(row.get("seq") or 0)
        if before_chapter is not None and seq >= int(before_chapter):
            continue
        chapters.append({
            "chapter_number": seq,
            "text": extract_body_text(row.get("body") or ""),
        })
    return build_opening_history(chapters, limit=3)


def _opening_plan_for_novel(novel_id: str, chapter_number: int) -> dict[str, Any]:
    return select_opening_plan(
        chapter_number,
        previous_history=_opening_history_for_novel(novel_id, before_chapter=chapter_number),
    )


def _market_benchmark_for_run(run_context: dict[str, Any] | None, *, chapter_number: int = 1) -> dict[str, Any]:
    """Return the selected empirical snapshot without creating a second policy store."""
    profile = profile_from_context(run_context if isinstance(run_context, dict) else {})
    strategy = profile.get("quality_strategy") if isinstance(profile, dict) else {}
    benchmark = strategy.get("market_benchmark") if isinstance(strategy, dict) else {}
    if not isinstance(benchmark, dict):
        return {}
    if int(chapter_number or 1) != int(benchmark.get("chapter_number") or 1):
        # Recompile only the chapter-sensitive opening hints; the source and
        # sample counts remain the same.  Keeping this here prevents chapter
        # generation from carrying a stale first-chapter-only hint.
        from app.v7.quality.market_snapshot import resolve_market_benchmark

        return resolve_market_benchmark(
            platform=profile.get("platform"),
            genre=profile.get("genre"),
            mechanic_families=profile.get("mechanic_families") or [],
            chapter_number=chapter_number,
        )
    return benchmark


# V3 Repair Engine (§8): three-tier repair classification. Pure, deterministic.
# Maps a failed consistency check to the least-invasive repair that can fix it.
_REPAIR_LEVEL_KEYWORDS = {
    # most severe first — the classifier returns the highest level hit
    "plot": ["剧情", "结构", "规划", "人设偏离", "偏离大纲", "崩坏", "节奏崩", "黄金三章缺失"],
    "chapter": ["逻辑", "连续性", "事实", "一致性", "矛盾", "时间线", "OOC", "穿帮", "设定冲突"],
    "paragraph": ["冗长", "啰嗦", "机械句式", "表达", "拖沓", "水字", "重复", "节奏单调"],
    "sentence": ["错字", "错别字", "标点", "格式", "用词", "语病", "文字问题", "typo"],
}
# Repair action per level (per §8.2 / §8.4):
#   sentence/paragraph -> local repair (in-place, no version branch)
#   chapter            -> whole-chapter rewrite (reuse existing)
#   plot               -> send back to Planner for re-planning (new branch)
REPAIR_LEVEL_ACTION = {
    "sentence": "repair_local",
    "paragraph": "repair_local",
    "chapter": "rewrite_chapter",
    "plot": "replan_chapter",
}


def _classify_repair_level(checks_output: Any) -> dict[str, Any]:
    """Classify the most severe repair level needed from a consistency check.

    Returns {level, action, reason, failed_dimensions}. `level` is one of
    sentence/paragraph/chapter/plot, or "none" when nothing failed. The action
    maps to the §8.2 repair strategy (local repair / rewrite / replan).
    """
    if not isinstance(checks_output, dict):
        return {"level": "none", "action": None, "reason": "", "failed_dimensions": []}
    checks = checks_output.get("checks") if isinstance(checks_output.get("checks"), dict) else {}
    failed: list[str] = []
    issue_text = ""
    for name, check in checks.items():
        if not isinstance(check, dict):
            continue
        if check.get("status") == "fail" or bool(check.get("issues")):
            failed.append(name)
            issue_text += " " + name + " " + " ".join(str(i) for i in (check.get("issues") or []))
    issue_text = issue_text.lower()
    if not failed:
        return {"level": "none", "action": None, "reason": "", "failed_dimensions": []}
    # severity order: plot > chapter > paragraph > sentence
    for level in ("plot", "chapter", "paragraph", "sentence"):
        kws = _REPAIR_LEVEL_KEYWORDS.get(level, [])
        if any(kw.lower() in issue_text for kw in kws):
            return {
                "level": level,
                "action": REPAIR_LEVEL_ACTION.get(level),
                "reason": f"命中 {level} 级修复关键词",
                "failed_dimensions": failed,
            }
    # failed but no keyword matched -> safest default is chapter rewrite
    return {
        "level": "chapter",
        "action": "rewrite_chapter",
        "reason": "未命中具体关键词，默认章级重写",
        "failed_dimensions": failed,
    }


# V3 Repair Engine (§8): sentence/paragraph-level local repair. Applies only the
# listed replacements in-place — never rewrites the chapter, never creates a new
# version branch (per §8.4: local fixes are incremental records on the same node).
def _apply_replacements(body: Any, replacements: list[dict]) -> tuple[Any, list[str], list[str]]:
    if not isinstance(replacements, list) or not replacements:
        return body, [], []
    applied: list[str] = []
    normalized = [
        (str(item.get("anchor", "")), str(item.get("replacement", "")))
        for item in replacements if isinstance(item, dict)
    ]

    def replace_text(text: str) -> str:
        updated = text
        for anchor, replacement in normalized:
            if anchor and anchor in updated:
                updated = updated.replace(anchor, replacement)
                if anchor not in applied:
                    applied.append(anchor)
        return updated

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return replace_text(value)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            copied = deepcopy(value)
            if isinstance(copied.get("text"), str):
                copied["text"] = replace_text(copied["text"])
            if isinstance(copied.get("content"), list):
                copied["content"] = [walk(item) for item in copied["content"]]
            return copied
        return value

    new_body = walk(body)
    skipped = [anchor for anchor, _replacement in normalized if anchor and anchor not in applied]
    return new_body, applied, skipped


def _preview_local_repair(chapter_id: str, novel_id: str, project_id: str, issues_text: str) -> dict[str, Any]:
    """Generate a local-repair proposal without mutating the chapter."""
    db = connect()
    chapter = db.execute(
        "SELECT id, title, body, meta, parent_id, project_id FROM contents WHERE id=%s",
        (chapter_id,),
    ).fetchone()
    if chapter is None:
        db.close()
        return {"error": "chapter not found"}
    body = chapter.get("body")
    meta = chapter.get("meta") if isinstance(chapter.get("meta"), dict) else {}
    outline = meta.get("outline") or {}
    db.close()

    output = complete(
        run_id=None, node_key="repair_local", project_id=project_id,
        task_type="repair_local", prompt_name="bootstrap.repair_local",
        variables={
            "chapter_text": extract_body_text(body),
            "repair_issues": issues_text,
            "_chapter_outline": json.dumps(outline, ensure_ascii=False),
        },
        client_mutation_id=f"repair_local:{chapter_id}:{int(time.time())}:v1",
    )
    output = validate_task_output("repair_local", output)
    replacements = output.get("replacements", []) if isinstance(output, dict) else []
    new_body, applied, skipped = _apply_replacements(body, replacements)
    if not applied or skipped:
        raise OutputValidationError(
            "repair preview contains anchors that do not exactly match the current chapter"
        )
    return {
        "action": "repair_local",
        "level": "local",
        "replacements": replacements,
        "proposed_body": new_body,
        "applied": applied,
        "skipped": skipped,
    }


def _preview_chapter_replan(chapter_id: str, novel_id: str, project_id: str, issues_text: str) -> dict[str, Any]:
    """Generate a revised-outline proposal without mutating the chapter."""
    db = connect()
    chapter = db.execute(
        "SELECT id, title, body, meta, parent_id, project_id FROM contents WHERE id=%s",
        (chapter_id,),
    ).fetchone()
    db.close()
    if chapter is None:
        return {"error": "chapter not found"}
    meta = chapter.get("meta") if isinstance(chapter.get("meta"), dict) else {}
    outline = meta.get("outline") or {}
    assembler = ContextAssembler(novel_id)
    assembler.build()
    book_state = assembler.layers_built.get("book_state", "")
    arc_summary = assembler.layers_built.get("arc_summary", "")

    output = complete(
        run_id=None, node_key="replan_chapter", project_id=project_id,
        task_type="replan_chapter", prompt_name="bootstrap.replan_chapter",
        variables={
            "_chapter_outline": json.dumps(outline, ensure_ascii=False),
            "repair_issues": issues_text,
            "book_state": book_state,
            "arc_summary": arc_summary,
        },
        client_mutation_id=f"replan_chapter:{chapter_id}:{int(time.time())}:v1",
    )
    output = validate_task_output("replan_chapter", output)
    revised = output.get("revised_outline", {}) if isinstance(output, dict) else {}
    rationale = str(output.get("rationale", "")) if isinstance(output, dict) else ""
    return {
        "action": "replan_chapter",
        "level": "plot",
        "revised_outline": revised,
        "rationale": rationale,
    }


def _humanize_quality_feedback(before_text: str, output: dict) -> str:
    paragraphs = _chapter_paragraphs_from_text(output.get("humanized_text", ""))
    after_chars = len("\n".join(paragraphs).strip())
    minimum_chars = max(MIN_CHAPTER_CHARS, math.ceil(len(before_text.strip()) * 0.75))
    try:
        _assert_story_revision_quality(
            task_type="final_humanize",
            before_text=before_text,
            after_paragraphs=paragraphs,
            min_ratio=0.75,
        )
        _assert_min_chapter_length("final_humanize", "\n".join(paragraphs))
    except OutputValidationError as exc:
        return (
            f"{exc}. 本次只有 {after_chars} 个字符；本章必须至少输出 {minimum_chars} 个字符。"
            "请逐段等量改写完整原文，保留全部事件、动作、对话和细节，不得概括或删段；"
            "自然分段数必须保留至少 60%。"
        )
    return ""


def _normalize_final_humanize_output(before_text: str, output: dict) -> tuple[dict, str]:
    """Make provider paragraph collapse lossless before the quality retry.

    The final humanizer is allowed to change wording, never to delete story
    material.  A provider can still serialize several paragraphs into one
    string, so normalize at sentence boundaries before applying the normal
    paragraph/length gate.  Returning feedback instead of raising lets the
    existing three-attempt loop obtain a fresh provider result.
    """
    candidate = str(output.get("humanized_text") or "")
    try:
        normalized, shape = normalize_and_validate_rewrite(
            before_text,
            candidate,
            min_ratio=0.8,
            max_ratio=1.2,
            minimum_chars=50,
        )
    except ValueError as exc:
        return output, (
            f"final_humanize {exc}。请逐段等量改写完整原文，保留全部事件、动作、对话和细节，"
            "不得概括、删减或只返回摘要。"
        )
    normalized_output = dict(output)
    normalized_output["humanized_text"] = normalized
    normalized_output["quality_shape"] = shape
    return normalized_output, ""


def _target_words_guard(output: dict, target_words: Any) -> str:
    """Require the creative bible to carry the user's exact length target.

    The model may express a round target as ``12万字`` instead of ``120000``;
    both forms are accepted.  A different explicit total is not accepted,
    because downstream volume/chapter planning would otherwise drift.
    """
    try:
        target = int(target_words or 0)
    except (TypeError, ValueError):
        target = 0
    if target <= 0:
        return ""
    bible = str(output.get("creative_bible") or "")
    compact = re.sub(r"[\\s,，_、]", "", bible)
    exact = str(target)
    if exact in compact:
        return ""
    if target % 10000 == 0:
        wan = str(target // 10000)
        if re.search(rf"{re.escape(wan)}(?:\\.0+)?万", compact):
            return ""
    else:
        wan = f"{target / 10000:.4f}".rstrip("0").rstrip(".")
        if re.search(rf"{re.escape(wan)}万", compact):
            return ""
    return f"原始需求目标总字数为 {target} 字，creative_bible 必须明确写出该目标，不能改成其他总字数"


def _planning_contract_feedback(output: dict[str, Any], context: dict[str, Any]) -> str:
    """Return deterministic planning defects for the provider retry loop."""
    try:
        target_words = int(context.get("target_words") or 0)
    except (TypeError, ValueError):
        target_words = 0
    if target_words <= 0:
        return ""
    defects = validate_longform_contract(
        output,
        idea=str(context.get("idea") or ""),
        target_words=target_words,
    )
    defects.extend(creative_bible_strategy_section_defects(output.get("creative_bible")))
    defects.extend(mechanic_innovation_defects(output.get("core_mechanic_contract")))
    if not defects:
        return ""
    idea = str(context.get("idea") or "")
    simulator_required = any(
        family == "simulator" for family in mechanic_families_for_idea(idea)
    )
    repair = (
        f"本轮规划未通过硬契约，必须重做而不是解释：目标总字数严格为 {target_words} 字；"
        "creative_bible 必须达到长篇最低字数并写出黄金三章、能力边界、六阶段路线、人物关系、篇幅账本、校验清单、爽点阶梯、反馈轮换和金手指创新路径；"
        "longform_contract 必须包含 target_words、volume_word_targets（合计精确闭合）、chapter_word_target、chapter_count、"
        "route_milestones（最后 end_words 精确等于目标）；"
        "core_mechanic_contract 必须完整包含 enabled、mechanic_type、reader_promise、trigger_and_loop、capability_loop、"
        "mechanic_specific_contract、choice_surface、visible_payoff、limits_and_costs、failure_and_risks、"
        "state_writeback、plot_coupling、progression、anti_inflation、innovation_contract，并落实对应的机制适配器。"
    )
    if simulator_required:
        repair += (
            "simulator_contract 还必须包含 enabled、horizon、terminal_condition、branches（至少两条）、observable_state、"
            "harvestable_rewards、selection_rules、costs_and_risks、reality_writeback、causal_recalculation、plot_guardrails；"
            "必须写清推演到死亡/终局、选择回收收益、代价、回收后因果重算，不能只写短期预知。"
        )
    return repair + "硬错误摘要：" + "；".join(defects[:8])


def _volume_plan_feedback(output: dict[str, Any], context: dict[str, Any]) -> str:
    """Validate volume word targets before downstream arcs/outlines consume them."""
    try:
        target_words = int(context.get("target_words") or 0)
    except (TypeError, ValueError):
        target_words = 0
    if target_words <= 0:
        return ""
    return "；".join(validate_volume_plan_contract(output, target_words=target_words)[:12])


def _reflow_polish_paragraphs(before_text: str, output: dict) -> dict:
    """Reflow only content-complete polish output; never invent or rewrite text."""
    polished = output.get("polished", output.get("chapter", output))
    body = polished.get("body", []) if isinstance(polished, dict) else []
    paragraphs = [
        str(part if isinstance(part, str) else part.get("text", ""))
        for part in body
        if isinstance(part, (str, dict))
        and str(part if isinstance(part, str) else part.get("text", "")).strip()
    ]
    before_count = len(_chapter_paragraphs_from_text(before_text))
    target_count = math.ceil(before_count * 0.60)
    after_text = "".join(paragraphs)
    if (
        before_count < 6
        or len(paragraphs) >= target_count
        or len(after_text) < math.ceil(len(before_text.replace("\n", "").strip()) * 0.75)
    ):
        return output

    sentences = [
        sentence
        for paragraph in paragraphs
        for sentence in re.findall(r".*?[。！？!?]|.+\Z", paragraph, flags=re.S)
        if sentence
    ]
    if len(sentences) < target_count:
        return output

    quotient, remainder = divmod(len(sentences), target_count)
    reflowed: list[str] = []
    cursor = 0
    for index in range(target_count):
        size = quotient + (1 if index < remainder else 0)
        reflowed.append("".join(sentences[cursor:cursor + size]))
        cursor += size

    normalized = dict(output)
    normalized["polished"] = {**polished, "body": reflowed}
    return normalized


def _polish_quality_feedback(before_text: str, output: dict) -> str:
    """Return actionable retry feedback without weakening the persistence gate."""
    polished = output.get("polished", output.get("chapter", output))
    body = polished.get("body", []) if isinstance(polished, dict) else []
    paragraphs = [
        str(part if isinstance(part, str) else part.get("text", "")).strip()
        for part in body
        if isinstance(part, (str, dict))
        and str(part if isinstance(part, str) else part.get("text", "")).strip()
    ]
    before_paragraphs = _chapter_paragraphs_from_text(before_text)
    minimum_paragraphs = math.ceil(len(before_paragraphs) * 0.60)
    after_chars = len("\n".join(paragraphs).strip())
    minimum_chars = max(MIN_CHAPTER_CHARS, math.ceil(len(before_text.strip()) * 0.75))
    try:
        _assert_story_revision_quality(
            task_type="write_polish",
            before_text=before_text,
            after_paragraphs=paragraphs,
            min_ratio=0.75,
        )
        _assert_min_chapter_length("write_polish", "\n".join(paragraphs))
    except OutputValidationError as exc:
        return (
            f"{exc}. 本次输出 {len(paragraphs)} 段、{after_chars} 个字符；"
            f"必须输出完整润色后全文，至少 {minimum_paragraphs} 段、{minimum_chars} 个字符。"
            "请保留全部事件、动作、对话和细节，只做必要润色，不得合并过多段落。"
        )
    return ""


def _draft_length_feedback(output: dict) -> str:
    """Return a concrete retry instruction when a draft misses the hard length gate."""
    chapter = output.get("chapter") if isinstance(output, dict) else None
    body = chapter.get("body", []) if isinstance(chapter, dict) else []
    text = "\n".join(
        part if isinstance(part, str) else str(part.get("text", ""))
        for part in body if isinstance(part, (str, dict))
    )
    try:
        _assert_min_chapter_length("write_chapter_draft", text)
    except OutputValidationError as exc:
        return str(exc)
    return ""


def _quality_evidence_payload(output: dict, self_review: dict | None = None,
                          pacing: dict | None = None, arc_check: dict | None = None) -> dict:
    """Build the durable seven-dimension provenance stored on a chapter."""
    score_by_status = {"pass": 90, "warning": 65, "fail": 35}
    checks = output.get("checks") if isinstance(output.get("checks"), dict) else {}
    dimensions = {
        name: score_by_status.get(str(check.get("status", "")), 0)
        for name, check in checks.items() if isinstance(check, dict)
    }
    # V3 Chapter Function: surface the pacing gate as a 节奏检测 dimension so the
    # seven-dimension score includes rhythm, without ever blocking the gate
    # (pacing is computed/stored separately from the consistency `checks`).
    if pacing and isinstance(pacing, dict) and pacing.get("sampled"):
        dimensions["节奏检测"] = score_by_status.get(str(pacing.get("status", "")), 0)
    # V3 Story Arc (§4): surface the arc-coverage check as an 弧线追踪 dimension.
    if arc_check and isinstance(arc_check, dict) and arc_check.get("sampled"):
        dimensions["弧线追踪"] = score_by_status.get(str(arc_check.get("status", "")), 0)
    sources = ["write_self_review", "final_consistency_check"]
    if pacing and isinstance(pacing, dict) and pacing.get("sampled"):
        sources.append("chapter_function_pacing")
    if arc_check and isinstance(arc_check, dict) and arc_check.get("sampled"):
        sources.append("story_arc_coverage")
    self_review = self_review if isinstance(self_review, dict) else {}
    score = self_review.get("self_score")
    if score is None and dimensions:
        score = sum(dimensions.values()) / len(dimensions)
    issues = [str(item) for item in self_review.get("weaknesses", []) if str(item).strip()]
    return {
        "score": float(score or 0),
        "dimensions": dimensions,
        "issues": issues,
        "source": "+".join(sources),
    }


def _chapter_outline_for_seq(context: dict, chapter_seq: int) -> dict:
    """Select exactly one outline so prose never consumes later chapters."""
    outlines = context.get("chapter_outlines") or []
    if not isinstance(outlines, list):
        return {}
    for outline in outlines:
        if not isinstance(outline, dict):
            continue
        try:
            if int(outline.get("seq") or 0) == chapter_seq:
                return outline
        except (TypeError, ValueError):
            continue
    if 0 < chapter_seq <= len(outlines) and isinstance(outlines[chapter_seq - 1], dict):
        return outlines[chapter_seq - 1]
    return {}


def _assemble_bootstrap_writing_context(
    novel_id: str,
    run_context: dict,
    assembler_factory: Any | None = None,
) -> dict:
    """Inject the real V3 context layers used by the bootstrap Writer.

    The bootstrap chain requires its own idea, creative bible and selected
    chapter outline. Historical chapters, foreshadows and other assembler
    layers remain optional for a first chapter, but an assembler failure must
    never be hidden as a successful write.
    """
    required = {
        "idea": run_context.get("idea") or run_context.get("idea_expanded"),
        "creative_bible": run_context.get("creative_bible"),
        "_chapter_outline": run_context.get("_chapter_outline"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise OutputValidationError(
            "bootstrap writing context missing required fields: " + ", ".join(missing)
        )

    if assembler_factory is None:
        from app.services.assembler import ContextAssembler
        assembler_factory = ContextAssembler

    assembler = assembler_factory(novel_id)
    assembled_text = assembler.build()
    if not isinstance(assembled_text, str):
        raise OutputValidationError("context assembler returned non-text output")

    enriched = dict(run_context)
    for key, value in assembler.layers_built.items():
        if key not in enriched:
            enriched[key] = value
    enriched["_assembled_context"] = assembled_text
    return enriched


# ── Isolated request context decorator ──────────────────────────────────────

def _isolated_request_context(fn):
    """Prevent BYOK credentials leaking between tasks in a reused worker process."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        _request_api_key.set(None)
        _request_api_base_url.set(None)
        _request_model.set(None)
        try:
            return fn(*args, **kwargs)
        finally:
            _request_api_key.set(None)
            _request_api_base_url.set(None)
            _request_model.set(None)
    return wrapped
# ═══════════════════════════════════════════════════════════════════════════
# Event ledger helpers
# ═══════════════════════════════════════════════════════════════════════════

def _record_bootstrap_event(run_id: str, event_type: str, node_key: str = "",
                            payload: dict | None = None) -> dict:
    """Record a bootstrap lifecycle event in the immutable audit ledger."""
    try:
        from app.services.fusion_deep_workflow import record_event
        return record_event(run_id, event_type, node_key=node_key,
                            payload=payload or {})
    except Exception:
        # Event ledger is BestEffort — never block workflow on audit failure
        return {"status": "ledger_unavailable"}
# ═══════════════════════════════════════════════════════════════════════════
# Context window management (write-before-search + write-after-reconcile)
# ═══════════════════════════════════════════════════════════════════════════

def _write_before_search(novel_id: str, chapter_seq: int, window_size: int = 100) -> dict:
    """Retrieve recent chapters as context before writing a new one.

    Implements a rolling memory window of up to `window_size` chapters,
    returning summaries + entity states to ground the generation.

    Returns:
        dict with keys: recent_chapters, entity_summary, world_state, total_retrieved
    """
    db = connect()
    start_seq = max(1, chapter_seq - window_size)
    recent = db.execute(
        """SELECT meta->>'seq' AS seq, title,
                  meta->>'chapter_summary' AS summary,
                  meta->>'word_count' AS word_count,
                  status
           FROM contents
           WHERE parent_id = %s AND type = 'chapter'
             AND (meta->>'seq')::int BETWEEN %s AND %s
           ORDER BY (meta->>'seq')::int""",
        (novel_id, start_seq, chapter_seq - 1),
    ).fetchall()
    recent_chapters = []
    for ch in recent:
        recent_chapters.append({
            "seq": int(ch.get("seq") or 0),
            "title": ch.get("title", ""),
            "summary": (ch.get("summary") or "")[:300],
            "word_count": int(ch.get("word_count") or 0),
        })

    # Entity snapshot for continuity
    entity_rows = db.execute(
        """SELECT entity_type, entity_name, location, relationships, possessions
           FROM entity_states es
           JOIN contents c ON c.id = es.chapter_id
           WHERE c.parent_id = %s""",
        (novel_id,),
    ).fetchall()
    entities_by_type: dict[str, list[dict]] = {}
    for er in entity_rows:
        etype = er.get("entity_type", "unknown")
        entities_by_type.setdefault(etype, []).append({
            "name": er.get("entity_name", ""),
            "location": er.get("location", ""),
        })

    # Character states
    char_rows = db.execute(
        "SELECT title, meta FROM contents WHERE parent_id = %s AND type = 'character' AND is_deleted = FALSE",
        (novel_id,),
    ).fetchall()

    db.close()
    return {
        "recent_chapters": recent_chapters,
        "entity_summary": {k: v[-5:] for k, v in entities_by_type.items()},
        "character_count": len(char_rows),
        "total_retrieved": len(recent),
    }
def _write_after_reconcile(novel_id: str, chapter_id: str, chapter_text: str) -> dict:
    """Post-write reconciliation: detect new facts and compare with existing state.

    Extracts signals from the freshly written chapter and cross-references
    them against the entity_states table to flag potential inconsistencies.
    """
    db = connect()
    # Collect all entity names for cross-reference
    entity_names = db.execute(
        """SELECT DISTINCT entity_name FROM entity_states es
           JOIN contents c ON c.id = es.chapter_id
           WHERE c.parent_id = %s""",
        (novel_id,),
    ).fetchall()
    db.close()

    known_names = {r.get("entity_name", "") for r in entity_names if r.get("entity_name")}
    mentioned = sorted(n for n in known_names if n and n in chapter_text)
    new_entities = sorted(
        n for n in _extract_names_from_text(chapter_text)
        if n not in known_names and len(n) >= 2
    )

    return {
        "known_entities_mentioned": len(mentioned),
        "mentioned": mentioned[:20],
        "new_entities_detected": len(new_entities),
        "new_entities": new_entities[:10],
        "reconciliation_needed": len(new_entities) > 0,
    }
def _extract_names_from_text(text: str) -> set[str]:
    """Simple Chinese name extraction heuristic for reconciliation."""
    import re
    # Two-character Chinese given names and common surname+name patterns
    names: set[str] = set()
    # Match 2-3 character Chinese words between sentence boundaries
    matches = re.findall(r'[\u4e00-\u9fff]{2,3}', text)
    # Filter out common non-name words
    stop_words = {"一个", "可以", "没有", "自己", "他们", "我们", "什么", "知道",
                  "已经", "这个", "那个", "就是", "不是", "如果", "因为", "所以",
                  "但是", "不过", "而且", "然后", "开始", "已经", "现在", "突然",
                  "感觉", "发现", "看到", "想到", "说道", "出来", "起来", "下来",
                  "这里", "那里", "忽然", "一股", "一道", "一声", "一阵", "无数"}
    for m in matches:
        if m not in stop_words:
            names.add(m)
    return names
def _track_budget(run_id, node_key, cost_cny):
    """Synchronize workflow budget with the real ai_calls ledger.

    The gateway already records every real provider call and increments
    budgets.spent_cny. This helper exists for workflow checkpoints, so it must
    report real numbers instead of a 0/0 placeholder. To avoid double-counting
    legacy/worker paths, we recompute the workflow project's bootstrap spend
    from ai_calls and sync the budget row to that value.
    """
    db = connect()
    try:
        run = db.execute("SELECT project_id FROM workflow_runs WHERE id=%s", (run_id,)).fetchone()
        if not run:
            return {"status": "error", "message": "workflow run not found"}
        project_id = run["project_id"]
        spent_row = db.execute(
            "SELECT COALESCE(SUM(cost_cny),0) AS spent FROM ai_calls WHERE project_id=%s AND status='succeeded'",
            (project_id,),
        ).fetchone()
        spent = float(spent_row["spent"] or 0)
        budget = db.execute(
            "SELECT * FROM budgets WHERE project_id=%s AND scope='bootstrap'",
            (project_id,),
        ).fetchone()
        if not budget:
            from app.config import settings
            db.execute(
                "INSERT INTO budgets (id, project_id, scope, limit_cny, spent_cny) VALUES (%s,%s,'bootstrap',%s,%s)",
                (new_id("bdg"), project_id, settings.default_monthly_budget_cny, spent),
            )
            limit = float(settings.default_monthly_budget_cny)
        else:
            limit = float(budget["limit_cny"])
            db.execute(
                "UPDATE budgets SET spent_cny=%s, updated_at=now() WHERE id=%s",
                (spent, budget["id"]),
            )
        db.commit()
        status = "exceeded" if limit and spent > limit else "ok"
        return {"status": status, "project_id": str(project_id), "scope": "bootstrap",
                "node_key": node_key, "last_cost_cny": float(cost_cny or 0),
                "spent": round(spent, 6), "limit": round(limit, 6)}
    finally:
        db.close()

def _create_checkpoint(run_id: str, node_key: str, context: dict) -> str:
    """Save a checkpoint snapshot for later resumption."""
    db = connect()
    ckpt_id = new_id()
    db.execute(
        """INSERT INTO audit_logs (id, entity_type, entity_id, action, details, created_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (ckpt_id, "workflow_run", run_id, "checkpoint.created",
         encode({"node": node_key, "context_snapshot": context,
                 "timestamp": datetime.now(timezone.utc).isoformat()}),
         datetime.now(timezone.utc)),
    )
    db.commit()
    db.close()
    _record_bootstrap_event(run_id, "checkpoint.created", node_key=node_key,
                            payload={"checkpoint_id": ckpt_id})
    return ckpt_id
def _resume_from_checkpoint(run_id: str) -> str | None:
    """Find the latest checkpoint and return the node_key to resume from."""
    db = connect()
    ckpt = db.execute(
        """SELECT details FROM audit_logs
           WHERE entity_type = 'workflow_run' AND entity_id = %s
             AND action = 'checkpoint.created'
           ORDER BY created_at DESC LIMIT 1""",
        (run_id,),
    ).fetchone()
    db.close()
    if not ckpt:
        return None
    details = ckpt.get("details", {})
    if isinstance(details, dict):
        return details.get("node")
    return None


def _attach_user_context(novel_id: str) -> None:
    """Best-effort: attribute a worker process's AI calls to the novel owner.

    Worker tasks run outside the HTTP request lifecycle, so the request-scoped
    ``_request_user_id`` ContextVar is unset. Without this, ``ai_calls.user_id``
    stays NULL for generated chapters and the user's token bill undercounts. We
    resolve the owner from the novel and set the ContextVar for the task's run.
    """
    try:
        from app.gateway import _request_user_id
        db = connect()
        try:
            owner = db.execute(
                "SELECT owner_id FROM contents WHERE id = %s", (novel_id,)
            ).fetchone()
        finally:
            db.close()
        if owner and owner.get("owner_id"):
            _request_user_id.set(owner["owner_id"])
    except Exception:
        pass


def _canonical_bootstrap_prompt(context: dict[str, Any]) -> str:
    """Turn V6 planning artifacts into the input for the canonical V7 writer."""
    blocks = [
        f"用户创意：{str(context.get('idea') or '')[:4000]}",
        f"创意扩展：{str(context.get('idea_expanded') or '')[:5000]}",
        f"创作圣经：{str(context.get('creative_bible') or '')[:9000]}",
        f"世界观：{str(context.get('_worldview_text') or context.get('worldview') or '')[:5000]}",
        f"人物系统：{str(context.get('_characters_text') or '')[:6000]}",
        f"冲突图谱：{json.dumps(context.get('conflict_map') or {}, ensure_ascii=False)[:5000]}",
        f"场景节拍：{json.dumps(context.get('scene_beat_sheet') or {}, ensure_ascii=False)[:5000]}",
        "这是唯一正文生成链路：请直接输出第一章小说正文，不要输出提纲、解释、审稿意见或 Markdown。必须让人物、世界规则、冲突代价和章末动作钩子落到具体场景中。",
    ]
    return "\n\n".join(block for block in blocks if block.split("：", 1)[-1].strip())


def _persist_canonical_bootstrap_result(
    run_id: str,
    result: dict[str, Any],
) -> None:
    """Close the legacy bootstrap progress UI around the canonical V7 result."""
    delegated_nodes = [
        "write_chapter_draft",
        "write_self_review",
        "write_polish",
        "write_length_check",
        "write_fact_reconcile",
        "final_humanize",
        "final_consistency_check",
        "final_continuity_audit",
    ]
    v7_status = str(result.get("status") or "needs_review")
    output = {
        "canonical_engine": "v7",
        "delegated": True,
        "v7_run_id": result.get("run_id"),
        "chapter_number": result.get("chapter_number"),
        "status": v7_status,
        "review_score": result.get("review_score"),
        "dimension_scores": result.get("dimension_scores") or {},
        "reader_experience": result.get("reader_experience") or {},
        "issues": result.get("issues") or [],
        "quality_gate": result.get("quality_gate") or {},
        "generation_quality": result.get("generation_quality") or {},
        "blocked_reason": result.get("blocked_reason") or "",
        "passed_review": result.get("passed_review"),
        "transition_contract": result.get("transition_contract") or {},
        "continuity": result.get("continuity") or {},
        "final_continuity_audit": result.get("final_continuity_audit") or {
            "continuity": result.get("continuity") or {},
        },
        "audit_report": result.get("audit_report") or {},
        "review_provenance": result.get("review_provenance") or result.get("provenance") or {},
        "provenance": result.get("review_provenance") or result.get("provenance") or {},
        "v6_content_id": result.get("v6_content_id"),
    }
    if v7_status in {"needs_review", "needs_rewrite"}:
        output.update({
            "retryable": False,
            "failure_kind": "quality_contract",
        })
    if v7_status == "completed":
        delegated_statuses = ["succeeded"] * len(delegated_nodes)
    elif v7_status == "pending_approval":
        # Prose has not started yet.  Keep the first delegated node waiting and
        # leave compatibility-only downstream nodes pending.
        delegated_statuses = ["waiting_human"] + ["pending"] * (len(delegated_nodes) - 1)
    elif v7_status in {"needs_review", "needs_rewrite"}:
        # V7 generated/reviewed a draft, but the strict gate rejected it.  The
        # first node carries the actionable result; downstream placeholders
        # were not independently executed.
        delegated_statuses = ["needs_review"] + ["skipped"] * (len(delegated_nodes) - 1)
    else:
        delegated_statuses = ["failed"] + ["skipped"] * (len(delegated_nodes) - 1)
    canonical_reason = output["blocked_reason"] or (
        "V7 质量门未通过，草稿已保存为待重写"
        if v7_status in {"needs_review", "needs_rewrite"}
        else "V7 canonical runtime did not complete"
    )
    db = connect()
    try:
        run = db.execute("SELECT context FROM workflow_runs WHERE id=%s", (run_id,)).fetchone()
        context = (run or {}).get("context") if isinstance((run or {}).get("context"), dict) else {}
        context.update({
            "canonical_engine": "v7",
            "canonical_generation_status": v7_status,
            "canonical_generation": output,
            "chapter_id": result.get("v6_content_id") or context.get("chapter_id", ""),
            "chapter_text": result.get("content") or context.get("chapter_text", ""),
            "chapter_title": result.get("title") or context.get("chapter_title", ""),
        })
        for node_key, delegated_status in zip(delegated_nodes, delegated_statuses):
            is_open = delegated_status in {"waiting_human", "pending", "running"}
            db.execute(
                """
                UPDATE run_nodes
                SET status=%s, output=%s,
                    finished_at=CASE WHEN %s THEN NULL ELSE now() END,
                    error=%s
                WHERE run_id=%s AND node_key=%s
                """,
                (
                    delegated_status,
                    encode({
                        **output,
                        "delegated_node": node_key,
                        "delegated_status": delegated_status,
                    }),
                    is_open,
                    canonical_reason if delegated_status not in {"succeeded", "pending"} else None,
                    run_id,
                    node_key,
                ),
            )
        workflow_status = {
            "completed": "running",
            "pending_approval": "waiting_human",
            "needs_review": "needs_review",
            "needs_rewrite": "needs_review",
        }.get(v7_status, "failed")
        current_node = "write_chapter_draft" if v7_status == "pending_approval" else None
        db.execute(
            """
            UPDATE workflow_runs
            SET context=%s, current_node_key=%s, status=%s, updated_at=now()
            WHERE id=%s
            """,
            (encode(context), current_node, workflow_status, run_id),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════
# Core bootstrap execution
# ═══════════════════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=3, default_retry_delay=5)
@_isolated_request_context
def execute_bootstrap(self, run_id: str, start_key: str = "plan_idea",
                       api_key: str = "", api_url: str = "", model: str = "",
                       api_key_ref: str = "") -> dict:
    """Execute the 4-stage bootstrap workflow with context management, budget
    tracking, event ledger, and checkpoint support.

    Stages:
      1. Planning (7 agent nodes) → human_confirm_title
      2. Blueprint (4 agent nodes)
      3. Writing (5 agent nodes per chapter, initially ch 1)
      4. Finalization (3 agent nodes)

    The workflow can resume from any failed node via checkpoint.
    """
    # Set context vars for this worker process. P2-T3 / Q5: the BYOK key arrives
    # as a short-lived reference (never plaintext in the Celery broker) and is
    # resolved here; a legacy plaintext ``api_key`` is still honoured.
    api_key = resolve_byok_key(api_key_ref, api_key)
    if api_key:
        _request_api_key.set(api_key)
    if api_url:
        _request_api_base_url.set(api_url)
    if model:
        _request_model.set(model)

    # Attribute AI calls to the novel owner for per-user metering/billing.
    _run_lookup = connect()
    _run_row = _run_lookup.execute("SELECT novel_id FROM workflow_runs WHERE id = %s", (run_id,)).fetchone()
    _run_lookup.close()
    if _run_row and _run_row.get("novel_id"):
        _attach_user_context(_run_row["novel_id"])

    # Determine start index in flattened node list
    try:
        start_index = next(i for i, node in enumerate(BOOTSTRAP_NODES) if node[0] == start_key)
    except StopIteration:
        # Try checkpoint resumption
        resume_key = _resume_from_checkpoint(run_id)
        if resume_key:
            try:
                start_index = next(i for i, node in enumerate(BOOTSTRAP_NODES) if node[0] == resume_key)
            except StopIteration:
                start_index = 0
        else:
            start_index = 0

    # Verify run exists
    conn = connect()
    run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s", (run_id,)).fetchone()
    if run is None:
        conn.close()
        return {"status": "error", "detail": "run not found"}
    conn.close()

    _record_bootstrap_event(run_id, "run.started", node_key=start_key)

    # ── Stage-aware iteration ───────────────────────────────────────────
    current_stage: str | None = None
    chapter_seq = 1  # Bootstrap always generates chapter 1

    for node_key, kind, agent, title, task_type in BOOTSTRAP_NODES[start_index:]:
        stage = NODE_STAGE.get(node_key, "unknown")

        # Stage transition: create checkpoint and log
        if stage != current_stage and stage != "human":
            current_stage = stage
            stage_label = BOOTSTRAP_STAGES.get(stage, {}).get("label", stage)
            conn = connect()
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s", (run_id,)).fetchone()
            context = run["context"] if run and isinstance(run["context"], dict) else {}
            conn.close()
            _create_checkpoint(run_id, node_key, context)
            _record_bootstrap_event(
                run_id, "checkpoint.created", node_key=node_key,
                payload={"stage": stage, "label": stage_label},
            )

        # ── DB state check ──────────────────────────────────────────────
        conn = connect()
        node = conn.execute(
            "SELECT * FROM run_nodes WHERE run_id = %s AND node_key = %s",
            (run_id, node_key),
        ).fetchone()
        run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s", (run_id,)).fetchone()
        if node is None or run is None:
            conn.close()
            return {"status": "error", "detail": "run or node not found"}

        # Skip already-completed nodes
        if node["status"] == "succeeded":
            conn.close()
            _record_bootstrap_event(run_id, "node.completed", node_key=node_key,
                                    payload={"skipped": "already_succeeded"})
            continue

        # ── Human node ──────────────────────────────────────────────────
        if kind == "human":
            run_context = run["context"] if isinstance(run["context"], dict) else {}
            if (run_context.get("auto_confirm_title") or run_context.get("title_locked")) and node_key == "human_confirm_title":
                title_candidates = run_context.get("title_candidates") or []
                selected_title = str(run_context.get("selected_title") or "").strip()
                if not selected_title and not run_context.get("title_locked"):
                    selected_title = str(title_candidates[0]).strip() if title_candidates else ""
                if not selected_title:
                    conn.execute(
                        """UPDATE run_nodes
                           SET status = 'failed',
                               started_at = COALESCE(started_at, now()),
                               finished_at = now(),
                               error = %s
                           WHERE run_id = %s AND node_key = %s""",
                        ("missing title candidates for auto confirm", run_id, node_key),
                    )
                    conn.execute(
                        "UPDATE workflow_runs SET status = 'failed', current_node_key = %s, updated_at = now() WHERE id = %s",
                        (node_key, run_id),
                    )
                    conn.commit()
                    conn.close()
                    _record_bootstrap_event(
                        run_id,
                        "human.rejected",
                        node_key=node_key,
                        payload={"reason": "missing_title_candidates"},
                    )
                    return {"status": "error", "detail": "missing title candidates for auto confirm"}
                run_context["selected_title"] = selected_title
                conn.execute(
                    """UPDATE contents
                       SET title = %s,
                           meta = jsonb_set(COALESCE(meta, '{}'::jsonb), '{selected_title}', to_jsonb(%s::text), true),
                           updated_at = now()
                       WHERE id = %s""",
                    (selected_title, selected_title, run["novel_id"]),
                )
                conn.execute(
                    """UPDATE run_nodes
                       SET status = 'succeeded',
                           started_at = COALESCE(started_at, now()),
                           finished_at = now(),
                           output = %s
                       WHERE run_id = %s AND node_key = %s""",
                    (encode({"selected_title": selected_title, "source": "locked_title" if run_context.get("title_locked") else "auto_confirm"}), run_id, node_key),
                )
                conn.execute(
                    """UPDATE workflow_runs
                       SET status = 'pending',
                           current_node_key = 'blueprint_volume_plan',
                           context = %s,
                           updated_at = now()
                       WHERE id = %s""",
                    (encode(run_context), run_id),
                )
                conn.commit()
                conn.close()
                _record_bootstrap_event(
                    run_id,
                    "human.confirmed",
                    node_key=node_key,
                    payload={"selected_title": selected_title, "action": "auto_confirmed"},
                )
                continue
            conn.execute(
                "UPDATE run_nodes SET status = 'waiting_human', started_at = COALESCE(started_at, now()) WHERE run_id = %s AND node_key = %s",
                (run_id, node_key),
            )
            conn.execute(
                "UPDATE workflow_runs SET status = 'waiting_human', current_node_key = %s, updated_at = now() WHERE id = %s",
                (node_key, run_id),
            )
            conn.commit()
            conn.close()
            celery_app.backend.set(f"run:{run_id}:human", node_key)
            _record_bootstrap_event(run_id, "human.confirmed", node_key=node_key,
                                    payload={"action": "waiting"})
            return {"status": "waiting_human", "node_key": node_key}

        # ── Claim node (with idempotency via status guard) ─────────────
        claim = conn.execute(
            """UPDATE run_nodes SET status='running', attempt=attempt+1, started_at=now(), error=NULL
               WHERE run_id=%s AND node_key=%s
                 AND status IN ('pending','failed','pending_budget','pending_provider')
               RETURNING id""", (run_id, node_key),
        )
        if hasattr(claim, "rowcount") and claim.rowcount != 1:
            conn.close()
            return {"status": "already_claimed", "node_key": node_key}
        conn.execute(
            "UPDATE workflow_runs SET status = 'running', current_node_key = %s, updated_at = now() WHERE id = %s",
            (node_key, run_id),
        )
        conn.commit()
        conn.close()

        _record_bootstrap_event(run_id, "node.started", node_key=node_key,
                                payload={"stage": stage, "agent": agent})

        time.sleep(0.3)

        # The old bootstrap workflow remains as the progress/blueprint
        # contract, but its prose-writing and finalization nodes are now one
        # delegated V7 Director run.  This prevents a first chapter from being
        # written once by V6 and then rewritten by V7 as a second chain.
        if stage == "writing" and node_key == "write_chapter_draft":
            run_context = run["context"] if isinstance(run["context"], dict) else {}
            project_id = run["project_id"]
            novel_id = run["novel_id"]
            try:
                result = _run_canonical_v7_task(
                    self,
                    novel_id,
                    project_id,
                    api_key=api_key,
                    api_url=api_url,
                    model=model,
                    chapter_number=chapter_seq,
                    prompt=_canonical_bootstrap_prompt(run_context),
                    outline=json.dumps(
                        run_context.get("_chapter_outline") or {}, ensure_ascii=False
                    ),
                    api_key_ref=api_key_ref,
                )
                _persist_canonical_bootstrap_result(run_id, result)
                _record_bootstrap_event(
                    run_id,
                    "canonical_v7.completed",
                    node_key=node_key,
                    payload={
                        "status": result.get("status"),
                        "v7_run_id": result.get("run_id"),
                        "v6_content_id": result.get("v6_content_id"),
                    },
                )
                break
            except OutputValidationError as exc:
                _mark_node(
                    run_id,
                    node_key,
                    "failed",
                    str(exc)[:500],
                    output={
                        "retryable": False,
                        "failure_kind": "output_validation",
                        "error": str(exc)[:300],
                    },
                )
                _record_bootstrap_event(
                    run_id,
                    "canonical_v7.invalid_output",
                    node_key=node_key,
                    payload={"error": str(exc)[:300]},
                )
                return {"status": "invalid_output", "node_key": node_key}
            except Exception as exc:
                retryable = is_retryable_provider_failure(exc)
                failure_output = {
                    "retryable": retryable,
                    "failure_kind": "provider_transient" if retryable else "generation_contract",
                    "error": str(exc)[:300],
                    "generation_version": CHAPTER_SINGLE_PASS_GENERATION_VERSION,
                }
                _mark_node(
                    run_id,
                    node_key,
                    "pending_provider" if retryable else "failed",
                    str(exc)[:500],
                    output=failure_output,
                )
                _record_bootstrap_event(
                    run_id,
                    "canonical_v7.retrying" if retryable else "canonical_v7.non_retryable_failure",
                    node_key=node_key,
                    payload=failure_output,
                )
                if retryable:
                    raise self.retry(exc=exc, countdown=5)
                return {"status": "non_retryable_failure", "node_key": node_key}

        # ── Build node execution context ───────────────────────────────
        run_context = run["context"] if isinstance(run["context"], dict) else {}
        project_id = run["project_id"]
        novel_id = run["novel_id"]

        # Stage-aware context enrichment
        if stage == "blueprint":
            # Blueprint needs planning outputs as inputs
            run_context = _enrich_blueprint_context(run_context, novel_id)
        elif stage == "writing":
            # Writing needs chapter context window + V3 assembler layers
            context_window = _write_before_search(novel_id, chapter_seq, window_size=100)
            run_context["_context_window"] = context_window
            run_context["_chapter_seq"] = chapter_seq
            run_context["_chapter_outline"] = _chapter_outline_for_seq(run_context, chapter_seq)
            # V3 context assembly is a writing prerequisite. Optional history
            # layers may be empty for chapter one; infrastructure errors and
            # missing core planning inputs must fail truthfully.
            try:
                run_context = _assemble_bootstrap_writing_context(novel_id, run_context)
            except OutputValidationError as exc:
                _mark_node(
                    run_id,
                    node_key,
                    "failed",
                    str(exc),
                    output={
                        "retryable": False,
                        "failure_kind": "output_validation",
                        "error": str(exc)[:300],
                    },
                )
                _record_bootstrap_event(
                    run_id,
                    "node.failed",
                    node_key=node_key,
                    payload={"reason": "invalid_context", "detail": str(exc)[:200]},
                )
                return {"status": "invalid_output", "node_key": node_key}
            except Exception as exc:
                _mark_node(run_id, node_key, "failed", f"context assembler failed: {exc}"[:500])
                _record_bootstrap_event(
                    run_id,
                    "node.failed",
                    node_key=node_key,
                    payload={"reason": "context_assembler_failed", "detail": str(exc)[:200]},
                )
                raise self.retry(exc=exc, countdown=5)
            # P2-T2 / Q9: cap the unbounded writing context (prior-chapter window +
            # planning text blobs) to a token budget, mirroring ContextAssembler's
            # 5400-token cap so a million-word outline cannot blow the prompt cost
            # or get silently truncated.
            for _field in ("_context_window", "_characters_text", "_worldview_text", "_chapter_outline"):
                _val = run_context.get(_field)
                if _val is None:
                    continue
                run_context[_field] = (
                    cap_context_tokens(_val, 5400) if isinstance(_val, str)
                    else cap_context_tokens(str(_val), 5400)
                )
            # Chapter idempotency: check if chapter already exists
            idem_key = _chapter_idempotency_key(novel_id, chapter_seq)
            conn = connect()
            existing_ch = conn.execute(
                """SELECT id, title, status, meta FROM contents
                   WHERE parent_id = %s AND type = 'chapter'
                     AND generation_key = %s AND is_deleted = FALSE""",
                (novel_id, idem_key),
            ).fetchone()
            conn.close()
            if existing_ch and node_key == "write_chapter_draft":
                run_context["_existing_chapter"] = {
                    "id": existing_ch["id"],
                    "title": existing_ch["title"],
                    "status": existing_ch["status"],
                }
        elif stage == "finalization":
            # Finalization needs full chapter text + all prior context
            run_context = _enrich_finalization_context(run_context, novel_id)

        # ── Execute AI call ─────────────────────────────────────────────
        # Idempotent within one claimed node attempt, but a deliberate retry
        # must obtain a fresh provider result instead of replaying the output
        # that caused the previous attempt to fail (or that depended on stale
        # upstream planning context).
        node_attempt = int(node.get("attempt") or 0) + 1
        client_mutation_id = f"bootstrap:{run_id}:{node_key}:attempt-v1:{node_attempt}"

        try:
            if task_type == "plan_idea":
                # A planning model is not allowed to grade its own fidelity.
                # Run an independent, recorded AI audit against the raw user
                # request and feed any concrete defects into a fresh sample.
                # The node cannot advance to title selection until the audit has
                # zero contradictions and omissions.
                fidelity_feedback: list[str] = []
                output = {}
                fidelity_cycle = int(node.get("attempt") or 0) + 1
                for fidelity_attempt in range(1, 4):
                    quality_directive, quality_metadata, payoff_contract = _quality_directive_for_chapter(run_context)
                    plan_variables = {
                        **run_context,
                        "fidelity_feedback": "；".join(fidelity_feedback),
                        "quality_profile_directive": quality_directive,
                        "opening_contract": _opening_contract_for_chapter(run_context),
                        "quality_profile": json.dumps(quality_metadata, ensure_ascii=False),
                        "payoff_contract": json.dumps(payoff_contract, ensure_ascii=False),
                        "market_benchmark": json.dumps(
                            _market_benchmark_for_run(run_context, chapter_number=1),
                            ensure_ascii=False,
                        ),
                        "core_mechanic_guidance": mechanic_contract_guidance(
                            str(run_context.get("idea") or "")
                        ),
                    }
                    output = complete(
                        run_id=run_id,
                        node_key=node_key,
                        project_id=project_id,
                        task_type=task_type,
                        prompt_name="bootstrap.plan_idea",
                        variables=plan_variables,
                        client_mutation_id=(
                            f"bootstrap:{run_id}:{node_key}:fidelity-v2:cycle:{fidelity_cycle}:plan:{fidelity_attempt}"
                        ),
                    )
                    output = validate_task_output(task_type, output)
                    target_feedback = _target_words_guard(
                        output,
                        run_context.get("target_words"),
                    ) or _planning_contract_feedback(output, run_context)
                    if target_feedback:
                        # Do not spend all fidelity attempts asking the same
                        # long prompt to remember omitted ledgers.  Use a
                        # focused repair call that returns only the creative
                        # bible and structured contracts, then merge it into
                        # the original plan before the independent audit.
                        repair_output = complete(
                            run_id=run_id,
                            node_key=node_key,
                            project_id=project_id,
                            task_type="repair_planning_contract",
                            prompt_name="bootstrap.repair_planning_contract",
                            variables={
                                **run_context,
                                "plan_output": json.dumps(output, ensure_ascii=False),
                                "repair_feedback": target_feedback,
                                "requires_simulator": str(any(
                                    family == "simulator"
                                    for family in mechanic_families_for_idea(
                                        str(run_context.get("idea") or "")
                                    )
                                )).lower(),
                                "core_mechanic_guidance": mechanic_contract_guidance(
                                    str(run_context.get("idea") or "")
                                ),
                            },
                            client_mutation_id=(
                                f"bootstrap:{run_id}:{node_key}:contract-repair:{fidelity_cycle}:{fidelity_attempt}"
                            ),
                        )
                        repair_output = validate_task_output(
                            "repair_planning_contract", repair_output
                        )
                        for repair_key in (
                            "creative_bible",
                            "longform_contract",
                            "core_mechanic_contract",
                            "simulator_contract",
                        ):
                            if repair_key in repair_output:
                                output[repair_key] = repair_output[repair_key]
                        # Keep the large creative bible on a separate, small
                        # response path.  Providers frequently return the
                        # structured ledgers correctly but truncate the prose
                        # bible when both are requested in one response.  The
                        # bible is expanded only when it is objectively below
                        # the long-form floor; this is not a second rewrite of
                        # an already valid plan.
                        try:
                            _target_for_bible = int(run_context.get("target_words") or 0)
                        except (TypeError, ValueError):
                            _target_for_bible = 0
                        _bible_minimum = 2200 if _target_for_bible >= 1_000_000 else 1600
                        _current_bible = str(output.get("creative_bible") or "")
                        _bible_section_defects = (
                            creative_bible_section_defects(_current_bible)
                            + creative_bible_strategy_section_defects(_current_bible)
                        )
                        if (
                            _target_for_bible >= 500_000
                            and (
                                len(_current_bible.replace("\n", "")) < _bible_minimum
                                or _bible_section_defects
                            )
                        ):
                            for _bible_attempt in range(1, 3):
                                _missing_sections = (
                                    "；".join(_bible_section_defects)
                                    if _bible_section_defects
                                    else "无"
                                )
                                bible_repair = complete(
                                    run_id=run_id,
                                    node_key=node_key,
                                    project_id=project_id,
                                    task_type="expand_creative_bible",
                                    prompt_name="bootstrap.expand_creative_bible",
                                    variables={
                                        **run_context,
                                        "creative_bible": _current_bible,
                                        "repair_feedback": (
                                            f"{target_feedback}；上一次扩写约 {len(_current_bible.replace(chr(10), ''))} 字，"
                                            f"本次至少扩写到 {_bible_minimum + 200} 字；当前缺失章节：{_missing_sections}。"
                                            "必须保留当前创作圣经的有效内容，只补写缺失章节，且九个必需章节都要用明确小标题呈现，"
                                            "不能只改标题、重复原句或用一句空话占位。"
                                        ),
                                    },
                                    client_mutation_id=(
                                        f"bootstrap:{run_id}:{node_key}:bible-repair:"
                                        f"{fidelity_cycle}:{fidelity_attempt}:{_bible_attempt}"
                                    ),
                                )
                                bible_repair = validate_task_output(
                                    "expand_creative_bible", bible_repair
                                )
                                output["creative_bible"] = bible_repair["creative_bible"]
                                _current_bible = str(output.get("creative_bible") or "")
                                _bible_section_defects = (
                                    creative_bible_section_defects(_current_bible)
                                    + creative_bible_strategy_section_defects(_current_bible)
                                )
                                if (
                                    len(_current_bible.replace("\n", "")) >= _bible_minimum
                                    and not _bible_section_defects
                                ):
                                    break
                        target_feedback = _target_words_guard(
                            output,
                            run_context.get("target_words"),
                        ) or _planning_contract_feedback(output, run_context)
                        if not target_feedback:
                            fidelity_feedback = []
                        else:
                            fidelity_feedback = [target_feedback]
                            continue
                    audit = complete(
                        run_id=run_id,
                        node_key=node_key,
                        project_id=project_id,
                        task_type="audit_plan_fidelity",
                        prompt_name="bootstrap.audit_plan_fidelity",
                        variables={
                            "idea": run_context.get("idea", ""),
                            "target_words": run_context.get("target_words", ""),
                            "plan_output": json.dumps(output, ensure_ascii=False),
                        },
                        client_mutation_id=(
                            f"bootstrap:{run_id}:{node_key}:fidelity-v2:cycle:{fidelity_cycle}:audit:{fidelity_attempt}"
                        ),
                    )
                    audit = validate_task_output("audit_plan_fidelity", audit)
                    contradictions = [str(item).strip() for item in audit.get("contradictions", []) if str(item).strip()]
                    omissions = [str(item).strip() for item in audit.get("omissions", []) if str(item).strip()]
                    # 审计模型常把"建议修改/未明确但仍符合/新增细节"误归类为矛盾，过滤掉
                    _SUGGESTION_MARKERS = ("符合", "未明确提及", "未指定", "新增", "建议明确", "未违反", "未强制")
                    real_contradictions = [
                        c for c in contradictions
                        if not any(m in c for m in _SUGGESTION_MARKERS)
                    ]
                    passed = not real_contradictions
                    # score + omissions → advisory only, not blocking
                    # Still feed omissions into retry feedback so the plan improves
                    if passed and omissions:
                        output["plan_fidelity_warnings"] = omissions
                    if passed:
                        output["plan_fidelity_audit"] = audit
                        break
                    fidelity_feedback = contradictions + omissions
                else:
                    raise OutputValidationError(
                        "plan fidelity audit rejected output after 3 real revisions: "
                        + "；".join(fidelity_feedback[:8])
                    )
            else:
                quality_feedback = ""
                if task_type == "blueprint_chapter_outline":
                    quality_attempts = 2
                elif task_type in {
                    "write_chapter_draft",
                    "write_polish",
                    "final_humanize",
                    "blueprint_volume_plan",
                }:
                    quality_attempts = 3
                else:
                    quality_attempts = 1
                for quality_attempt in range(1, quality_attempts + 1):
                    quality_directive, quality_metadata, payoff_contract = _quality_directive_for_chapter(run_context)
                    variables = {
                        **run_context,
                        "length_retry_feedback": quality_feedback,
                        "quality_retry_feedback": quality_feedback,
                        "quality_profile_directive": quality_directive,
                        "opening_contract": _opening_contract_for_chapter(run_context),
                        "quality_profile": json.dumps(quality_metadata, ensure_ascii=False),
                        "payoff_contract": json.dumps(payoff_contract, ensure_ascii=False),
                        "market_benchmark": json.dumps(
                                _market_benchmark_for_run(run_context, chapter_number=chapter_seq),
                            ensure_ascii=False,
                        ),
                    }
                    # V3 Strategy Library (§6): inject the matched strategy
                    # directive + Writer skill hints into the prompt. Missing
                    # strategy degrades to "" / [] (no-op), so generation is
                    # never blocked.
                    if task_type == "write_chapter_draft":
                        directive, skill_hints = _strategy_directive_for_chapter(run_context)
                        variables["strategy_directive"] = directive
                        variables["skill_hints"] = "；".join(skill_hints) if skill_hints else ""
                    output = complete(
                        run_id=run_id,
                        node_key=node_key,
                        project_id=project_id,
                        task_type=task_type or "",
                        prompt_name=f"bootstrap.{task_type}" if task_type else "",
                        variables=variables,
                        client_mutation_id=(
                            f"{client_mutation_id}:quality:{quality_attempt}"
                            if quality_attempts > 1 else client_mutation_id
                        ),
                    )
                    output = validate_task_output(task_type or "", output)
                    if task_type == "final_humanize":
                        output, normalize_feedback = _normalize_final_humanize_output(
                            str(run_context.get("_chapter_body") or ""),
                            output,
                        )
                        if normalize_feedback:
                            quality_feedback = normalize_feedback
                            continue
                    if task_type == "write_polish":
                        output = _reflow_polish_paragraphs(
                            str(run_context.get("chapter_text") or ""),
                            output,
                        )
                    if task_type == "write_chapter_draft":
                        quality_feedback = _draft_length_feedback(output)
                    elif task_type == "write_polish":
                        quality_feedback = _polish_quality_feedback(
                            str(run_context.get("chapter_text") or ""),
                            output,
                        )
                    elif task_type == "final_humanize":
                        quality_feedback = _humanize_quality_feedback(
                            str(run_context.get("_chapter_body") or ""),
                            output,
                        )
                    elif task_type == "blueprint_volume_plan":
                        quality_feedback = _volume_plan_feedback(output, run_context)
                    elif task_type == "blueprint_chapter_outline":
                        defects = validate_chapter_outline_user_contract(
                            output,
                            idea=str(run_context.get("idea") or ""),
                        )
                        quality_feedback = "；".join(defects)
                    else:
                        break
                    if not quality_feedback:
                        break
                else:
                    raise OutputValidationError(
                        f"{task_type} failed quality gate after {quality_attempts} real generations: "
                        f"{quality_feedback}"
                    )
        except BudgetExceeded:
            _mark_node(run_id, node_key, "pending_budget", "budget exceeded")
            _record_bootstrap_event(run_id, "node.failed", node_key=node_key,
                                    payload={"reason": "budget_exceeded"})
            return {"status": "pending_budget", "node_key": node_key}
        except OutputValidationError as exc:
            _mark_node(
                run_id,
                node_key,
                "failed",
                str(exc),
                output={
                    "retryable": False,
                    "failure_kind": "output_validation",
                    "error": str(exc)[:300],
                },
            )
            _record_bootstrap_event(run_id, "node.failed", node_key=node_key,
                                    payload={"reason": "invalid_output"})
            return {"status": "invalid_output", "node_key": node_key}
        except ProviderError as exc:
            retryable = is_retryable_provider_failure(exc)
            failure_output = {
                "retryable": retryable,
                "failure_kind": "provider_transient" if retryable else "provider_configuration",
                "error": str(exc)[:300],
            }
            _mark_node(
                run_id,
                node_key,
                "pending_provider" if retryable else "failed",
                f"provider error: {exc}"[:500],
                output=failure_output,
            )
            _record_bootstrap_event(
                run_id,
                "node.retrying" if retryable else "node.non_retryable_failure",
                node_key=node_key,
                payload={"reason": "provider_error", **failure_output},
            )
            if retryable:
                raise self.retry(exc=exc, countdown=5)
            return {"status": "provider_failure", "node_key": node_key}
        except Exception as exc:
            retryable = is_retryable_provider_failure(exc)
            failure_output = {
                "retryable": retryable,
                "failure_kind": "provider_transient" if retryable else "deterministic_failure",
                "error": str(exc)[:300],
            }
            _mark_node(
                run_id,
                node_key,
                "pending_provider" if retryable else "failed",
                str(exc),
                output=failure_output,
            )
            _record_bootstrap_event(
                run_id,
                "node.retrying" if retryable else "node.non_retryable_failure",
                node_key=node_key,
                payload=failure_output,
            )
            if retryable:
                raise self.retry(exc=exc, countdown=5)
            return {"status": "non_retryable_failure", "node_key": node_key}

        # ── Persist output + track budget ──────────────────────────────
        budget_info = _estimate_node_cost(run_id, node_key, output)
        _track_budget(run_id, node_key, budget_info.get("cost_cny", 0))

        try:
            _persist_output(run_id, node_key, task_type or "", output, novel_id, project_id)
        except OutputValidationError as exc:
            _mark_node(
                run_id,
                node_key,
                "failed",
                str(exc),
                output={
                    "retryable": False,
                    "failure_kind": "output_validation",
                    "error": str(exc)[:300],
                },
            )
            _record_bootstrap_event(run_id, "node.failed", node_key=node_key,
                                    payload={"reason": "invalid_persisted_output", "detail": str(exc)[:200]})
            return {"status": "invalid_output", "node_key": node_key}

        _record_bootstrap_event(run_id, "node.completed", node_key=node_key,
                                payload={"budget": budget_info})

    # ── Workflow complete ──────────────────────────────────────────────────
    conn = connect()
    completed_run = conn.execute("SELECT * FROM workflow_runs WHERE id=%s", (run_id,)).fetchone()
    completed_context = completed_run["context"] if completed_run and isinstance(completed_run["context"], dict) else {}
    chapter_id = completed_context.get("chapter_id")
    chapter = conn.execute("SELECT status FROM contents WHERE id=%s", (chapter_id,)).fetchone() if chapter_id else None
    canonical_status = completed_context.get("canonical_generation_status")
    if canonical_status == "pending_approval":
        final_status = "waiting_human"
        current_node_key = "write_chapter_draft"
        finished_at_sql = "NULL"
    elif canonical_status in {"needs_review", "needs_rewrite"} or (
        chapter and chapter["status"] == "needs_rewrite"
    ):
        final_status = "needs_review"
        current_node_key = None
        finished_at_sql = "now()"
    elif canonical_status and canonical_status != "completed":
        final_status = "failed"
        current_node_key = "write_chapter_draft"
        finished_at_sql = "now()"
    else:
        final_status = "succeeded"
        current_node_key = None
        finished_at_sql = "now()"
    conn.execute(
        f"""UPDATE workflow_runs
            SET status=%s, current_node_key=%s, finished_at={finished_at_sql}, updated_at=now()
            WHERE id=%s""",
        (final_status, current_node_key, run_id),
    )
    novel_status = "needs_review" if final_status != "succeeded" else "draft"
    topic_status = "needs_review" if final_status != "succeeded" else "generated"
    if completed_run and completed_run.get("novel_id"):
        conn.execute("UPDATE contents SET status=%s,updated_at=now() WHERE id=%s",
                     (novel_status, completed_run["novel_id"]))
        conn.execute("UPDATE topic_candidates SET status=%s WHERE novel_id=%s",
                     (topic_status, completed_run["novel_id"]))
    conn.commit()
    conn.close()
    celery_app.backend.set(f"run:{run_id}:status", final_status)
    _record_bootstrap_event(run_id, "run.completed", payload={"status": final_status})
    return {"status": final_status}


@celery_app.task(name="app.core.billing.reset_monthly_usage")
def monthly_usage_reset() -> dict[str, Any]:
    """Celery wrapper for the monthly usage reset (P1-T2).

    The heavy lifting lives in ``app.core.billing.reset_monthly_usage`` so it
    stays testable without a Celery runtime. Registered under the canonical
    name so the beat schedule can reference ``app.core.billing.reset_monthly_usage``.
    """
    from app.core.billing import reset_monthly_usage
    return reset_monthly_usage()
# ═══════════════════════════════════════════════════════════════════════════
# Context enrichment helpers
# ═══════════════════════════════════════════════════════════════════════════

def _enrich_blueprint_context(context: dict, novel_id: str) -> dict:
    """Enrich context for blueprint stage with character/worldview/conflict data."""
    db = connect()
    # Fetch all knowledge items produced in planning stage
    knowledge_rows = db.execute(
        """SELECT kind, title, body, meta FROM knowledge_items
           WHERE content_id = %s AND is_deleted = FALSE
           ORDER BY kind""",
        (novel_id,),
    ).fetchall()
    db.close()

    enriched = dict(context)
    worldview = ""
    characters_text = ""
    for kr in knowledge_rows:
        kind = kr.get("kind", "")
        body = kr.get("body", "")
        if kind == "worldview" and body:
            worldview = body
        elif kind == "character" and body:
            characters_text += f"\n- {kr.get('title', '')}: {body}"

    if worldview:
        enriched["_worldview_text"] = worldview[:3000]
    if characters_text:
        enriched["_characters_text"] = characters_text[:3000]
    # V3 Story Arc (§4): expose the volume plan to blueprint-stage nodes
    # (including generate_story_arc) so arcs can estimate chapter_range.
    _db2 = connect()
    meta_row = _db2.execute("SELECT meta FROM contents WHERE id = %s", (novel_id,)).fetchone()
    _db2.close()
    if meta_row:
        _m = meta_row["meta"] if isinstance(meta_row["meta"], dict) else {}
        _vp = _m.get("volume_plan")
        if _vp:
            enriched["_volume_plan"] = json.dumps(_vp, ensure_ascii=False)[:2000]
        if _m.get("longform_contract"):
            enriched["longform_contract"] = json.dumps(
                _m.get("longform_contract"), ensure_ascii=False
            )[:4000]
        if _m.get("core_mechanic_contract"):
            enriched["core_mechanic_contract"] = json.dumps(
                _m.get("core_mechanic_contract"), ensure_ascii=False
            )[:5000]
        if _m.get("simulator_contract"):
            enriched["simulator_contract"] = json.dumps(
                _m.get("simulator_contract"), ensure_ascii=False
            )[:5000]
    return enriched
def _enrich_finalization_context(context: dict, novel_id: str) -> dict:
    """Enrich context for finalization with full chapter body + entity states."""
    enriched = dict(context)
    chapter_seq = int(context.get("_chapter_seq") or 1)
    enriched["_chapter_outline"] = _chapter_outline_for_seq(context, chapter_seq)
    # Get chapter body text
    chapter_id = context.get("chapter_id", "")
    if chapter_id:
        db = connect()
        ch = db.execute("SELECT body, meta FROM contents WHERE id = %s", (chapter_id,)).fetchone()
        db.close()
        if ch:
            body = ch.get("body", "")
            enriched["_chapter_body"] = extract_body_text(body)[:12000]

    # Get entity snapshot
    reconc_res = _write_after_reconcile(novel_id, chapter_id or "",
                                        enriched.get("_chapter_body", ""))
    enriched["_reconciliation"] = reconc_res
    return enriched
# ═══════════════════════════════════════════════════════════════════════════
# Budget estimation
# ═══════════════════════════════════════════════════════════════════════════

def _estimate_node_cost(run_id: str, node_key: str, output: dict) -> dict:
    """Estimate the cost of a single node execution.

    Queries the ai_calls table for the most recent call matching this
    run_id + node_key to get actual token usage.
    """
    try:
        db = connect()
        ai_call = db.execute(
            """SELECT prompt_tokens, completion_tokens, cost_cny
               FROM ai_calls
               WHERE client_mutation_id LIKE %s
               ORDER BY created_at DESC LIMIT 1""",
            (f"bootstrap:{run_id}:{node_key}%",),
        ).fetchone()
        db.close()
        if ai_call:
            return {
                "cost_cny": float(ai_call.get("cost_cny") or 0),
                "prompt_tokens": int(ai_call.get("prompt_tokens") or 0),
                "completion_tokens": int(ai_call.get("completion_tokens") or 0),
            }
    except Exception:
        pass
    # Fallback: multiplier-based estimate
    multiplier = NODE_BUDGET_MULTIPLIERS.get(node_key, 1.0)
    return {"cost_cny": round(multiplier * 0.02, 6), "prompt_tokens": 0,
            "completion_tokens": 0, "estimated": True}
# ═══════════════════════════════════════════════════════════════════════════
# Run creation + dispatch
# ═══════════════════════════════════════════════════════════════════════════

def create_run(project_id: str, novel_id: str,
               api_key: str = "", api_url: str = "", model: str = "",
               selected_title: str = "", idempotency_key: str | None = None,
               auto_confirm_title: bool = False) -> str:
    """Create a workflow run through the complete planning-to-audit pipeline.

    A preselected title locks only the title gate. It never bypasses source
    decomposition, creative-bible planning, or quality controls.
    """
    db = connect()
    if idempotency_key:
        existing = db.execute(
            "SELECT * FROM workflow_runs WHERE project_id=%s AND idempotency_key=%s",
            (project_id, idempotency_key),
        ).fetchone()
        if existing:
            db.close()
            if existing["status"] == "dispatch_failed" or (
                existing["status"] == "pending" and not existing.get("last_dispatched_at")
            ):
                dispatch_bootstrap_run(existing["id"], existing.get("current_node_key") or "plan_idea",
                                       api_key, api_url, model)
            return existing["id"]

    novel = db.execute("SELECT * FROM contents WHERE id = %s", (novel_id,)).fetchone()
    if novel is None:
        db.close()
        raise ValueError("novel not found")

    meta = novel["meta"] if isinstance(novel["meta"], dict) else {}
    context = {"novel_id": novel_id, "idea": meta.get("idea", ""), "suggested_title": "", **meta}
    if selected_title:
        context["suggested_title"] = selected_title
        # Ranking-originated titles are already the product's selected title.
        # Keep the value explicit so the unattended human-node branch does not
        # replace it with the first title candidate returned by plan_idea.
        if auto_confirm_title:
            context["selected_title"] = selected_title
    if auto_confirm_title:
        context["auto_confirm_title"] = True

    run_id = new_id()
    db.execute(
        "INSERT INTO workflow_runs "
        "(id, project_id, novel_id, workflow_key, status, current_node_key, context, idempotency_key) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, project_id, novel_id, "bootstrap", "pending", "plan_idea", encode(context), idempotency_key),
    )

    # Seed all nodes from BOOTSTRAP_NODES
    for node_key, kind, agent, title, _task_type in BOOTSTRAP_NODES:
        db.execute(
            "INSERT INTO run_nodes (id, run_id, node_key, kind, agent, title) VALUES (%s, %s, %s, %s, %s, %s)",
            (new_id(), run_id, node_key, kind, agent, title),
        )

    start_key = "plan_idea"

    db.commit()
    db.close()

    # Record ledger event
    _record_bootstrap_event(run_id, "run.created", node_key=start_key,
                            payload={
                                "selected_title": selected_title if selected_title else None,
                                "auto_confirm_title": bool(auto_confirm_title),
                            })

    dispatch_bootstrap_run(run_id, start_key, api_key, api_url, model)
    return run_id
def dispatch_bootstrap_run(run_id: str, start_key: str, api_key: str = "",
                           api_url: str = "", model: str = "") -> None:
    """Dispatch or redrive one committed run, persisting broker failures."""
    try:
        api_key_ref = stash_byok_key(api_key)
        try:
            execute_bootstrap.delay(
                run_id, start_key, "", api_url, model, api_key_ref=api_key_ref
            )
        except TypeError as exc:
            if "api_key_ref" not in str(exc):
                raise
            execute_bootstrap.delay(run_id, start_key, "", api_url, model)
    except Exception as exc:
        db = connect()
        db.execute("""UPDATE workflow_runs SET status='dispatch_failed', dispatch_attempts=dispatch_attempts+1,
                      dispatch_error=%s, updated_at=now() WHERE id=%s""", (str(exc), run_id))
        db.commit(); db.close()
        raise
    db = connect()
    db.execute("""UPDATE workflow_runs SET status=CASE WHEN status='dispatch_failed' THEN 'pending' ELSE status END,
                  dispatch_attempts=dispatch_attempts+1, last_dispatched_at=now(), dispatch_error=NULL, updated_at=now()
                  WHERE id=%s""", (run_id,))
    db.commit(); db.close()
def confirm_human(run_id: str, selected_title: str,
                  api_key: str = "", api_url: str = "", model: str = "") -> None:
    """Confirm human node selection and continue workflow to blueprint stage."""
    db = connect()
    run = db.execute("SELECT * FROM workflow_runs WHERE id = %s", (run_id,)).fetchone()
    if run is None:
        db.close()
        raise ValueError("run not found")
    context = run["context"] if isinstance(run["context"], dict) else {}
    context["selected_title"] = selected_title
    db.execute("UPDATE contents SET title = %s, updated_at = now() WHERE id = %s", (selected_title, run["novel_id"]))
    db.execute(
        "UPDATE run_nodes SET status = 'succeeded', output = %s, finished_at = now() WHERE run_id = %s AND node_key = %s",
        (encode({"selected_title": selected_title}), run_id, "human_confirm_title"),
    )
    db.execute(
        "UPDATE workflow_runs SET status = 'pending', current_node_key = %s, context = %s, updated_at = now() WHERE id = %s",
        ("blueprint_volume_plan", encode(context), run_id),
    )
    db.commit()
    db.close()

    _record_bootstrap_event(run_id, "human.confirmed", node_key="human_confirm_title",
                            payload={"selected_title": selected_title})
    api_key_ref = stash_byok_key(api_key)
    try:
        execute_bootstrap.delay(
            run_id, "blueprint_volume_plan", "", api_url, model,
            api_key_ref=api_key_ref,
        )
    except TypeError as exc:
        if "api_key_ref" not in str(exc):
            raise
        execute_bootstrap.delay(run_id, "blueprint_volume_plan", "", api_url, model)
# ═══════════════════════════════════════════════════════════════════════════
# Node marking + output persistence
# ═══════════════════════════════════════════════════════════════════════════

def _mark_node(
    run_id: str,
    node_key: str,
    status: str,
    error: str,
    *,
    output: dict[str, Any] | None = None,
) -> None:
    db = connect()
    if output is None:
        db.execute(
            "UPDATE run_nodes SET status = %s, error = %s, finished_at = now() WHERE run_id = %s AND node_key = %s",
            (status, error, run_id, node_key),
        )
    else:
        db.execute(
            "UPDATE run_nodes SET status = %s, error = %s, output = %s, finished_at = now() WHERE run_id = %s AND node_key = %s",
            (status, error, encode(output), run_id, node_key),
        )
    db.execute(
        "UPDATE workflow_runs SET status = %s, current_node_key = %s, updated_at = now() WHERE id = %s",
        (status, node_key, run_id),
    )
    db.commit()
    db.close()
def _persist_output(run_id: str, node_key: str, task_type: str, output: dict,
                    novel_id: str = "", project_id: str = "") -> None:
    """Persist node output to DB, update context, handle knowledge items."""
    db = connect()
    knowledge_ids_to_reindex: list[str] = []

    run = db.execute("SELECT * FROM workflow_runs WHERE id = %s", (run_id,)).fetchone()
    if run is None:
        db.close()
        return

    node = db.execute("SELECT * FROM run_nodes WHERE run_id=%s AND node_key=%s FOR UPDATE",
                      (run_id, node_key)).fetchone()
    if node and node["status"] == "succeeded":
        db.close()
        return

    context = run["context"] if isinstance(run["context"], dict) else {}
    context.update(output)
    _novel_id = novel_id or run["novel_id"]
    _project_id = project_id or run["project_id"]

    # ── Stage-aware output handling ─────────────────────────────────────
    stage = NODE_STAGE.get(node_key, "unknown")

    if task_type == "plan_idea":
        context["idea_expanded"] = output.get("idea_expanded", output.get("idea", ""))
        creative_bible = str(output.get("creative_bible") or "").strip()
        if creative_bible:
            meta_row = db.execute("SELECT meta FROM contents WHERE id = %s", (_novel_id,)).fetchone()
            if meta_row:
                m = meta_row["meta"] if isinstance(meta_row["meta"], dict) else {}
                # User-selected genre/subgenre/platform/plugin are authoritative
                # generation inputs. A planning model may describe the story in
                # different words, but it must not silently downgrade an enabled
                # style plugin (for example long-life -> generic upgrade).
                authoritative_context = dict(context)
                for _key in ("genre_id", "genre", "subgenre", "platform", "style", "style_plugin", "writing_plugin", "web_research_mode"):
                    if m.get(_key) not in (None, ""):
                        authoritative_context[_key] = m.get(_key)
                if isinstance(m.get("quality_profile"), dict):
                    authoritative_context["quality_profile"] = m["quality_profile"]
                selected_profile = profile_from_context(authoritative_context)
                for _key in ("genre_id", "genre", "subgenre", "platform", "style", "style_plugin", "web_research_mode"):
                    if m.get(_key) not in (None, ""):
                        context[_key] = m.get(_key)
                m["quality_profile"] = quality_profile_metadata(selected_profile)
                context["quality_profile"] = quality_profile_metadata(selected_profile)
                m["creative_bible"] = creative_bible
                synopsis = str(output.get("synopsis") or "").strip()
                if synopsis:
                    m["synopsis"] = synopsis
                m["core_hook"] = output.get("core_hook", "")
                m["target_audience"] = output.get("target_audience", "")
                m["source_facts"] = output.get("source_facts", [])
                m["design_additions"] = output.get("design_additions", [])
                m["forbidden_changes"] = output.get("forbidden_changes", [])
                m["planning_module"] = "creative_bible_v2"
                # Store the numeric long-form ledger and any core fictional
                # mechanic as first-class metadata.  The prose runtime reads
                # these on every chapter, so later generations cannot silently
                # fall back to a shorter route or a weaker simulator rule.
                if isinstance(output.get("longform_contract"), dict):
                    m["longform_contract"] = output["longform_contract"]
                if isinstance(output.get("core_mechanic_contract"), dict):
                    m["core_mechanic_contract"] = output["core_mechanic_contract"]
                if isinstance(output.get("simulator_contract"), dict):
                    m["simulator_contract"] = output["simulator_contract"]
                m["planning_contract_version"] = "v2"
                # V3 Novel DNA (§3): store as structured novel metadata merged
                # with the creative bible, and carry it + forbidden_deviations
                # into run context so the Writer injects the red lines.
                novel_dna = {
                    "commercial_positioning": str(output.get("commercial_positioning", "")),
                    "story_promise": str(output.get("story_promise", "")),
                    "forbidden_deviations": list(output.get("forbidden_deviations", []) or []),
                }
                dna_self_check = _check_novel_dna_consistency(novel_dna)
                m["novel_dna"] = novel_dna
                m["novel_dna_self_check"] = dna_self_check
                # Top-level key (mirrors source_facts/forbidden_changes) so a fresh
                # run seeded from **novel_meta has forbidden_deviations available
                # to the Writer without waiting for in-memory context mutation.
                m["forbidden_deviations"] = novel_dna["forbidden_deviations"]
                context["novel_dna"] = novel_dna
                context["forbidden_deviations"] = novel_dna["forbidden_deviations"]
                db.execute("UPDATE contents SET meta = %s, updated_at = now() WHERE id = %s", (encode(m), _novel_id))
            knowledge_id = new_id()
            generation_key = f"run:{run_id}:node:{node_key}:creative-bible:v1"
            stored = db.execute(
                """INSERT INTO knowledge_items
                   (id, project_id, content_id, kind, title, body, meta, generation_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (content_id, generation_key) WHERE generation_key IS NOT NULL AND is_deleted=FALSE
                   DO UPDATE SET title=EXCLUDED.title, body=EXCLUDED.body, meta=EXCLUDED.meta, updated_at=now()
                   RETURNING id""",
                (
                    knowledge_id,
                    _project_id,
                    _novel_id,
                    "creative_bible",
                    "创作圣经",
                    creative_bible,
                    encode({"source_node": node_key, "core_hook": output.get("core_hook", "")}),
                    generation_key,
                ),
            ).fetchone()
            knowledge_ids_to_reindex.append(stored["id"] if stored else knowledge_id)
    elif task_type == "plan_market_fit":
        market_fit = dict(output)
        if not isinstance(market_fit.get("evidence"), dict) or not market_fit.get("evidence"):
            market_fit["evidence"] = _market_benchmark_for_run(context, chapter_number=1)
        context["market_fit"] = market_fit
    elif task_type == "plan_story_pattern":
        context["story_pattern"] = output
    elif task_type == "plan_core_gameplay":
        context["core_gameplay"] = output
    elif task_type == "plan_world_architecture":
        wv = output.get("worldview", output)
        if isinstance(wv, dict) and wv.get("name"):
            knowledge_id = new_id()
            generation_key = f"run:{run_id}:node:{node_key}:worldview:v2"
            stored = db.execute(
                """INSERT INTO knowledge_items
                   (id, project_id, content_id, kind, title, body, meta, generation_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (content_id, generation_key) WHERE generation_key IS NOT NULL AND is_deleted=FALSE
                   DO UPDATE SET title=EXCLUDED.title, body=EXCLUDED.body, meta=EXCLUDED.meta, updated_at=now()
                   RETURNING id""",
                (knowledge_id, _project_id, _novel_id, "worldview",
                 wv.get("name", ""), "\n".join(wv.get("rules", [])), encode(wv), generation_key),
            ).fetchone()
            knowledge_ids_to_reindex.append(stored["id"] if stored else knowledge_id)
        # Also update novel metadata
        meta_row = db.execute("SELECT meta FROM contents WHERE id = %s", (_novel_id,)).fetchone()
        if meta_row:
            m = meta_row["meta"] if isinstance(meta_row["meta"], dict) else {}
            m["worldview"] = wv
            db.execute("UPDATE contents SET meta = %s, updated_at = now() WHERE id = %s", (encode(m), _novel_id))
    elif task_type == "plan_character_system":
        characters = output.get("characters", [])
        for index, c in enumerate(characters):
            knowledge_id = new_id()
            generation_key = f"run:{run_id}:node:{node_key}:character:{index}:v2"
            stored = db.execute(
                """INSERT INTO knowledge_items
                   (id, project_id, content_id, kind, title, body, meta, generation_key)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (content_id, generation_key) WHERE generation_key IS NOT NULL AND is_deleted=FALSE
                   DO UPDATE SET title=EXCLUDED.title, body=EXCLUDED.body, meta=EXCLUDED.meta, updated_at=now()
                   RETURNING id""",
                (knowledge_id, _project_id, _novel_id, "character", c.get("name", ""),
                 c.get("arc", ""), encode(c), generation_key),
            ).fetchone()
            knowledge_ids_to_reindex.append(stored["id"] if stored else knowledge_id)
    elif task_type == "plan_conflict_map":
        context["conflict_map"] = output
    elif task_type == "blueprint_volume_plan":
        meta_row = db.execute("SELECT meta FROM contents WHERE id = %s", (_novel_id,)).fetchone()
        if meta_row:
            m = meta_row["meta"] if isinstance(meta_row["meta"], dict) else {}
            m["volume_plan"] = output.get("volumes", output.get("volume_plan", []))
            m["chapter_tree"] = output.get("chapter_tree", [])
            if "total_word_target" in output:
                m["volume_plan_total_word_target"] = output.get("total_word_target")
            if isinstance(output.get("volume_word_targets"), list):
                m["volume_word_targets"] = output.get("volume_word_targets")
            db.execute("UPDATE contents SET meta = %s, updated_at = now() WHERE id = %s", (encode(m), _novel_id))
    elif task_type == "blueprint_chapter_outline":
        meta_row = db.execute("SELECT meta FROM contents WHERE id = %s", (_novel_id,)).fetchone()
        if meta_row:
            m = meta_row["meta"] if isinstance(meta_row["meta"], dict) else {}
            m["chapter_outlines"] = output.get("chapter_outlines", output.get("outlines", []))
            db.execute("UPDATE contents SET meta = %s, updated_at = now() WHERE id = %s", (encode(m), _novel_id))
    elif task_type == "blueprint_scene_beat":
        context["scene_beat_sheet"] = output
    elif task_type == "generate_story_arc":
        # V3 Story Arc (§4): persist each arc as an independent content entity
        # (type='story_arc'), parent_id = novel, backward compatible (old novels
        # without arcs simply have no story_arc rows). Active arcs feed the
        # 7-layer context assembler and the per-chapter Arc-deviation check.
        arcs = output.get("story_arcs", []) if isinstance(output, dict) else []
        if isinstance(arcs, list):
            for idx, arc in enumerate(arcs):
                if not isinstance(arc, dict):
                    continue
                arc_id = new_id()
                arc_meta = {
                    "goal": str(arc.get("goal", "")),
                    "start_state": str(arc.get("start_state", "")),
                    "end_state": str(arc.get("end_state", "")),
                    "participants": list(arc.get("participants", []) or []),
                    "core_conflict": str(arc.get("core_conflict", "")),
                    "key_events": list(arc.get("key_events", []) or []),
                    "payoff_points": list(arc.get("payoff_points", []) or []),
                    "foreshadowing_refs": list(arc.get("foreshadowing_refs", []) or []),
                    "outcome_impact": str(arc.get("outcome_impact", "")),
                    "status": str(arc.get("status", "planning")),
                    "chapter_range": list(arc.get("chapter_range", []) or []),
                    "arc_index": idx,
                }
                db.execute(
                    """INSERT INTO contents
                       (id, project_id, parent_id, type, title, status, meta, created_at)
                       VALUES (%s, %s, %s, 'story_arc', %s, 'planning', %s, now())""",
                    (arc_id, _project_id, _novel_id, str(arc.get("name", f"故事弧{idx+1}")), encode(arc_meta)),
                )
            # Also keep the full list on the novel meta for quick access / audits.
            meta_row = db.execute("SELECT meta FROM contents WHERE id = %s", (_novel_id,)).fetchone()
            if meta_row:
                m = meta_row["meta"] if isinstance(meta_row["meta"], dict) else {}
                m["story_arcs"] = arcs
                db.execute("UPDATE contents SET meta = %s, updated_at = now() WHERE id = %s", (encode(m), _novel_id))
            context["story_arcs"] = arcs
    elif task_type == "write_chapter_draft":
        _persist_chapter_draft(db, run, node_key, output, context, _novel_id, _project_id, run_id,
                               knowledge_ids_to_reindex)
    elif task_type == "write_self_review":
        context["self_review"] = output
        cid = context.get("chapter_id", "")
        if cid:
            db.execute(
                "UPDATE contents SET meta = meta || %s, updated_at = now() WHERE id = %s",
                (encode({"self_review": output, "review_score": output.get("self_score")}), cid),
            )
    elif task_type == "write_polish":
        _persist_chapter_polish(db, node_key, output, context, run_id)
    elif task_type == "write_length_check":
        context["length_check"] = output
    elif task_type == "write_fact_reconcile":
        _persist_fact_reconcile(db, node_key, output, context, _novel_id, run_id)
    elif task_type in ("final_consistency_check", "final_continuity_audit", "final_humanize"):
        context[task_type] = output
        if task_type == "final_consistency_check":
            checks = output.get("checks") if isinstance(output.get("checks"), dict) else {}
            from app.services.reader_experience import summarize_reader_experience
            reader_experience = summarize_reader_experience(output.get("reader_experience"))
            # V3 Chapter Function: pacing gate over the whole outline's
            # function_type sequence. Stored as a dimension, never blocks the
            # consistency gate itself.
            pacing = _check_chapter_function_pacing(context.get("chapter_outlines"))
            # V3 Story Arc (§4): deterministic arc-coverage check for the current
            # chapter. db is still open here, so read active arcs + the chapter's
            # seq/outline participants in one shot.
            arc_check: dict[str, Any] = {"status": "pass", "issues": [], "sampled": 0, "covered": False}
            _cid = context.get("chapter_id", "")
            if _cid:
                _row = db.execute("SELECT seq, meta FROM contents WHERE id=%s", (_cid,)).fetchone()
                if _row:
                    _seq = int(_row.get("seq") or (_row.get("meta") or {}).get("seq") or 0)
                    _arc_rows = db.execute(
                        "SELECT meta FROM contents WHERE parent_id=%s AND type='story_arc' AND is_deleted=FALSE",
                        (_novel_id,),
                    ).fetchall()
                    _arcs = [r["meta"] for r in _arc_rows if isinstance(r.get("meta"), dict)]
                    _outline = _chapter_outline_for_seq(context, _seq) or {}
                    arc_check = _check_story_arc_coverage(_arcs, _seq, _outline.get("participants") or [])
            # V3 Timeline Anchor (§10): deterministic anachronism check.
            # Only enabled when Novel DNA marks the book as reality-based
            # (现实向); fantasy books degrade to pass. Warning-only dimension —
            # like pacing/arc it is recorded but never blocks the gate on its
            # own (keyword table cannot be a hard blocker).
            anchor_check: dict[str, Any] = {"status": "pass", "issues": [], "anchor_year": None}
            if _cid:
                from app.services.timeline import (
                    check_anachronisms, is_reality_based, parse_year_anchor,
                )
                _nrow = db.execute("SELECT meta FROM contents WHERE id=%s", (_novel_id,)).fetchone()
                _dna = ((_nrow or {}).get("meta") or {}).get("novel_dna") \
                    if isinstance((_nrow or {}).get("meta"), dict) else None
                if is_reality_based(_dna):
                    _ev_rows = db.execute(
                        "SELECT real_world_anchor FROM timeline_events "
                        "WHERE chapter_id=%s AND real_world_anchor IS NOT NULL",
                        (_cid,),
                    ).fetchall()
                    _years = [y for y in (parse_year_anchor(r["real_world_anchor"]) for r in _ev_rows) if y]
                    _body_row = db.execute("SELECT body FROM contents WHERE id=%s", (_cid,)).fetchone()
                    _body_text = extract_body_text(_body_row["body"]) if _body_row else ""
                    if not _years:  # 章内事件无锚点时回退到正文年份标记
                        _years = [y for y in [parse_year_anchor(_body_text[:3000])] if y]
                    anchor_check = check_anachronisms(min(_years) if _years else None, _body_text)
            failed_checks = {
                name: check for name, check in checks.items()
                if not isinstance(check, dict)
                or check.get("status") != "pass"
                or bool(check.get("issues"))
            }
            if output.get("overall_status") != "pass" or failed_checks:
                cid = context.get("chapter_id", "")
                if cid:
                    # V3 Repair Engine (§8): classify the least-invasive repair
                    # and record the recommendation. sentence/paragraph ->
                    # repair_local (in-place); plot -> replan_chapter. The existing
                    # needs_rewrite path is preserved (chapter rewrite remains the
                    # safe default fallback).
                    repair_rec = _classify_repair_level(output)
                    db.execute(
                        "UPDATE contents SET status='needs_rewrite', meta=meta || %s, updated_at=now() WHERE id=%s",
                        (encode({"quality_gate": {"status": "failed", "checks": checks},
                                  "pacing_check": pacing, "arc_check": arc_check,
                                  "timeline_anchor_check": anchor_check,
                                  "reader_experience": reader_experience,
                                  "repair_recommendation": repair_rec}), cid),
                    )
                db.commit()
                db.close()
                raise OutputValidationError(
                    "final consistency gate rejected chapter: " + ", ".join(failed_checks.keys())
                )
            cid = context.get("chapter_id", "")
            if cid:
                db.execute(
                    "UPDATE contents SET meta = meta || %s, updated_at = now() WHERE id = %s",
                    (encode({
                        "final_consistency_check": output,
                        "pacing_check": pacing,
                        "arc_check": arc_check,
                        "timeline_anchor_check": anchor_check,
                        "reader_experience": reader_experience,
                        "review_7dim": _quality_evidence_payload(output, context.get("self_review"), pacing, arc_check),
                    }), cid),
                )
        if task_type == "final_continuity_audit":
            continuity = output.get("continuity") if isinstance(output.get("continuity"), dict) else {}
            if continuity.get("status") != "continuous" or bool(continuity.get("gaps")):
                cid = context.get("chapter_id", "")
                if cid:
                    db.execute(
                        "UPDATE contents SET status='needs_rewrite', meta=meta || %s, updated_at=now() WHERE id=%s",
                        (encode({"continuity_gate": {"status": "failed", "audit": continuity}}), cid),
                    )
                db.commit()
                db.close()
                raise OutputValidationError("final continuity gate rejected chapter")
            cid = context.get("chapter_id", "")
            if cid:
                db.execute(
                    "UPDATE contents SET meta = meta || %s, updated_at = now() WHERE id = %s",
                    (encode({"final_continuity_audit": output}), cid),
                )
        if task_type == "final_humanize":
            cid = context.get("chapter_id", "")
            if cid and output.get("humanized_text"):
                current = db.execute("SELECT body FROM contents WHERE id = %s", (cid,)).fetchone()
                before_text = extract_body_text(current["body"] if current else "")
                try:
                    normalized_text, shape = normalize_and_validate_rewrite(
                        before_text,
                        str(output.get("humanized_text") or ""),
                        min_ratio=0.8,
                        max_ratio=1.2,
                        minimum_chars=50,
                    )
                    output["humanized_text"] = normalized_text
                    output["quality_shape"] = shape
                    paragraphs = _chapter_paragraphs_from_text(normalized_text)
                    _assert_story_revision_quality(
                        task_type=task_type,
                        before_text=before_text,
                        after_paragraphs=paragraphs,
                        min_ratio=0.8,
                    )
                    _assert_min_chapter_length(task_type, "\n".join(paragraphs))
                except ValueError as exc:
                    db.close()
                    raise OutputValidationError(f"final_humanize {exc}") from exc
                except OutputValidationError:
                    db.close()
                    raise
                from app.services.text_metrics import count_content_chars
                db.execute(
                    "UPDATE contents SET body = %s, meta = meta || %s, updated_at = now() WHERE id = %s",
                    (
                        encode(_chapter_doc_from_paragraphs(paragraphs)),
                        encode({
                            "humanized": True,
                            "humanized_at": datetime.now(timezone.utc).isoformat(),
                            "word_count": count_content_chars("\n".join(paragraphs)),
                        }),
                        cid,
                    ),
                )
                context["chapter_text"] = "\n".join(paragraphs)

    # ── Common persist ──────────────────────────────────────────────────
    db.execute(
        "UPDATE run_nodes SET status = 'succeeded', output = %s, finished_at = now() WHERE run_id = %s AND node_key = %s",
        (encode(output), run_id, node_key),
    )
    db.execute(
        "UPDATE workflow_runs SET context = %s, updated_at = now() WHERE id = %s",
        (encode(context), run_id),
    )
    db.commit()
    db.close()

    # Reindex knowledge items
    if knowledge_ids_to_reindex:
        from app.services.knowledge_hub import rebuild_item_embeddings
        for knowledge_id in knowledge_ids_to_reindex:
            try:
                rebuild_item_embeddings(knowledge_id)
            except Exception as exc:
                from app.core.alerts import send_alert
                send_alert(f"知识向量重建失败 {knowledge_id}: {exc}", "warning")
def _persist_chapter_draft(db, run, node_key: str, output: dict, context: dict,
                           novel_id: str, project_id: str, run_id: str,
                           knowledge_ids_to_reindex: list[str]) -> None:
    """Persist chapter draft to contents table with idempotency key."""
    from app.services.text_metrics import count_content_chars
    chapter = output.get("chapter", {})
    body = {"type": "doc", "content": [{"type": "paragraph", "text": t} for t in chapter.get("body", [])]}
    chapter_text = "\n".join(t if isinstance(t, str) else t.get("text", "") for t in chapter.get("body", []))
    if _looks_like_non_narrative_text(chapter_text):
        db.close()
        raise OutputValidationError("write_chapter_draft returned non-narrative instructional text")
    # 字数下限 + 7 维评分硬门禁由末尾的 _review_and_finalize_chapter 统一处理（先落库，再进入重写循环）。
    chapter_seq = int(context.get("_chapter_seq", 1))
    chapter_meta = {"seq": chapter_seq, "word_count": count_content_chars(chapter_text)}
    cid = new_id()
    generation_key = _chapter_idempotency_key(novel_id, chapter_seq)
    stored = db.execute(
        """INSERT INTO contents
           (id, project_id, parent_id, type, title, body, meta, status, scope_status, generation_key, seq)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (project_id, generation_key) WHERE generation_key IS NOT NULL AND is_deleted=FALSE
           DO UPDATE SET title=EXCLUDED.title, body=EXCLUDED.body, meta=EXCLUDED.meta,
                         scope_status=EXCLUDED.scope_status, seq=EXCLUDED.seq, updated_at=now()
           RETURNING id""",
        (cid, project_id, novel_id, "chapter", chapter.get("title", f"第一章"),
         encode(body), encode(chapter_meta), "pending_review", "canonical", generation_key, chapter_seq),
    ).fetchone()
    cid = stored["id"] if stored else cid
    context["chapter_id"] = cid
    context["chapter_text"] = chapter_text
    db.execute(
        """INSERT INTO versions (id, entity_type, entity_id, label, snapshot, client_mutation_id)
           VALUES (%s,'content',%s,'ai_generate',%s,%s)
           ON CONFLICT (client_mutation_id) WHERE client_mutation_id IS NOT NULL DO NOTHING""",
        (new_id(), cid, encode({"title": chapter.get("title", ""), "body": body, "meta": chapter_meta}),
         f"run:{run_id}:node:{node_key}:version"),
    )
    # Flush the draft so the review gate (separate connection) can UPDATE the
    # same row without blocking on an uncommitted-insert row lock.
    db.commit()
    # 硬门禁（与续章/批量一致）：首章同样必须 ≥2000 字且 7 维评分 ≥80，不达标自动重写
    # （最多 3 次）；用尽仍不达标则标记 needs_rewrite 交付，不硬失败整次建书。
    continuity = _continuity_report(novel_id, chapter_seq)
    review = _review_and_finalize_chapter(
        cid, novel_id, project_id, chapter_seq, generation_key,
        chapter.get("title", f"第一章"), list(chapter.get("body", [])), continuity,
    )
    context["chapter_text"] = "\n".join(review["body"])
    # Auto-summarize final (possibly rewritten) chapter body
    _summarize_and_store(db, cid, review["body"])
def _persist_chapter_polish(db, node_key: str, output: dict, context: dict, run_id: str) -> None:
    """Apply polished text to the chapter in contents."""
    cid = context.get("chapter_id", "")
    if not cid:
        return
    polished = output.get("polished", output.get("chapter", output))
    if isinstance(polished, dict) and polished.get("body"):
        current = db.execute("SELECT body FROM contents WHERE id = %s", (cid,)).fetchone()
        before_text = extract_body_text(current["body"] if current else "")
        polished_paragraphs = [
            str(t if isinstance(t, str) else t.get("text", "")).strip()
            for t in polished.get("body", [])
            if str(t if isinstance(t, str) else t.get("text", "")).strip()
        ]
        try:
            _assert_story_revision_quality(
                task_type="write_polish",
                before_text=before_text or context.get("chapter_text", ""),
                after_paragraphs=polished_paragraphs,
                min_ratio=0.75,
            )
            _assert_min_chapter_length("write_polish", "\n".join(polished_paragraphs))
        except OutputValidationError:
            db.close()
            raise
        polished_body = _chapter_doc_from_paragraphs(polished_paragraphs)
        db.execute("UPDATE contents SET body = %s, updated_at = now() WHERE id = %s", (encode(polished_body), cid))
        context["chapter_text"] = "\n".join(polished_paragraphs)
def _persist_fact_reconcile(db, node_key: str, output: dict, context: dict,
                            novel_id: str, run_id: str) -> None:
    """Reconcile chapter facts against entity states."""
    cid = context.get("chapter_id", "")
    if not cid:
        return
    # Record reconciliation result in chapter meta
    reconc_result = output.get("reconciliation", output)
    prev_meta = db.execute("SELECT meta->'fact_reconcile' AS prev FROM contents WHERE id = %s", (cid,)).fetchone()
    db.execute(
        "UPDATE contents SET meta = meta || %s, updated_at = now() WHERE id = %s",
        (encode({"fact_reconcile": reconc_result}), cid),
    )
    # show-me-the-story fact chain: every reconcile is a reversible fact transaction
    from app.services.fusion_deep_workflow import create_fact_transaction
    create_fact_transaction(
        "fact_reconcile", cid,
        previous_value=(prev_meta or {}).get("prev") or {},
        new_value=reconc_result if isinstance(reconc_result, dict) else {"value": reconc_result},
    )
    # Run cross-reference reconciliation
    chapter_text = context.get("chapter_text", "")
    if chapter_text:
        reconc = _write_after_reconcile(novel_id, cid, chapter_text)
        db.execute(
            "UPDATE contents SET meta = meta || %s WHERE id = %s",
            (encode({"_auto_reconcile": reconc}), cid),
        )
def _summarize_and_store(db, chapter_id: str, body: list) -> None:
    """M2: Generate and store chapter summary after generation."""
    try:
        from app.services.summarizer import summarize_chapter
        texts = []
        for p in body:
            if isinstance(p, dict):
                texts.append(p.get("text", ""))
            elif isinstance(p, str):
                texts.append(p)
        text = "\n".join(texts)
        if not text.strip():
            return
        result = summarize_chapter(chapter_id, text)
        summary = result.get("summary", "")
        if summary:
            db.execute(
                "UPDATE contents SET meta = meta || %s, updated_at = now() WHERE id = %s",
                (encode({"chapter_summary": summary}), chapter_id),
            )
    except Exception:
        pass  # Non-critical
# ══════════════════════════════════════════════════════════════════════════
# Chapter generation (M2 — unchanged from original)
# ══════════════════════════════════════════════════════════════════════════

def _batch_slot_not_runnable(batch_id: str, ordinal: int) -> dict[str, Any] | None:
    """Return a terminal batch result before acquiring a Provider slot.

    Celery can redeliver a message that was already queued when a batch was
    cancelled or when a worker was restarted.  Checking only after generation
    wastes API calls and can resurrect a cancelled slot.  This is intentionally
    a fail-closed read: if the batch row cannot be read, the caller raises
    before any Provider request is made.
    """
    if not batch_id:
        return None
    db = connect()
    try:
        batch = db.execute(
            "SELECT status, cancel_requested FROM generation_batches WHERE id=%s",
            (batch_id,),
        ).fetchone()
    finally:
        db.close()
    if not batch:
        return {
            "status": "failed",
            "batch_id": batch_id,
            "ordinal": ordinal,
            "reason": "batch not found; Provider call skipped",
        }
    status = str(batch.get("status") or "failed")
    if bool(batch.get("cancel_requested")) or status not in {"pending", "running"}:
        # Release a stale claim, but preserve the terminal status and history.
        _clear_batch_current_ordinal(batch_id, ordinal)
        return {
            "status": "cancelled" if bool(batch.get("cancel_requested")) else status,
            "batch_id": batch_id,
            "ordinal": ordinal,
            "reason": "batch is not active; Provider call skipped",
        }
    return None

def _run_canonical_v7_task(
    task: Any,
    novel_id: str,
    project_id: str,
    *,
    api_key: str = "",
    api_url: str = "",
    model: str = "",
    batch_id: str = "",
    batch_ordinal: int = 0,
    chapter_number: int | None = None,
    api_key_ref: str = "",
    prompt: str | None = None,
    outline: str | None = None,
) -> dict[str, Any]:
    """Run the single canonical V7 prose path from a Celery worker."""
    from app.v7.runtime import generate_v7_chapter_sync
    from .lock import acquire_lock, release_lock

    # Malformed legacy queue messages can still contain placeholder IDs from
    # pre-V7 tests/imports.  Keep their old lock/release semantics so a bad
    # message cannot consume a Provider slot or obscure the original error.
    # Real product requests have UUID-backed novel rows and always continue
    # through the V7 path below; this is not a second production generator.
    try:
        uuid.UUID(str(novel_id))
    except (ValueError, TypeError, AttributeError):
        legacy_lock_key = f"lock:novel:{novel_id}:gen_chapter"
        if not acquire_lock(legacy_lock_key):
            raise task.retry(exc=RuntimeError("chapter is already running"), countdown=3)
        try:
            if batch_id or batch_ordinal:
                return _generate_next_chapter_unlocked(
                    novel_id,
                    project_id,
                    batch_id=batch_id,
                    batch_ordinal=batch_ordinal,
                )
            return _generate_next_chapter_unlocked(novel_id, project_id)
        finally:
            release_lock(legacy_lock_key)

    # A valid UUID is not enough: direct Celery messages must still resolve to
    # a live novel in the requested project before they can acquire an AI slot
    # or enter the V7 runtime.  The HTTP endpoints already perform this check;
    # repeating it here closes the queue/direct-task bypass.
    from app.services.chapter_scope import ChapterScopeError, validate_novel_parent
    scope_db = connect()
    try:
        validate_novel_parent(db=scope_db, project_id=project_id, novel_id=novel_id)
    except ChapterScopeError as exc:
        if batch_id:
            _mark_batch_failed(batch_id, exc)
        raise
    finally:
        scope_db.close()

    api_key = resolve_byok_key(api_key_ref, api_key)
    blocked = _batch_slot_not_runnable(batch_id, batch_ordinal)
    if blocked is not None:
        return blocked
    acquired = False
    for _ in range(6):
        if acquire_ai_slot(timeout=5):
            acquired = True
            break
        time.sleep(1)
    if not acquired:
        raise task.retry(exc=RuntimeError("global AI concurrency limit reached"), countdown=3)

    lock_key = f"lock:novel:{novel_id}:canonical_v7_chapter"
    if not acquire_lock(lock_key):
        release_ai_slot()
        raise task.retry(exc=RuntimeError("canonical V7 chapter is already running"), countdown=3)

    try:
        result = generate_v7_chapter_sync(
            novel_id,
            project_id,
            chapter_number=chapter_number,
            user_id=None,
            api_key=api_key,
            api_url=api_url,
            model=model,
            prompt=prompt,
            outline=outline,
            batch_id=batch_id,
            batch_ordinal=batch_ordinal,
        )
        if batch_id:
            # A rejected V7 draft is still a durable batch slot.  It is
            # persisted as ``needs_rewrite`` and the batch finalizer will mark
            # the batch ``needs_review``; treating this truthful quality result
            # as a transport/task failure used to show a generic "batch
            # failed" and strand the generated text outside the library.
            if result.get("status") == "pending_approval":
                if result.get("retryable_planning_failure"):
                    retry_count = int(
                        getattr(getattr(task, "request", None), "retries", 0) or 0
                    )
                    # Keep the slot claim in place while Celery retries the
                    # same task.  The task is bounded by max_retries=4; after
                    # exhaustion the generic exception path marks the batch
                    # failed truthfully for manual resume.
                    raise task.retry(
                        exc=RuntimeError(
                            "V7 planning/provider failure is transient; retrying "
                            f"ordered batch slot {batch_ordinal}"
                        ),
                        countdown=min(60, 10 * (retry_count + 1)),
                    )
                # No chapter row exists for a planning-only approval block.
                # Do not mislabel it as a prose quality failure: the caller
                # needs the actual permission reason to decide whether to
                # approve/resume or fix the story brief.
                _mark_batch_failed(
                    batch_id,
                    RuntimeError(
                        "V7 planning approval required: "
                        f"{result.get('blocked_reason') or 'chapter plan is awaiting approval'}"
                    ),
                )
            elif result.get("v6_content_id") and result.get("status") in {
                "completed",
                "needs_review",
                "needs_rewrite",
            }:
                _clear_batch_current_ordinal(batch_id, batch_ordinal)
                _reconcile_batch_progress(batch_id)
                _dispatch_next_batch_slot(
                    batch_id,
                    api_url=api_url,
                    model=model,
                    api_key_ref=api_key_ref,
                )
            else:
                _mark_batch_failed(
                    batch_id,
                    RuntimeError(
                        "canonical V7 returned no durable chapter result"
                    ),
                )
        return result
    except Retry:
        # Celery owns the retry lifecycle.  Do not mark the batch failed while
        # the same claimed ordinal is waiting for its bounded retry.
        raise
    except Exception as exc:
        if batch_id:
            _mark_batch_failed(batch_id, exc)
        raise
    finally:
        release_lock(lock_key)
        release_ai_slot()

# A canonical chapter can make five bounded Provider calls (plot, scene/text,
# optional humanize, review, memory).  The previous worker-level soft limit
# could terminate a slow but healthy chapter around eight minutes, after which
# the batch was reported failed even though no quality decision had failed.
# Keep the transport bounded, but give the complete V7 contract enough room to
# finish and persist its ledger/state atomically.
@celery_app.task(bind=True, max_retries=4, soft_time_limit=1200, time_limit=1500)
@_isolated_request_context
def gen_next_chapter_task(self, novel_id: str, project_id: str,
                           api_key: str = "", api_url: str = "", model: str = "",
                           batch_id: str = "", batch_ordinal: int = 0,
                           api_key_ref: str = "", canonical: bool = True,
                           chapter_number: int | None = None) -> dict:
    """M2: Generate the next chapter using context assembler (with distributed lock).

    P2 hardening:
      * The BYOK key arrives as a short-lived ``api_key_ref`` (never plaintext in
        the broker) and is resolved here via ``resolve_byok_key``.
      * A global AI semaphore (P2-T5 / Q11) caps concurrent provider calls so a
        50-chapter batch / auto-serial burst cannot overwhelm DeepSeek.
      * If the per-novel lock is held (another slot of the same novel is still
        generating), a *batch* slot is re-queued (P2-T4 / Q10) instead of
        crashing the whole batch.
    """
    # ``canonical`` remains an API compatibility argument for queued messages
    # created before the V7 cutover.  It is intentionally ignored: every new
    # chapter request now enters the one V7 runtime, including callers that did
    # not know about the flag.
    return _run_canonical_v7_task(
        self,
        novel_id,
        project_id,
        api_key=api_key,
        api_url=api_url,
        model=model,
        batch_id=batch_id,
        batch_ordinal=batch_ordinal,
        chapter_number=chapter_number,
        api_key_ref=api_key_ref,
    )


def _batch_generation_key(batch_id: str, ordinal: int) -> str:
    return f"batch:{batch_id}:slot:{ordinal}:v1"
def _generate_next_chapter_unlocked(novel_id: str, project_id: str,
                                    batch_id: str = "", batch_ordinal: int = 0) -> dict:
    """Generate one chapter. The caller owns the per-novel distributed lock."""
    from app.services.assembler import ContextAssembler
    from app.services.entity_tracker import extract_and_store
    db = connect()
    slot_key = _batch_generation_key(batch_id, batch_ordinal) if batch_id and batch_ordinal else ""
    if slot_key:
        existing = db.execute("""SELECT * FROM contents WHERE project_id=%s AND parent_id=%s
                                  AND generation_key=%s AND type='chapter' AND is_deleted=FALSE""",
                              (project_id, novel_id, slot_key)).fetchone()
        if existing:
            db.close()
            meta = existing["meta"] if isinstance(existing.get("meta"), dict) else {}
            continuity = meta.get("continuity")
            if not isinstance(continuity, dict):
                continuity = _continuity_report(novel_id, int(meta.get("seq") or 0))
                repair_db = connect()
                repair_db.execute("UPDATE contents SET meta=meta || %s,updated_at=now() WHERE id=%s",
                                  (encode({"continuity": continuity}), existing["id"]))
                repair_db.commit(); repair_db.close()
            if existing["status"] in {"reviewed", "needs_rewrite"}:
                return {"chapter_id": existing["id"], "title": existing["title"], "seq": meta.get("seq"),
                        "continuity": continuity,
                        "accepted": existing["status"] == "reviewed",
                        "review_status": existing["status"], "final_score": meta.get("review_score"),
                        "rewrite_attempts": meta.get("rewrite_attempts", 0), "reused": True}
            from app.services.novel_export import extract_body_text
            paragraphs = [part for part in extract_body_text(existing.get("body", "")).splitlines() if part.strip()]
            review = _review_and_finalize_chapter(
                existing["id"], novel_id, project_id, int(meta.get("seq") or 0), slot_key,
                existing["title"], paragraphs, continuity,
            )
            return {"chapter_id": existing["id"], "title": review["title"], "seq": meta.get("seq"),
                    "continuity": meta.get("continuity", {"status": "unchecked"}),
                    "accepted": review["accepted"], "review_status": review["review_status"],
                    "final_score": review["final_score"], "rewrite_attempts": review["rewrite_attempts"],
                    "reused": True}
    # Find last chapter seq
    last = db.execute(
        "SELECT COALESCE(MAX(seq), MAX((meta->>'seq')::int), 0) as seq FROM contents WHERE parent_id = %s AND type='chapter'",
        (novel_id,),
    ).fetchone()
    next_seq = (last["seq"] if last else 0) + 1
    db.close()

    # Build context
    assembler = ContextAssembler(novel_id)
    context = assembler.build()

    # M2: Check for due foreshadows + inject into context
    from app.services.narrative_engine import check_foreshadow_due, inject_foreshadow_context
    due_foreshadows = check_foreshadow_due(novel_id, next_seq)
    if due_foreshadows:
        inject_str = inject_foreshadow_context(due_foreshadows)
        context = inject_str + "\n\n" + context

    opening_plan = _opening_plan_for_novel(novel_id, next_seq)
    opening_contract = opening_prompt_block(opening_plan)

    # Generate — output is schema-validated by the gateway; the stable mutation id
    # lets a retry replay the succeeded ai_call instead of paying for a new one.
    generation_key = slot_key or f"novel:{novel_id}:chapter:{next_seq}:v1"
    output = complete(
        run_id=None, node_key=None, project_id=project_id,
        task_type="gen_next_chapter", prompt_name="narrative.gen_next_chapter",
        variables={
            "context": context,
            "context_length": len(context),
            "assembled_layers": list(assembler.layers_built.keys()),
            "opening_contract": opening_contract,
        },
        client_mutation_id=generation_key,
    )

    chapter = output["chapter"]
    body = {"type": "doc", "content": [{"type": "paragraph", "text": t} for t in chapter["body"]]}
    cid = new_id()

    db = connect()
    from app.services.text_metrics import count_content_chars
    text = "\n".join(t if isinstance(t, str) else t.get("text", "") for t in chapter["body"])
    # 字数硬门禁交由下方 _review_and_finalize_chapter 的统一重写循环处理：
    # 过短会在评审循环里触发重写，用尽配额则标记 needs_rewrite 交付，不硬失败整次任务。
    chapter_meta = {"seq": next_seq, "word_count": count_content_chars(text)}
    if batch_id and batch_ordinal:
        chapter_meta.update({"batch_id": batch_id, "batch_ordinal": batch_ordinal,
                             "ordinal": batch_ordinal, "quality_status": "draft_pending_review"})
    stored = db.execute(
        """INSERT INTO contents
           (id, project_id, parent_id, type, title, body, meta, status, scope_status, generation_key, seq, batch_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (project_id, generation_key) WHERE generation_key IS NOT NULL AND is_deleted=FALSE
           DO UPDATE SET title=EXCLUDED.title, body=EXCLUDED.body, meta=EXCLUDED.meta, seq=EXCLUDED.seq,
                         scope_status=EXCLUDED.scope_status, batch_id=EXCLUDED.batch_id, updated_at=now()
           RETURNING id""",
        (cid, project_id, novel_id, "chapter", chapter.get("title", f"第{next_seq}章"),
         encode(body), encode(chapter_meta), "pending_review", "canonical", generation_key,
         next_seq, batch_id or None),
    ).fetchone()
    cid = stored["id"] if stored else cid
    db.execute(
        """INSERT INTO versions (id, entity_type, entity_id, label, snapshot, client_mutation_id)
           VALUES (%s, 'content', %s, 'ai_generate', %s, %s)
           ON CONFLICT (client_mutation_id) WHERE client_mutation_id IS NOT NULL DO NOTHING""",
        (new_id(), cid, encode({"title": chapter.get("title", ""), "body": body, "meta": chapter_meta}),
         generation_key),
    )
    db.commit()
    db.close()

    # Enrichments must never prevent the persisted draft from reaching the
    # continuity/review gates. Failures are recorded for later reconciliation.
    from app.services.foreshadowing import extract_and_store_foreshadowing
    from app.services.timeline import extract_timeline, update_arcs
    enrichment_errors = []
    for label, action in (
        ("entities", lambda: extract_and_store(cid, novel_id, text)),
        ("foreshadowing", lambda: extract_and_store_foreshadowing(cid, next_seq, text)),
        ("timeline", lambda: extract_timeline(cid, text)),
        ("arcs", lambda: update_arcs(novel_id, text)),
    ):
        try:
            action()
        except Exception as exc:
            enrichment_errors.append({"stage": label, "error": str(exc)[:300]})

    # Continuity check + risk report (DB comparison, no extra AI spend); a check
    # failure is recorded as unchecked, never silently dropped.
    continuity = _continuity_report(novel_id, next_seq)

    # Persist continuity evidence before the review gate so the reviewer can see it.
    db = connect()
    db.execute(
        "UPDATE contents SET meta = meta || %s, updated_at = now() WHERE id = %s",
        (encode({"continuity": continuity, "enrichment_errors": enrichment_errors}), cid),
    )
    db.commit()
    db.close()
    review = _review_and_finalize_chapter(
        cid, novel_id, project_id, next_seq, generation_key, chapter.get("title", ""),
        list(chapter["body"]), continuity,
    )
    db = connect()
    _summarize_and_store(db, cid, review["body"])
    db.commit(); db.close()
    return {"chapter_id": cid, "title": chapter.get("title", ""), "seq": next_seq,
            "continuity": continuity, "accepted": review["accepted"],
            "review_status": review["review_status"], "final_score": review["final_score"],
            "rewrite_attempts": review["rewrite_attempts"]}
def _try_canonical_v7_review(
    chapter_id: str,
    novel_id: str,
    project_id: str,
    chapter_text: str,
) -> dict[str, Any] | None:
    """Review a real persisted chapter through V7.

    The old gate is still exercised by historical unit doubles and by legacy
    rows that cannot be mapped to UUID-backed V7 state.  A real production row
    is never sent to the old ``review_7dim`` scoring contract: V7 owns the
    model call, 33-item evidence, continuity and provenance.
    """
    try:
        uuid.UUID(str(chapter_id))
        uuid.UUID(str(novel_id))
    except (ValueError, TypeError, AttributeError):
        return None

    db = connect()
    try:
        chapter = db.execute(
            "SELECT * FROM contents WHERE id=%s AND type='chapter' AND is_deleted=FALSE",
            (chapter_id,),
        ).fetchone()
    finally:
        db.close()
    if not chapter:
        return None
    if str(chapter.get("project_id") or "") != str(project_id):
        raise OutputValidationError("canonical V7 review target does not belong to project")

    from app.v7.review_service import review_chapter_v7_sync

    review = review_chapter_v7_sync(
        chapter,
        chapter_text,
        api_key=str(_request_api_key.get() or ""),
        api_url=str(_request_api_base_url.get() or ""),
        model=str(_request_model.get() or ""),
        use_cache=False,
    )
    return {
        **review,
        # Compatibility aliases for old persistence/readers.  They are copied
        # from the canonical V7 result and never independently scored.
        "score": review.get("overall_score"),
        "dimensions": review.get("dimension_scores") or {},
        "canonical_engine": "v7",
    }


def _review_and_finalize_chapter(chapter_id: str, novel_id: str, project_id: str, chapter_seq: int,
                                 generation_key: str, title: str, paragraphs: list[str],
                                 continuity: dict, threshold: float = REVIEW_SCORE_THRESHOLD,
                                 max_rewrites: int = MAX_CHAPTER_REWRITES) -> dict:
    """Hard AI gate for every generated chapter (code-level, not prompt suggestion).

    Enforces two gates:
      1) length >= MIN_CHAPTER_CHARS (non-whitespace Chinese chars)
      2) review_7dim score >= threshold
    If either fails, the chapter is rewritten (up to max_rewrites times).
    Only a chapter that passes BOTH is marked 'reviewed'. If the rewrite budget
    is exhausted, the chapter is persisted but marked 'needs_rewrite' — delivered
    and clearly flagged for human rewrite, never silently delivered as done.
    """
    from app.services.text_metrics import count_content_chars
    current_title = title
    current_body = list(paragraphs)
    last_score = 0.0
    last_chars = 0
    opening_plan = _opening_plan_for_novel(novel_id, chapter_seq)
    opening_contract = opening_prompt_block(opening_plan)
    for attempt in range(max_rewrites + 1):
        current_text = "\n".join(current_body)
        last_chars = count_content_chars(current_text)
        opening_quality = inspect_opening(
            current_text,
            requested_mode=opening_plan.get("mode"),
            chapter_number=chapter_seq,
            recent_modes=opening_plan.get("forbidden_recent_modes") or [],
        )
        length_ok = last_chars >= MIN_CHAPTER_CHARS
        length_issue = "" if length_ok else f"字数不足：{last_chars}/{MIN_CHAPTER_CHARS} 字"
        review = _try_canonical_v7_review(
            chapter_id,
            novel_id,
            project_id,
            current_text,
        )
        canonical_v7 = review is not None
        review_continuity = (review.get("continuity") if canonical_v7 else None) or continuity
        if review is None:
            # Compatibility-only path for pre-V7 test doubles/rows.  New
            # production chapters are UUID-backed and take the branch above.
            review = complete(
                run_id=None, node_key=None, project_id=project_id,
                task_type="review_7dim", prompt_name="bootstrap.review_7dim",
                variables={"chapter_id": chapter_id, "chapter_seq": chapter_seq, "body": current_text,
                           "continuity": continuity, "threshold": threshold},
                client_mutation_id=f"{generation_key}:review:{attempt}:v1",
            )
        score = float(review.get("score", review.get("overall_score", 0)) or 0)
        last_score = score
        issues = list(review.get("issues", []))
        duplicate_paragraphs = duplicate_paragraph_stats(current_text)
        duplicate_ratio = float(duplicate_paragraphs.get("duplicate_ratio") or 0.0)
        if duplicate_ratio >= 0.01:
            issues.append({
                "dimension": "writing_quality",
                "type": "duplicate_paragraph",
                "severity": "high",
                "description": (
                    "正文存在完整段落重复，重复字符占比 "
                    f"{duplicate_ratio:.1%}；必须删除重复副本，不能只标记完成"
                ),
                "suggestion": "保留一份原始段落，并重新检查段落边界和上下文承接",
                "evidence": duplicate_paragraphs.get("examples") or [],
            })
        if length_issue:
            score = min(score, threshold - 1)
            issues.append(length_issue)
        if not opening_quality["passed"]:
            for flag in opening_quality.get("flags") or []:
                issues.append({
                    "dimension": "opening_quality",
                    "severity": "high",
                    "description": flag.get("message") or "开场质量门禁未通过",
                    "suggestion": "只重写前300-500字的起笔，保留人物、事件、时间线和因果，并执行指定开场类型",
                    "excerpt": flag.get("evidence") or "",
                })
        # V3 §11.1 reader experience: advisory only — surfaces weak dims as
        # issues + durable meta, never changes score / never blocks the gate.
        from app.services.reader_experience import (
            reader_experience_issues, summarize_reader_experience)
        rx_summary = summarize_reader_experience(review.get("reader_experience"))
        issues.extend(reader_experience_issues(rx_summary))
        from app.services.quality_risks import build_quality_repair_contract, repair_feedback
        quality_contract = build_quality_repair_contract(
            {
                "overall_score": score,
                "dimensions": review.get("dimensions") or review.get("dimension_scores") or {},
                "issues": issues,
            },
            dimension_minimums={
                "continuity": max(85.0, threshold),
                "plot_logic": max(85.0, threshold),
                "pacing": max(85.0, threshold),
                "writing_quality": max(85.0, threshold),
            },
            continuity=review_continuity,
        )
        # Keep blocking evidence visible to editors and the audit trail.  The
        # label is not a fake pass/fail replacement; it is the exact reason a
        # targeted rewrite is required before the chapter can be reviewed.
        for risk in quality_contract["blocking_risks"]:
            evidence = f"质量整改：{risk['label']}：{risk.get('description') or risk.get('text')}"
            if evidence not in issues:
                issues.append(evidence)
        review_key = f"{generation_key}:review-record:{attempt}:v1"
        db = connect()
        db.execute(
            """INSERT INTO reviews (id,content_id,score,dimensions,issues,generation_key)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (content_id,generation_key) WHERE generation_key IS NOT NULL
               DO UPDATE SET score=EXCLUDED.score,dimensions=EXCLUDED.dimensions,issues=EXCLUDED.issues""",
            (new_id(), chapter_id, score,
             encode(review.get("dimensions") or review.get("dimension_scores") or {}),
             encode(issues), review_key),
        )
        passed = (score >= threshold) and length_ok and quality_contract["passed"]
        if canonical_v7:
            from app.v7.quality.review_gate import can_mark_reviewed
            passed = passed and can_mark_reviewed({
                **review,
                "continuity": review_continuity,
                "final_continuity_audit": review.get("final_continuity_audit") or {"continuity": review_continuity},
            })
        review_meta = {
            "review_score": score,
            "review_issues": issues,
            "review_attempts": attempt + 1,
            "reader_experience": rx_summary,
            "quality_repair_contract": quality_contract,
            "quality_risks": quality_contract["risks"],
            "quality_status": "ai_review_passed" if passed else "needs_rewrite",
            "quality_reason": f"score={score:.0f}/{threshold:.0f}, chars={last_chars}",
            "opening_quality": opening_quality,
        }
        if canonical_v7:
            review_meta.update({
                "canonical_engine": "v7",
                "canonical_review": review,
                "audit_report": review.get("audit_report") or {},
                "continuity": review.get("continuity") or continuity or {},
                "final_continuity_audit": review.get("final_continuity_audit") or {
                    "continuity": review.get("continuity") or continuity or {},
                },
                "review_provenance": review.get("provenance") or {},
                "provenance": review.get("provenance") or {},
            })
        if passed:
            db.execute("""UPDATE contents SET status='reviewed',meta=meta || %s,updated_at=now() WHERE id=%s""",
                       (encode(review_meta), chapter_id))
            db.commit(); db.close()
            return {"accepted": True, "review_status": "reviewed", "final_score": score,
                    "rewrite_attempts": attempt, "title": current_title, "body": current_body,
                    "review": review, "canonical_engine": "v7" if canonical_v7 else "v6_compat",
                    "quality_reason": f"score={score:.0f}/{threshold:.0f}, chars={last_chars}"}
        if canonical_v7:
            # A real V7 chapter must not fall back to the retired V6 prose
            # rewrite call.  The V7 Director is the only generation owner;
            # this compatibility gate records the truthful result and lets the
            # normal V7/manual repair flow handle the draft.
            reason = f"score={score:.0f}/{threshold:.0f}"
            if not length_ok:
                reason += f", chars={last_chars}/{MIN_CHAPTER_CHARS}"
            if quality_contract["blocking_categories"]:
                reason += ", blocking=" + ",".join(quality_contract["blocking_categories"])
            review_meta["quality_reason"] = reason
            db.execute(
                "UPDATE contents SET status='needs_rewrite', meta=meta || %s, updated_at=now() WHERE id=%s",
                (encode(review_meta), chapter_id),
            )
            db.commit(); db.close()
            return {
                "accepted": False,
                "review_status": "needs_rewrite",
                "final_score": score,
                "rewrite_attempts": attempt,
                "title": current_title,
                "body": current_body,
                "review": review,
                "canonical_engine": "v7",
                "quality_reason": reason,
            }
        if attempt == max_rewrites:
            reason = f"score={score:.0f}/{threshold:.0f}"
            if not length_ok:
                reason += f", chars={last_chars}/{MIN_CHAPTER_CHARS}"
            if quality_contract["blocking_categories"]:
                reason += ", blocking=" + ",".join(quality_contract["blocking_categories"])
            db.execute("""UPDATE contents SET status='needs_rewrite',meta=meta || %s,updated_at=now()
                          WHERE id=%s""",
                       (encode({**review_meta, "quality_reason": reason}), chapter_id))
            db.commit(); db.close()
            return {"accepted": False, "review_status": "needs_rewrite", "final_score": score,
                    "rewrite_attempts": attempt, "title": current_title, "body": current_body,
                    "review": review, "canonical_engine": "v6_compat",
                    "quality_reason": reason}
        # Rewrite and retry
        rewritten = complete(
            run_id=None, node_key=None, project_id=project_id,
            task_type="gen_next_chapter", prompt_name="narrative.gen_next_chapter",
            variables={"rewrite": True, "chapter_seq": chapter_seq, "current_title": current_title,
                       "current_body": current_text,
                       "review_feedback": repair_feedback(quality_contract, issues),
                       "continuity": continuity,
                       "opening_contract": opening_contract},
            client_mutation_id=f"{generation_key}:rewrite:{attempt + 1}:v1",
        )["chapter"]
        current_title = rewritten["title"]
        current_body = list(rewritten["body"])
        rewritten_doc = {"type": "doc", "content": [{"type": "paragraph", "text": text}
                                                    for text in current_body]}
        db.execute("""UPDATE contents SET title=%s,body=%s,meta=meta || %s,status='pending_review',updated_at=now()
                      WHERE id=%s""",
                   (current_title, encode(rewritten_doc),
                    encode({"word_count": count_content_chars("\n".join(current_body)),
                            "rewrite_attempts": attempt + 1,
                            "quality_status": "draft_pending_review"}), chapter_id))
        db.commit(); db.close()
def _continuity_report(novel_id: str, chapter_seq: int) -> dict:
    """Cross-chapter conflicts + overdue foreshadows as a persisted risk report."""
    from datetime import datetime, timezone
    from app.services.narrative_engine import check_foreshadow_due, detect_cross_chapter_conflicts
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        conflicts = detect_cross_chapter_conflicts(novel_id)
        overdue = check_foreshadow_due(novel_id, chapter_seq)
    except Exception as exc:
        return {"status": "unchecked", "error": str(exc), "checked_at": checked_at}
    risks = ([{"type": "conflict", **c} for c in conflicts]
             + [{"type": "foreshadow_due", "content": f.get("content", ""), "foreshadow_id": f.get("id")}
                for f in overdue])
    return {"status": "flagged" if risks else "clean", "risks": risks, "checked_at": checked_at}


@celery_app.task(bind=True, max_retries=1)
@_isolated_request_context
def regenerate_chapter_task(self, chapter_id: str, reason: str = "",
                            api_key: str = "", api_url: str = "", model: str = "",
                            api_key_ref: str = "", canonical: bool = True) -> dict:
    """Regenerate one rejected chapter in place.

    This is the manual-review path: rejecting a chapter must not create the next
    chapter by accident. It rewrites the same content row and leaves it
    ``pending_review`` for another human decision.
    """
    # P2-T3 / Q5: resolve the BYOK key from its ref (legacy plaintext still honoured).
    api_key = resolve_byok_key(api_key_ref, api_key)
    if api_key:
        _request_api_key.set(api_key)
    if api_url:
        _request_api_base_url.set(api_url)
    if model:
        _request_model.set(model)
    from app.services.novel_export import extract_body_text
    from app.services.text_metrics import count_content_chars

    db = connect()
    chapter = db.execute("SELECT * FROM contents WHERE id=%s AND type='chapter' AND is_deleted=FALSE", (chapter_id,)).fetchone()
    if not chapter:
        db.close()
        return {"status": "error", "message": "chapter not found"}
    from app.services.chapter_scope import ChapterScopeError, require_canonical_v7_chapter
    try:
        require_canonical_v7_chapter(db, dict(chapter), operation="worker_regenerate")
    except ChapterScopeError:
        # Do not snapshot, spend quota, or call a Provider for an orphan,
        # cross-project, or still-unresolved historical chapter.
        db.close()
        raise
    novel = db.execute("SELECT * FROM contents WHERE id=%s AND type='novel' AND is_deleted=FALSE", (chapter["parent_id"],)).fetchone()
    if not novel:
        db.close()
        return {"status": "error", "message": "novel not found"}
    chapter_meta = chapter["meta"] if isinstance(chapter.get("meta"), dict) else {}
    seq = int(chapter.get("seq") or chapter_meta.get("seq") or 1)
    current_text = extract_body_text(chapter.get("body", ""))
    project_id = chapter["project_id"]
    novel_id = chapter["parent_id"]
    db.close()
    opening_contract = opening_prompt_block(_opening_plan_for_novel(novel_id, seq))

    # ``canonical`` is retained only for queued-message compatibility.  Manual
    # regeneration is also V7-only now; otherwise an old retry could silently
    # recreate the second prose chain the product has already removed.
    canonical = True
    if canonical:
        # Preserve the rejected draft before the V7 canonical engine replaces
        # the same V6 contents row.  A passed V7 result is then left pending
        # human approval, matching the manual-review contract.
        snapshot_db = connect()
        snapshot_db.execute(
            """
            INSERT INTO versions (id, entity_type, entity_id, label, snapshot, reason)
            VALUES (%s,'content',%s,'before_manual_regenerate_v7',%s,%s)
            """,
            (new_id("ver"), chapter_id,
             encode({"title": chapter["title"], "body": chapter["body"], "meta": chapter_meta}),
             reason[:500]),
        )
        snapshot_db.commit()
        snapshot_db.close()
        try:
            result = _run_canonical_v7_task(
                self,
                novel_id,
                project_id,
                api_key=api_key,
                api_url=api_url,
                model=model,
                chapter_number=seq,
                api_key_ref=api_key_ref,
            )
        except Exception as exc:
            failed_db = connect()
            failed_db.execute(
                "UPDATE contents SET status='needs_rewrite', meta=meta || %s, updated_at=now() WHERE id=%s",
                (encode({"manual_review": {"status": "regenerate_failed", "reason": str(exc)[:500]}}), chapter_id),
            )
            failed_db.commit()
            failed_db.close()
            raise

        # V7 分支：只要生成了内容（有 v6_content_id）就算成功
        # V7 引擎的 _persist_v7_chapter 已经更新了 title、body 和 status
        # 我们只需要更新 manual_rewrite 和 manual_review 的状态
        if result.get("v6_content_id"):
            v7_status = result.get("status", "unknown")
            updated_db = connect()
            updated_db.execute(
                """
                UPDATE contents
                SET meta=meta || %s,
                    updated_at=now()
                WHERE id=%s
                """,
                (encode({
                    "quality_status": (
                        "v7_quality_gate_passed" if v7_status == "completed"
                        else "v7_quality_gate_failed"
                    ),
                    "canonical_engine": "v7",
                    # 兼容 manual_review 场景
                    "manual_review": {
                        "status": "regenerated",
                        "reason": reason,
                        "regenerated_at": datetime.now(timezone.utc).isoformat(),
                        "v7_status": v7_status,
                    },
                    # 主动重写场景
                    "manual_rewrite": {
                        "status": "completed",
                        "reason": reason,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "v7_status": v7_status,
                        "review_score": result.get("review_score"),
                    },
                }), chapter_id),
            )
            updated_db.commit()
            updated_db.close()
            return {
                "status": "completed" if v7_status == "completed" else "needs_review",
                "chapter_id": chapter_id,
                "title": result.get("title") or chapter["title"],
                "seq": seq,
                "canonical_engine": "v7",
                "v7_status": v7_status,
            }

        # V7 没有生成内容，重写失败
        failed_reason = result.get("blocked_reason") or "V7 引擎未能生成章节内容"
        failed_db = connect()
        failed_db.execute(
            "UPDATE contents SET status='needs_rewrite', meta=meta || %s, updated_at=now() WHERE id=%s",
            (encode({
                # 兼容 manual_review 场景
                "manual_review": {
                    "status": "regenerate_failed",
                    "reason": failed_reason,
                },
                # 主动重写场景
                "manual_rewrite": {
                    "status": "failed",
                    "reason": failed_reason,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "v7_status": result.get("status", "unknown"),
                },
                "canonical_engine": "v7",
            }), chapter_id),
        )
        failed_db.commit()
        failed_db.close()
        return {
            "status": "needs_rewrite",
            "chapter_id": chapter_id,
            "seq": seq,
            "canonical_engine": "v7",
            "failed_reason": failed_reason,
        }

    output = complete(
        run_id=None,
        node_key=None,
        project_id=project_id,
        task_type="gen_next_chapter",
        prompt_name="narrative.gen_next_chapter",
        variables={
            "rewrite": True,
            "manual_review_rejected": True,
            "chapter_seq": seq,
            "current_title": chapter["title"],
            "current_body": current_text,
            "review_feedback": [reason or "人工审核拒绝：请重写本章，保留章节序号，强化冲突、叙事和可读性。"],
            "context": f"小说：《{novel['title']}》\n章节序号：第{seq}章\n拒绝原因：{reason}",
            "opening_contract": opening_contract,
        },
        client_mutation_id=f"manual-review:{chapter_id}:regenerate:{int(time.time())}:v1",
    )
    rewritten = output["chapter"]
    paragraphs = [str(p).strip() for p in rewritten.get("body", []) if str(p).strip()]
    text = "\n".join(paragraphs)
    if _looks_like_non_narrative_text(text):
        db = connect()
        db.execute(
            "UPDATE contents SET status='needs_rewrite', meta=meta || %s, updated_at=now() WHERE id=%s",
            (encode({"manual_review": {"status": "regenerate_failed", "reason": "non_narrative_output"}}), chapter_id),
        )
        db.commit(); db.close()
        raise OutputValidationError("manual regeneration returned non-narrative text")
    # 字数硬门禁：与自动生成一致，过短则标记「待人工重写」而非硬失败整次任务。
    chars = count_content_chars(text)
    if chars < MIN_CHAPTER_CHARS:
        db = connect()
        db.execute(
            "UPDATE contents SET status='needs_rewrite', meta=meta || %s, updated_at=now() WHERE id=%s",
            (encode({"quality_status": "needs_rewrite",
                     "quality_reason": f"manual_regeneration chars={chars}/{MIN_CHAPTER_CHARS} 字"}), chapter_id),
        )
        db.commit(); db.close()
        return {"status": "needs_rewrite", "chapter_id": chapter_id, "seq": seq,
                "reason": "regenerated chapter too short"}

    db = connect()
    previous_snapshot = {"title": chapter["title"], "body": chapter["body"], "meta": chapter_meta}
    db.execute(
        "INSERT INTO versions (id, entity_type, entity_id, label, snapshot, reason) VALUES (%s,'content',%s,'before_manual_regenerate',%s,%s)",
        (new_id("ver"), chapter_id, encode(previous_snapshot), reason[:500]),
    )
    meta_patch = {
        "word_count": count_content_chars(text),
        "manual_review": {
            "status": "regenerated",
            "reason": reason,
            "regenerated_at": datetime.now(timezone.utc).isoformat(),
        },
        "quality_status": "draft_pending_review",
    }
    db.execute(
        """UPDATE contents
           SET title=%s, body=%s, status='pending_review', meta=meta || %s, updated_at=now()
           WHERE id=%s""",
        (
            rewritten.get("title") or chapter["title"],
            encode(_chapter_doc_from_paragraphs(paragraphs)),
            encode(meta_patch),
            chapter_id,
        ),
    )
    db.commit(); db.close()
    return {"status": "pending_review", "chapter_id": chapter_id, "title": rewritten.get("title") or chapter["title"], "seq": seq}
def _run_batch_slot(batch: dict, ordinal: int, api_key: str = "", api_url: str = "",
                    model: str = "") -> dict:
    """Run or resume one stable batch slot."""
    generation_key = _batch_generation_key(batch["id"], ordinal)
    db = connect()
    existing = db.execute("""SELECT * FROM contents WHERE project_id=%s AND parent_id=%s
                              AND generation_key=%s AND type='chapter' AND is_deleted=FALSE""",
                          (batch["project_id"], batch["novel_id"], generation_key)).fetchone()
    db.close()
    if existing:
        meta = existing["meta"] if isinstance(existing.get("meta"), dict) else {}
        continuity = meta.get("continuity")
        if not isinstance(continuity, dict):
            continuity = _continuity_report(batch["novel_id"], int(meta.get("seq") or 0))
            repair_db = connect()
            repair_db.execute("UPDATE contents SET meta=meta || %s,updated_at=now() WHERE id=%s",
                              (encode({"continuity": continuity}), existing["id"]))
            repair_db.commit(); repair_db.close()
        if existing.get("status") in {"reviewed", "needs_rewrite"}:
            accepted = existing["status"] == "reviewed"
            return {"chapter_id": existing["id"], "accepted": accepted,
                    "review_status": existing["status"], "reused": True}
        from app.services.novel_export import extract_body_text
        paragraphs = [line for line in extract_body_text(existing.get("body", "")).splitlines() if line.strip()]
        review = _review_and_finalize_chapter(
            existing["id"], batch["novel_id"], batch["project_id"], int(meta.get("seq") or 0),
            generation_key, existing["title"], paragraphs,
            continuity,
        )
        return {"chapter_id": existing["id"], **review, "reused": True}
    return gen_next_chapter_task.run(
        batch["novel_id"], batch["project_id"], api_key, api_url, model,
        batch["id"], ordinal,
        canonical=True,
    )
def _recount_batch_progress(db, batch_id: str) -> dict | None:
    """Rebuild counters from distinct persisted slots; never blindly trust increments."""
    cursor = db.execute("""SELECT status,meta FROM contents WHERE type='chapter'
                           AND (batch_id = %s OR meta->>'batch_id' = %s) AND is_deleted=FALSE""", (batch_id, batch_id))
    if not hasattr(cursor, "fetchall"):
        return None
    rows = cursor.fetchall()
    by_ordinal = {}
    for row in rows:
        meta = row.get("meta", {}) if isinstance(row.get("meta"), dict) else {}
        if meta.get("batch_id") and meta.get("batch_id") != batch_id:
            continue
        ordinal = int(meta.get("batch_ordinal") or meta.get("ordinal") or 0)
        if ordinal > 0:
            by_ordinal[ordinal] = meta.get("quality_status") or row.get("status")
    generated = len(by_ordinal)
    accepted = sum(status in {
        "accepted", "reviewed", "v7_quality_gate_passed", "ai_review_passed",
    } for status in by_ordinal.values())
    # A generated draft has not completed AI/manual review yet and must not
    # inflate reviewed/completed counters.
    needs_review = sum(status in {
        "needs_review", "needs_rewrite", "pending_review",
        "v7_quality_gate_failed", "v7_review_validation_failed",
    } for status in by_ordinal.values())
    reviewed = accepted + needs_review
    terminal = reviewed
    db.execute("""UPDATE generation_batches SET generated_count=%s,reviewed_count=%s,
                  accepted_count=%s,needs_review_count=%s,completed_count=%s,updated_at=now() WHERE id=%s""",
               (generated, reviewed, accepted, needs_review, terminal, batch_id))
    return {"generated_count": generated, "reviewed_count": reviewed, "accepted_count": accepted,
            "needs_review_count": needs_review, "completed_count": terminal}
def _increment_batch_progress_legacy(db, batch_id: str, accepted: bool) -> None:
    """Only for non-production lightweight adapters without fetchall support."""
    db.execute("UPDATE generation_batches SET completed_count = completed_count + 1, updated_at=now() WHERE id=%s",
               (batch_id,))
    db.execute("""UPDATE generation_batches SET generated_count=generated_count+1,
                   reviewed_count=reviewed_count+1,accepted_count=accepted_count+%s,
                   needs_review_count=needs_review_count+%s,updated_at=now() WHERE id=%s""",
               (1 if accepted else 0, 0 if accepted else 1, batch_id))


def _maybe_finalize_batch(db, batch_id: str) -> None:
    """Finalize a batch once every requested ordinal has been generated.

    Idempotent: safe to call after every slot. Recounts already updated
    ``completed_count`` / ``needs_review_count`` in the prior
    ``_recount_batch_progress`` call, so we just decide the terminal state.
    Mirrors the previous all-serial finalization semantics.
    """
    batch = db.execute("SELECT * FROM generation_batches WHERE id=%s", (batch_id,)).fetchone()
    if not batch:
        return
    requested = int(batch.get("requested_count", 0) or 0)
    completed = int(batch.get("completed_count", 0) or 0)
    if completed < requested:
        return  # not all slots generated yet
    had_needs_review = int(batch.get("needs_review_count", 0) or 0) > 0
    final_status = "needs_review" if had_needs_review else "succeeded"
    db.execute(
        """UPDATE generation_batches SET status=%s, quality_status=%s, current_ordinal=NULL, updated_at=now()
           WHERE id=%s""",
        (final_status, "needs_review" if had_needs_review else "verified", batch_id),
    )


def _reconcile_batch_progress(batch_id: str) -> dict | None:
    """Recount and persist a batch's terminal progress in one transaction."""
    db = connect()
    try:
        progress = _recount_batch_progress(db, batch_id)
        _maybe_finalize_batch(db, batch_id)
        # The chapter task commits its content before reaching this point, but
        # progress/final-state updates above are a separate transaction. Without
        # this commit the API can poll a permanently ``running`` batch even when
        # its chapter and review rows are already durable.
        db.commit()
        return progress
    finally:
        db.close()


def _clear_batch_current_ordinal(batch_id: str, ordinal: int) -> None:
    """Release the serialized slot claim after its worker persisted a result."""
    db = connect()
    try:
        db.execute(
            """UPDATE generation_batches
               SET current_ordinal=NULL, updated_at=now()
               WHERE id=%s AND current_ordinal=%s""",
            (batch_id, ordinal),
        )
        db.commit()
    finally:
        db.close()


def _next_missing_batch_ordinal(db: Any, batch: dict[str, Any]) -> int | None:
    """Return the first unpersisted slot, preserving chapter order on resume."""
    requested = int(batch.get("requested_count", 0) or 0)
    if requested <= 0:
        return None
    rows = db.execute(
        """SELECT meta FROM contents
           WHERE type='chapter'
             AND (batch_id=%s OR meta->>'batch_id'=%s)
             AND is_deleted=FALSE""",
        (batch["id"], batch["id"]),
    ).fetchall()
    persisted: set[int] = set()
    for row in rows or []:
        meta = row.get("meta") if isinstance(row, dict) else None
        if not isinstance(meta, dict):
            continue
        try:
            ordinal = int(meta.get("batch_ordinal") or meta.get("ordinal") or 0)
        except (TypeError, ValueError):
            ordinal = 0
        if 1 <= ordinal <= requested:
            persisted.add(ordinal)
    for ordinal in range(1, requested + 1):
        if ordinal not in persisted:
            return ordinal
    return None


def _dispatch_next_batch_slot(
    batch_id: str,
    *,
    api_url: str = "",
    model: str = "",
    api_key_ref: str = "",
) -> dict[str, Any]:
    """Claim and dispatch exactly one batch slot.

    The per-novel lock remains a safety net, not the scheduler. Serializing the
    dispatch prevents five Celery messages from racing for one novel, exhausting
    retries, and leaving gaps between chapter 1 and chapter 5.
    """
    db = connect()
    try:
        batch = db.execute(
            "SELECT * FROM generation_batches WHERE id=%s FOR UPDATE",
            (batch_id,),
        ).fetchone()
        if not batch:
            return {"status": "error", "batch_id": batch_id, "message": "batch not found"}
        # ``failed`` is recoverable: the resume endpoint deliberately
        # re-enters this helper after clearing cancel_requested.
        if batch.get("status") in {"succeeded", "needs_review", "cancelled"}:
            return {"status": batch.get("status"), "batch_id": batch_id}
        if batch.get("cancel_requested"):
            db.execute(
                "UPDATE generation_batches SET status='cancelled', current_ordinal=NULL, updated_at=now() WHERE id=%s",
                (batch_id,),
            )
            db.commit()
            return {"status": "cancelled", "batch_id": batch_id}
        current = batch.get("current_ordinal")
        if current is not None:
            return {"status": "running", "batch_id": batch_id, "ordinal": int(current), "dispatched": False}

        ordinal = _next_missing_batch_ordinal(db, batch)
        if ordinal is None:
            _recount_batch_progress(db, batch_id)
            _maybe_finalize_batch(db, batch_id)
            db.commit()
            refreshed = db.execute("SELECT status FROM generation_batches WHERE id=%s", (batch_id,)).fetchone()
            return {"status": (refreshed or {}).get("status", "succeeded"), "batch_id": batch_id}

        db.execute(
            """UPDATE generation_batches
               SET status='running', current_ordinal=%s, error=NULL, updated_at=now()
               WHERE id=%s""",
            (ordinal, batch_id),
        )
        db.commit()
        project_id = batch["project_id"]
        novel_id = batch["novel_id"]
    finally:
        db.close()

    try:
        # The provider key is deliberately passed by reference only.
        gen_next_chapter_task.delay(
            novel_id,
            project_id,
            "",
            api_url,
            model,
            batch_id,
            ordinal,
            api_key_ref=api_key_ref,
            canonical=True,
        )
    except Exception as exc:
        _mark_batch_failed(batch_id, exc)
        raise
    return {"status": "running", "batch_id": batch_id, "ordinal": ordinal, "dispatched": True}


def _mark_batch_failed(batch_id: str, error: Exception) -> None:
    """Make a child-slot exception observable and resumable by the API."""
    db = connect()
    try:
        detail = f"{type(error).__name__}: {str(error)[:1800]}"
        db.execute(
            """UPDATE generation_batches
               SET status='failed', quality_status='failed', current_ordinal=NULL,
                   error=%s, updated_at=now()
               WHERE id=%s AND status NOT IN ('succeeded','needs_review','cancelled')""",
            (detail, batch_id),
        )
        db.commit()
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=1)
def batch_generate_chapters_task(
    self,
    batch_id: str,
    api_key: str = "",
    api_url: str = "",
    model: str = "",
    api_key_ref: str = "",
) -> dict:
    """Start or resume a persisted batch with one ordered slot in flight.

    A batch is intentionally serialized per novel. The provider call is still
    asynchronous at the queue level, but slot N+1 is not dispatched until slot
    N has a durable accepted/review-needed result. This makes chapter order a
    correctness invariant rather than a best-effort consequence of retries.
    """
    db = connect()
    batch = db.execute("SELECT * FROM generation_batches WHERE id = %s", (batch_id,)).fetchone()
    if not batch:
        db.close()
        return {"status": "error", "message": "batch not found"}
    if batch.get("status") in {"succeeded", "needs_review", "cancelled"}:
        db.close()
        return {"status": batch.get("status"), "batch_id": batch_id}
    db.execute("UPDATE generation_batches SET status = 'running', error = NULL, updated_at = now() WHERE id = %s", (batch_id,))
    db.commit()
    db.close()
    try:
        return _dispatch_next_batch_slot(
            batch_id,
            api_url=api_url,
            model=model,
            api_key_ref=api_key_ref,
        )
    except BudgetExceeded as exc:
        _mark_batch_failed(batch_id, exc)
        from app.core.alerts import send_alert
        send_alert(f"批次 {batch_id} 因预算不足失败：{exc}", "warning")
        return {"status": "failed", "batch_id": batch_id, "reason": str(exc)}
    except ProviderError as exc:
        _mark_batch_failed(batch_id, exc)
        from app.core.alerts import send_alert
        send_alert(f"批次 {batch_id} 因 AI provider 失败：{exc}", "warning")
        return {"status": "failed", "batch_id": batch_id, "reason": str(exc)}
    except Exception as exc:
        _mark_batch_failed(batch_id, exc)
        from app.core.alerts import send_alert
        send_alert(f"批次 {batch_id} 失败：{exc}", "error")
        raise
@celery_app.task
def expand_outline_task(novel_id: str, project_id: str) -> dict:
    """M2: Expand volume outline into chapter-level outlines."""
    db = connect()
    meta_row = db.execute("SELECT meta FROM contents WHERE id = %s", (novel_id,)).fetchone()
    db.close()
    if not meta_row:
        return {"error": "novel not found"}
    meta = meta_row["meta"] if isinstance(meta_row["meta"], dict) else {}
    outline = meta.get("outline", [])
    if not outline:
        return {"error": "no outline to expand"}

    chapters = []
    for vol_idx, vol_line in enumerate(outline):
        output = complete(
            run_id=None, node_key=None, project_id=project_id,
            task_type="expand_outline", prompt_name="narrative.expand_outline",
            variables={"volume": vol_line, "volume_num": vol_idx + 1, "chapters_per_volume": 10},
        )
        for ch in output.get("chapters", []):
            chapters.append({"volume": vol_idx + 1, "seq": len(chapters) + 1, "title": ch.get("title", ""), "outline": ch.get("outline", "")})

    db = connect()
    db.execute(
        "UPDATE contents SET meta = meta || %s, updated_at = now() WHERE id = %s",
        (encode({"chapter_outlines": chapters}), novel_id),
    )
    db.commit()
    db.close()
    return {"chapters": len(chapters), "sample": chapters[:3]}
@celery_app.task
def auto_serial_check() -> dict:
    """M2 beat: generate the next chapter for novels with auto-serial enabled.

    P2-T5 / Q11: throttle the burst. On a整点 beat tick the whole fleet could
    fire at once, so we (a) cap dispatches per tick and (b) shard the fleet by
    novel-id hash so only a rotating slice is considered each minute.
    """
    import hashlib
    import os

    max_per_tick = int(os.getenv("AUTO_SERIAL_MAX_PER_TICK", "5"))
    shards = int(os.getenv("AUTO_SERIAL_SHARDS", "4"))
    tick = int(time.time() // 60)  # rotates each minute

    db = connect()
    novels = db.execute(
        """SELECT id, project_id FROM contents
           WHERE type='novel'
             AND (auto_serial IS TRUE OR meta->>'auto_serial' = 'true')
             AND is_deleted = FALSE"""
    ).fetchall()
    db.close()
    results = []
    dispatched = 0
    for novel in novels:
        if dispatched >= max_per_tick:
            break
        if shards > 1:
            digest = hashlib.sha256(novel["id"].encode("utf-8")).hexdigest()
            if int(digest, 16) % shards != tick % shards:
                continue
        try:
            gen_next_chapter_task.delay(
                novel["id"], novel["project_id"], canonical=True
            )
            results.append({"novel_id": novel["id"], "status": "dispatched"})
            dispatched += 1
        except Exception as e:
            results.append({"novel_id": novel["id"], "status": f"error: {e}"})
    return {"checked": len(novels), "results": results}
@celery_app.task
def purge_stale_autosaves() -> dict:
    """C5-05: 7-day retention for routine save versions.

    Only manual_save/offline_save are purged, and the 10 most recent per
    entity are always kept; semantic branches (ai_edit/ai_generate/
    initial_idea/offline_conflict/before_restore) are never touched."""
    db = connect()
    # ``versions.parent_version_id`` is a real self-referencing FK.  A stale
    # autosave may still be the parent of a newer semantic or manual version,
    # so detach those surviving children before deleting the old row instead
    # of letting the periodic cleanup fail with a foreign-key violation.
    db.execute(
        """
        UPDATE versions
        SET parent_version_id = NULL
        WHERE parent_version_id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY entity_id ORDER BY created_at DESC
                ) AS rn
                FROM versions
                WHERE label IN ('manual_save', 'offline_save')
                  AND created_at < now() - interval '7 days'
            ) ranked
            WHERE ranked.rn > 10
        )
        """
    )
    db.execute(
        """DELETE FROM versions WHERE id IN (
             SELECT id FROM (
               SELECT id, ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY created_at DESC) AS rn
               FROM versions
               WHERE label IN ('manual_save', 'offline_save')
                 AND created_at < now() - interval '7 days'
             ) ranked WHERE ranked.rn > 10
           )"""
    )
    deleted = getattr(db._cur, "rowcount", 0)
    db.commit()
    db.close()
    return {"deleted": deleted}
@celery_app.task
def purge_stale_operational_data() -> dict:
    """Bound unbounded operational tables while retaining recent audit evidence."""
    import os

    ai_days = max(30, int(os.getenv("AI_CALL_RETENTION_DAYS", "365")))
    operation_days = max(30, int(os.getenv("OPERATION_LOG_RETENTION_DAYS", "180")))
    audit_days = max(30, int(os.getenv("AUDIT_LOG_RETENTION_DAYS", "365")))
    db = connect()
    deleted = {}
    for table, days in (("ai_calls", ai_days), ("operation_logs", operation_days),
                        ("audit_logs", audit_days)):
        db.execute(
            f"DELETE FROM {table} WHERE created_at < now() - (%s * interval '1 day')", (days,),
        )
        deleted[table] = max(0, int(getattr(db._cur, "rowcount", 0)))
    db.commit()
    db.close()
    return {"deleted": deleted, "retention_days": {
        "ai_calls": ai_days, "operation_logs": operation_days, "audit_logs": audit_days,
    }}
def check_queue_backlog(threshold: int | None = None) -> str | None:
    """Alert when the celery queue piles up (e.g. stale dispatches burning
    provider credits — 404 messages were found queued on 2026-07-12)."""
    import os

    import redis as redis_lib

    limit = threshold if threshold is not None else int(os.getenv("QUEUE_BACKLOG_THRESHOLD", "50"))
    try:
        client = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        depth = int(client.llen("celery"))
    except Exception:
        return None
    if depth > limit:
        return f"celery queue backlog: {depth} messages (threshold {limit})"
    return None
@celery_app.task
def daily_cost_report() -> dict:
    """Beat: 昨日 AI 成本日报 — 个人部署最实用的一条监控。"""
    from app.core.alerts import send_alert

    db = connect()
    rows = db.execute(
        """SELECT task_type, COUNT(*) AS n, COALESCE(SUM(prompt_tokens),0) AS pt,
                  COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(cost_cny),0) AS cost
           FROM ai_calls
           WHERE created_at >= now() - interval '24 hours' AND status = 'succeeded'
           GROUP BY task_type ORDER BY cost DESC"""
    ).fetchall()
    failed = db.execute(
        "SELECT COUNT(*) AS n FROM ai_calls WHERE created_at >= now() - interval '24 hours' AND status != 'succeeded'"
    ).fetchone()["n"]
    db.close()
    total_calls = sum(r["n"] for r in rows)
    total_tokens = sum(r["pt"] + r["ct"] for r in rows)
    total_cost = float(sum(r["cost"] for r in rows))
    if total_calls or failed:
        lines = [f"过去24h：{total_calls} 次调用 / {total_tokens} tokens / ¥{total_cost:.4f}，失败 {failed} 次"]
        lines += [f"• {r['task_type']}: {r['n']} 次, {r['pt'] + r['ct']} tokens" for r in rows[:6]]
        send_alert("AI 成本日报\n" + "\n".join(lines), "info")
    return {"calls": total_calls, "tokens": total_tokens, "cost_cny": round(total_cost, 4), "failed": failed}
@celery_app.task
def patrol_check() -> dict:
    """M2 beat: consistency patrol — check foreshadowing, chapter gaps, quality."""
    db = connect()
    # Check for overdue foreshadowing (planted but past planned chapter)
    overdue = db.execute(
        """SELECT f.id, f.content, f.planned_resolve_chapter, c.title as chapter_title
           FROM foreshadowings f
           JOIN contents c ON f.chapter_id = c.id
           WHERE f.status = 'planted'
             AND f.planned_resolve_chapter IS NOT NULL
             AND f.planned_resolve_chapter <= (
               SELECT COALESCE(MAX((latest.meta->>'seq')::int), 0)
               FROM contents latest
               WHERE latest.parent_id = c.parent_id AND latest.type = 'chapter'
                 AND latest.is_deleted = FALSE
             )"""
    ).fetchall()

    # Check for chapters needing rewrite
    needs_rewrite = db.execute(
        "SELECT id, title FROM contents WHERE status = 'needs_rewrite' AND is_deleted = FALSE"
    ).fetchall()

    # Check for orphan chapters (no parent novel)
    orphans = db.execute(
        "SELECT id, title FROM contents WHERE type='chapter' AND parent_id IS NULL AND is_deleted = FALSE"
    ).fetchall()

    # V3 Story Arc (§4.4): Arc progress / integrity check. Flag active or
    # completed arcs whose trajectory fields are empty (data-integrity drift)
    # and active arcs with no chapters produced within their chapter_range.
    weak_arcs = db.execute(
        """SELECT c.id, c.title, c.meta
           FROM contents c
           WHERE c.type = 'story_arc' AND c.is_deleted = FALSE
             AND c.status IN ('active', 'completed')
             AND (c.meta->>'goal' IS NULL OR c.meta->>'goal' = ''
                  OR c.meta->>'end_state' IS NULL OR c.meta->>'end_state' = '')"""
    ).fetchall()
    active_no_progress = db.execute(
        """SELECT c.id, c.title, c.meta
           FROM contents c
           WHERE c.type = 'story_arc' AND c.is_deleted = FALSE AND c.status = 'active'
             AND jsonb_array_length(COALESCE(c.meta->'chapter_range', '[0,0]')::jsonb) = 2
             AND (
               SELECT COUNT(*) FROM contents ch
               WHERE ch.parent_id = c.parent_id AND ch.type = 'chapter'
                 AND ch.is_deleted = FALSE
                 AND (ch.meta->>'seq')::int BETWEEN (c.meta->'chapter_range'->>0)::int
                                               AND (c.meta->'chapter_range'->>1)::int
             ) = 0"""
    ).fetchall()

    # V3 Timeline Anchor (§10): chapters whose deterministic anachronism check
    # produced a warning (reality-based books only; others never write it).
    anachronism_warns = db.execute(
        """SELECT id, title FROM contents
           WHERE type = 'chapter' AND is_deleted = FALSE
             AND meta->'timeline_anchor_check'->>'status' = 'warning'"""
    ).fetchall()

    # V3 Reader Experience (§11.1): chapters whose review-time reader
    # experience summary flagged weak sub-dimensions (advisory only).
    weak_reader_exp = db.execute(
        """SELECT id, title FROM contents
           WHERE type = 'chapter' AND is_deleted = FALSE
             AND meta->'reader_experience'->>'status' = 'warning'"""
    ).fetchall()

    db.close()

    issues = []
    if overdue:
        issues.append(f"{len(overdue)} unfulfilled foreshadowings")
    if needs_rewrite:
        issues.append(f"{len(needs_rewrite)} chapters need rewrite")
    if orphans:
        issues.append(f"{len(orphans)} orphan chapters")
    if weak_arcs:
        issues.append(f"{len(weak_arcs)} story arcs with empty goal/end_state")
    if active_no_progress:
        issues.append(f"{len(active_no_progress)} active arcs with no chapters in range")
    if anachronism_warns:
        issues.append(f"{len(anachronism_warns)} chapters with anachronism warnings")
    if weak_reader_exp:
        issues.append(f"{len(weak_reader_exp)} chapters with weak reader experience")
    backlog = check_queue_backlog()
    if backlog:
        issues.append(backlog)

    # Send alerts for issues
    if issues:
        from app.core.alerts import send_alert
        send_alert("巡检发现问题:\n" + "\n".join(f"• {i}" for i in issues), "warning")

    return {
        "status": "ok" if not issues else "issues_found",
        "issues": issues,
        "foreshadowing_count": len(overdue),
        "needs_rewrite_count": len(needs_rewrite),
    }

# ── Stale bootstrap run recovery (deploy/crash resilience) ────────────────
# Worker SIGKILL during deploy (docker compose up --force-recreate) can orphan
# a run in status='running' with an in-flight node never finishing. This beat
# task finds runs that have been 'running' longer than a stall threshold with
# no active node progress, marks their stale in-flight nodes failed, and
# re-dispatches the run — execute_bootstrap skips already-succeeded nodes and
# resumes from the failed one (checkpoint-safe, idempotent).

STALE_RUN_STALL_MINUTES = int(os.getenv("STALE_RUN_STALL_MINUTES", "10"))
# Runs older than this while stuck in 'running' are treated as abandoned
# zombies and terminated instead of resurrected (avoid burning AI budget on
# long-dead test runs after a deploy).
STALE_RUN_MAX_AGE_HOURS = int(os.getenv("STALE_RUN_MAX_AGE_HOURS", "24"))


@celery_app.task(name="app.workers.tasks.resume_stale_bootstrap_runs")
def resume_stale_bootstrap_runs() -> dict:
    """Re-dispatch bootstrap runs stuck in 'running' past the stall threshold."""
    db = connect()
    stale = db.execute(
        """SELECT id, current_node_key, created_at, updated_at
           FROM workflow_runs
           WHERE status = 'running'
             AND updated_at < now() - make_interval(mins => %s)
           ORDER BY updated_at""",
        (STALE_RUN_STALL_MINUTES,),
    ).fetchall()
    db.close()

    recovered, skipped, terminated = [], [], []
    for run in stale:
        run_id = run["id"]
        node_key = run["current_node_key"] or "plan_idea"

        # Abandoned run: created too long ago to be a live book being built
        # right now — terminate it instead of spending AI budget re-running it.
        if run["created_at"] < datetime.now(timezone.utc) - timedelta(hours=STALE_RUN_MAX_AGE_HOURS):
            db = connect()
            db.execute(
                """UPDATE workflow_runs SET status = 'failed', finished_at = now(),
                           updated_at = now(),
                           dispatch_error = 'abandoned run (age > %s h); terminated by resume_stale_bootstrap_runs'
                   WHERE id = %s""",
                (STALE_RUN_MAX_AGE_HOURS, run_id),
            )
            db.commit()
            db.close()
            _record_bootstrap_event(
                run_id, "run.terminated_abandoned",
                node_key=node_key,
                payload={"reason": "abandoned", "age_hours": STALE_RUN_MAX_AGE_HOURS},
            )
            terminated.append({"run_id": run_id, "reason": "abandoned"})
            continue

        # A live worker heartbeats the run while executing: if updated_at is
        # stale AND no node is currently 'running' with a recent started_at,
        # the original worker is gone. Flip stale in-flight nodes to failed so
        # the claim guard (status IN pending/failed/...) accepts a re-dispatch.
        db = connect()
        active_node = db.execute(
            """SELECT id FROM run_nodes
               WHERE run_id = %s AND status = 'running'
                 AND started_at > now() - make_interval(mins => %s)""",
            (run_id, STALE_RUN_STALL_MINUTES),
        ).fetchone()
        if active_node:
            db.close()
            skipped.append({"run_id": run_id, "reason": "active_node"})
            continue

        # Zombie run: 'running' but has zero run_nodes (creation aborted before
        # seeding). Nothing to resume — terminate it so the UI stops spinning.
        node_count = db.execute(
            "SELECT COUNT(*) AS n FROM run_nodes WHERE run_id = %s", (run_id,)
        ).fetchone()["n"]
        if node_count == 0:
            db.execute(
                """UPDATE workflow_runs SET status = 'failed', finished_at = now(),
                           updated_at = now(),
                           dispatch_error = 'zombie run: no nodes seeded; terminated by resume_stale_bootstrap_runs'
                   WHERE id = %s""",
                (run_id,),
            )
            db.commit()
            db.close()
            _record_bootstrap_event(
                run_id, "run.terminated_zombie",
                node_key=node_key,
                payload={"reason": "no run_nodes seeded"},
            )
            terminated.append({"run_id": run_id, "reason": "no_nodes"})
            continue

        db.execute(
            """UPDATE run_nodes SET status = 'failed', finished_at = now(),
                       error = COALESCE(error, '') || ' [stale: worker lost; auto-resumed]'
               WHERE run_id = %s AND status = 'running'""",
            (run_id,),
        )
        db.commit()
        db.close()

        _record_bootstrap_event(
            run_id, "run.resumed_after_stall",
            node_key=node_key,
            payload={"reason": "stale running beyond threshold"},
        )
        dispatch_bootstrap_run(run_id, node_key)
        recovered.append({"run_id": run_id, "from_node": node_key})

    return {"status": "ok", "recovered": recovered, "skipped": skipped, "terminated": terminated}


@celery_app.task(bind=True, max_retries=2)
def bootstrap_short_story_task(self, project_id: str, short_id: str) -> dict:
    """M3: Generate short story from idea."""
    from app.services.short_story import SHORT_STORY_TEMPLATES

    db = connect()
    story = row_to_dict(db.execute("SELECT * FROM contents WHERE id = %s", (short_id,)).fetchone())
    if not story:
        db.close()
        return {"error": "story not found"}
    meta = story.get("meta", {})
    if not isinstance(meta, dict):
        meta = {}
    template_key = meta.get("template", "viral")
    template = SHORT_STORY_TEMPLATES.get(template_key, SHORT_STORY_TEMPLATES["viral"])
    context = {"idea": meta.get("idea",""), "genre": meta.get("genre",""),
               "style": meta.get("style",""), "template": template["name"],
               "max_words": meta.get("max_words", template["max_words"])}
    db.close()

    output = complete(run_id=None, node_key="s1", project_id=project_id,
                     task_type="gen_short_titles", prompt_name="shortstory.gen_titles",
                     variables=context)
    titles = output.get("titles", [])
    context["title"] = titles[0] if titles else "未命名短篇"

    output = complete(run_id=None, node_key="s2", project_id=project_id,
                     task_type="gen_short_story", prompt_name="shortstory.gen_story",
                     variables=context)
    story_out = output.get("story", {})
    body = {"type":"doc","content":[{"type":"paragraph","text":t} for t in story_out.get("body",[])]}
    db = connect()
    db.execute("UPDATE contents SET title=%s, body=%s, meta=meta||%s, status=%s, updated_at=now() WHERE id=%s",
               (story_out.get("title", context["title"]), encode(body),
                encode({"short_score": 0, "template": template_key}), "completed", short_id))
    db.commit()
    db.close()
    return {"status": "completed", "title": story_out.get("title", "")}
