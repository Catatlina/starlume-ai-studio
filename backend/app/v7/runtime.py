"""Canonical V7 chapter runtime used by every product generation entrypoint.

The product keeps the V6 ``contents`` model because the editor, library and
export APIs already depend on it.  It no longer keeps V6 as a second prose
generation path: workers call this module, V7 owns context/quality/memory, and
the accepted result is bridged back into ``contents``.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from ..db import connect, decode
from ..services.novel_export import extract_body_text
from ..services.planning_contract import mechanic_runtime_directive
from ..services.quality_profiles import profile_from_context, quality_profile_metadata
from .brain.novel_brain import NovelBrain
from .db import AsyncSessionLocal, async_engine
from .director.story_director import StoryDirector
from .events.event_bus import EventBus
from .generation.generation_engine import chapter_state_key
from .trace.tracer import ExecutionTracer


def _v6_seed_snapshot(novel_id: str, before_chapter: int) -> dict[str, Any]:
    """Read the existing V6 story into a deterministic V7 import snapshot."""
    conn = connect()
    try:
        chapter_rows = conn.execute(
            """
            SELECT id, title, body, meta, status, seq
            FROM contents
            WHERE parent_id=%s AND type='chapter' AND is_deleted=FALSE
              AND COALESCE(seq, (meta->>'seq')::int, 0) < %s
            ORDER BY COALESCE(seq, (meta->>'seq')::int, 0) DESC
            LIMIT 200
            """,
            (novel_id, before_chapter),
        ).fetchall()
        novel_row = conn.execute(
            "SELECT title, meta FROM contents WHERE id=%s AND type='novel'",
            (novel_id,),
        ).fetchone()
        knowledge_rows = conn.execute(
            """
            SELECT kind, title, body, meta
            FROM knowledge_items
            WHERE content_id=%s AND is_deleted=FALSE
            ORDER BY updated_at DESC
            LIMIT 200
            """,
            (novel_id,),
        ).fetchall()
        return {
            "chapters": list(reversed(chapter_rows or [])),
            "novel": novel_row or {},
            "knowledge": knowledge_rows or [],
        }
    finally:
        conn.close()


def _chapter_value(row: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    meta = decode(row.get("meta"), {}) or {}
    try:
        seq = int(row.get("seq") or meta.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    if seq <= 0:
        return None
    text = extract_body_text(row.get("body", ""))
    transition = meta.get("transition_contract")
    if not isinstance(transition, dict):
        transition = {
            "schema_version": "v6-import",
            "chapter_number": seq,
            "end_state": {
                "title": row.get("title") or f"第{seq}章",
                "summary": meta.get("chapter_summary") or "",
                "last_tail": text[-1200:],
                "word_count": len(text.replace("\n", "")),
            },
            "next_chapter_bridge": text[-600:],
            "source": "v6_contents_import",
        }
    return chapter_state_key(seq), {
        "chapter_number": seq,
        "title": row.get("title") or f"第{seq}章",
        "text": text,
        "summary": str(meta.get("chapter_summary") or ""),
        "word_count": int(meta.get("word_count") or len(text.replace("\n", ""))),
        "review_score": meta.get("review_score"),
        "passed_review": row.get("status") == "reviewed",
        "rework_count": int(meta.get("rewrite_attempts") or 0),
        "run_id": meta.get("v7_run_id"),
        "transition_contract": transition,
        "v6_content_id": str(row.get("id") or ""),
    }


async def seed_v6_context(
    brain: NovelBrain,
    novel_id: str,
    before_chapter: int,
) -> dict[str, int]:
    """Import old V6 facts only when the V7 Brain does not have them yet."""
    snapshot = await asyncio.to_thread(_v6_seed_snapshot, novel_id, before_chapter)
    existing_chapters = {
        item.get("key")
        for item in await brain.state.list_states("chapter", limit=500)
    }
    imported_chapters = 0
    for row in snapshot["chapters"]:
        parsed = _chapter_value(row)
        if not parsed:
            continue
        key, value = parsed
        if key in existing_chapters:
            continue
        await brain.state.update_state(
            "chapter",
            key,
            value,
            0.95,
            source="v6_compat_import",
            reason="Seed canonical V7 context from existing V6 chapter fact source",
        )
        imported_chapters += 1

    imported_knowledge = 0
    existing_characters = {
        item.get("key")
        for item in await brain.state.list_states("character", limit=500)
    }
    existing_world = {
        item.get("key") for item in await brain.state.list_states("world", limit=500)
    }
    for row in snapshot["knowledge"]:
        kind = str(row.get("kind") or "")
        if kind not in {"character", "worldview", "world"}:
            continue
        state_type = "character" if kind == "character" else "world"
        key = f"v6:{kind}:{str(row.get('title') or 'untitled').strip()}"
        existing = existing_characters if state_type == "character" else existing_world
        if key in existing:
            continue
        meta = decode(row.get("meta"), {}) or {}
        body = str(row.get("body") or "").strip()
        value = {
            "title": row.get("title") or "",
            "summary": body[:2400],
            "detail": body[:8000],
            "source_meta": meta,
        }
        await brain.state.update_state(
            state_type,
            key,
            value,
            0.95,
            source="v6_compat_import",
            reason="Seed canonical V7 context from V6 knowledge items",
        )
        existing.add(key)
        imported_knowledge += 1

    return {
        "chapters": imported_chapters,
        "knowledge": imported_knowledge,
    }


def _resolve_chapter_number(
    novel_id: str,
    requested: int | None,
    *,
    batch_id: str = "",
    batch_ordinal: int = 0,
) -> int:
    if requested and requested > 0:
        return int(requested)
    conn = connect()
    try:
        if batch_id and batch_ordinal:
            batch = conn.execute(
                "SELECT start_seq FROM generation_batches WHERE id=%s",
                (batch_id,),
            ).fetchone()
            if batch and batch.get("start_seq"):
                return int(batch["start_seq"]) + int(batch_ordinal) - 1
        row = conn.execute(
            """
            SELECT COALESCE(MAX(seq), MAX((meta->>'seq')::int), 0) AS seq
            FROM contents
            WHERE parent_id=%s AND type='chapter' AND is_deleted=FALSE
            """,
            (novel_id,),
        ).fetchone()
        return int(row.get("seq") or 0) + 1 if row else 1
    finally:
        conn.close()


def _default_story_prompt(
    novel_id: str,
    chapter_number: int,
    outline: str | None,
) -> str:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT title, meta FROM contents WHERE id=%s AND type='novel'",
            (novel_id,),
        ).fetchone()
        if not row:
            return f"请直接创作第{chapter_number}章正文，推进冲突并在章末留下具体动作钩子。"
        meta = decode(row.get("meta"), {}) or {}
        outline_text = outline or ""
        if not outline_text:
            outlines = meta.get("chapter_outlines") or []
            for item in outlines:
                if isinstance(item, dict) and int(item.get("seq") or 0) == chapter_number:
                    outline_text = json.dumps(item, ensure_ascii=False)
                    break
        blocks = [
            f"小说：{row.get('title') or ''}",
            f"创作圣经：{str(meta.get('creative_bible') or '')[:9000]}",
            f"世界观：{json.dumps(meta.get('worldview') or {}, ensure_ascii=False)[:5000]}",
            f"人物系统：{str(meta.get('_characters_text') or '')[:5000]}",
            f"第{chapter_number}章细纲：{str(outline_text)[:5000]}",
            _generation_contract_prompt(meta),
            "只输出小说正文，不要解释、提纲、标题说明或 Markdown；必须承接上一章交接契约，推进本章冲突，并以具体动作/信息变化收束。",
        ]
        return "\n\n".join(block for block in blocks if block.split("：", 1)[-1].strip())
    finally:
        conn.close()


def _generation_contract_prompt(meta: dict[str, Any]) -> str:
    """Render immutable planning contracts into every prose request."""
    blocks: list[str] = []
    longform = meta.get("longform_contract")
    if isinstance(longform, dict) and longform:
        blocks.append(
            "篇幅闭合契约（不可改写）："
            + json.dumps(longform, ensure_ascii=False)[:4500]
        )
    core_mechanic = meta.get("core_mechanic_contract")
    if isinstance(core_mechanic, dict) and core_mechanic.get("enabled") is True:
        blocks.append(
            "核心金手指闭环（不可弱化）："
            + json.dumps(core_mechanic, ensure_ascii=False)[:5500]
            + "\n金手指剧情必须形成：触发→主角选择/取舍→具体行动→可见收益→代价或风险→人物/资源/关系状态变化→新的主线冲突；"
              "不能用面板播报替代事件，不能让金手指替主角自动通关。"
        )
        adapter_directive = mechanic_runtime_directive(core_mechanic)
        if adapter_directive:
            blocks.append(adapter_directive)
    simulator = meta.get("simulator_contract")
    if isinstance(simulator, dict) and simulator.get("enabled") is True:
        blocks.append(
            "人生模拟器硬规则（不可弱化）："
            + json.dumps(simulator, ensure_ascii=False)[:5500]
            + "\n模拟相关剧情必须遵守：从当前状态推演到死亡/终局；展示分支、因果和死亡原因；"
              "主角可选择回收模拟中得到的机缘/修为/功法/资源/能力；回收必须有选择、代价和现实后果，"
              "不能无条件全拿，也不能把未执行的模拟结果当成现实事实。"
        )
    return "\n".join(blocks)


def _load_quality_profile(novel_id: str) -> dict[str, Any]:
    """Select the one runtime quality policy from the novel metadata."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT meta FROM contents WHERE id=%s AND type='novel'",
            (novel_id,),
        ).fetchone()
        meta = decode((row or {}).get("meta"), {}) or {}
        return profile_from_context(meta)
    finally:
        conn.close()


