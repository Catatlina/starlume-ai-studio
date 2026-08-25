from __future__ import annotations

import json
import logging
import os
import re
import time
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import settings
from .core.circuit_breaker import (
    circuit_breaker,
    record_failure,
    record_success,
    acquire_provider_token,
)
from .core.retry import with_provider_retry
from .core.alerts import alert_budget, alert_provider_error
from .db import connect, decode, encode, new_id, row_to_dict
from .core.billing import get_active_subscription, monthly_window
from .prompt_registry import OUTPUT_CONTRACTS, render_prompt
from .services.ai_runtime import (
    SHARED_LEDGER_TABLE,
    execution_key as build_execution_key,
    ensure_shared_ledger_schema,
    record_sync_execution,
)
from .services.unified_gateway import UnifiedAIGateway, UnifiedGatewayError

logger = logging.getLogger(__name__)

# Context variable for per-request API key (set by middleware from X-Api-Key header)
_request_api_key: ContextVar[str | None] = ContextVar("request_api_key", default=None)
_request_api_base_url: ContextVar[str | None] = ContextVar("request_api_base_url", default=None)
_request_model: ContextVar[str | None] = ContextVar("request_model", default=None)
_request_user_id: ContextVar[str | None] = ContextVar("request_user_id", default=None)


# Default model when no route exists. Must be a model that actually exists on
# the DeepSeek API — a fictional name here fails every unrouted task at call time.
MODEL = "deepseek-chat"
PROVIDER = "deepseek"

LONG_FORM_TASKS = {
    "gen_chapter1",
    "gen_next_chapter",
    "write_chapter_draft",
    "chapter_draft",
    "write_polish",
    "final_humanize",
    "editor_continue",
    "editor_rewrite",
    "editor_expand",
    "style_imitation",
}


class BudgetExceeded(RuntimeError):
    """Raised when a project budget would be exceeded by an AI call."""


class ProviderError(RuntimeError):
    """Raised when a configured provider cannot return a usable JSON result."""


class OutputValidationError(ProviderError):
    """Raised when a provider response is JSON but violates the task contract."""


class ProviderRateLimitError(RuntimeError):
    """Raised on an HTTP 429 from a provider; triggers the rate-limit backoff (P1-T1).

    Carries an optional ``retry_after`` (seconds) parsed from the Retry-After
    header so callers / the envelope can surface a precise backoff hint.
    """

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class _StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SynopsisOutput(_StrictOutput):
    synopsis: str = Field(min_length=20)
    selling_points: list[str] = Field(min_length=2)


class _WorldviewBody(_StrictOutput):
    name: str = Field(min_length=2)
    rules: list[str] = Field(min_length=1)


class _WorldviewOutput(_StrictOutput):
    worldview: _WorldviewBody


class _CharacterBody(_StrictOutput):
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    arc: str = Field(min_length=5)


class _CharactersOutput(_StrictOutput):
    characters: list[_CharacterBody] = Field(min_length=2, max_length=8)


class _OutlineOutput(_StrictOutput):
    outline: list[str] = Field(min_length=3)


class _ChapterBody(_StrictOutput):
    title: str = Field(min_length=2)
    body: list[str] = Field(min_length=2)


class _ChapterOutput(_StrictOutput):
    chapter: _ChapterBody


class _ReviewDimensions(_StrictOutput):
    prose: float = Field(ge=0, le=100)
    plot: float = Field(ge=0, le=100)
    character_ooc: float = Field(ge=0, le=100)
    world_conflict: float = Field(ge=0, le=100)
    logic_consistency: float = Field(ge=0, le=100)
    pace: float = Field(ge=0, le=100)
    foreshadowing: float = Field(ge=0, le=100)


# V3 §11.1 reader-experience sub-dimensions, merged into the existing
# review_7dim call (no new agent / call chain). Optional so legacy outputs
# without the block still validate.
class _ReaderExperience(_StrictOutput):
    expectation: float = Field(ge=0, le=100)
    conflict: float = Field(ge=0, le=100)
    payoff: float = Field(ge=0, le=100)
    emotion_shift: float = Field(ge=0, le=100)
    worth_continuing: float = Field(ge=0, le=100)


class _ReviewOutput(_StrictOutput):
    score: float = Field(ge=0, le=100)
    dimensions: _ReviewDimensions
    issues: list[str]
    reader_experience: _ReaderExperience | None = None


class _OocOutput(_StrictOutput):
    ooc_count: int = Field(ge=0)
    violations: list[dict[str, Any]]


class _ConsistencyOutput(_StrictOutput):
    contradictions: list[dict[str, Any]]


class _RhythmOutput(_StrictOutput):
    pacing_score: float = Field(ge=0, le=100)
    sections: list[dict[str, Any]]


class _LenientOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _AiDisclosureOutput(_LenientOutput):
    """Provider-backed publication disclosure; confirmation remains human-gated."""

    disclosure_text: str = Field(min_length=20)
    ai_models_used: list[str] = Field(min_length=1)
    usage_estimate: float | None = Field(default=None, ge=0, le=100)
    rationale: str = ""


class _SemanticPayoffItem(_LenientOutput):
    event: str = Field(min_length=2)
    evidence_quote: str = Field(min_length=2)
    reader_effect: str = Field(min_length=2)
    consequence: str = Field(min_length=2)
    confidence: float = Field(ge=0, le=1)


class _SemanticPayoffOutput(_LenientOutput):
    payoff_count: int = Field(ge=0)
    payoffs: list[_SemanticPayoffItem] = Field(default_factory=list)
    ending_pressure: bool
    semantic_score: float = Field(ge=0, le=100)
    rationale: str = ""


# ── V2 four-stage bootstrap output models ──────────────────────────────────
# V6.1.2 structured 7-dim review (closed-loop routing source).
# score_7dim is fixed {dim:{score,reason}} (never flat {style:85}); issues are
# structured objects carrying type/severity/location/repair_scope/confidence so
# the chapter loop can route A(local)/B(fact)/C(replan) without guesswork.
class _Review7DimIssue(_LenientOutput):
    type: str = Field(pattern=r"^(style|continuity|plot|logic|character|emotion|pacing)$")
    severity: str = Field(pattern=r"^(high|medium|low)$")
    location: str = ""
    description: str = ""
    repair_scope: str = Field(default="local", pattern=r"^(local|section|chapter)$")
    confidence: float = Field(default=0.9, ge=0, le=1)


class _Review7DimDim(_LenientOutput):
    score: float = Field(ge=0, le=100)
    reason: str = ""


class _Review7DimStructuredOutput(_LenientOutput):
    score_7dim: dict[str, _Review7DimDim]
    issues: list[_Review7DimIssue] = Field(default_factory=list)


class _KnownInfoItem(_LenientOutput):
    layer: str = Field(
        pattern=r"^(world_facts|reader_known|protagonist_known|character_known|character_misunderstood)$"
    )
    text: str = Field(min_length=1)


class _ExtractEntityItem(_LenientOutput):
    type: str = Field(pattern=r"^(character|location|item)$")
    name: str = Field(min_length=1)
    state: str = ""
    location: str = ""
    known_info: list[_KnownInfoItem] = Field(min_length=1)


class _ExtractEntitiesOutput(_LenientOutput):
    entities: list[_ExtractEntityItem] = Field(min_length=1)


class _ExtractTimelineEvent(_LenientOutput):
    event: str = Field(min_length=1)
    real_world_anchor: str | None = None


