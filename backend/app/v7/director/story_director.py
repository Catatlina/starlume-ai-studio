"""Story Director - Sprint 2.

The decision-making layer that coordinates all engines through a real 7-step
agent loop:

    perceive -> assess -> decide -> plan -> execute -> observe -> update

AI is the writer, human is the producer: the permission system and the
confidence gate are enforced in `decide` and cannot be bypassed.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..brain.novel_brain import NovelBrain
from ..trace.tracer import ExecutionTracer
from ..events.event_bus import EventBus
from ..events.subscribers import BrainStateSubscribers
from ..engines.plot_engine import PlotEngine
from ..engines.memory_engine import MemoryEngine
from ..engines.review_engine import ReviewEngine
from ..engines.base import EngineResult
from ..generation.generation_engine import (
    AIGatewayError,
    CHAPTER_STATE_TYPE,
    GenerationEngine,
    chapter_state_key,
    chinese_word_count,
    is_retryable_provider_failure,
)
from ..repositories.decision import DecisionPermissionRepository
from ..integration.quality import (
    MAX_REWORKS,
    MAX_LOCAL_REPAIRS,  # P2-1 质量整改：本地修复最大次数，不计入MAX_REWORKS
    QUALITY_REWORK_SCORE,
    evaluate_review,
)
from ..integration.v6_bridge import (
    build_transition_contract,
    persist_accepted_v7_chapter,
    persist_review_hold_v7_draft,
    persist_rejected_v7_draft,
)
from ..quality.continuity import validate_prose_continuity, validate_transition_contract
from ..quality.review_evidence import validate_review_evidence
from ..quality.writing_methodology import (
    build_writing_workflow_contract,
    transition_workflow_status,
    validate_writing_workflow,
)
from ..quality.consistency_checker import (
    CONSISTENCY_PASS_SCORE,
    ConsistencyCheckResult,
    ConsistencyChecker,
    format_consistency_issues,
)
from ...services.quality_profiles import (
    quality_profile_metadata,
    reader_chapter_budget,
    select_quality_profile,
)

AGENT_LOOP_STEPS: tuple[str, ...] = (
    "perceive",
    "assess",
    "decide",
    "plan",
    "execute",
    "observe",
    "update",
)

# A long-run is a controlled quality-observation mode. It must not bypass
# structural blockers or the post-generation prose gate. Confidence is a
# planning signal, not a second prose-quality gate: when the plot assessment
# succeeds and reports no blocker, a low-but-valid score must be retained as a
# warning and allowed to reach the generation contract. Otherwise a 20-chapter
# run can stop before producing any prose simply because the planner is unsure
# about a warming story state. The floor is the minimum valid confidence value
# emitted by PlotEngine, and only applies when the caller supplied a persisted
# batch id. Explicit approve/forbidden permissions still block below.
BATCH_AUTOGENERATION_CONFIDENCE_FLOOR = 0.05

# These defects are safe to repair against the already generated chapter.
# Anything involving story structure, score dimensions, payoff, continuity or
# length must still take the full bounded rewrite path.
# P0-2 质量整改：缩小本地修复范围，只保留纯确定性问题
# 需要语义理解的问题（AI味、节奏、结构等）走完整重写，不要在本地修
LOCAL_PROSE_REPAIR_DIMENSIONS = frozenset({
    "third_person_narrative",    # 人称错误，纯替换
    "profanity_or_insult",       # 违禁词，纯替换
    "sensitive_content",         # 敏感内容，纯替换
    "urban_real_world_entity",   # 真实实体，纯替换
    "duplicate_paragraph",       # 重复段落，纯删除
})


def _format_quality_failure(item: dict[str, Any]) -> str:
    """Render a gate failure without assuming every value is numeric.

    Model-derived dimension failures use numeric scores, while deterministic
    risk failures intentionally use labels such as ``high``/``resolved``.
    Rework feedback must remain diagnostic for both shapes; formatting a
    string with ``:.0f`` used to crash the real-provider path before a rejected
    chapter could be persisted.
    """
    def render(value: Any) -> str:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{value:.0f}"
        return str(value or "—")

    dimension = str(item.get("dimension") or "质量项")
    return f"{dimension} {render(item.get('actual'))}/{render(item.get('minimum'))}"

class DecisionPermissionSystem:
    """
    Decision permission system.

    Permission levels:
    - auto: AI can decide automatically
    - notify: AI decides, but human is notified
    - approve: AI proposes, human must approve
    - forbidden: AI cannot decide, only human can
    """

    def __init__(self, db: AsyncSession, novel_id: uuid.UUID):
        self.db = db
        self.novel_id = novel_id
        self.perm_repo = DecisionPermissionRepository(db)

    async def get_permission(self, decision_type: str) -> str:
        """Get permission level for a decision type."""
        perm = await self.perm_repo.get_by_type(self.novel_id, decision_type)
        if perm:
            return perm.permission_level
        return "auto"  # default

    async def get_confidence_threshold(self, decision_type: str) -> float:
        """Minimum AI confidence required for this decision type."""
        perm = await self.perm_repo.get_by_type(self.novel_id, decision_type)
        threshold = getattr(perm, "confidence_threshold", None) if perm else None
        return float(threshold) if threshold is not None else 0.7

    async def evaluate(
        self, decision_type: str, confidence: float
    ) -> dict[str, Any]:
        """
        Combined permission + confidence gate.

        Returns {allowed, level, threshold, blocked_reason}.
        """
        level = await self.get_permission(decision_type)
        threshold = await self.get_confidence_threshold(decision_type)

        if level == "forbidden":
            return {
                "allowed": False,
                "level": level,
                "threshold": threshold,
                "blocked_reason": "decision type is forbidden for AI",
            }
        if level == "approve":
            return {
                "allowed": False,
                "level": level,
                "threshold": threshold,
                "blocked_reason": "human approval required",
            }
        if confidence < threshold:
            return {
                "allowed": False,
                "level": level,
                "threshold": threshold,
                "blocked_reason": (
                    f"confidence {confidence:.2f} below threshold {threshold:.2f}"
                ),
            }
        return {
            "allowed": True,
            "level": level,
            "threshold": threshold,
            "blocked_reason": None,
        }

    async def can_auto_decide(self, decision_type: str, confidence: float = 0.9) -> bool:
        """Check if AI can auto-decide."""
        return (await self.evaluate(decision_type, confidence))["allowed"]

    async def needs_approval(self, decision_type: str) -> bool:
        """Check if decision needs human approval."""
        level = await self.get_permission(decision_type)
        return level in ("approve", "forbidden")


class StoryDirector:
    """Story Director - runs the 7-step agent loop for chapter generation."""

    def __init__(
        self,
        db: AsyncSession,
        novel_id: uuid.UUID,
        brain: NovelBrain,
        tracer: ExecutionTracer,
        event_bus: EventBus,
        project_id: str | None = None,
        user_id: str | None = None,
        provider_config: dict[str, str] | None = None,
        generation_metadata: dict[str, Any] | None = None,
        quality_profile: dict[str, Any] | None = None,
        genre_id: str | None = None,
    ):
        self.db = db
        self.novel_id = novel_id
        self.brain = brain
        self.tracer = tracer
        self.event_bus = event_bus
        self.project_id = project_id
        self.user_id = user_id
        self.provider_config = provider_config or {}
        self.generation_metadata = generation_metadata or {}
        self.quality_profile = quality_profile or select_quality_profile()
        self.permission_system = DecisionPermissionSystem(db, novel_id)

        # Engines
        self.plot_engine = PlotEngine(
            db, novel_id, brain, tracer, event_bus,
            project_id=project_id,
            provider_config=self.provider_config,
            quality_profile=self.quality_profile,
            genre_id=genre_id,
        )
        self.memory_engine = MemoryEngine(
            db, novel_id, brain, tracer, event_bus,
            project_id=project_id,
            provider_config=self.provider_config,
        )
        self.review_engine = ReviewEngine(
            db, novel_id, brain, tracer, event_bus,
            project_id=project_id,
            provider_config=self.provider_config,
            quality_profile=self.quality_profile,
        )
        self.generation_engine = GenerationEngine(
            db, novel_id, brain, tracer, event_bus,
            project_id=project_id,
            provider_config=self.provider_config,
            quality_profile=self.quality_profile,
            genre_id=genre_id,
        )
        # 一致性检查器
        self.consistency_checker = ConsistencyChecker(self.generation_engine.ai_gateway)
        # Ordered batches are diagnostic runs: a rejected/held chapter remains
        # provisional context so the run can measure cross-chapter continuity.
        # It is never treated as accepted Novel Brain truth and single-chapter
        # generation keeps the strict block below.
        self.generation_engine.include_rejected_context = bool(
            self.generation_metadata.get("batch_id")
        )

        # Event-driven state projection
        self.subscribers = BrainStateSubscribers(brain, event_bus)
        self.subscribers.register()

    # ── main entry point ────────────────────────────────────────────────
    async def generate_chapter(
        self,
        chapter_number: int,
        *,
        prompt: str | None = None,
        outline: str | None = None,
        target_word_count: int = 3000,
        allow_rework: bool = False,
        max_reworks: int | None = None,
    ) -> dict[str, Any]:
        """Run the full 7-step agent loop for one chapter.

        Generation-time scene contracts are the primary quality control.  A
        post-write audit rewrite is an explicit, bounded fallback and is off
        by default so a failed audit cannot silently turn into repeated prose
        rewrites or hide a generation defect.
        """
        requested_target_word_count = target_word_count
        reader_budget = reader_chapter_budget(
            self.quality_profile,
            requested_target=target_word_count,
        )
        target_word_count = int(reader_budget["target_word_count"])
        run_id = await self.tracer.start_run(
            "chapter_generation",
            trigger="manual",
            chapter_number=chapter_number,
            input_data={
                "chapter_number": chapter_number,
                "prompt": prompt,
                "outline": outline,
                "target_word_count": target_word_count,
                "requested_target_word_count": requested_target_word_count,
                "reader_chapter_budget": reader_budget,
            },
        )

        try:
            # ── 1. PERCEIVE ────────────────────────────────────────────
            async with self.tracer.trace_step(
                "director.perceive", "perceive",
                input_summary=f"Perceive story state before chapter {chapter_number}",
            ) as step:
                perception = await self._perceive(chapter_number)
                step.set_output(
                    f"{perception['state_total']} states, "
                    f"{perception['pending_review']} pending, "
                    f"last chapter={perception['last_chapter']}",
                    data=perception,
                )

            if perception.get("blocked_by_quality"):
                result = {
                    "run_id": str(run_id),
                    "chapter_number": chapter_number,
                    "status": "blocked_quality",
                    "blocked_reason": perception["blocked_by_quality"],
                    "steps_executed": ["perceive"],
                }
                await self.tracer.complete_run(run_id, output_data=result)
                return result

            # ── 2. ASSESS ──────────────────────────────────────────────
            async with self.tracer.trace_step(
                "director.assess", "assess",
                input_summary="Assess plot situation",
            ) as step:
                # Bootstrap sometimes passes an empty JSON placeholder as the
                # chapter outline while the actual creative context is in the
                # prompt.  The pre-generation gate must assess the same story
                # brief that the writer will receive; otherwise a valid first
                # chapter is blocked as "low confidence" before any prose is
                # generated.
                assessment_outline = (outline or "").strip()
                if assessment_outline in {"", "{}", "[]", "null"}:
                    assessment_outline = (prompt or "").strip()
                assessment = await self._assess(
                    chapter_number, assessment_outline or None, perception
                )
                step.set_output(
                    assessment["summary"],
                    data={"plot_success": assessment["plot_success"]},
                    confidence=assessment["confidence"],
                )

            # ── 3. DECIDE (permission + confidence gate) ───────────────
            async with self.tracer.trace_step(
                "director.decide", "decide",
                input_summary="Apply permission and confidence gate",
            ) as step:
                decision = await self._decide(
                    chapter_number, assessment, run_id=run_id
                )
                step.set_output(
                    f"gate={'allowed' if decision['allowed'] else 'blocked'} "
                    f"({decision['level']}, thr={decision['threshold']})",
                    data=decision,
                    confidence=assessment["confidence"],
                )

            if not decision["allowed"]:
                result = {
                    "run_id": str(run_id),
                    "chapter_number": chapter_number,
                    "status": "pending_approval",
                    "blocked_reason": decision["blocked_reason"],
                    "permission_level": decision["level"],
                    "decision_id": decision.get("decision_id"),
                    # Planning/provider transport failures are operationally
                    # retryable, not human approval requests.  Keep the
                    # assessment evidence small but explicit so the worker
                    # can retry the same ordered batch slot without guessing.
                    "planning_assessment": {
                        "plot_success": bool(assessment.get("plot_success")),
                        "confidence": assessment.get("confidence"),
                        "blockers": list(assessment.get("blockers") or []),
                    },
                    "retryable_planning_failure": bool(
                        assessment.get("retryable_planning_failure")
                    ),
                    "steps_executed": ["perceive", "assess", "decide"],
                }
                await self.tracer.complete_run(run_id, output_data=result)
                return result

            # ── 4. PLAN ────────────────────────────────────────────────
            async with self.tracer.trace_step(
                "director.plan", "plan",
                input_summary=f"Plan chapter {chapter_number}",
            ) as step:
                plan = await self._plan(
                    chapter_number, prompt, outline, assessment, perception,
                    target_word_count=target_word_count,
                )
                step.set_output(
                    f"target {plan['target_word_count']} chars, "
                    f"{len(plan['goals_to_advance'])} goal(s), "
                    f"{len(plan['constraints_to_respect'])} constraint(s)",
                    data=plan,
                )

            # ── 5. EXECUTE (real AI generation) ────────────────────────
            async with self.tracer.trace_step(
                "director.execute", "execute",
                input_summary="Generate chapter text with AI",
            ) as step:
                generation = await self.generation_engine.generate_chapter(
                    chapter_number,
                    prompt=plan["prompt"],
                    outline=plan["outline"],
                    target_word_count=plan["target_word_count"],
                    plot_brief=plan.get("plot_brief"),
                    writing_workflow=plan.get("writing_workflow"),
                )
                step.set_output(
                    f"{generation['word_count']} chars "
                    f"(target {generation['target_word_count']})",
                    data={
                        "word_count": generation["word_count"],
                        "meets_target": generation["meets_target"],
                        "title": generation["title"],
                    },
                )

            # ── 6. OBSERVE (7-dimension review + quality gate) ─────────
            async with self.tracer.trace_step(
                "director.observe", "observe",
                input_summary="Review generated chapter (7 dimensions)",
            ) as step:
                observation = await self._observe(
                    chapter_number,
                    generation,
                    allow_rework=allow_rework,
                    max_reworks=max_reworks,
                    plan=plan,
                )
                generation = observation["generation"]
                step.set_output(
                    f"score={observation['review_score']} "
                    f"passed={observation['passed_review']} "
                    f"rework={observation['rework_count']}",
                    data={
                        "review_score": observation["review_score"],
                        "dimension_scores": observation["dimension_scores"],
                        "rework_count": observation["rework_count"],
                    },
                    confidence=observation["review_confidence"],
                )

            # ── 7. UPDATE (persist chapter + memory extraction) ────────
            async with self.tracer.trace_step(
                "director.update", "update",
                input_summary="Persist chapter and update Novel Brain",
            ) as step:
                update_result = await self._update(
                    chapter_number, generation, observation, run_id=run_id
                )
                step.set_output(
                    f"states applied={update_result['states_applied']} "
                    f"pending={update_result['states_pending_review']}",
                    data=update_result,
                )

            result = {
                "run_id": str(run_id),
                "chapter_number": chapter_number,
                "status": "completed" if observation["passed_review"] else "needs_review",
                "title": generation["title"],
                "content": generation["text"],
                "word_count": generation["word_count"],
                "meets_target": generation["meets_target"],
                "deai": generation["deai"],
                "scene_plan": generation["scene_plan"],
                "review_score": observation["review_score"],
                "dimension_scores": observation["dimension_scores"],
                "generation_quality": generation.get("generation_quality") or {},
                "reader_chapter_budget": reader_budget,
                "reader_experience": observation.get("reader_experience", {}),
                "issues": observation.get("issues", []),
                "strengths": observation.get("strengths", []),
                "quality_gate": observation.get("quality_gate", {}),
                "audit_report": observation.get("audit_report", {}),
                "review_provenance": observation.get("review_provenance", {}),
                "review_evidence": observation.get("review_evidence", {}),
                "review_hold": observation.get("review_hold", False),
                "review_validation": observation.get("review_validation", []),
                "passed_review": observation["passed_review"],
                "rework_count": observation["rework_count"],
                "memory": {
                    "states_applied": update_result["states_applied"],
                    "states_pending_review": update_result["states_pending_review"],
                    "states_discarded": update_result["states_discarded"],
                    "conflicts_found": update_result["conflicts_found"],
                },
                "transition_contract": update_result.get("transition_contract", {}),
                "continuity": update_result.get("continuity", {}),
                "writing_workflow": generation.get("writing_workflow") or {},
                "external_evaluation": (generation.get("writing_workflow") or {}).get("external_evaluation") or {},
                "final_continuity_audit": {
                    "continuity": update_result.get("continuity", {}),
                },
                "rule_learning": update_result.get("rule_learning", []),
                "v6_content": update_result.get("v6_content"),
                "v6_content_id": (update_result.get("v6_content") or {}).get("content_id"),
                "steps_executed": list(AGENT_LOOP_STEPS),
                "usage": generation["usage"],
            }

            # The chapter body is persisted as a story state and rejected
            # drafts remain visible in V6 as needs_rewrite; keep the run
            # output_data lean so v7_agent_runs stays queryable.
            run_output = {k: v for k, v in result.items() if k not in ("content", "scene_plan")}
            run_stats = await self.tracer.complete_run(run_id, output_data=run_output)
            result["run_stats"] = run_stats

            await self.event_bus.publish(
                "chapter_generated",
                f"Chapter {chapter_number} generated",
                "generation",
                source="director",
                source_run_id=run_id,
                event_data={
                    "chapter_number": chapter_number,
                    "word_count": generation["word_count"],
                    "review_score": observation["review_score"],
                    "passed_review": observation["passed_review"],
                    "run_id": str(run_id),
                    "title": generation["title"],
                },
            )

            return result

        except Exception as e:
            await self.tracer.complete_run(
                run_id,
                error_message=str(e),
                error_type=type(e).__name__,
            )
            raise

    # ── step 1 ──────────────────────────────────────────────────────────
    async def _perceive(self, chapter_number: int) -> dict[str, Any]:
        overview = await self.brain.get_overview()
        progress_state = await self.brain.state.get_state("global", "story_progress")
        progress = (progress_state or {}).get("value") or {}
        pending_states = await self.brain.state.get_pending_review(limit=20)
        previous = await self.generation_engine.context_assembler.load_previous_chapters(
            chapter_number, count=1, include_rejected=True
        )
        blocked_by_quality = None
        batch_mode = bool(self.generation_metadata.get("batch_id"))
        if previous and previous[-1].get("passed_review") is False and not batch_mode:
            blocked_by_quality = (
                f"第{previous[-1].get('chapter_number')}章未通过质量门禁，"
                "必须先完成返工，不能继续生成下一章"
            )

        return {
            "chapter_number": chapter_number,
            "state_total": overview.get("states", {}).get("total", 0),
            "pending_review": overview.get("states", {}).get("pending_review", 0),
            "goals": overview.get("goals", {}),
            "constraints": overview.get("constraints", {}),
            "last_chapter": progress.get("last_chapter"),
            "chapters_done": progress.get("chapter_count", 0),
            "total_words": progress.get("total_words", 0),
            "pending_review_keys": [
                s.get("key") for s in (pending_states or [])
            ][:20],
            "has_previous_chapter": bool(previous),
            "provisional_previous_chapter": bool(
                batch_mode and previous and previous[-1].get("passed_review") is False
            ),
            "blocked_by_quality": blocked_by_quality,
            "truth_domains": await self.brain.truth.digest(),
        }

    # ── step 2 ──────────────────────────────────────────────────────────
    async def _assess(
        self,
        chapter_number: int,
        outline: str | None,
        perception: dict[str, Any],
    ) -> dict[str, Any]:
        # Pre-generation assessment: the plot engine looks at goals, open
        # threads and the previous chapter node, then reports how safe it is to
        # auto-generate this chapter. It runs the full 5-phase engine contract
        # so the plot tree is actually written before we start writing prose.
        plot_run = await self.plot_engine.run(
            {
                "chapter_number": chapter_number,
                "outline": outline or "",
                "perception": perception,
                "mode": "assess",
            }
        )

        phases = plot_run.metadata.get("phases") or {}
        analyze_phase = phases.get("analyze") or {}
        assessment_data = analyze_phase.get("result") or {}
        execute_phase = phases.get("execute") or {}

        gaps: list[str] = list(assessment_data.get("signals") or [])
        blockers: list[str] = list(assessment_data.get("blockers") or [])

        # The gate must key off the assessment's own confidence, not the
        # confidence of the last phase (which only reflects state persistence).
        confidence = float(analyze_phase.get("confidence") or 0.0)
        plot_failure_reason = plot_run.reason or ""
        retryable_planning_failure = bool(
            not plot_run.success
            and is_retryable_provider_failure(plot_failure_reason)
        )
        if not plot_run.success:
            blockers.append(plot_failure_reason or "plot engine pipeline failed")
            confidence = min(confidence, 0.4)

        return {
            "plot_success": bool(plot_run.success),
            "plot_result": assessment_data,
            "plot_execute": execute_phase.get("result"),
            "plot_warnings": list(plot_run.warnings or []),
            "must_accomplish": assessment_data.get("must_accomplish") or [],
            "suggested_beats": assessment_data.get("suggested_beats") or [],
            "chapter_title_hint": assessment_data.get("chapter_title_hint"),
            "payoff_contract": assessment_data.get("payoff_contract") or {},
            "writing_workflow": build_writing_workflow_contract(
                chapter_number,
                plot_brief=assessment_data,
            ),
            "gaps": gaps,
            "blockers": blockers,
            "retryable_planning_failure": retryable_planning_failure,
            # A bootstrap first chapter is allowed to start when the writer
            # received a substantive creative brief.  The confidence number
            # still remains auditable; this is a narrowly scoped policy for
            # starting from a blank novel, while the independent quality gate
            # below remains fail-closed.
            "context_ready": len((outline or "").strip()) >= 200,
            "confidence": round(confidence, 3),
            "usage": plot_run.metadata.get("usage"),
            "summary": (
                f"plot={'ok' if plot_run.success else 'failed'}, "
                f"objectives={len(assessment_data.get('must_accomplish') or [])}, "
                f"gaps={len(gaps)}, blockers={len(blockers)}, "
                f"confidence={confidence:.2f}"
            ),
        }

    # ── step 3 ──────────────────────────────────────────────────────────
    async def _decide(
        self,
        chapter_number: int,
        assessment: dict[str, Any],
        *,
        run_id: uuid.UUID,
    ) -> dict[str, Any]:
        gate = await self.permission_system.evaluate(
            "chapter_plan", assessment["confidence"]
        )

        confidence_block = (
            not gate["allowed"]
            and gate["level"] in {"auto", "notify"}
            and str(gate.get("blocked_reason") or "").startswith("confidence ")
        )
        batch_observation_mode = bool(
            getattr(self, "generation_metadata", {}).get("batch_id")
        )

        # Do not strand a new novel before the first word is written merely
        # because a model conservatively scores an otherwise complete brief
        # below the generic confidence threshold.  This does not bypass an
        # explicit human-only permission level or a structural blocker; it
        # only lets a context-complete first chapter reach the independent
        # prose quality gate.
        if (
            confidence_block
            and chapter_number == 1
            and assessment.get("plot_success")
            and assessment.get("context_ready")
            and not (assessment.get("blockers") or [])
        ):
            gate = {
                **gate,
                "allowed": True,
                "blocked_reason": None,
                "policy_override": "first_chapter_context_complete",
            }

        # Ordered 20-chapter diagnostics need to observe the actual prose
        # quality even while PlotEngine is still warming up its state. This
        # exception is narrower than the first-chapter bootstrap rule: it is
        # batch-only, requires a successful plot assessment and no structural
        # blockers, and still sends the resulting prose through every V7
        # generation/review/continuity gate. Explicit approve/forbidden
        # permissions never enter this branch.
        elif (
            confidence_block
            and batch_observation_mode
            and assessment.get("plot_success")
            and not (assessment.get("blockers") or [])
            and float(assessment.get("confidence") or 0.0)
            >= BATCH_AUTOGENERATION_CONFIDENCE_FLOOR
        ):
            gate = {
                **gate,
                "allowed": True,
                "blocked_reason": None,
                "policy_override": "batch_quality_observation",
                "confidence_floor": BATCH_AUTOGENERATION_CONFIDENCE_FLOOR,
                "confidence_warning": (
                    f"planning confidence {assessment.get('confidence'):.2f} is below "
                    f"the normal threshold {gate['threshold']:.2f}; prose quality gates remain required"
                    if float(assessment.get("confidence") or 0.0) < float(gate["threshold"])
                    else None
                ),
            }

        # `decision` is a short verb column (varchar 50) — the human-readable
        # explanation belongs in decision_reason / context.
        if not gate["allowed"]:
            decision = await self.brain.record_decision(
                "chapter_plan",
                "escalate",
                decision_reason=(
                    f"Chapter {chapter_number} blocked: {gate['blocked_reason']}"
                ),
                confidence=assessment["confidence"],
                permission_level=gate["level"],
                status="pending",
                run_id=run_id,
                decided_by="ai",
                context={
                    "chapter_number": chapter_number,
                    "gaps": assessment["gaps"],
                    "blockers": assessment.get("blockers") or [],
                    "threshold": gate["threshold"],
                },
            )
            return {**gate, "decision_id": decision["id"]}

        decision = await self.brain.record_decision(
            "chapter_plan",
            "approve",
            decision_reason=(
                (
                    f"Chapter {chapter_number} approved to reach the strict quality gate: "
                    + (
                        "first-chapter creative brief is complete; generic confidence "
                        "override is recorded and does not waive post-generation review"
                        if gate.get("policy_override") == "first_chapter_context_complete"
                        else (
                            "batch quality-observation mode is recorded; confidence "
                            f"floor is {BATCH_AUTOGENERATION_CONFIDENCE_FLOOR:.2f} and "
                            "post-generation review remains strict"
                            + (
                                f"; warning: {gate['confidence_warning']}"
                                if gate.get("confidence_warning")
                                else ""
                            )
                        )
                    )
                )
                if gate.get("policy_override")
                else (
                    f"Chapter {chapter_number} approved for auto-generation: "
                    f"confidence {assessment['confidence']:.2f} >= "
                    f"threshold {gate['threshold']:.2f}, permission={gate['level']}"
                )
            ),
            confidence=assessment["confidence"],
            permission_level=gate["level"],
            status="completed",
            run_id=run_id,
            decided_by="ai",
            context={
                "chapter_number": chapter_number,
                "gaps": assessment["gaps"],
                "policy_override": gate.get("policy_override"),
                "confidence_floor": gate.get("confidence_floor"),
                "confidence_warning": gate.get("confidence_warning"),
            },
        )
        return {**gate, "decision_id": decision["id"]}

    # ── step 4 ──────────────────────────────────────────────────────────
    async def _plan(
        self,
        chapter_number: int,
        prompt: str | None,
        outline: str | None,
        assessment: dict[str, Any],
        perception: dict[str, Any],
        *,
        target_word_count: int = 3000,
    ) -> dict[str, Any]:
        goals = await self.brain.goals.list_goals(limit=50)
        goals_to_advance = [
            {"id": g["id"], "name": g["name"], "progress": g.get("progress")}
            for g in goals
            if g.get("status") in ("in_progress", "pending")
        ][:5]

        constraints = await self.brain.constraints.list_constraints(limit=50)
        constraints_to_respect = [
            {"name": c["name"], "severity": c["severity"]} for c in constraints
        ]

        return {
            "chapter_number": chapter_number,
            "prompt": prompt,
            "outline": outline,
            "goals_to_advance": goals_to_advance,
            "constraints_to_respect": constraints_to_respect,
            "target_word_count": target_word_count,
            "plot_hint": assessment.get("plot_result"),
            "plot_brief": {
                "must_accomplish": assessment.get("must_accomplish") or [],
                "suggested_beats": assessment.get("suggested_beats") or [],
                "chapter_title_hint": assessment.get("chapter_title_hint"),
                "tension_target": (assessment.get("plot_result") or {}).get(
                    "tension_target"
                ),
                "pacing_advice": (assessment.get("plot_result") or {}).get(
                    "pacing_advice"
                ),
                "risks": (assessment.get("plot_result") or {}).get("risks") or [],
                "reader_promise": assessment.get("reader_promise") or (assessment.get("plot_result") or {}).get("reader_promise"),
                "emotional_target": assessment.get("emotional_target") or (assessment.get("plot_result") or {}).get("emotional_target"),
                "opening_anchor": assessment.get("opening_anchor") or (assessment.get("plot_result") or {}).get("opening_anchor"),
                "hook": (assessment.get("plot_result") or {}).get("hook") or "",
                "payoff_contract": assessment.get("payoff_contract") or (assessment.get("plot_result") or {}).get("payoff_contract") or {},
                "chapter_contract": (assessment.get("plot_result") or {}).get("chapter_contract") or {},
                "causal_ledger": (assessment.get("plot_result") or {}).get("causal_ledger") or [],
                "state_delta": (assessment.get("plot_result") or {}).get("state_delta") or {},
                "writing_workflow": assessment.get("writing_workflow") or {},
            },
            "writing_workflow": assessment.get("writing_workflow") or {},
            "quality_profile": quality_profile_metadata(self.quality_profile),
            "status": "planned",
        }

    # ── step 5 is generation_engine.generate_chapter ────────────────────

    @staticmethod
    def _can_use_local_prose_repair(gate: dict[str, Any]) -> bool:
        """Allow the fast path only when all failures are expression-local."""
        failures = [
            item for item in (gate.get("failures") or [])
            if isinstance(item, dict)
        ]
        if not failures:
            return False
        return all(
            str(item.get("dimension") or "") in LOCAL_PROSE_REPAIR_DIMENSIONS
            for item in failures
        )

    # ── step 6 ──────────────────────────────────────────────────────────
    async def _observe(
        self,
        chapter_number: int,
        generation: dict[str, Any],
        *,
        allow_rework: bool,
        max_reworks: int | None,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        def review_input(current: dict[str, Any]) -> dict[str, Any]:
            metrics = (current.get("deai") or {}).get("metrics") or {}
            context = current.get("context") or {}
            previous_titles = context.get("previous_titles") or []
            return {
                "chapter_text": current["text"],
                "chapter_number": chapter_number,
                "previous_chapter_tail": context.get("previous_tail", ""),
                "previous_transition_contract": context.get(
                    "previous_transition_contract", {}
                ),
                "active_rules": context.get("active_rules") or [],
                "chapter_title": current.get("title") or "",
                "previous_chapter_title": previous_titles[-1] if previous_titles else "",
                "chapter_plan": plan.get("plot_brief") or {},
                "scene_plan": current.get("scene_plan") or {},
                "deai_metrics": metrics.get("after") or metrics,
                "pov_metrics": current.get("pov_metrics") or (current.get("generation_quality") or {}).get("pov_metrics") or {},
                "content_policy": current.get("content_policy") or (current.get("generation_quality") or {}).get("content_policy") or {},
                "generation_quality": current.get("generation_quality") or {},
                "quality_profile": current.get("quality_profile") or quality_profile_metadata(self.quality_profile),
                "payoff_contract": current.get("payoff_contract") or (plan.get("plot_brief") or {}).get("payoff_contract") or {},
                "writing_workflow": current.get("writing_workflow") or plan.get("writing_workflow") or (plan.get("plot_brief") or {}).get("writing_workflow") or {},
            }

        review = await self.review_engine.run(review_input(generation))
        review_data = review.result or {}
        review_data["chapter_text"] = generation.get("text", "")
        review_hold = False
        if not review.success:
            # Provider output/schema failures must not strand a generated
            # chapter as a Celery exception. Keep the real draft, expose the
            # failed review contract, and let the product mark it needs_review.
            raw = review_data.get("raw") if isinstance(review_data, dict) else {}
            raw = raw if isinstance(raw, dict) else {}
            validation_failures = review_data.get("validation_failures") or []
            if not validation_failures:
                validation_failures = [{
                    "code": "review_execution_failed",
                    "message": review.reason or "审稿输出未通过校验",
                }]
            review_data = {
                **raw,
                **review_data,
                "chapter_number": chapter_number,
                "overall_score": float(raw.get("overall_score") or 0.0),
                "dimension_scores": raw.get("dimension_scores") or {},
                "reader_experience": raw.get("reader_experience") or {},
                "issues": raw.get("issues") or [],
                "constraint_violations": raw.get("constraint_violations") or [],
                "audit_report": raw.get("audit_report") or {},
                "quality_profile": generation.get("quality_profile") or quality_profile_metadata(self.quality_profile),
                "payoff_contract": generation.get("payoff_contract") or (plan.get("plot_brief") or {}).get("payoff_contract") or {},
                "payoff_evidence": raw.get("payoff_evidence") or [],
                "validation_failures": validation_failures,
            }
            review_hold = True
        elif review_data.get("review_valid") is False:
            # The ReviewEngine keeps a structured but invalid contract alive
            # so the director can persist it as a review hold instead of
            # confusing it with a transport failure.
            review_hold = True
        score = float(review_data.get("overall_score") or 0.0)
        rework_count = 0  # 完整重写次数，计入MAX_REWORKS配额
        rework_limit = (
            MAX_REWORKS
            if max_reworks is None
            else max(0, min(MAX_REWORKS, int(max_reworks)))
        )
        local_repair_count = 0  # P2-1 质量整改：本地修复次数，不计入MAX_REWORKS配额
        force_full_rework = False

        # ── 一致性检查 ──────────────────────────────────────────────
        consistency_result = None
        consistency_failed = False
        if not review_hold:
            try:
                gen_context = generation.get("context") or {}
                consistency_result = await self.consistency_checker.check(
                    chapter_text=generation.get("text", ""),
                    chapter_number=chapter_number,
                    core_settings=plan.get("outline") or plan.get("prompt") or "",
                    chapter_outline=plan.get("plot_brief") or {},
                    previous_chapter_tail=gen_context.get("previous_tail", ""),
                    previous_transition_contract=gen_context.get(
                        "previous_transition_contract", {}
                    ),
                    scene_plan=generation.get("scene_plan") or {},
                    active_rules=gen_context.get("active_rules") or [],
                    chapter_title=generation.get("title") or "",
                    previous_chapter_title=(gen_context.get("previous_titles") or [""])[-1],
                )
                # 把一致性检查结果加到 review_data 中
                review_data["consistency_check"] = consistency_result.to_dict()
                consistency_failed = not consistency_result.passed

                # 把一致性问题加到 issues 中
                if consistency_result.issues:
                    consistency_issues = [
                        {
                            "dimension": f"一致性-{issue.get('type', '其他')}",
                            "severity": issue.get("severity", "轻微"),
                            "description": issue.get("description", ""),
                            "suggestion": issue.get("suggestion", ""),
                        }
                        for issue in consistency_result.issues
                    ]
                    existing_issues = review_data.get("issues") or []
                    review_data["issues"] = existing_issues + consistency_issues
            except Exception as exc:  # noqa: BLE001 - convert to a visible gate failure
                consistency_result = ConsistencyCheckResult(
                    passed=False,
                    score=0.0,
                    issues=[{
                        "type": "审阅执行",
                        "severity": "严重",
                        "location": "一致性检查",
                        "description": f"一致性检查异常，结果未验证：{type(exc).__name__}",
                        "suggestion": "修复一致性检查链路后重新执行",
                    }],
                    summary="一致性检查异常，不能放行",
                )
                review_data["consistency_check"] = consistency_result.to_dict()
                consistency_failed = True

        # Rework is bounded, but the gate is not fail-open: after the retry
        # budget is exhausted the chapter remains needs_review and never enters
        # the V6 library as a reviewed chapter.
        # P2-1 质量整改：本地修复不计入MAX_REWORKS配额，单独计数
        while (
            allow_rework
            and not review_hold
            and (
                not evaluate_review(review_data, project_id=self.project_id, user_id=self.user_id)["passed"]
                or consistency_failed  # 一致性检查不通过也触发重写
            )
            and rework_count < rework_limit
            and (local_repair_count < MAX_LOCAL_REPAIRS or force_full_rework)
            and await self.permission_system.can_auto_decide("chapter_rework", 0.9)
        ):
            gate = evaluate_review(review_data, project_id=self.project_id, user_id=self.user_id)
            issues = review_data.get("issues") or []
            failures = "；".join(
                _format_quality_failure(item)
                for item in gate["failures"][:8]
                if isinstance(item, dict)
            )
            issue_text = "；".join(
                (
                    f"{i.get('dimension') or i.get('type') or '问题'}: {i.get('description')}; "
                    f"建议：{i.get('suggestion') or '直接修复该问题'}"
                    if isinstance(i, dict)
                    else str(i)
                )
                for i in issues[:8]
            )
            repair_feedback = "；".join(
                str(item) for item in (gate.get("quality_repair_contract") or {}).get("required_repair_feedback") or []
            )
            # 加上一致性问题
            consistency_feedback = ""
            if consistency_failed and consistency_result:
                consistency_feedback = f"一致性检查未通过（{consistency_result.score}分）：" + format_consistency_issues(consistency_result.issues)
            feedback = f"质量门禁未通过：{failures}。{issue_text}。{repair_feedback}。{consistency_feedback}".strip("；。")
            gate_for_rework = evaluate_review(review_data, project_id=self.project_id, user_id=self.user_id)
            use_local_repair = (
                not force_full_rework
                and local_repair_count < MAX_LOCAL_REPAIRS
                and self._can_use_local_prose_repair(gate_for_rework)
            )
            if use_local_repair:
                local_repair_count += 1
                await self.brain.record_decision(
                    "chapter_rework",
                    "local_repair",
                    decision_reason=(
                        f"Chapter {chapter_number} local repair {local_repair_count}/{MAX_LOCAL_REPAIRS} (不计入重写配额); "
                        f"score {score:.1f}, rework threshold {QUALITY_REWORK_SCORE:.0f}: {feedback}"
                    ),
                    confidence=0.85,
                    permission_level="notify",
                    status="completed",
                    decided_by="ai",
                )
                try:
                    generation = await self.generation_engine.repair_local_quality(
                        generation,
                        feedback=feedback,
                    )
                    # If the focused repair does not pass, the next bounded
                    # attempt must be a fresh scene rewrite rather than
                    # repeatedly polishing the same failed text.
                    force_full_rework = True
                except AIGatewayError:
                    # Preserve the old quality-preserving fallback: a
                    # transport/schema failure in the shortcut never turns a
                    # chapter into a false success.
                    force_full_rework = True
                    use_local_repair = False

            if not use_local_repair:
                rework_count += 1
                await self.brain.record_decision(
                    "chapter_rework",
                    "full_rework",
                    decision_reason=(
                        f"Chapter {chapter_number} full rewrite {rework_count}/{MAX_REWORKS}; "
                        f"score {score:.1f}, rework threshold {QUALITY_REWORK_SCORE:.0f}: {feedback}"
                    ),
                    confidence=0.85,
                    permission_level="notify",
                    status="completed",
                    decided_by="ai",
                )
                generation = await self.generation_engine.generate_chapter(
                    chapter_number,
                    prompt=(plan.get("prompt") or "")
                    + "\n\n【严格重写任务】\n"
                    + f"上一稿未通过严格质量门禁（{score:.1f}分），必须修复：{feedback}\n"
                    + "下面是上一稿正文。请保留已经成立的人物、地点、时间线和因果事实，"
                    + "不要只做同义词替换；要重排场景动作、对白和信息揭示，使冲突真正推进，"
                    + "并让章末钩子落到具体动作/发现/选择上。\n"
                    + f"【上一稿正文】\n{generation.get('text', '')[:16000]}",
                    outline=plan.get("outline"),
                    target_word_count=plan["target_word_count"],
                    plot_brief=plan.get("plot_brief"),
                )
            review = await self.review_engine.run(review_input(generation))
            if not review.success:
                raw = review.result.get("raw") if isinstance(review.result, dict) else {}
                raw = raw if isinstance(raw, dict) else {}
                review_data = {
                    **raw,
                    **(review.result or {}),
                    "chapter_number": chapter_number,
                    "overall_score": float(raw.get("overall_score") or 0.0),
                    "dimension_scores": raw.get("dimension_scores") or {},
                    "reader_experience": raw.get("reader_experience") or {},
                    "issues": raw.get("issues") or [],
                    "constraint_violations": raw.get("constraint_violations") or [],
                    "audit_report": raw.get("audit_report") or {},
                    "quality_profile": generation.get("quality_profile") or quality_profile_metadata(self.quality_profile),
                    "payoff_contract": generation.get("payoff_contract") or (plan.get("plot_brief") or {}).get("payoff_contract") or {},
                    "payoff_evidence": raw.get("payoff_evidence") or [],
                    "validation_failures": (review.result or {}).get("validation_failures") or [{
                        "code": "review_execution_failed",
                        "message": review.reason or "重写后的审稿输出未通过校验",
                    }],
                }
                review_hold = True
                score = 0.0
                break
            review_data = review.result or {}
            review_data["chapter_text"] = generation.get("text", "")
            score = float(review_data.get("overall_score") or 0.0)

            # 重写后重新做一致性检查
            if not review_hold:
                try:
                    gen_context = generation.get("context") or {}
                    consistency_result = await self.consistency_checker.check(
                        chapter_text=generation.get("text", ""),
                        chapter_number=chapter_number,
                        core_settings=plan.get("outline") or plan.get("prompt") or "",
                        chapter_outline=plan.get("plot_brief") or {},
                        previous_chapter_tail=gen_context.get("previous_tail", ""),
                        previous_transition_contract=gen_context.get(
                            "previous_transition_contract", {}
                        ),
                        scene_plan=generation.get("scene_plan") or {},
                        active_rules=gen_context.get("active_rules") or [],
                        chapter_title=generation.get("title") or "",
                        previous_chapter_title=(gen_context.get("previous_titles") or [""])[-1],
                    )
                    review_data["consistency_check"] = consistency_result.to_dict()
                    consistency_failed = not consistency_result.passed

                    # 把一致性问题加到 issues 中
                    if consistency_result.issues:
                        consistency_issues = [
                            {
                                "dimension": f"一致性-{issue.get('type', '其他')}",
                                "severity": issue.get("severity", "轻微"),
                                "description": issue.get("description", ""),
                                "suggestion": issue.get("suggestion", ""),
                            }
                            for issue in consistency_result.issues
                        ]
                        existing_issues = review_data.get("issues") or []
                        review_data["issues"] = existing_issues + consistency_issues
                except Exception as exc:  # noqa: BLE001 - convert to a visible gate failure
                    consistency_result = ConsistencyCheckResult(
                        passed=False,
                        score=0.0,
                        issues=[{
                            "type": "审阅执行",
                            "severity": "严重",
                            "location": "一致性检查",
                            "description": f"一致性检查异常，结果未验证：{type(exc).__name__}",
                            "suggestion": "修复一致性检查链路后重新执行",
                        }],
                        summary="一致性检查异常，不能放行",
                    )
                    review_data["consistency_check"] = consistency_result.to_dict()
                    consistency_failed = True

        gate = evaluate_review(review_data, project_id=self.project_id, user_id=self.user_id)
        validation_failures = review_data.get("validation_failures") or []
        if validation_failures:
            for failure in validation_failures:
                if not isinstance(failure, dict):
                    continue
                gate["failures"].append({
                    "dimension": str(failure.get("code") or "review_validation"),
                    "actual": "invalid",
                    "minimum": "valid",
                    "reason": str(failure.get("message") or "审稿契约校验失败"),
                })
            gate["passed"] = False
        if consistency_failed:
            gate["failures"].append({
                "dimension": "cross_chapter_consistency",
                "actual": consistency_result.score if consistency_result else "unverified",
                "minimum": CONSISTENCY_PASS_SCORE if consistency_result else "verified",
                "reason": (
                    (consistency_result.summary if consistency_result else "一致性检查未执行")
                    or "跨章一致性未通过"
                ),
            })
            gate["passed"] = False
        blocking = gate["blocking_violations"]
        passed = gate["passed"]

        await self.event_bus.publish(
            "review_completed",
            f"Chapter {chapter_number} reviewed",
            "review",
            source="director",
            event_data={
                "chapter_number": chapter_number,
                "overall_score": score,
                "dimension_scores": review_data.get("dimension_scores", {}),
                "reader_experience": review_data.get("reader_experience", {}),
                "blocking_violations": blocking,
            },
        )

        issues = list(review_data.get("issues") or [])
        if validation_failures:
            issues.extend(
                {
                    "dimension": str(item.get("code") or "review_validation"),
                    "severity": "high",
                    "description": str(item.get("message") or "审稿契约校验失败"),
                    "suggestion": "重新执行审稿并确认评分、33维审计和爽点证据均可验证",
                }
                for item in validation_failures
                if isinstance(item, dict)
            )
        if consistency_failed:
            issues.append({
                "dimension": "cross_chapter_consistency",
                "severity": "high",
                "description": (
                    consistency_result.summary
                    if consistency_result
                    else "一致性检查未执行，结果未验证"
                ),
                "suggestion": "修复跨章承接或一致性检查链路后重新提交",
            })
        review_data["issues"] = issues

        if not passed:
            await self.brain.record_decision(
                "chapter_quality",
                "needs_human_attention",
                decision_reason=(
                    f"quality gate failures: {gate['failures']}"
                ),
                confidence=review.confidence,
                permission_level="notify",
                status="completed",
                decided_by="ai",
            )

        return {
            "generation": generation,
            "review_score": score,
            "dimension_scores": review_data.get("dimension_scores", {}),
            "reader_experience": review_data.get("reader_experience", {}),
            "issues": review_data.get("issues", []),
            "strengths": review_data.get("strengths", []),
            "constraint_violations": review_data.get("constraint_violations", []),
            "audit_report": review_data.get("audit_report", {}),
            "review_provenance": review_data.get("provenance", {}),
            "review_evidence": review_data.get("review_evidence") or {},
            "blocking_violations": blocking,
            "passed_review": passed,
            "rework_count": rework_count,
            "review_confidence": review.confidence,
            "quality_gate": gate,
            "review_hold": review_hold,
            "review_validation": validation_failures,
            "consistency_check": review_data.get("consistency_check") or {},
            "pov_metrics": review_data.get("pov_metrics") or generation.get("pov_metrics") or {},
            "content_policy": review_data.get("content_policy") or generation.get("content_policy") or {},
            "payoff_contract": review_data.get("payoff_contract") or generation.get("payoff_contract") or {},
            "payoff_evidence": review_data.get("payoff_evidence") or [],
        }

    # ── step 7 ──────────────────────────────────────────────────────────
    async def _update(
        self,
        chapter_number: int,
        generation: dict[str, Any],
        observation: dict[str, Any],
        *,
        run_id: uuid.UUID,
    ) -> dict[str, Any]:
        memory = await self.memory_engine.run(
            {
                "chapter_text": generation["text"],
                "chapter_number": chapter_number,
                "run_id": str(run_id),
                # Extract first; commit to Novel Brain only after the review
                # and continuity gates have accepted the final text.
                "apply_updates": False,
            }
        )
        memory_data = memory.result or {}
        summary = memory_data.get("chapter_summary") or ""
        memory_items = memory_data.get("valid_items") or memory_data.get("extracted_items") or []

        if not memory.success:
            observation["passed_review"] = False
            quality_gate = observation.get("quality_gate") or {}
            quality_gate["passed"] = False
            quality_gate["failures"] = [
                *(quality_gate.get("failures") or []),
                {
                    "dimension": "memory_state_extraction",
                    "actual": "failed",
                    "minimum": "succeeded",
                    "reason": memory.reason or "章节状态抽取失败，不能更新真相状态",
                },
            ]
            observation["quality_gate"] = quality_gate
            observation["issues"] = [
                *(observation.get("issues") or []),
                {
                    "dimension": "continuity",
                    "severity": "high",
                    "description": memory.reason or "章节状态抽取失败",
                    "suggestion": "修复状态抽取后重新提交，不能让未确认状态进入后续章节",
                },
            ]

        transition_contract = build_transition_contract(
            chapter_number=chapter_number,
            title=generation["title"],
            text=generation["text"],
            summary=summary,
            word_count=generation["word_count"],
            review_score=observation["review_score"],
            dimension_scores=observation["dimension_scores"],
            reader_experience=observation.get("reader_experience"),
            payoff_contract=generation.get("payoff_contract") or {},
            payoff_evidence=observation.get("payoff_evidence") or [],
            previous_context=generation.get("context"),
            memory_items=memory_items,
            constraints=(generation.get("context") or {}).get("constraints") or [],
            state_conflicts=memory_data.get("conflicts") or [],
        )
        continuity = validate_transition_contract(
            transition_contract,
            chapter_number=chapter_number,
            previous_contract=(generation.get("context") or {}).get(
                "previous_transition_contract", {}
            ),
            state_conflicts=memory_data.get("conflicts") or [],
        )
        previous_contract = (generation.get("context") or {}).get(
            "previous_transition_contract", {}
        ) or {}
        previous_titles = (generation.get("context") or {}).get("previous_titles") or []
        prose_continuity = validate_prose_continuity(
            chapter_number=chapter_number,
            current_text=generation.get("text") or "",
            current_title=generation.get("title") or "",
            previous_title=previous_titles[-1] if previous_titles else "",
            current_contract=transition_contract,
            previous_contract=previous_contract,
        )
        continuity["prose_continuity"] = prose_continuity
        if not prose_continuity.get("passed"):
            continuity["passed"] = False
            continuity["issues"] = [
                *(continuity.get("issues") or []),
                *(prose_continuity.get("issues") or []),
            ]
        consistency_evidence = observation.get("consistency_check") or {}
        if consistency_evidence.get("passed") is not True and chapter_number > 1:
            continuity["passed"] = False
            continuity["issues"] = [
                *(continuity.get("issues") or []),
                {
                    "code": "consistency_unverified",
                    "severity": "high",
                    "message": str(
                        consistency_evidence.get("summary")
                        or "跨章一致性检查未通过或未验证"
                    ),
                },
            ]
        continuity.update({
            "status": "continuous" if continuity.get("passed") else "broken",
            "checked": True,
            "source": "v7.transition_contract",
            "gaps": continuity.get("issues") or [],
            "narrative_flow": (
                "V7 转场契约已检查章节号、上一章承接、状态变化和下一章桥接。"
                if continuity.get("passed")
                else "V7 转场契约发现：" + "；".join(
                    str(item.get("message") or "连续性缺口")
                    for item in (continuity.get("issues") or [])[:5]
                )
            ),
        })
        transition_contract["continuity"] = continuity
        # Rebuild the one V7 evidence read model after the durable transition
        # contract exists.  Review-time evidence can prove the model audit;
        # only this update step can prove the chapter-to-chapter hand-off.
        review_evidence = validate_review_evidence(
            {
                "canonical_engine": "v7",
                "dimension_scores": observation.get("dimension_scores") or {},
                "reader_experience": observation.get("reader_experience") or {},
                "audit_report": observation.get("audit_report") or {},
                "provenance": observation.get("review_provenance") or {},
                "continuity": continuity,
            },
            require_continuity=True,
        )
        observation["review_evidence"] = review_evidence
        if not review_evidence.get("passed"):
            observation["passed_review"] = False
            quality_gate = observation.get("quality_gate") or {}
            quality_gate["passed"] = False
            quality_gate["failures"] = [
                *(quality_gate.get("failures") or []),
                {
                    "dimension": "review_evidence_incomplete",
                    "actual": review_evidence.get("missing") or "unknown",
                    "minimum": "complete",
                    "reason": "；".join(review_evidence.get("issues") or ["V7 审阅证据链不完整"]),
                },
            ]
            observation["quality_gate"] = quality_gate
            observation["issues"] = [
                *(observation.get("issues") or []),
                {
                    "dimension": "review_evidence_incomplete",
                    "severity": "high",
                    "description": "；".join(review_evidence.get("issues") or ["V7 审阅证据链不完整"]),
                    "suggestion": "重新执行 V7 审阅，补齐 33 维逐项证据、连续性和审阅溯源",
                },
            ]
        if not continuity["passed"]:
            # This is an application hard gate.  A high model score cannot
            # make a chapter publishable when its durable hand-off is broken.
            observation["passed_review"] = False
            quality_gate = observation.get("quality_gate") or {}
            quality_gate["passed"] = False
            quality_gate["continuity"] = continuity
            quality_gate["failures"] = [
                *(quality_gate.get("failures") or []),
                *[
                    {
                        "dimension": "continuity",
                        "actual": item["severity"],
                        "minimum": "resolved",
                        "reason": item["message"],
                    }
                    for item in continuity["issues"]
                    if item["severity"] == "high"
                ],
            ]
            observation["quality_gate"] = quality_gate
            observation["issues"] = [
                *(observation.get("issues") or []),
                *[
                    {
                        "dimension": "continuity",
                        "severity": item["severity"],
                        "description": item["message"],
                        "suggestion": "补齐上一章承接、状态变化和下一章入口后再提交",
                    }
                    for item in continuity["issues"]
                ],
            ]

        workflow_seed = generation.get("writing_workflow") or {}
        scene_plan = generation.get("scene_plan") or {}
        methodology_workflow = build_writing_workflow_contract(
            chapter_number,
            context_layers=generation.get("context") or {},
            plot_brief={
                **scene_plan,
                "chapter_contract": workflow_seed.get("chapter_contract") or scene_plan.get("chapter_contract") or {},
                "causal_ledger": workflow_seed.get("causal_ledger") or scene_plan.get("causal_ledger") or [],
                "state_delta": workflow_seed.get("state_delta") or scene_plan.get("state_delta") or {},
            },
            scene_plan=scene_plan,
            chapter_text=generation.get("text") or "",
            review={
                "causal_passed": bool(continuity.get("passed")) and bool(
                    (consistency_evidence or {}).get("passed", True)
                ),
                "style_passed": bool(observation.get("passed_review")),
                "review_score": observation.get("review_score"),
            },
        )
        if not methodology_workflow["review"]["causal_passed"]:
            transition_workflow_status(methodology_workflow, "blocked")
        elif observation.get("passed_review"):
            transition_workflow_status(methodology_workflow, "external_pending")
        else:
            transition_workflow_status(methodology_workflow, "causal_passed")
        methodology_workflow["validation"] = validate_writing_workflow(methodology_workflow)
        generation["writing_workflow"] = methodology_workflow

        payoff_score = generation.get("payoff_score") or {}
        payoff_score_value = (
            payoff_score.get("score") if isinstance(payoff_score, dict) else payoff_score
        )
        quality_store = getattr(self.brain, "quality_learning", None)
        quality_learning = []
        if quality_store is not None:
            quality_learning = await quality_store.observe_sample(
                chapter_number=chapter_number,
                accepted=bool(observation["passed_review"]),
                payoff_type=(generation.get("payoff_contract") or {}).get("payoff_type"),
                payoff_score=payoff_score_value,
                review_score=observation.get("review_score"),
                reader_payoff=(observation.get("reader_experience") or {}).get("payoff"),
                continuity_passed=bool(continuity.get("passed")),
                source_run_id=run_id,
            )

        rule_learning = await self.brain.rules.observe(
            chapter_number=chapter_number,
            accepted=bool(observation["passed_review"]),
            deai_metrics=(generation.get("deai") or {}).get("metrics") or {},
            issues=observation.get("issues") or [],
            source_run_id=run_id,
        )

        # Commit extracted truth only after every quality gate has passed and
        # before the V6 boundary is allowed to call the chapter "reviewed".
        # Otherwise a database/state-write failure could leave an accepted V6
        # chapter behind a failed Novel Brain update.
        memory_update = memory
        if observation["passed_review"] and memory.success:
            try:
                async with self.db.begin_nested():
                    memory_update = await self.memory_engine.apply_validated_items(
                        {
                            **memory_data,
                            "valid_items": memory_items,
                            "chapter_number": chapter_number,
                            "run_id": str(run_id),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - convert to a visible gate failure
                observation["passed_review"] = False
                memory_update = EngineResult(
                    success=False,
                    reason=f"accepted chapter state write failed: {type(exc).__name__}",
                )
                quality_gate = observation.get("quality_gate") or {}
                quality_gate["passed"] = False
                quality_gate["failures"] = [
                    *(quality_gate.get("failures") or []),
                    {
                        "dimension": "memory_state_write",
                        "actual": "failed",
                        "minimum": "succeeded",
                        "reason": memory_update.reason,
                    },
                ]
                observation["quality_gate"] = quality_gate
                observation["issues"] = [
                    *(observation.get("issues") or []),
                    {
                        "dimension": "continuity",
                        "severity": "high",
                        "description": memory_update.reason,
                        "suggestion": "状态写回成功后才能把章节标记为已完成",
                    },
                ]
            if observation["passed_review"] and not memory_update.success:
                observation["passed_review"] = False
                quality_gate = observation.get("quality_gate") or {}
                quality_gate["passed"] = False
                quality_gate["failures"] = [
                    *(quality_gate.get("failures") or []),
                    {
                        "dimension": "memory_state_write",
                        "actual": "failed",
                        "minimum": "succeeded",
                        "reason": memory_update.reason or "章节状态写回未完成",
                    },
                ]
                observation["quality_gate"] = quality_gate
                observation["issues"] = [
                    *(observation.get("issues") or []),
                    {
                        "dimension": "continuity",
                        "severity": "high",
                        "description": memory_update.reason or "章节状态写回未完成",
                        "suggestion": "状态写回成功后才能把章节标记为已完成",
                    },
                ]

        # Persist both outcomes at the product boundary.  A failed quality
        # gate stays fail-closed (`needs_rewrite`), but the author must still
        # be able to inspect the actual draft and repair evidence.
        if observation["passed_review"]:
            bridge = persist_accepted_v7_chapter
        elif observation.get("review_hold"):
            bridge = persist_review_hold_v7_draft
        else:
            bridge = persist_rejected_v7_draft
        bridge_kwargs: dict[str, Any] = {
            "novel_id": str(self.novel_id),
            "project_id": self.project_id,
            "chapter_number": chapter_number,
            "title": generation["title"],
            "text": generation["text"],
            "review_score": observation["review_score"],
            "dimension_scores": observation["dimension_scores"],
            "run_id": str(run_id),
            "chapter_summary": summary,
            "deai": generation.get("deai") or {},
            "transition_contract": transition_contract,
            "extra_meta": {
                **self.generation_metadata,
                "audit_report": observation.get("audit_report") or {},
                "quality_gate": observation.get("quality_gate") or {},
                "continuity": continuity,
                "final_continuity_audit": {"continuity": continuity},
                "review_provenance": observation.get("review_provenance") or {},
                "review_evidence": observation.get("review_evidence") or {},
                "canonical_review": {
                    "canonical_engine": "v7",
                    "overall_score": observation.get("review_score", 0),
                    "dimension_scores": observation.get("dimension_scores") or {},
                    "audit_report": observation.get("audit_report") or {},
                    "reader_experience": observation.get("reader_experience") or {},
                    "issues": observation.get("issues") or [],
                    "strengths": observation.get("strengths") or [],
                    "constraint_violations": observation.get("constraint_violations") or [],
                    "provenance": observation.get("review_provenance") or {},
                    "continuity": continuity,
                    "final_continuity_audit": {"continuity": continuity},
                    "review_evidence": observation.get("review_evidence") or {},
                },
                "generation_quality": generation.get("generation_quality") or {},
                "opening": generation.get("opening_quality") or (generation.get("generation_quality") or {}).get("opening") or {},
                "quality_profile": generation.get("quality_profile") or quality_profile_metadata(self.quality_profile),
                "payoff_contract": generation.get("payoff_contract") or {},
                "payoff_validation": generation.get("payoff_validation") or {},
                "payoff_evidence": observation.get("payoff_evidence") or [],
                "review_validation": observation.get("review_validation") or [],
                "writing_workflow": methodology_workflow,
                "external_evaluation": methodology_workflow.get("external_evaluation") or {},
            },
        }
        if not observation["passed_review"]:
            bridge_kwargs.update(
                {
                    "review_issues": observation.get("issues") or [],
                    "quality_gate": observation.get("quality_gate") or {},
                    "reader_experience": observation.get("reader_experience") or {},
                    "rework_count": observation.get("rework_count") or 0,
                }
            )
        v6_result = await asyncio.to_thread(bridge, **bridge_kwargs)

        await self.brain.state.update_state(
            CHAPTER_STATE_TYPE,
            chapter_state_key(chapter_number),
            {
                "chapter_number": chapter_number,
                "title": generation["title"],
                "text": generation["text"],
                "summary": summary,
                "word_count": generation["word_count"],
                "review_score": observation["review_score"],
                "reader_experience": observation.get("reader_experience", {}),
                "passed_review": observation["passed_review"],
                "generation_quality": generation.get("generation_quality") or {},
                "opening": generation.get("opening_quality") or (generation.get("generation_quality") or {}).get("opening") or {},
                "quality_profile": generation.get("quality_profile") or quality_profile_metadata(self.quality_profile),
                "payoff_contract": generation.get("payoff_contract") or {},
                "quality_gate": observation.get("quality_gate") or {},
                "review_evidence": observation.get("review_evidence") or {},
                "writing_workflow": methodology_workflow,
                "quality_learning": quality_learning,
                "rework_count": observation["rework_count"],
                "run_id": str(run_id),
                "transition_contract": transition_contract,
                "v6_content_id": (v6_result or {}).get("content_id"),
            },
            0.95,
            source="generation_engine",
            source_run_id=run_id,
            reason=f"chapter {chapter_number} generated and reviewed",
        )

        return {
            "chapter_persisted": bool(v6_result),
            "v6_content": v6_result,
            "transition_contract": transition_contract,
            "continuity": continuity,
            "writing_workflow": methodology_workflow,
            "external_evaluation": methodology_workflow.get("external_evaluation") or {},
            "rule_learning": rule_learning,
            "quality_learning": quality_learning,
            "chapter_summary": summary,
            "states_applied": (memory_update.result or {}).get("states_applied", 0),
            "states_pending_review": (memory_update.result or {}).get("states_pending_review", 0),
            "states_discarded": (memory_update.result or {}).get("states_discarded", 0),
            "conflicts_found": (memory_update.result or {}).get("conflicts_found", 0),
            "memory_success": memory_update.success,
            "memory_deferred": bool((memory_update.result or {}).get("deferred")),
        }

    # ── helpers ─────────────────────────────────────────────────────────
    async def get_decision_queue(self) -> list[dict[str, Any]]:
        """Get decisions pending human approval."""
        return await self.brain.get_decision_logs(status="pending", limit=50)

    async def get_chapter(self, chapter_number: int) -> dict[str, Any] | None:
        """Read a previously generated chapter back out of the Novel Brain."""
        state = await self.brain.state.get_state(
            CHAPTER_STATE_TYPE, chapter_state_key(chapter_number)
        )
        if not state:
            return None
        value = state.get("value") or {}
        return {
            **value,
            "confidence": state.get("confidence"),
            "version": state.get("version"),
        }

    async def list_chapters(self) -> list[dict[str, Any]]:
        """List generated chapters (metadata only, no full text)."""
        states = await self.brain.state.list_states(CHAPTER_STATE_TYPE, limit=500)
        chapters = []
        for s in states:
            value = s.get("value") or {}
            chapters.append(
                {
                    "chapter_number": value.get("chapter_number"),
                    "title": value.get("title"),
                    "summary": value.get("summary"),
                    "word_count": value.get(
                        "word_count", chinese_word_count(value.get("text", ""))
                    ),
                    "review_score": value.get("review_score"),
                    "passed_review": value.get("passed_review"),
                    "version": s.get("version"),
                }
            )
        chapters.sort(key=lambda c: c.get("chapter_number") or 0)
        return chapters