def _load_genre_id(novel_id: str) -> str | None:
    """Read the selected real genre pack id from novel metadata."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT meta FROM contents WHERE id=%s AND type='novel'",
            (novel_id,),
        ).fetchone()
        meta = decode((row or {}).get("meta"), {}) or {}
        genre_id = str(meta.get("genre_id") or "").strip()
        return genre_id or None
    finally:
        conn.close()


async def generate_v7_chapter(
    novel_id: str,
    project_id: str,
    *,
    chapter_number: int | None = None,
    prompt: str | None = None,
    outline: str | None = None,
    user_id: str | None = None,
    api_key: str = "",
    api_url: str = "",
    model: str = "",
    batch_id: str = "",
    batch_ordinal: int = 0,
) -> dict[str, Any]:
    """Run the only canonical prose generation path and return its evidence."""
    # Keep the runtime safe even when called outside FastAPI/Celery (for
    # example from a maintenance script).  The queue and HTTP layers perform
    # the same check, but the V7 core must not trust either caller implicitly.
    from ..services.chapter_scope import validate_novel_parent
    scope_conn = connect()
    try:
        validate_novel_parent(db=scope_conn, project_id=project_id, novel_id=novel_id)
    finally:
        scope_conn.close()
    novel_uuid = uuid.UUID(str(novel_id))
    resolved_number = _resolve_chapter_number(
        novel_id,
        chapter_number,
        batch_id=batch_id,
        batch_ordinal=batch_ordinal,
    )
    effective_outline = outline
    if prompt:
        # Custom editor prompts still inherit the project's immutable planning
        # contracts; otherwise a manual prompt could silently disable the
        # simulator rules or word/route ledger.
        contract_conn = connect()
        try:
            contract_row = contract_conn.execute(
                "SELECT meta FROM contents WHERE id=%s AND type='novel'",
                (novel_id,),
            ).fetchone()
        finally:
            contract_conn.close()
        contract_meta = decode((contract_row or {}).get("meta"), {}) or {}
        contract_prompt = _generation_contract_prompt(contract_meta)
        effective_prompt = f"{prompt}\n\n{contract_prompt}" if contract_prompt else prompt
    else:
        effective_prompt = _default_story_prompt(novel_id, resolved_number, outline)
    quality_profile = await asyncio.to_thread(_load_quality_profile, novel_id)
    genre_id = await asyncio.to_thread(_load_genre_id, novel_id)
    provider_config = {
        key: value
        for key, value in {
            "api_key": api_key,
            "base_url": api_url,
            "model": model,
        }.items()
        if value
    }

    async with AsyncSessionLocal() as db:
        brain = NovelBrain(db, novel_uuid)
        seed = await seed_v6_context(brain, novel_id, resolved_number)
        tracer = ExecutionTracer(db, novel_uuid)
        event_bus = EventBus(db, novel_uuid)
        director = StoryDirector(
            db,
            novel_uuid,
            brain,
            tracer,
            event_bus,
            project_id=project_id,
            user_id=user_id,
            provider_config=provider_config,
            quality_profile=quality_profile,
            genre_id=genre_id,
            generation_metadata={
                key: value
                for key, value in {
                    "batch_id": batch_id,
                    "batch_ordinal": batch_ordinal,
                }.items()
                if value
            },
        )
        result = await director.generate_chapter(
            resolved_number,
            prompt=effective_prompt,
            outline=effective_outline,
            # One review-informed full-chapter rewrite is a generation
            # fallback, not the normal post-write humanizer.  Keeping it
            # bounded prevents the old 3-attempt audit loop from consuming
            # Provider budget while allowing a first draft with a concrete
            # pacing/foreshadowing defect to be repaired before persistence.
            allow_rework=True,
            max_reworks=1,
        )
        result["canonical_engine"] = "v7"
        result["chapter_number"] = resolved_number
        result["v7_context_seed"] = seed
        result["quality_profile"] = quality_profile_metadata(quality_profile)
        result["genre_id"] = genre_id
        if batch_id:
            result["batch_id"] = batch_id
            result["batch_ordinal"] = batch_ordinal
        await db.commit()
        return result


def generate_v7_chapter_sync(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Celery-safe synchronous bridge for the async V7 Director."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_generate_v7_chapter_worker(*args, **kwargs))
    raise RuntimeError("generate_v7_chapter_sync cannot run inside an active event loop")


async def _generate_v7_chapter_worker(*args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return await generate_v7_chapter(*args, **kwargs)
    finally:
        # Celery may execute multiple tasks in one process, each with its own
        # event loop.  Do not leak asyncpg connections across those loops.
        await async_engine.dispose()