class _ExtractTimelineOutput(_LenientOutput):
    events: list[_ExtractTimelineEvent] = Field(min_length=1)


class _PlanIdeaOutput(_LenientOutput):
    idea_expanded: str = Field(min_length=20)
    # Reader-facing synopsis is intentionally separate from the raw creative
    # brief.  Older providers may omit it, so the bootstrap remains backward
    # compatible while new prompts produce it as a first-class deliverable.
    synopsis: str = Field(default="", max_length=500)
    core_hook: str = Field(min_length=5)
    target_audience: str = Field(min_length=2)
    title_candidates: list[str] = Field(min_length=3, max_length=10)
    creative_bible: str = Field(min_length=300)
    source_facts: list[str] = Field(min_length=3, max_length=30)
    design_additions: list[str] = Field(default_factory=list, max_length=20)
    forbidden_changes: list[str] = Field(min_length=3, max_length=20)
    downstream_deliverables: list[str] = Field(min_length=1, max_length=20)
    # V3 Novel DNA (§3): produced in the same plan_idea call as creative_bible.
    commercial_positioning: str = Field(default="")
    story_promise: str = Field(default="")
    forbidden_deviations: list[str] = Field(default_factory=list, max_length=20)
    # Long-form planning is a closed numeric ledger, not prose-only advice.
    longform_contract: dict[str, Any] = Field(default_factory=dict)
    # Shared contract for every core cheat/mechanic; specialised contracts
    # (for example simulator_contract) add only type-specific rules.
    core_mechanic_contract: dict[str, Any] = Field(default_factory=dict)
    # The fictional simulator contract is required only when the source idea
    # actually contains a simulator/life-simulation mechanic.
    simulator_contract: dict[str, Any] = Field(default_factory=dict)


class _PlanningContractRepairOutput(_LenientOutput):
    """Focused repair payload used when a provider omits planning ledgers."""
    creative_bible: str = Field(min_length=300)
    longform_contract: dict[str, Any] = Field(min_length=1)
    core_mechanic_contract: dict[str, Any] = Field(min_length=1)
    simulator_contract: dict[str, Any] = Field(min_length=1)


class _CreativeBibleRepairOutput(_LenientOutput):
    creative_bible: str = Field(min_length=300)


class _PlanFidelityAuditOutput(_LenientOutput):
    passed: bool
    score: float = Field(ge=0, le=100)
    matched_requirements: list[str] = Field(min_length=3)
    contradictions: list[str] = Field(default_factory=list)
    omissions: list[str] = Field(default_factory=list)


class _RegenerateTitlesOutput(_LenientOutput):
    title_candidates: list[str] = Field(min_length=3, max_length=10)


class _PlanMarketFitOutput(_LenientOutput):
    market_score: float = Field(ge=0, le=100)
    competitive_landscape: str = Field(min_length=5)
    market_gap: str = Field(min_length=5)
    evidence: dict[str, Any] = Field(default_factory=dict)


class _PlanStoryPatternOutput(_LenientOutput):
    story_model: str = Field(min_length=2)
    act_structure: list[str] = Field(min_length=1)
    turning_points: list[Any]


class _PlanCoreGameplayOutput(_LenientOutput):
    power_system: str = Field(min_length=5)
    progression_path: str = Field(min_length=5)
    pleasure_points: list[str] = Field(min_length=2)


class _LenientWorldviewBody(_LenientOutput):
    name: str = Field(min_length=2)
    rules: list[str] = Field(min_length=3)


class _PlanWorldArchitectureOutput(_LenientOutput):
    worldview: _LenientWorldviewBody


class _LenientCharacterBody(_LenientOutput):
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    arc: str = Field(min_length=2)


class _PlanCharacterSystemOutput(_LenientOutput):
    characters: list[_LenientCharacterBody] = Field(min_length=3, max_length=10)


class _PlanConflictMapOutput(_LenientOutput):
    conflicts: list[dict[str, Any]] = Field(min_length=1)


class _BlueprintVolumePlanOutput(_LenientOutput):
    volumes: list[dict[str, Any]] = Field(min_length=1)
    total_word_target: int | None = None
    volume_word_targets: list[int] = Field(default_factory=list)
    chapter_word_target: int | None = None
    chapter_count: int | None = None


class _BlueprintChapterOutlineItem(_LenientOutput):
    """One chapter outline. V3 Chapter Function fields (function_type /
    chapter_goal / reader_expectation) are required — a missing field fails
    schema validation and the generation is retried (打回重新细纲), per the
    V3 fusion doc §5, instead of silently producing a water-filling outline."""

    volume: int | None = None
    seq: int | None = None
    title: str = Field(default="")
    outline: str = Field(default="")
    beats: list[str] = Field(default_factory=list)
    foreshadow_plant: list[str] = Field(default_factory=list)
    foreshadow_reap: list[str] = Field(default_factory=list)
    function_type: str = Field(min_length=1)
    chapter_goal: str = Field(min_length=1)
    reader_expectation: str = Field(min_length=1)
    payoff_contract: dict[str, Any] = Field(default_factory=dict)
    explicit_user_contract: str = Field(default="")
    must_deliver: list[str] = Field(default_factory=list)
    delivery_state: str = Field(default="")
    visible_result: str = Field(default="")


class _BlueprintChapterOutlineOutput(_LenientOutput):
    chapter_outlines: list[_BlueprintChapterOutlineItem] = Field(min_length=3)


class _BlueprintSceneBeatOutput(_LenientOutput):
    scene_beats: list[dict[str, Any]] = Field(min_length=3)


class _ScenePlanOutput(_LenientOutput):
    """V3-P3-⑪: Scene Director 输出的章节场景分镜。"""
    scenes: list[dict[str, Any]] = Field(min_length=1)


class _ReaderExperiencePlan(_LenientOutput):
    """Writing targets for an ordinary reader; not a detector or audit score."""
    opening_anchor: str = Field(min_length=1)
    reader_discovery: str = Field(min_length=1)
    interest_change: str = Field(min_length=1)
    aftertaste: str = Field(min_length=1)
    continuation_question: str = Field(min_length=1)


class _ChapterSkeletonOutput(_LenientOutput):
    """Author-led writing artifact: a chapter blueprint, never final prose."""
    title: str = Field(min_length=1)
    chapter_goal: str = Field(min_length=1)
    current_state: str = Field(min_length=1)
    main_conflict: str = Field(min_length=1)
    scenes: list[dict[str, Any]] = Field(min_length=3, max_length=6)
    character_moves: list[str] = Field(default_factory=list)
    mainline_progress: str = Field(min_length=1)
    payoff: str = Field(min_length=1)
    foreshadowing: list[str] = Field(default_factory=list)
    continuity_warnings: list[str] = Field(default_factory=list)
    next_hook: str = Field(min_length=1)
    skeleton_text: str = Field(min_length=1)
    reader_experience_plan: _ReaderExperiencePlan


class _LenientChapterBody(_LenientOutput):
    title: str = Field(min_length=2)
    body: list[str] = Field(min_length=4)


class _WriteChapterDraftOutput(_LenientOutput):
    chapter: _LenientChapterBody


class _WriteSelfReviewOutput(_LenientOutput):
    self_score: float = Field(ge=0, le=100)
    strengths: list[str]
    weaknesses: list[str]


class _LenientPolishedBody(_LenientOutput):
    body: list[str] = Field(min_length=4)


class _WritePolishOutput(_LenientOutput):
    polished: _LenientPolishedBody
    changes_summary: str


class _WriteLengthCheckOutput(_LenientOutput):
    actual_chars: int = Field(ge=0)
    is_acceptable: bool
    advice: str = ""


class _WriteFactReconcileOutput(_LenientOutput):
    reconciliation: dict[str, Any]


class _ConsistencyDimension(_LenientOutput):
    status: str = Field(pattern=r"^(pass|warning|fail)$")
    issues: list[Any] = Field(default_factory=list)


class _FinalConsistencyDimensions(_LenientOutput):
    source_fidelity: _ConsistencyDimension
    characters: _ConsistencyDimension
    locations: _ConsistencyDimension
    timeline: _ConsistencyDimension
    objects: _ConsistencyDimension
    settings: _ConsistencyDimension
    foreshadowing: _ConsistencyDimension


class _FinalConsistencyCheckOutput(_LenientOutput):
    checks: _FinalConsistencyDimensions
    overall_status: str = Field(pattern=r"^(pass|warning|fail)$")
    warning_count: int = Field(ge=0)
    reader_experience: _ReaderExperience


class _ContinuityAuditBody(_LenientOutput):
    status: str = Field(pattern=r"^(continuous|warning|broken)$")
    gaps: list[Any] = Field(default_factory=list)
    narrative_flow: str = Field(min_length=2)


class _FinalContinuityAuditOutput(_LenientOutput):
    continuity: _ContinuityAuditBody


class _FinalHumanizeOutput(_LenientOutput):
    humanized_text: str = Field(min_length=50)
    changes: list[str]


class _BookAnalysisOutput(_LenientOutput):
    title: str = Field(min_length=1)
    total_paragraphs: int = Field(ge=0)
    opening_hook: str = ""
    detected_tropes: list[str] = Field(default_factory=list)
    rhythm: str = Field(min_length=1)
    avg_paragraph_length: int = Field(ge=0)
    structure_cards: dict[str, Any] = Field(default_factory=dict)
    style_profile: dict[str, Any] = Field(default_factory=dict)
    risks: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class _HotspotContentOutput(_LenientOutput):
    title: str = Field(min_length=1)
    body: list[str] = Field(min_length=1)
    meta: dict[str, Any] = Field(default_factory=dict)


class _DailyBriefOutput(_LenientOutput):
    wechat_draft: str = Field(min_length=1)
    toutiao_draft: str = Field(min_length=1)
    xhs_draft: str = Field(min_length=1)


class _TitleVariantsOutput(_LenientOutput):
    titles: list[str] = Field(min_length=1, max_length=20)


class _VideoScriptOutput(_LenientOutput):
    title: str = Field(min_length=1)
    scenes: list[dict[str, Any]] = Field(min_length=1)
    narration_style: str = ""
    cover_text: str = ""


class _MaterialSuggestionsOutput(_LenientOutput):
    cover_image_prompt: str = ""
    suggested_charts: list[str] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    recommended_tags: list[str] = Field(default_factory=list)


class _TopicSuggestionItem(_LenientOutput):
    suggestion: str = Field(min_length=2)
    rationale: str = ""
    based_on: list[str] = Field(default_factory=list)


class _PerformanceFeedbackOutput(_LenientOutput):
    topic_suggestions: list[_TopicSuggestionItem] = Field(min_length=1)
    writing_advice: list[str] = Field(default_factory=list)


class _TranslateSegmentOutput(_LenientOutput):
    translated: str = Field(min_length=1)


class _CulturalLocalizeOutput(_LenientOutput):
    localized: str = Field(min_length=1)
    notes: list[str] = Field(default_factory=list)


class _LocalizeNamesOutput(_LenientOutput):
    name_map: dict[str, str] = Field(default_factory=dict)


class _TextOutput(_StrictOutput):
    """Editor operations must never turn an empty provider payload into success."""

    text: str = Field(min_length=1)


class _DeaiLayerOutput(_LenientOutput):
    """De-AI pipeline layers: text + optional change log."""
    text: str = Field(min_length=1)
    changes: list[str] = Field(default_factory=list)


class _DeaiScoreOutput(_LenientOutput):
    """De-AI scoring result."""
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)


class _DeaiRewriteOutput(_LenientOutput):
    """V3 deai.rewrite: full de-AI rewrite for web novel style."""
    text: str = Field(min_length=20)


class _StyleImitationOutput(_LenientOutput):
    title: str = Field(min_length=2)
    style_profile: dict[str, Any]
    text: str = Field(min_length=800)


class _StoryArcItem(_LenientOutput):
    """V3 Story Arc (§4) — one narrative arc entity, single layer (no phase/scene)."""
    name: str = Field(min_length=2)
    goal: str = Field(min_length=5)
    start_state: str = Field(default="")
    end_state: str = Field(default="")
    participants: list[str] = Field(default_factory=list)
    core_conflict: str = Field(default="")
    key_events: list[str] = Field(default_factory=list)
    payoff_points: list[str] = Field(default_factory=list)
    foreshadowing_refs: list[str] = Field(default_factory=list)
    outcome_impact: str = Field(default="")
    status: str = Field(default="planning")  # planning / active / completed
    chapter_range: list[int] = Field(default_factory=list)  # [start_seq, end_seq]


class _GenerateStoryArcOutput(_LenientOutput):
    story_arcs: list[_StoryArcItem] = Field(min_length=1, max_length=12)


class _ReplacementItem(_LenientOutput):
    anchor: str = Field(min_length=2, description="需要替换的原文片段（精确匹配）")
    replacement: str = Field(min_length=1, description="替换后的文本")


class _RepairLocalOutput(_LenientOutput):
    """Sentence/paragraph-level local repair (§8): in-place fixes, no full rewrite."""
    replacements: list[_ReplacementItem] = Field(min_length=1, max_length=60)


class _ReplanChapterOutput(_LenientOutput):
    """Plot-level repair (§8.4): send back to Planner for re-planning."""
    revised_outline: dict[str, Any] = Field(min_length=1)
    rationale: str = Field(min_length=10)


BOOTSTRAP_OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "gen_synopsis": _SynopsisOutput,
    "gen_worldview": _WorldviewOutput,
    "gen_characters": _CharactersOutput,
    "gen_outline": _OutlineOutput,
    "gen_chapter1": _ChapterOutput,
    "gen_next_chapter": _ChapterOutput,
    "review_7dim": _ReviewOutput,
    "review_7dim_structured": _Review7DimStructuredOutput,
    "extract_entities": _ExtractEntitiesOutput,
    "extract_timeline": _ExtractTimelineOutput,
    "review_ooc": _OocOutput,
    "review_consistency": _ConsistencyOutput,
    "review_rhythm": _RhythmOutput,
    "publishing_ai_disclosure": _AiDisclosureOutput,
    "publishing_payoff_semantic": _SemanticPayoffOutput,
    # V2 four-stage bootstrap (18 agent nodes)
    "plan_idea": _PlanIdeaOutput,
    "repair_planning_contract": _PlanningContractRepairOutput,
    "expand_creative_bible": _CreativeBibleRepairOutput,
    "audit_plan_fidelity": _PlanFidelityAuditOutput,
    "regenerate_titles": _RegenerateTitlesOutput,
    "plan_market_fit": _PlanMarketFitOutput,
    "plan_story_pattern": _PlanStoryPatternOutput,
    "plan_core_gameplay": _PlanCoreGameplayOutput,
    "plan_world_architecture": _PlanWorldArchitectureOutput,
    "plan_character_system": _PlanCharacterSystemOutput,
    "plan_conflict_map": _PlanConflictMapOutput,
    "blueprint_volume_plan": _BlueprintVolumePlanOutput,
    "blueprint_chapter_outline": _BlueprintChapterOutlineOutput,
    "blueprint_scene_beat": _BlueprintSceneBeatOutput,
    "generate_story_arc": _GenerateStoryArcOutput,
    "write_chapter_draft": _WriteChapterDraftOutput,
    "chapter_draft": _WriteChapterDraftOutput,
    "write_self_review": _WriteSelfReviewOutput,
    "write_polish": _WritePolishOutput,
    "write_length_check": _WriteLengthCheckOutput,
    "write_fact_reconcile": _WriteFactReconcileOutput,
    "final_consistency_check": _FinalConsistencyCheckOutput,
    "final_continuity_audit": _FinalContinuityAuditOutput,
    "final_humanize": _FinalHumanizeOutput,
    "repair_local": _RepairLocalOutput,
    "replan_chapter": _ReplanChapterOutput,
    "book_analysis": _BookAnalysisOutput,
    "gen_daily_brief": _HotspotContentOutput,
    "hm_daily_brief": _DailyBriefOutput,
    "hm_title_variants": _TitleVariantsOutput,
    "gen_video_script": _VideoScriptOutput,
    "hm_material_suggestions": _MaterialSuggestionsOutput,
    "performance_feedback": _PerformanceFeedbackOutput,
    "translate_segment": _TranslateSegmentOutput,
    "cultural_localize": _CulturalLocalizeOutput,
    "localize_names": _LocalizeNamesOutput,
    "editor_polish": _TextOutput,
    "editor_rewrite": _TextOutput,
    "editor_continue": _TextOutput,
    "editor_expand": _TextOutput,
    "editor_condense": _TextOutput,
    "editor_deai": _TextOutput,
    # ── De-AI 7-layer pipeline ──
    "deai_detect": _DeaiLayerOutput,
    "deai_colloquialize": _DeaiLayerOutput,
    "deai_rhythm": _DeaiLayerOutput,
    "deai_character": _DeaiLayerOutput,
    "deai_context": _DeaiLayerOutput,
    "deai_deduplicate": _DeaiLayerOutput,
    "deai_polish": _DeaiLayerOutput,
    "deai_score": _DeaiScoreOutput,
    "deai_rewrite": _DeaiRewriteOutput,
    "style_imitation": _StyleImitationOutput,
    "scene_direct": _ScenePlanOutput,
    "chapter_skeleton": _ChapterSkeletonOutput,
}


def _split_into_paragraphs(text: str) -> list[str]:
    """Split arbitrary model text into a list of non-empty paragraphs.

    Handles windows line endings and single-newline separators, and the common
    failure mode where a model returns one long string with no newlines by
    falling back to sentence-group chunking so downstream paragraph-count gates
    still hold. Guarantees at least one paragraph for non-empty input.
    """
    text = str(text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if len(paragraphs) >= 2:
        return paragraphs
    # Few / one newline: the model likely returned a single block. Re-split by
    # sentence punctuation into groups so the schema's min-length gate holds.
    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?])", text) if s.strip()]
    if len(sentences) <= 1:
        return [text]
    grouped: list[str] = []
    for i in range(0, len(sentences), 5):
        group = "".join(sentences[i : i + 5]).strip()
        if group:
            grouped.append(group)
    return grouped or [text]


def _normalize_bootstrap_output(task_type: str, output: dict) -> dict:
    """Repair realistic model-output drift before schema validation (KI-007).

    Real models (especially deepseek-chat) sometimes return a structurally valid
    but schema-loose payload: a bare ``body`` instead of ``polished.body``, a
    string ``body`` instead of a paragraph list, dict items in a ``body`` list,
    or a missing ``changes_summary``. We normalise these into the canonical
    contract instead of hard-failing the whole run on a re-sampled mismatch.
    Genuinely empty or non-narrative output still fails downstream gates.
    """
    if not isinstance(output, dict):
        return output
    out = dict(output)
    if task_type == "chapter_draft":
        chapter = out.get("chapter")
        if not isinstance(chapter, dict):
            body_src: Any = None
            title = str(out.get("title") or "")
            if isinstance(out.get("body"), (list, str)):
                body_src = out["body"]
            elif isinstance(out.get("text"), str):
                body_src = out["text"]
            if body_src is not None:
                chapter = {"title": title, "body": body_src}
        if isinstance(chapter, dict):
            body = chapter.get("body")
            if isinstance(body, str):
                chapter["body"] = _split_into_paragraphs(body)
            elif isinstance(body, list):
                cleaned: list[str] = []
                for item in body:
                    if isinstance(item, str):
                        cleaned.append(item.strip())
                    elif isinstance(item, dict):
                        cleaned.append(str(item.get("text", item.get("content", ""))).strip())
                chapter["body"] = [item for item in cleaned if item]
            chapter.setdefault("title", out.get("title", "") or "")
            out["chapter"] = chapter
    if task_type == "write_polish":
        polished = out.get("polished")
        # 1. No polished wrapper -> derive from bare body / chapter / text.
        if not isinstance(polished, dict):
            body_src: Any = None
            title = out.get("title", "")
            if isinstance(out.get("body"), (list, str)):
                body_src = out["body"]
            elif isinstance(out.get("chapter"), dict) and out["chapter"].get("body"):
                body_src = out["chapter"]["body"]
                title = title or out["chapter"].get("title", "")
            elif isinstance(out.get("text"), str):
                body_src = out["text"]
            if body_src is not None:
                polished = {"title": title, "body": body_src}
        if isinstance(polished, dict):
            body = polished.get("body")
            # 2. body as string -> paragraph list; body as list -> clean items.
            if isinstance(body, str):
                polished["body"] = _split_into_paragraphs(body)
            elif isinstance(body, list):
                cleaned: list[str] = []
                for item in body:
                    if isinstance(item, str):
                        cleaned.append(item.strip())
                    elif isinstance(item, dict):
                        cleaned.append(str(item.get("text", item.get("content", ""))).strip())
                polished["body"] = [c for c in cleaned if c]
            polished.setdefault("title", out.get("title", "") or "")
            out["polished"] = polished
        # 3. missing changes_summary -> derive from changes list if present.
        if not out.get("changes_summary"):
            changes = out.get("changes")
            if isinstance(changes, list):
                out["changes_summary"] = "；".join(str(c) for c in changes if str(c).strip())
            else:
                out["changes_summary"] = ""
    return out


def validate_task_output(task_type: str, output: Any) -> dict[str, Any]:
    """Reject malformed creative output before it can be persisted as success."""
    if not isinstance(output, dict):
        raise OutputValidationError(f"provider returned non-object output for {task_type}")
    model = BOOTSTRAP_OUTPUT_MODELS.get(task_type)
    if not model:
        return output
    metadata = output.get("_meta")
    payload = {key: value for key, value in output.items() if key != "_meta"}
    payload = _normalize_bootstrap_output(task_type, payload)
    try:
        validated = model.model_validate(payload).model_dump()
        if task_type.startswith("editor_") or task_type == "style_imitation":
            if not str(validated.get("text") or "").strip():
                raise OutputValidationError(f"provider returned empty text for {task_type}")
        if metadata is not None:
            validated["_meta"] = metadata
        return validated
    except ValidationError as exc:
        raise OutputValidationError(f"provider output schema mismatch for {task_type}: {exc}") from exc


def _execute_provider_call(
    provider: str, task_type: str, prompt_text: str, model: str, params: dict,
    project_id: str | None = None,
) -> tuple[dict[str, Any], int, int, str, str]:
    """Execute one real provider call and normalise its return tuple.

    Raises ``ProviderError`` / ``ProviderRateLimitError`` on transport / 429 / 5xx
    failures (these are retried by the caller's ``with_provider_retry``), and
    ``OutputValidationError`` only for malformed JSON (handled as a schema
    concern by the caller).

    P2-T9 / Q5: before calling, the per-provider / per-scope circuit breaker and
    token bucket are checked. ``scope`` is the ``project_id`` so a single
    tenant's 429 storm no longer fuses the whole site, and the token bucket caps
    outbound rate to protect DeepSeek during fan-out.
    """
    scope = project_id or "global"
    try:
        breaker_open = circuit_breaker(provider, scope=scope)
    except TypeError as exc:
        if "scope" not in str(exc):
            raise
        breaker_open = circuit_breaker(provider)
    if not breaker_open:
        raise ProviderError(
            f"{provider} circuit breaker open — too many failures (scope={scope})"
        )
    try:
        token_acquired = acquire_provider_token(provider, scope=scope)
    except TypeError as exc:
        if "scope" not in str(exc):
            raise
        token_acquired = acquire_provider_token(provider)
    if not token_acquired:
        raise ProviderRateLimitError(
            f"{provider} provider token bucket exhausted (scope={scope})"
        )
    if provider == "deepseek":
        model_ = _request_model.get() or model or settings.deepseek_model
        output, prompt_tokens, completion_tokens = _deepseek_complete(task_type, prompt_text, model_, params)
        try:
            record_success("deepseek", scope=scope)
        except TypeError as exc:
            if "scope" not in str(exc):
                raise
            record_success("deepseek")
        return output, prompt_tokens, completion_tokens, "deepseek", model_
    if provider in ("claude", "openai", "gemini", "doubao"):
        output, prompt_tokens, completion_tokens, provider_name, model_name = _call_real_provider(
            provider, model or "", prompt_text, params
        )
        try:
            record_success(provider, scope=scope)
        except TypeError as exc:
            if "scope" not in str(exc):
                raise
            record_success(provider)
        return output, prompt_tokens, completion_tokens, provider_name, model_name
    raise ProviderError(f"unsupported real provider: {provider}")


def _complete_impl(
    *,
    run_id: str | None,
    node_key: str | None,
    project_id: str,
    user_id: str | None = None,
    task_type: str,
    prompt_name: str,
    variables: dict[str, Any],
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    # Kept local so runtime introspection and diagnostics describe the providers
    # this execution path can actually route to.
    supported_real_providers = ("deepseek", "claude", "openai", "gemini", "doubao")
    if client_mutation_id:
        existing_db = connect()
        existing = existing_db.execute(
            "SELECT output FROM ai_calls WHERE project_id = %s AND client_mutation_id = %s AND status = 'succeeded'",
            (project_id, client_mutation_id),
        ).fetchone()
        existing_db.close()
        if existing:
            return decode(existing["output"], {})
    start = time.perf_counter()
    prompt_text, provider, model, params = _load_prompt_and_route(prompt_name, task_type, variables)
    prompt_version = _active_prompt_version(prompt_name)
    estimated_cost = _estimate_cost(variables, {"prompt": prompt_text})
    _assert_budget(user_id, project_id, "bootstrap", estimated_cost)

    prompt_tokens, completion_tokens = 0, 0  # default
    provider_name = model_name = ""
    output: dict[str, Any] = {}

    # Route to a real provider, retrying schema-contract violations. Real models
    # are non-deterministic, so a malformed structured payload is retried
    # (FR-C3-07, <=2 retries) before it is surfaced as a failure.
    MAX_SCHEMA_ATTEMPTS = 3
    # Transport / 429 failures are retried by with_provider_retry. The circuit
    # breaker records a failure ONLY after every retry is exhausted
    # (on_final_failure), so transient provider blips do not trip the breaker.
    # Schema-contract violations (OutputValidationError) are NOT transport errors
    # and are retried separately by the loop below (no_retry_exc re-raises them).
    for schema_attempt in range(MAX_SCHEMA_ATTEMPTS):
        prompt_tokens, completion_tokens = 0, 0
        try:
            output, prompt_tokens, completion_tokens, provider_name, model_name = with_provider_retry(
                rate_limit_exc=(ProviderRateLimitError,),
                transport_exc=(ProviderError,),
                no_retry_exc=(OutputValidationError,),
                on_final_failure=lambda exc: _record_failure_compat(
                    provider, project_id or "global"
                ),
            )(_execute_provider_call)(
                provider, task_type, prompt_text, model, params, project_id=project_id
            )
        except OutputValidationError:
            # Schema-contract violation (non-JSON / malformed structured output)
            # is a model-sample failure, not a transport outage — retry with a
            # fresh real completion under the same contract.
            if schema_attempt >= MAX_SCHEMA_ATTEMPTS - 1:
                raise
            continue

        try:
            output = validate_task_output(task_type, output)
            break
        except OutputValidationError:
            # A real model may succeed on a fresh sample. Exhausted retries
            # re-raise for the caller.
            if schema_attempt >= MAX_SCHEMA_ATTEMPTS - 1:
                raise

    latency_ms = int((time.perf_counter() - start) * 1000)
    cost_cny = _calculate_cost(provider_name, model_name, prompt_tokens, completion_tokens)

    conn = connect()
    try:
        # Old development databases may predate the Alembic head.  Provision
        # the additive shared table in the same transaction before writing the
        # V6 row; production still runs the named migration explicitly.
        ensure_shared_ledger_schema(conn)
        # ON CONFLICT lets a successful retry overwrite the failed-attempt ledger
        # row that shares this mutation id, instead of colliding on the unique
        # index and permanently breaking failed-state retry recovery.
        call_id = new_id("call")
        conn.execute(
            """
            INSERT INTO ai_calls (
                id, run_id, node_key, provider, model, prompt_name, task_type,
                input, output, prompt_tokens, completion_tokens, cost_cny, latency_ms, status,
                client_mutation_id, project_id, user_id
            ) VALUES (%s, %s, %s ,%s, %s ,%s, %s ,%s, %s ,%s, %s ,%s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, client_mutation_id) WHERE client_mutation_id IS NOT NULL
            DO UPDATE SET provider = EXCLUDED.provider, model = EXCLUDED.model,
                prompt_name = EXCLUDED.prompt_name, task_type = EXCLUDED.task_type,
                input = EXCLUDED.input, output = EXCLUDED.output,
                prompt_tokens = EXCLUDED.prompt_tokens, completion_tokens = EXCLUDED.completion_tokens,
                cost_cny = EXCLUDED.cost_cny, latency_ms = EXCLUDED.latency_ms,
                status = 'succeeded', error = NULL
            """,
            (
                call_id,
                run_id,
                node_key,
                provider_name,
                model_name,
                prompt_name,
                task_type,
                encode({"variables": variables, "prompt": prompt_text}),
                encode(output),
                prompt_tokens,
                completion_tokens,
                cost_cny,
                latency_ms,
                "succeeded",
                client_mutation_id,
                project_id,
                user_id,
            ),
        )
        # V6 and V7 close the same shared execution contract.  This write is
        # in the same transaction as ai_calls: a successful provider result
        # cannot be reported as complete if the unified cost/provenance ledger
        # is unavailable.
        record_sync_execution(
            conn,
            execution_key=build_execution_key(
                "v6",
                scope=project_id,
                client_mutation_id=client_mutation_id or call_id,
            ),
            gateway_version="v6",
            project_id=project_id,
            novel_id=None,
            run_id=run_id,
            step_id=None,
            task_type=task_type,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            rendered_prompt=prompt_text,
            provider=provider_name,
            model=model_name,
            status="succeeded",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_cny=cost_cny,
            latency_ms=latency_ms,
            client_mutation_id=client_mutation_id,
            metadata={
                "source": "v6_gateway",
                "variables_hash_input": True,
                "provider_attempts": schema_attempt + 1,
            },
        )
        conn.commit()
    finally:
        conn.close()
    return output


def _record_failure_compat(provider: str, scope: str) -> None:
    """Call old one-argument breaker hooks used by integrations and new scoped hooks."""
    try:
        record_failure(provider, scope=scope)
    except TypeError as exc:
        if "scope" not in str(exc):
            raise
        record_failure(provider)


def complete(
    *,
    run_id: str | None,
    node_key: str | None,
    project_id: str,
    task_type: str,
    prompt_name: str,
    variables: dict[str, Any],
    user_id: str | None = None,
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    """Execute an AI call and keep both successful and failed attempts in the ledger."""
    user_id = user_id or _request_user_id.get()
    started = time.perf_counter()
    try:
        return _complete_impl(
            run_id=run_id, node_key=node_key, project_id=project_id,
            task_type=task_type, prompt_name=prompt_name, variables=variables,
            user_id=user_id, client_mutation_id=client_mutation_id,
        )
    except Exception as exc:
        _record_failed_call(
            run_id=run_id, node_key=node_key, project_id=project_id, task_type=task_type,
            prompt_name=prompt_name, variables=variables, user_id=user_id,
            client_mutation_id=client_mutation_id,
            started=started, error=exc,
        )
        raise


def _record_failed_call(*, run_id: str | None, node_key: str | None, project_id: str,
                        task_type: str, prompt_name: str, variables: dict[str, Any],
                        user_id: str | None = None, client_mutation_id: str | None,
                        started: float, error: Exception) -> None:
    conn = None
    try:
        route = _load_route(task_type) or {}
        provider = str(route.get("provider") or settings.ai_provider or "unknown")
        model = str(route.get("model") or settings.deepseek_model or "unknown")
        try:
            failed_prompt, _prompt_provider, _prompt_model, _prompt_params = _load_prompt_and_route(
                prompt_name, task_type, variables
            )
        except Exception:
            # If prompt loading itself failed there is no exact provider input;
            # preserve the variables as an explicit diagnostic payload rather
            # than inventing a successful-looking prompt version.
            failed_prompt = encode({"variables": variables})
        conn = connect()
        ensure_shared_ledger_schema(conn)
        # DO NOTHING keeps the first attempt's row and never clobbers a prior
        # succeeded row; a later successful retry upgrades the row via complete()'s
        # own ON CONFLICT DO UPDATE.
        call_id = new_id("call")
        conn.execute(
            """INSERT INTO ai_calls (
                   id, run_id, node_key, provider, model, prompt_name, task_type,
                   input, output, prompt_tokens, completion_tokens, cost_cny,
                   latency_ms, status, error, client_mutation_id, project_id, user_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,0,%s,'failed',%s,%s,%s,%s)
               ON CONFLICT (project_id, client_mutation_id) WHERE client_mutation_id IS NOT NULL
               DO NOTHING""",
            (call_id, run_id, node_key, provider, model, prompt_name, task_type,
             encode({"variables": variables}), encode({}),
             int((time.perf_counter() - started) * 1000), str(error)[:2000],
             client_mutation_id, project_id, user_id),
        )
        # A failed provider attempt is still provenance.  Keep this best
        # effort so the original provider error is never masked by an outage
        # in the audit table; successful calls use the fail-closed path above.
        try:
            record_sync_execution(
                conn,
                execution_key=build_execution_key(
                    "v6",
                    scope=project_id,
                    client_mutation_id=client_mutation_id or call_id,
                ),
                gateway_version="v6",
                project_id=project_id,
                novel_id=None,
                run_id=run_id,
                step_id=node_key,
                task_type=task_type,
                prompt_name=prompt_name,
                prompt_version=_active_prompt_version(prompt_name),
                rendered_prompt=failed_prompt,
                provider=provider,
                model=model,
                status="failed",
                latency_ms=int((time.perf_counter() - started) * 1000),
                client_mutation_id=client_mutation_id,
                error=str(error)[:2000],
                metadata={"source": "v6_gateway", "failure": True},
            )
        except Exception:
            logger.warning("shared AI execution ledger unavailable for failed V6 call", exc_info=True)
        conn.commit()
    except Exception:
        pass  # Preserve the original provider/budget error if the ledger is unavailable.
    finally:
        if conn is not None:
            conn.close()


def _call_real_provider(provider: str, model: str, prompt_text: str, params: dict) -> tuple:
    """Call one configured real provider. No mock or fallback generation."""
    from .ai.providers import PROVIDERS

    fn = PROVIDERS.get(provider)
    if not fn:
        raise ProviderError(f"unknown provider: {provider}")
    output, pt, ct = fn(prompt_text, model, params)
    return output, pt, ct, provider, model


def _load_route(task_type: str) -> dict | None:
    conn = connect()
    route = row_to_dict(
        conn.execute(
            "SELECT * FROM model_routes WHERE task_type = %s AND is_active = TRUE",
            (task_type,),
        ).fetchone()
    )
    conn.close()
    if route:
        route["params"] = decode(route.get("params"), {})
        route["fallback_json"] = decode(route.get("fallback_json"), [])
    return route


def _load_prompt_and_route(
    prompt_name: str,
    task_type: str,
    variables: dict[str, Any],
    include_contract: bool = True,
) -> tuple[str, str, str, dict[str, Any]]:
    route = _load_route(task_type)
    provider = (route or {}).get("provider", "deepseek")
    model = (route or {}).get("model", MODEL)
    params = (route or {}).get("params", {})
    conn = connect()
    prompt = row_to_dict(
        conn.execute(
            """
            SELECT * FROM prompts
            WHERE name = %s AND is_active = TRUE
            ORDER BY string_to_array(version, '.')::int[] DESC, created_at DESC
            LIMIT 1
            """,
            (prompt_name,),
        ).fetchone()
    )
    conn.close()
    if prompt:
        template = prompt["template"]
    else:
        # A missing prompt used to fall back to a stub that contains none of the
        # caller's variables — the model then "succeeds" while having seen no
        # input at all. If the name is a known seed, that is seed/DB drift and
        # must fail loudly instead of producing fabricated output.
        from .prompt_registry import PROMPT_SEEDS
        if prompt_name in {n for n, *_ in PROMPT_SEEDS}:
            raise RuntimeError(
                f"prompt {prompt_name!r} is in PROMPT_SEEDS but missing from the "
                f"prompts table — run init_db() to sync seeds before generating."
            )
        logger.warning(
            "prompt %s not found; falling back to stub template (task_type=%s)",
            prompt_name, task_type,
        )
        template = "请执行任务 $task_type，并输出 JSON。"
    enriched_variables = {"task_type": task_type, **variables}
    prompt_text = render_prompt(template, enriched_variables)
    contract = OUTPUT_CONTRACTS.get(task_type) or OUTPUT_CONTRACTS.get(task_type.replace("editor_", "editor_"))
    if contract and include_contract:
        prompt_text += "\n\n只输出合法 JSON（不得包含 JSON 以外的任何文本，不得增删字段），结构必须匹配：\n" + contract
    return prompt_text, provider, model, params


def _active_prompt_version(prompt_name: str) -> str:
    """Resolve the exact V6 prompt version used by the active DB row."""
    try:
        conn = connect()
        try:
            row = row_to_dict(
                conn.execute(
                    """
                    SELECT version FROM prompts
                    WHERE name = %s AND is_active = TRUE
                    ORDER BY string_to_array(version, '.')::int[] DESC, created_at DESC
                    LIMIT 1
                    """,
                    (prompt_name,),
                ).fetchone()
            )
        finally:
            conn.close()
        if row and row.get("version"):
            return str(row["version"])
    except Exception:
        # Prompt loading already performed the authoritative DB check.  This
        # fallback keeps test doubles and old read-only workers observable while
        # still exposing a concrete version label in the shared ledger.
        logger.warning("could not resolve active V6 prompt version", exc_info=True)
    from .prompt_registry import PROMPT_SEEDS

    for name, version, _provider, _template in PROMPT_SEEDS:
        if name == prompt_name:
            return str(version)
    return "runtime-1"


def _assert_budget(user_id: str | None, project_id: str, scope: str, estimated_cost: float) -> None:
    """Enforce the user's plan-derived monthly cost budget.

    The limit is sourced from the user's active plan (plans.monthly_budget_cny)
    instead of the previously hardcoded 2.0 CNY per-project 'bootstrap' budget.
    When no user context is available (e.g. background workers), fall back to the
    configured default and aggregate spend by project_id. Spend is read from
    the shared V6/V7 execution ledger when migrated, with the legacy ``ai_calls``
    table as an explicitly labelled compatibility fallback.
    """
    limit = float(settings.default_monthly_budget_cny)
    if user_id:
        try:
            sub = get_active_subscription(user_id)
            limit = float(sub.get("monthly_budget_cny") or settings.default_monthly_budget_cny)
        except Exception:
            limit = float(settings.default_monthly_budget_cny)
    start, end = monthly_window()
    conn = connect()
    try:
        try:
            agg = row_to_dict(conn.execute(
                f"""
                SELECT COALESCE(SUM(cost_cny), 0)::float AS spent
                FROM {SHARED_LEDGER_TABLE}
                WHERE created_at >= %s AND created_at < %s
                  AND status = 'succeeded' AND project_id = %s
                """,
                (start, end, project_id),
            ).fetchone())
        except Exception:
            rollback = getattr(conn, "rollback", None)
            if rollback:
                rollback()
            agg = row_to_dict(conn.execute(
                """
                SELECT COALESCE(SUM(cost_cny), 0)::float AS spent
                FROM ai_calls
                WHERE created_at >= %s AND created_at < %s
                  AND (user_id = %s OR (user_id IS NULL AND project_id = %s))
                """,
                (start, end, user_id, project_id),
            ).fetchone())
    finally:
        conn.close()
    spent = float(agg.get("spent") or 0)
    if spent + estimated_cost > limit:
        alert_budget(project_id, scope, spent, limit)
        raise BudgetExceeded(
            f"{scope} monthly budget exceeded: {spent:.4f}/{limit:.2f} CNY"
        )


def _estimate_cost(variables: dict[str, Any], output_hint: dict[str, Any]) -> float:
    prompt_tokens = max(80, len(encode(variables)) // 3)
    completion_tokens = max(120, len(encode(output_hint)) // 3)
    return round((prompt_tokens + completion_tokens) * 0.000002, 4)


def _calculate_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Provider-aware CNY pricing; deployments may override the full table as JSON."""
    default_rates = {
        "deepseek": {"input": 2.0, "output": 3.0},
        "openai": {"input": 18.0, "output": 72.0},
        "anthropic": {"input": 21.0, "output": 105.0},
        "claude": {"input": 21.0, "output": 105.0},
        "gemini": {"input": 0.75, "output": 3.0},
        "doubao": {"input": 1.0, "output": 2.0},
    }
    try:
        overrides = json.loads(os.getenv("AI_PRICE_CNY_PER_MILLION", "{}"))
        if isinstance(overrides, dict):
            default_rates.update(overrides)
    except json.JSONDecodeError:
        pass
    rate = default_rates.get(provider, default_rates.get(model, {"input": 0.0, "output": 0.0}))
    return round(
        (prompt_tokens * float(rate.get("input", 0)) + completion_tokens * float(rate.get("output", 0))) / 1_000_000,
        6,
    )


def _deepseek_complete(task_type: str, prompt: str, model: str, params: dict[str, Any]) -> dict[str, Any]:
    api_key = _request_api_key.get() or settings.deepseek_api_key
    if not api_key:
        raise ProviderError("DEEPSEEK_API_KEY is not configured")
    max_tokens = params.get("max_tokens")
    if max_tokens is None and task_type in LONG_FORM_TASKS:
        # Long-form fiction nodes must be able to return 3000+ Chinese
        # characters inside a JSON payload. Relying on provider defaults makes
        # the prompt say "write long" while the API may still truncate output.
        max_tokens = 8192
    if max_tokens is not None:
        try:
            max_tokens = max(1024, min(int(max_tokens), 8192))
        except (TypeError, ValueError):
            max_tokens = 8192
    else:
        max_tokens = 8192
    from .core.url_security import validate_ai_base_url
    base_url = validate_ai_base_url(_request_api_base_url.get() or settings.deepseek_base_url)
    try:
        response = UnifiedAIGateway(
            provider="deepseek",
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=settings.request_timeout_seconds,
        ).complete_sync(
            prompt,
            system_prompt=(
                "你是 NovelCraft 的职业网文创作 Agent。只输出合法 JSON；"
                "正文必须是可直接连载发布的小说叙事，不写说明文、计划书或创作建议。"
            ),
            temperature=params.get("temperature", 0.7),
            max_tokens=max_tokens,
            json_mode=True,
        )
        content = response.content
    except UnifiedGatewayError as exc:
        if exc.status_code == 429:
            raise ProviderRateLimitError(
                f"deepseek rate limited (429): {exc}"
            ) from exc
        if exc.status_code:
            alert_provider_error(task_type, f"deepseek http {exc.status_code}")
        else:
            alert_provider_error(task_type, str(exc))
        raise ProviderError(str(exc)) from exc
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Fallback: deepseek sometimes prefixes JSON with explanation text or
        # includes invalid control characters. Try to extract the first JSON
        # object from the response before giving up.
        import re
        # Strip invalid control characters (common deepseek quirk)
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', content)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            # Try to find first {...} block
            m = re.search(r'\{[\s\S]*\}', cleaned)
            if m:
                try:
                    parsed = json.loads(m.group())
                except json.JSONDecodeError as exc:
                    raise OutputValidationError(f"deepseek returned non-json for {task_type}") from exc
            else:
                raise OutputValidationError(f"deepseek returned non-json for {task_type}")
    return parsed, response.prompt_tokens, response.completion_tokens


# ===== Streaming (pure-text tasks only) =====
# Structured tasks (gen_chapter1 etc.) must be validated as a whole JSON object
# before persisting, so streaming is limited to plain-text editor operations.

TEXT_STREAM_TASKS = {
    "editor_polish", "editor_rewrite", "editor_continue",
    "editor_expand", "editor_condense", "editor_deai", "engine_chat",
}


def _deepseek_stream(prompt: str, model: str, params: dict[str, Any], usage_out: dict[str, int]):
    """Yield content deltas from an OpenAI-compatible streaming endpoint."""
    api_key = _request_api_key.get() or settings.deepseek_api_key
    if not api_key:
        raise ProviderError("DEEPSEEK_API_KEY is not configured")
    from .core.url_security import validate_ai_base_url
    base_url = validate_ai_base_url(_request_api_base_url.get() or settings.deepseek_base_url)
    try:
        max_tokens = params.get("max_tokens", 8192)
        try:
            max_tokens = max(1024, min(int(max_tokens), 8192))
        except (TypeError, ValueError):
            max_tokens = 8192
        for delta in UnifiedAIGateway(
            provider="deepseek",
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=settings.request_timeout_seconds,
        ).stream_sync(
            prompt,
            system_prompt="你是 NovelCraft 的创作助手。直接输出正文文本，不要任何解释、标题或格式包裹。",
            temperature=params.get("temperature", 0.7),
            max_tokens=max_tokens,
            usage_out=usage_out,
        ):
            yield delta
    except UnifiedGatewayError as exc:
        if exc.status_code == 429:
            raise ProviderRateLimitError(f"deepseek rate limited (429): {exc}") from exc
        raise ProviderError(f"deepseek stream failed: {exc}") from exc


def _complete_stream_impl(
    *,
    project_id: str,
    user_id: str | None = None,
    task_type: str,
    prompt_name: str,
    variables: dict[str, Any],
    client_mutation_id: str | None = None,
):
    """Stream text deltas for a pure-text task, then write the ai_calls ledger.

    Same semantics as complete(): budget assert up front, mutation replay from
    the ledger (yielded as one delta), circuit breaker + failure recording, and
    a succeeded ai_calls row with usage once the stream finishes."""
    if task_type not in TEXT_STREAM_TASKS:
        raise ProviderError(f"streaming is not supported for {task_type}")
    if client_mutation_id:
        conn = connect()
        existing = conn.execute(
            "SELECT output FROM ai_calls WHERE project_id = %s AND client_mutation_id = %s AND status = 'succeeded'",
            (project_id, client_mutation_id),
        ).fetchone()
        conn.close()
        if existing:
            yield decode(existing["output"], {}).get("text", "")
            return

    start = time.perf_counter()
    prompt_text, provider, model, params = _load_prompt_and_route(
        prompt_name, task_type, variables, include_contract=False
    )
    if task_type == "engine_chat":
        params = {
            **params,
            "temperature": variables.get("_temperature", params.get("temperature", 0.7)),
            "max_tokens": variables.get("_max_tokens", params.get("max_tokens", 2000)),
        }
    _assert_budget(user_id, project_id, "bootstrap", _estimate_cost(variables, {"prompt": prompt_text}))

    chunks: list[str] = []
    usage: dict[str, int] = {}
    if provider == "deepseek":
        scope = project_id or "global"
        if not circuit_breaker("deepseek", scope=scope):
            raise ProviderError("deepseek circuit breaker open — too many failures")
        if not acquire_provider_token("deepseek", scope=scope):
            raise ProviderRateLimitError("deepseek provider token bucket exhausted")
        model_name = _request_model.get() or model or settings.deepseek_model
        provider_name = "deepseek"
        try:
            for delta in _deepseek_stream(prompt_text, model_name, params, usage):
                chunks.append(delta)
                yield delta
            record_success("deepseek", scope=scope)
        except ProviderError:
            record_failure("deepseek", scope=scope)
            raise
    else:
        # Other providers use different auth/protocol implementations in the
        # non-streaming gateway. Until matching stream adapters exist, fail
        # explicitly.
        raise ProviderError(f"streaming is not supported for provider: {provider}")

    full_text = "".join(chunks)
    if not full_text.strip():
        raise OutputValidationError(f"provider returned empty streamed text for {task_type}")
    prompt_tokens = int(usage.get("prompt_tokens", 0)) or max(1, len(prompt_text) // 4)
    completion_tokens = int(usage.get("completion_tokens", 0)) or max(1, len(full_text) // 4)
    latency_ms = int((time.perf_counter() - start) * 1000)
    conn = connect()
    try:
        ensure_shared_ledger_schema(conn)
        # Same replay-safety as complete(): a successful stream upgrades any prior
        # failed-attempt row sharing this mutation id instead of colliding.
        call_id = new_id("call")
        conn.execute(
            """INSERT INTO ai_calls (
                   id, run_id, node_key, provider, model, prompt_name, task_type,
                   input, output, prompt_tokens, completion_tokens, cost_cny, latency_ms, status,
                   client_mutation_id, project_id, user_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (project_id, client_mutation_id) WHERE client_mutation_id IS NOT NULL
               DO UPDATE SET provider = EXCLUDED.provider, model = EXCLUDED.model,
                   prompt_name = EXCLUDED.prompt_name, task_type = EXCLUDED.task_type,
                   input = EXCLUDED.input, output = EXCLUDED.output,
                   prompt_tokens = EXCLUDED.prompt_tokens, completion_tokens = EXCLUDED.completion_tokens,
                   cost_cny = EXCLUDED.cost_cny, latency_ms = EXCLUDED.latency_ms,
                   status = 'succeeded', error = NULL""",
            (call_id, None, None, provider_name, model_name, prompt_name, task_type,
             encode({"variables": variables, "prompt": prompt_text, "stream": True}),
             encode({"text": full_text}),
             prompt_tokens, completion_tokens,
             _calculate_cost(provider_name, model_name, prompt_tokens, completion_tokens), latency_ms,
             "succeeded", client_mutation_id, project_id, user_id),
        )
        record_sync_execution(
            conn,
            execution_key=build_execution_key(
                "v6",
                scope=project_id,
                client_mutation_id=client_mutation_id or call_id,
            ),
            gateway_version="v6",
            project_id=project_id,
            novel_id=None,
            run_id=None,
            step_id=None,
            task_type=task_type,
            prompt_name=prompt_name,
            prompt_version=_active_prompt_version(prompt_name),
            rendered_prompt=prompt_text,
            provider=provider_name,
            model=model_name,
            status="succeeded",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_cny=_calculate_cost(provider_name, model_name, prompt_tokens, completion_tokens),
            latency_ms=latency_ms,
            client_mutation_id=client_mutation_id,
            metadata={"source": "v6_gateway", "stream": True},
        )
        conn.commit()
    finally:
        conn.close()


def complete_stream(
    *, project_id: str, task_type: str, prompt_name: str,
    variables: dict[str, Any], user_id: str | None = None,
    client_mutation_id: str | None = None,
):
    """Public streaming wrapper with the same failed-call ledger contract as complete()."""
    user_id = user_id or _request_user_id.get()
    started = time.perf_counter()
    try:
        yield from _complete_stream_impl(
            project_id=project_id, user_id=user_id, task_type=task_type, prompt_name=prompt_name,
            variables=variables, client_mutation_id=client_mutation_id,
        )
    except Exception as exc:
        _record_failed_call(
            run_id=None, node_key=None, project_id=project_id, task_type=task_type,
            prompt_name=prompt_name, variables={**variables, "stream": True},
            user_id=user_id, client_mutation_id=client_mutation_id, started=started, error=exc,
        )
        raise
