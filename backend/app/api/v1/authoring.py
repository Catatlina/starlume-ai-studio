"""Starlume AI human-led authoring APIs.

This router deliberately keeps deterministic product state separate from model
output.  Sessions, context, Bible facts and writing events are durable
records; an AI result is only considered provider-backed when the shared
``ai_calls`` ledger can resolve the client mutation id.
"""
from __future__ import annotations

import os
import json
import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.authz import ensure_project_member, ok, require_member
from app.core.security import get_current_user
from app.db import connect, decode, encode, new_id


router = APIRouter(prefix="/api/v1/authoring", tags=["authoring"])


RoleKey = Literal[
    "planner", "chapter_skeleton", "scene_expander", "dialogue_editor", "continuity_reviewer", "publication_editor"
]
ProviderName = Literal["openai", "deepseek", "doubao", "claude", "gemini"]
WritingEventType = Literal[
    "manual_input", "delete", "paste", "ai_accept", "ai_reject", "ai_revert", "active_window", "save"
]


class SessionCreateRequest(BaseModel):
    content_id: str | None = None
    novel_id: str | None = None
    role_key: RoleKey = "scene_expander"
    base_version_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionMessageRequest(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str = Field(min_length=1, max_length=24000)
    message_kind: str = Field(default="chat", max_length=32)
    provider: str = Field(default="", max_length=50)
    model: str = Field(default="", max_length=120)
    ai_call_id: str | None = None
    candidate: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoryBibleCreateRequest(BaseModel):
    novel_id: str
    kind: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=50000)
    fact_type: Literal["hard", "soft"] = "hard"
    source_chapter: int | None = Field(default=None, ge=1)
    approved: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)


class StoryBibleUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = Field(default=None, min_length=1, max_length=50000)
    fact_type: Literal["hard", "soft"] | None = None
    source_chapter: int | None = Field(default=None, ge=1)
    meta: dict[str, Any] | None = None


class ProviderRoleRequest(BaseModel):
    role_key: RoleKey
    provider: ProviderName
    model: str = Field(min_length=1, max_length=120)
    params: dict[str, Any] = Field(default_factory=dict)


class WritingEventRequest(BaseModel):
    content_id: str
    event_type: WritingEventType
    source: str = Field(default="editor", max_length=24)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int = Field(default=0, ge=0, le=86400000)
    chars_added: int = Field(default=0, ge=0, le=1000000)
    chars_removed: int = Field(default=0, ge=0, le=1000000)
    client_event_id: str | None = Field(default=None, min_length=8, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


class CleanRunPrepareRequest(BaseModel):
    novel_id: str
    target_chapters: int = Field(default=3, ge=1, le=3)


class CleanRunCleanRequest(BaseModel):
    confirm_clean: bool = False


class ChapterSkeletonRequest(BaseModel):
    """Author intent for one chapter blueprint; it never contains final prose."""
    author_intent: str = Field(default="", max_length=6000)
    target_chars: int = Field(default=850, ge=700, le=1000)
    client_mutation_id: str | None = Field(default=None, min_length=8, max_length=120)


class ChapterSkeletonSaveRequest(BaseModel):
    skeleton: dict[str, Any]
    base_version_id: str | None = None


class ChapterDraftRequest(BaseModel):
    """Author intent for one provider-backed full-text candidate."""
    author_intent: str = Field(default="", max_length=6000)
    target_chars: int = Field(default=2600, ge=2200, le=3000)
    client_mutation_id: str | None = Field(default=None, min_length=8, max_length=120)


class ChapterDraftSaveRequest(BaseModel):
    draft: dict[str, Any]
    base_version_id: str | None = None


class HumanReceiptRequest(BaseModel):
    platform: str = Field(min_length=1, max_length=50)
    publish_record_id: str | None = None
    status: Literal["submitted", "accepted", "rejected", "unknown"] = "submitted"
    external_url: str = Field(default="", max_length=2000)
    external_id: str = Field(default="", max_length=300)
    receipt_text: str = Field(default="", max_length=5000)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _content(db: Any, content_id: str, user: dict, *, write: bool = False) -> dict[str, Any]:
    row = db.execute(
        "SELECT id, project_id, parent_id, type, title, body, meta, updated_at "
        "FROM contents WHERE id=%s AND is_deleted=FALSE",
        (content_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="content not found")
    item = dict(row)
    require_member(db, str(item["project_id"]), user, write=write)
    item["body"] = decode(item.get("body"), {})
    item["meta"] = decode(item.get("meta"), {})
    return item


def _novel(db: Any, novel_id: str, user: dict, *, write: bool = False) -> dict[str, Any]:
    item = _content(db, novel_id, user, write=write)
    if item.get("type") != "novel":
        raise HTTPException(status_code=400, detail="content is not a novel")
    return item


def _session(db: Any, session_id: str, user: dict, *, write: bool = False) -> dict[str, Any]:
    row = db.execute(
        """SELECT s.*, n.title AS novel_title, c.title AS content_title
           FROM authoring_sessions s
           LEFT JOIN contents n ON n.id=s.novel_id
           LEFT JOIN contents c ON c.id=s.content_id
           WHERE s.id=%s""",
        (session_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="authoring session not found")
    item = dict(row)
    require_member(db, str(item["project_id"]), user, write=write)
    item["metadata"] = decode(item.get("metadata"), {})
    return item


def _message_payload(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for key in ("candidate", "metadata"):
        item[key] = decode(item.get(key), {})
    return item


def _provider_status(provider: str) -> dict[str, Any]:
    env_key = f"{provider.upper()}_API_KEY"
    configured = bool(os.getenv(env_key, "").strip())
    implemented = provider in {"deepseek", "openai", "claude", "gemini", "doubao"}
    return {
        "provider": provider,
        "implemented": implemented,
        "key_configured": configured,
        "status": "available" if implemented and configured else "needs_key" if implemented else "unsupported",
        "env_key": env_key,
    }


@router.post("/sessions")
def create_session(req: SessionCreateRequest, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        target = req.content_id or req.novel_id
        if not target:
            raise HTTPException(status_code=422, detail="content_id or novel_id is required")
        item = _content(db, target, user, write=True)
        novel_id = req.novel_id or (item["parent_id"] if item.get("type") == "chapter" else item["id"])
        if not novel_id:
            raise HTTPException(status_code=422, detail="novel scope is required")
        _novel(db, str(novel_id), user, write=True)
        session_id = new_id("authoring_session")
        db.execute(
            """INSERT INTO authoring_sessions
               (id, project_id, novel_id, content_id, author_id, role_key, base_version_id, metadata)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                session_id, item["project_id"], novel_id,
                req.content_id, user["id"], req.role_key, req.base_version_id,
                encode(req.metadata),
            ),
        )
        db.commit()
        session_status = "active"
        return ok({
            "id": session_id, "project_id": str(item["project_id"]), "novel_id": str(novel_id),
            "content_id": req.content_id, "role_key": req.role_key, "status": session_status, "messages": [],
        }, message="创作会话已建立")
    finally:
        db.close()


@router.get("/sessions/current")
def get_current_session(content_id: str, user: dict = Depends(get_current_user)):
    """Restore the latest active editor session instead of creating duplicates."""
    db = connect()
    try:
        item = _content(db, content_id, user)
        row = db.execute(
            """SELECT id FROM authoring_sessions
               WHERE content_id=%s AND author_id=%s AND status='active'
               ORDER BY updated_at DESC LIMIT 1""",
            (content_id, user["id"]),
        ).fetchone()
        if not row:
            return ok(None, message="当前章节暂无创作会话")
        session = _session(db, str(row["id"]), user)
        messages = db.execute(
            "SELECT * FROM authoring_messages WHERE session_id=%s ORDER BY sequence_no",
            (row["id"],),
        ).fetchall()
        session["messages"] = [_message_payload(dict(message)) for message in messages]
        return ok(session, message="已恢复创作会话")
    finally:
        db.close()


@router.get("/sessions/{session_id}")
def get_session(session_id: str, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        session = _session(db, session_id, user)
        rows = db.execute(
            "SELECT * FROM authoring_messages WHERE session_id=%s ORDER BY sequence_no",
            (session_id,),
        ).fetchall()
        session["messages"] = [_message_payload(dict(row)) for row in rows]
        return ok(session, message="创作会话")
    finally:
        db.close()


@router.post("/sessions/{session_id}/messages")
def append_session_message(
    session_id: str,
    req: SessionMessageRequest,
    user: dict = Depends(get_current_user),
):
    db = connect()
    try:
        session = _session(db, session_id, user, write=True)
        if session["status"] != "active":
            raise HTTPException(status_code=409, detail="authoring session is not active")
        if req.role == "assistant":
            mutation_id = str(req.metadata.get("client_mutation_id") or "").strip()
            if mutation_id:
                ledger = db.execute(
                    """SELECT id, provider, model, output, status
                       FROM ai_calls WHERE project_id=%s AND client_mutation_id=%s
                       ORDER BY created_at DESC LIMIT 1""",
                    (session["project_id"], mutation_id),
                ).fetchone()
                if ledger:
                    req.provider = str(ledger.get("provider") or req.provider)
                    req.model = str(ledger.get("model") or req.model)
                    req.ai_call_id = str(ledger.get("id") or req.ai_call_id or "") or None
                    req.candidate = {
                        **req.candidate,
                        "provider_verified": ledger.get("status") == "succeeded",
                        "ledger_status": ledger.get("status"),
                        "ai_call_id": req.ai_call_id,
                    }
            else:
                req.candidate = {**req.candidate, "provider_verified": False, "verification": "client_recorded"}
        seq = db.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence FROM authoring_messages WHERE session_id=%s",
            (session_id,),
        ).fetchone()["next_sequence"]
        message_id = new_id("authoring_message")
        db.execute(
            """INSERT INTO authoring_messages
               (id, session_id, sequence_no, role, message_kind, content, provider, model,
                ai_call_id, candidate, metadata)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                message_id, session_id, seq, req.role, req.message_kind, req.content,
                req.provider or None, req.model or None, req.ai_call_id,
                encode(req.candidate), encode(req.metadata),
            ),
        )
        db.execute("UPDATE authoring_sessions SET updated_at=now() WHERE id=%s", (session_id,))
        db.commit()
        return ok({
            "id": message_id, "session_id": session_id, "sequence_no": seq,
            "role": req.role, "message_kind": req.message_kind, "content": req.content,
            "provider": req.provider or None, "model": req.model or None,
            "ai_call_id": req.ai_call_id, "candidate": req.candidate, "metadata": req.metadata,
        }, message="会话消息已记录")
    finally:
        db.close()


@router.post("/context/{content_id}")
def get_editor_context(content_id: str, user: dict = Depends(get_current_user)):
    """Return deterministic editor context; it never invents story facts."""
    db = connect()
    try:
        chapter = _content(db, content_id, user)
        novel_id = chapter["parent_id"] if chapter.get("type") == "chapter" else chapter["id"]
        novel = _novel(db, str(novel_id), user)
        chapters = [dict(row) for row in db.execute(
            """SELECT id,title,body,meta,updated_at FROM contents
               WHERE parent_id=%s AND type='chapter' AND is_deleted=FALSE
               ORDER BY created_at ASC""",
            (novel_id,),
        ).fetchall()]
        current_index = next((i for i, row in enumerate(chapters) if str(row["id"]) == str(content_id)), -1)
        # Context must be scoped to the selected novel.  Including NULL
        # content_id rows here pulled project-wide/imported world entries into
        # every editor, which made a clean novel look populated with unrelated
        # worldbuilding while leaving its actual V7 state invisible.
        knowledge = [dict(row) for row in db.execute(
            """SELECT id,project_id,content_id,kind,title,body,meta,fact_type,approved,source_chapter,updated_at
               FROM knowledge_items WHERE project_id=%s AND content_id=%s
               AND is_deleted=FALSE ORDER BY kind,title""",
            (novel["project_id"], novel_id),
        ).fetchall()]
        for item in knowledge:
            item["meta"] = decode(item.get("meta"), {})
        # Only explicit novel-bound Bible entries belong in the editor.  NULL
        # content_id is project/import scope and is intentionally available to
        # the knowledge browser, not silently merged into a chapter workspace.
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in knowledge:
            groups.setdefault(str(item.get("kind") or "reference"), []).append(item)

        def context_item(*, item_id: str, title: str, body: str, approved: bool, source: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "id": item_id,
                "title": title.strip() or "未命名条目",
                "body": body.strip(),
                "approved": approved,
                "source": source,
                "meta": meta or {},
            }

        # V7 memory is the chapter-aware context produced from the actual
        # manuscript.  The previous UI only read static Bible rows, so after a
        # chapter was written it still showed 0 characters / 0 plot / 0
        # foreshadowings.  Surface the current novel's active states as
        # deterministic evidence, never as AI-invented prose.
        state_rows = [dict(row) for row in db.execute(
            """SELECT id,state_type,state_key,state_value,confidence,is_pending_review,source,updated_at
               FROM v7_story_states
               WHERE novel_id=%s AND is_active=TRUE
                 AND state_type IN ('character','plot','world')
               ORDER BY updated_at DESC, state_key
               LIMIT 160""",
            (novel_id,),
        ).fetchall()]
        state_groups: dict[str, list[dict[str, Any]]] = {"character": [], "plot": [], "world": [], "foreshadowing": []}
        for row in state_rows:
            value = decode(row.get("state_value"), {})
            value = value if isinstance(value, dict) else {"summary": str(value)}
            category = str(value.get("category") or "")
            state_type = str(row.get("state_type") or "")
            if state_type == "plot" and row.get("state_key") == "plot_tree_status":
                continue
            group = "foreshadowing" if state_type == "plot" and category == "foreshadowing" else state_type
            if group not in state_groups:
                continue
            state_key = str(row.get("state_key") or "")
            title = state_key.replace(".", " · ")
            summary = str(value.get("summary") or value.get("detail") or state_key)
            detail = str(value.get("detail") or "")
            body = summary if not detail or detail == summary else f"{summary}。{detail}"
            state_groups[group].append(context_item(
                item_id=f"v7-state:{row['id']}",
                title=title,
                body=body,
                approved=not bool(row.get("is_pending_review")),
                source=f"V7 {row.get('source') or 'story state'}",
                meta={"confidence": row.get("confidence"), "category": category, "updated_at": row.get("updated_at")},
            ))

        plot_threads = [dict(row) for row in db.execute(
            """SELECT id,name,status,progress,importance,last_chapter_seq
               FROM plot_threads
               WHERE novel_id=%s AND is_deleted=FALSE
               ORDER BY importance DESC NULLS LAST, updated_at DESC
               LIMIT 40""",
            (novel_id,),
        ).fetchall()]
        thread_items = [context_item(
            item_id=f"plot-thread:{row['id']}",
            title=str(row.get("name") or "未命名故事线"),
            body=str(row.get("progress") or "故事线已建立，当前进度待补充"),
            approved=True,
            source="故事线",
            meta={"status": row.get("status"), "last_chapter_seq": row.get("last_chapter_seq")},
        ) for row in plot_threads]

        foreshadowing_rows = [dict(row) for row in db.execute(
            """SELECT f.id,f.content,f.status,f.planned_resolve_chapter,f.expected_payoff_window,
                      f.reader_awareness,c.seq AS planted_chapter
               FROM foreshadowings f
               JOIN contents c ON c.id=f.chapter_id
               WHERE c.parent_id=%s
               ORDER BY c.seq DESC, f.created_at DESC
               LIMIT 40""",
            (novel_id,),
        ).fetchall()]
        foreshadowing_items = [context_item(
            item_id=f"foreshadowing:{row['id']}",
            title=str(row.get("content") or "未命名伏笔"),
            body=(f"第{row.get('planted_chapter') or '-'}章埋下 · 状态：{row.get('status') or '未标记'}"
                  + (f" · 计划第{row['planned_resolve_chapter']}章回收" if row.get("planned_resolve_chapter") else "")),
            approved=True,
            source="伏笔账本",
            meta={"status": row.get("status"), "reader_awareness": row.get("reader_awareness"), "expected_payoff_window": row.get("expected_payoff_window")},
        ) for row in foreshadowing_rows]

        # Current chapter V7 evidence goes first, followed by human-authored
        # Bible material.  Dedupe exact title/body pairs so the sidebar remains
        # useful instead of becoming a dump of parallel records.
        def merge_items(*lists: list[dict[str, Any]], dedupe_title: bool = False) -> list[dict[str, Any]]:
            merged: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for items in lists:
                for item in items:
                    key = (str(item.get("title") or ""), "") if dedupe_title else (str(item.get("title") or ""), str(item.get("body") or ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(item)
            return merged

        groups["character"] = merge_items(state_groups["character"], groups.get("character", []), groups.get("characters", []))
        groups["plot"] = merge_items(thread_items, state_groups["plot"], groups.get("plot", []), groups.get("outline", []), groups.get("story_arc", []))
        groups["worldview"] = merge_items(state_groups["world"], groups.get("worldview", []), groups.get("world_background", []), dedupe_title=True)
        groups["foreshadowing"] = merge_items(foreshadowing_items, state_groups["foreshadowing"], groups.get("foreshadowing", []), groups.get("foreshadowings", []))
        def chapter_preview(row: dict[str, Any] | None) -> dict[str, Any] | None:
            if not row:
                return None
            body = decode(row.get("body"), {})
            return {
                "id": str(row["id"]), "title": row.get("title"),
                "seq": decode(row.get("meta"), {}).get("seq"),
                "text": _text_from_body(body)[:1600],
            }
        current = chapters[current_index] if current_index >= 0 else chapter
        return ok({
            "source": "deterministic",
            "novel": {"id": str(novel["id"]), "title": novel.get("title"), "meta": novel.get("meta", {})},
            "chapter": chapter_preview(current),
            "previous_chapter": chapter_preview(chapters[current_index - 1] if current_index > 0 else None),
            "next_chapter": chapter_preview(chapters[current_index + 1] if current_index >= 0 and current_index + 1 < len(chapters) else None),
            "knowledge": groups,
            "characters": groups["character"],
            "plot": groups["plot"],
            "worldview": groups["worldview"],
            "foreshadowing": groups["foreshadowing"],
        }, message="编辑上下文")
    finally:
        db.close()


def _text_from_body(body: Any) -> str:
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, list):
            return "\n\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        if isinstance(body.get("text"), str):
            return body["text"]
    return ""


def _skeleton_char_count(text: str) -> int:
    """Count visible Chinese characters, excluding layout whitespace."""
    return len(re.sub(r"\s+", "", str(text or "")))


def _draft_char_count(text: str) -> int:
    """Count visible characters for a full-text candidate."""
    return _skeleton_char_count(text)


def _draft_body_text(body: Any) -> str:
    if isinstance(body, list):
        return "\n\n".join(str(item).strip() for item in body if str(item).strip())
    return str(body or "").strip()


def _validate_chapter_draft_protocol(chapter: dict[str, Any]) -> list[str]:
    """Deterministic gate for a complete prose candidate, not an AI detector."""
    issues: list[str] = []
    title = str(chapter.get("title") or "").strip()
    body = chapter.get("body")
    if not title:
        issues.append("chapter.title is empty")
    if not isinstance(body, list) or len(body) < 6:
        issues.append("chapter.body must contain at least 6 paragraphs")
    if isinstance(body, list) and any(not str(item).strip() for item in body):
        issues.append("chapter.body contains an empty paragraph")
    text = _draft_body_text(body)
    if re.search(r"(?m)^\s*(?:【(?:本章目标|主线推进|场景一|下一章钩子|章节骨架)】|(?:本章目标|主线推进|场景一|下一章钩子|正文如下|章节骨架)\s*[:：])", text):
        issues.append("chapter.body contains planning labels instead of publishable prose")
    if "```" in text or re.search(r"^\s*[<{]\s*\"(?:chapter|body)\"", text):
        issues.append("chapter.body contains serialized output markers")
    return issues


SKELETON_AUTHORING_PROTOCOL = "reader-grounded-author-led-v0.1"
_SCENE_PROTOCOL_FIELDS = ("title", "purpose", "trigger", "action", "choice", "conflict", "cost", "outcome", "visible_change", "characters")
_READER_EXPERIENCE_FIELDS = ("opening_anchor", "reader_discovery", "interest_change", "aftertaste", "continuation_question")


def _validate_chapter_skeleton_protocol(output: dict[str, Any]) -> list[str]:
    """Deterministic generation gate for the author-led skeleton protocol.

    This is deliberately not an AI detector and does not score prose.  It only
    prevents a Provider response from being persisted when the writing plan is
    missing the causal choices and reader-facing targets the author needs.
    """
    issues: list[str] = []
    scenes = output.get("scenes")
    if not isinstance(scenes, list) or not 3 <= len(scenes) <= 6:
        issues.append("scenes must contain 3-6 scene nodes")
    else:
        outcomes: list[str] = []
        for index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                issues.append(f"scene_{index} is not an object")
                continue
            for field in _SCENE_PROTOCOL_FIELDS:
                value = scene.get(field)
                if field == "characters":
                    if not isinstance(value, list) or not any(str(item).strip() for item in value):
                        issues.append(f"scene_{index}.{field} is empty")
                elif not str(value or "").strip():
                    issues.append(f"scene_{index}.{field} is empty")
            outcome = str(scene.get("outcome") or "").strip()
            if outcome:
                outcomes.append(outcome)
        if len(set(outcomes)) < 2:
            issues.append("scene outcomes must contain visible change, not repeated placeholders")

    plan = output.get("reader_experience_plan")
    if not isinstance(plan, dict):
        issues.append("reader_experience_plan is missing")
    else:
        for field in _READER_EXPERIENCE_FIELDS:
            if not str(plan.get(field) or "").strip():
                issues.append(f"reader_experience_plan.{field} is empty")

    skeleton_text = str(output.get("skeleton_text") or "")
    if re.search(r"(?:^|[\s，。；：、])(?:待补充|略|同上|见上文|省略号)(?=$|[\s，。；：、])", skeleton_text):
        issues.append("skeleton_text contains a placeholder")
    for field in ("chapter_goal", "main_conflict", "payoff", "next_hook"):
        if not str(output.get(field) or "").strip():
            issues.append(f"{field} is empty")
    return issues


def _context_lines(items: list[dict[str, Any]], limit: int = 40) -> str:
    lines = []
    for item in items[:limit]:
        title = str(item.get("title") or item.get("name") or "未命名条目").strip()
        body = str(item.get("body") or item.get("summary") or item.get("progress") or item.get("content") or item.get("status") or "").strip()
        if title or body:
            lines.append(f"- {title}：{body[:700]}")
    return "\n".join(lines) or "（暂无已确认资料）"


def _chapter_skeleton_context(db: Any, chapter: dict[str, Any], novel: dict[str, Any]) -> dict[str, str]:
    """Build bounded, deterministic context for the skeleton prompt."""
    chapters = [dict(row) for row in db.execute(
        """SELECT id,title,body,meta FROM contents
           WHERE parent_id=%s AND type='chapter' AND is_deleted=FALSE
           ORDER BY created_at ASC""",
        (novel["id"],),
    ).fetchall()]
    index = next((i for i, row in enumerate(chapters) if str(row["id"]) == str(chapter["id"])), -1)
    chapter_meta = chapter.get("meta") if isinstance(chapter.get("meta"), dict) else {}
    chapter_seq = int(chapter_meta.get("seq") or (index + 1 if index >= 0 else 1))
    previous_tail = "（第一章，无上一章）"
    if index > 0:
        previous = decode(chapters[index - 1].get("body"), {})
        previous_text = _text_from_body(previous)
        previous_tail = previous_text[-1800:] or "（上一章暂无正文）"

    knowledge = [dict(row) for row in db.execute(
        """SELECT kind,title,body,meta,fact_type,approved FROM knowledge_items
           WHERE project_id=%s AND content_id=%s AND is_deleted=FALSE
           ORDER BY approved DESC, kind, title LIMIT 100""",
        (novel["project_id"], novel["id"]),
    ).fetchall()]
    for item in knowledge:
        item["meta"] = decode(item.get("meta"), {})
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in knowledge:
        grouped.setdefault(str(item.get("kind") or "reference"), []).append(item)

    plot = [dict(row) for row in db.execute(
        """SELECT name,status,progress,importance,last_chapter_seq FROM plot_threads
           WHERE novel_id=%s AND is_deleted=FALSE
           ORDER BY importance DESC NULLS LAST, updated_at DESC LIMIT 30""",
        (novel["id"],),
    ).fetchall()]
    foreshadowing = [dict(row) for row in db.execute(
        """SELECT f.content,f.status,f.planned_resolve_chapter,f.reader_awareness,c.seq AS planted_chapter
           FROM foreshadowings f JOIN contents c ON c.id=f.chapter_id
           WHERE c.parent_id=%s
           ORDER BY c.seq DESC, f.created_at DESC LIMIT 30""",
        (novel["id"],),
    ).fetchall()]
    state_rows = [dict(row) for row in db.execute(
        """SELECT state_type,state_key,state_value,confidence,is_pending_review,source
           FROM v7_story_states WHERE novel_id=%s AND is_active=TRUE
           ORDER BY updated_at DESC, state_key LIMIT 100""",
        (novel["id"],),
    ).fetchall()]
    state_items: list[dict[str, Any]] = []
    for row in state_rows:
        value = decode(row.get("state_value"), {})
        value = value if isinstance(value, dict) else {"summary": str(value)}
        state_items.append({
            "title": str(row.get("state_key") or "未命名状态"),
            "body": str(value.get("summary") or value.get("detail") or ""),
        })

    def as_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:9000]

    worldview = grouped.get("worldview", []) + grouped.get("world_background", []) + grouped.get("world", [])
    characters = grouped.get("character", []) + grouped.get("characters", [])
    plot_items = grouped.get("plot", []) + grouped.get("outline", []) + grouped.get("story_arc", [])
    return {
        "chapter_seq": str(chapter_seq),
        "chapter_title": str(chapter.get("title") or "未命名章节"),
        "chapter_text": _text_from_body(chapter.get("body"))[-5000:] or "（尚未动笔）",
        "previous_chapter_tail": previous_tail,
        "characters": _context_lines(characters + [item for item in state_items if "character" in item["title"]]),
        "plot": _context_lines(plot_items + plot + [item for item in state_items if "plot" in item["title"]]),
        "foreshadowing": _context_lines(foreshadowing + grouped.get("foreshadowing", [])),
        "worldview": _context_lines(worldview + [item for item in state_items if "world" in item["title"]]),
        "novel_meta": as_json(novel.get("meta") or {}),
    }


def _skeleton_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    snapshot = decode(row.get("snapshot"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    return {
        "id": str(row.get("id")),
        "version_no": int(row.get("version_no") or 1),
        "label": row.get("label"),
        "reason": row.get("reason"),
        "created_at": row.get("created_at"),
        **snapshot,
    }


def _draft_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    snapshot = decode(row.get("snapshot"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    return {
        "id": str(row.get("id")),
        "version_no": int(row.get("version_no") or 1),
        "label": row.get("label"),
        "reason": row.get("reason"),
        "created_at": row.get("created_at"),
        **snapshot,
    }


@router.get("/chapters/{chapter_id}/skeletons")
def list_chapter_skeletons(chapter_id: str, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        _content(db, chapter_id, user)
        rows = db.execute(
            """SELECT id,version_no,label,reason,snapshot,created_at FROM versions
               WHERE entity_type='chapter_skeleton' AND entity_id=%s
               ORDER BY created_at DESC LIMIT 30""",
            (chapter_id,),
        ).fetchall()
        return ok([_skeleton_snapshot(dict(row)) for row in rows], message="章节骨架版本")
    finally:
        db.close()


@router.post("/chapters/{chapter_id}/skeleton")
def generate_chapter_skeleton(
    chapter_id: str,
    req: ChapterSkeletonRequest,
    user: dict = Depends(get_current_user),
):
    """Generate one provider-backed 700-1000 character blueprint, not prose."""
    db = connect()
    try:
        chapter = _content(db, chapter_id, user, write=True)
        novel_id = chapter.get("parent_id") if chapter.get("type") == "chapter" else chapter["id"]
        novel = _novel(db, str(novel_id), user)
        context = _chapter_skeleton_context(db, chapter, novel)
    finally:
        db.close()

    from app.gateway import complete

    mutation_id = req.client_mutation_id or new_id("skeleton")
    output = complete(
        run_id=None,
        node_key="chapter_skeleton",
        project_id=str(novel["project_id"]),
        user_id=str(user["id"]),
        task_type="chapter_skeleton",
        prompt_name="authoring.chapter_skeleton",
        client_mutation_id=mutation_id,
        variables={
            **context,
            "author_intent": req.author_intent or "（作者暂未补充意图，请严格依据已有资料提出一个最小推进方案）",
            "target_chars": req.target_chars,
        },
    )
    skeleton_text = str(output.get("skeleton_text") or "").strip()
    char_count = _skeleton_char_count(skeleton_text)
    if not 700 <= char_count <= 1000:
        raise HTTPException(
            status_code=502,
            detail=f"Provider returned skeleton_text with {char_count} visible characters; expected 700-1000. No draft was changed.",
        )
    protocol_issues = _validate_chapter_skeleton_protocol(output)
    if protocol_issues:
        raise HTTPException(
            status_code=502,
            detail=("Provider returned a skeleton that failed the author-led reader-grounded protocol: "
                    + "; ".join(protocol_issues[:8])
                    + ". No draft was changed."),
        )

    db = connect()
    try:
        ledger = db.execute(
            """SELECT id,provider,model,status FROM ai_calls
               WHERE project_id=%s AND client_mutation_id=%s
               ORDER BY created_at DESC LIMIT 1""",
            (novel["project_id"], mutation_id),
        ).fetchone()
        version_no = db.execute(
            """SELECT COALESCE(MAX(version_no),0)+1 AS next_version FROM versions
               WHERE entity_type='chapter_skeleton' AND entity_id=%s""",
            (chapter_id,),
        ).fetchone()["next_version"]
        version_id = new_id("ver")
        snapshot = {
            "artifact_type": "chapter_skeleton",
            "status": "ai_generated",
            "authoring_protocol": SKELETON_AUTHORING_PROTOCOL,
            "target_chars": req.target_chars,
            "char_count": char_count,
            "author_intent": req.author_intent,
            "skeleton": {key: value for key, value in output.items() if key != "_meta"},
            "provider_verified": bool(ledger and ledger.get("status") == "succeeded"),
            "provider": ledger.get("provider") if ledger else None,
            "model": ledger.get("model") if ledger else None,
            "ai_call_id": str(ledger.get("id")) if ledger and ledger.get("id") else None,
        }
        db.execute(
            """INSERT INTO versions
               (id,entity_type,entity_id,version_no,label,snapshot,reason,author_id)
               VALUES (%s,'chapter_skeleton',%s,%s,'ai_skeleton',%s,'ai_generated',%s)""",
            (version_id, chapter_id, version_no, encode(snapshot), user["id"]),
        )
        db.commit()
        return ok({"version": _skeleton_snapshot({"id": version_id, "version_no": version_no, "label": "ai_skeleton", "reason": "ai_generated", "snapshot": snapshot}),
                   "provider_verified": snapshot["provider_verified"], "char_count": char_count}, message="章节骨架已生成，正文未修改")
    finally:
        db.close()


@router.post("/chapters/{chapter_id}/skeletons/save")
def save_chapter_skeleton(
    chapter_id: str,
    req: ChapterSkeletonSaveRequest,
    user: dict = Depends(get_current_user),
):
    """Save the author's edited blueprint as a separate version."""
    db = connect()
    try:
        _content(db, chapter_id, user, write=True)
        skeleton = dict(req.skeleton or {})
        nested = skeleton.get("skeleton") if isinstance(skeleton.get("skeleton"), dict) else skeleton
        text = str(nested.get("skeleton_text") or "").strip()
        char_count = _skeleton_char_count(text)
        if not 700 <= char_count <= 1000:
            raise HTTPException(status_code=422, detail=f"章节骨架需保持700-1000字，当前为{char_count}字")
        version_no = db.execute(
            """SELECT COALESCE(MAX(version_no),0)+1 AS next_version FROM versions
               WHERE entity_type='chapter_skeleton' AND entity_id=%s""",
            (chapter_id,),
        ).fetchone()["next_version"]
        version_id = new_id("ver")
        snapshot = {
            "artifact_type": "chapter_skeleton",
            "status": "human_edited",
            "authoring_protocol": SKELETON_AUTHORING_PROTOCOL,
            "char_count": char_count,
            "base_version_id": req.base_version_id,
            "skeleton": nested,
            "provider_verified": False,
            "human_confirmed": True,
        }
        db.execute(
            """INSERT INTO versions
               (id,entity_type,entity_id,version_no,label,snapshot,reason,author_id)
               VALUES (%s,'chapter_skeleton',%s,%s,'skeleton_human_edit',%s,'human_edit',%s)""",
            (version_id, chapter_id, version_no, encode(snapshot), user["id"]),
        )
        db.commit()
        return ok({"version": _skeleton_snapshot({"id": version_id, "version_no": version_no, "label": "skeleton_human_edit", "reason": "human_edit", "snapshot": snapshot}),
                   "char_count": char_count, "human_confirmed": True}, message="人工修改后的章节骨架已保存，正文仍未修改")
    finally:
        db.close()


@router.get("/chapters/{chapter_id}/drafts")
def list_chapter_drafts(chapter_id: str, user: dict = Depends(get_current_user)):
    """List full-text candidates without reading or replacing contents.body."""
    db = connect()
    try:
        _content(db, chapter_id, user)
        rows = db.execute(
            """SELECT id,version_no,label,reason,snapshot,created_at FROM versions
               WHERE entity_type='chapter_draft' AND entity_id=%s
               ORDER BY created_at DESC LIMIT 30""",
            (chapter_id,),
        ).fetchall()
        return ok([_draft_snapshot(dict(row)) for row in rows], message="章节正文候选版本")
    finally:
        db.close()


@router.post("/chapters/{chapter_id}/draft")
def generate_chapter_draft(
    chapter_id: str,
    req: ChapterDraftRequest,
    user: dict = Depends(get_current_user),
):
    """Generate a provider-backed 2200-3000 character prose candidate."""
    db = connect()
    try:
        chapter = _content(db, chapter_id, user, write=True)
        novel_id = chapter.get("parent_id") if chapter.get("type") == "chapter" else chapter["id"]
        novel = _novel(db, str(novel_id), user)
        context = _chapter_skeleton_context(db, chapter, novel)
    finally:
        db.close()

    from app.gateway import complete

    mutation_id = req.client_mutation_id or new_id("draft")
    output = complete(
        run_id=None,
        node_key="chapter_draft",
        project_id=str(novel["project_id"]),
        user_id=str(user["id"]),
        task_type="chapter_draft",
        prompt_name="authoring.chapter_draft",
        client_mutation_id=mutation_id,
        variables={
            **context,
            "author_intent": req.author_intent or "（作者暂未补充意图，请依据已确认资料写出本章最自然、最有推进力的正文）",
            "target_chars": req.target_chars,
        },
    )
    chapter_output = output.get("chapter") if isinstance(output.get("chapter"), dict) else {}
    protocol_issues = _validate_chapter_draft_protocol(chapter_output)
    if protocol_issues:
        raise HTTPException(
            status_code=502,
            detail=("Provider returned a full-text candidate that failed the prose protocol: "
                    + "; ".join(protocol_issues[:8])
                    + ". No draft was changed."),
        )
    body_text = _draft_body_text(chapter_output.get("body"))
    char_count = _draft_char_count(body_text)
    if not 2200 <= char_count <= 3000:
        raise HTTPException(
            status_code=502,
            detail=f"Provider returned body with {char_count} visible characters; expected 2200-3000. No draft was changed.",
        )

    db = connect()
    try:
        ledger = db.execute(
            """SELECT id,provider,model,status FROM ai_calls
               WHERE project_id=%s AND client_mutation_id=%s
               ORDER BY created_at DESC LIMIT 1""",
            (novel["project_id"], mutation_id),
        ).fetchone()
        version_no = db.execute(
            """SELECT COALESCE(MAX(version_no),0)+1 AS next_version FROM versions
               WHERE entity_type='chapter_draft' AND entity_id=%s""",
            (chapter_id,),
        ).fetchone()["next_version"]
        version_id = new_id("ver")
        snapshot = {
            "artifact_type": "chapter_draft",
            "status": "ai_generated",
            "authoring_protocol": "reader-grounded-author-led-fulltext-v0.1",
            "target_chars": req.target_chars,
            "char_count": char_count,
            "author_intent": req.author_intent,
            "draft": {key: value for key, value in chapter_output.items()},
            "provider_verified": bool(ledger and ledger.get("status") == "succeeded"),
            "provider": ledger.get("provider") if ledger else None,
            "model": ledger.get("model") if ledger else None,
            "ai_call_id": str(ledger.get("id")) if ledger and ledger.get("id") else None,
        }
        db.execute(
            """INSERT INTO versions
               (id,entity_type,entity_id,version_no,label,snapshot,reason,author_id)
               VALUES (%s,'chapter_draft',%s,%s,'ai_draft',%s,'ai_generated',%s)""",
            (version_id, chapter_id, version_no, encode(snapshot), user["id"]),
        )
        db.commit()
        return ok({
            "version": _draft_snapshot({
                "id": version_id, "version_no": version_no, "label": "ai_draft",
                "reason": "ai_generated", "snapshot": snapshot,
            }),
            "provider_verified": snapshot["provider_verified"],
            "char_count": char_count,
        }, message="正文候选已生成，原正文未修改")
    finally:
        db.close()


@router.post("/chapters/{chapter_id}/drafts/save")
def save_chapter_draft(
    chapter_id: str,
    req: ChapterDraftSaveRequest,
    user: dict = Depends(get_current_user),
):
    """Save an edited full-text candidate as a version; it still does not publish to the chapter."""
    db = connect()
    try:
        _content(db, chapter_id, user, write=True)
        raw = dict(req.draft or {})
        nested = raw.get("draft") if isinstance(raw.get("draft"), dict) else raw
        title = str(nested.get("title") or "本章正文候选").strip()
        body = nested.get("body")
        body_text = _draft_body_text(body)
        char_count = _draft_char_count(body_text)
        if not body_text or not 2200 <= char_count <= 3000:
            raise HTTPException(status_code=422, detail=f"正文候选需保持2200-3000个可见字，当前为{char_count}字")
        version_no = db.execute(
            """SELECT COALESCE(MAX(version_no),0)+1 AS next_version FROM versions
               WHERE entity_type='chapter_draft' AND entity_id=%s""",
            (chapter_id,),
        ).fetchone()["next_version"]
        version_id = new_id("ver")
        snapshot = {
            "artifact_type": "chapter_draft",
            "status": "human_edited",
            "authoring_protocol": "reader-grounded-author-led-fulltext-v0.1",
            "char_count": char_count,
            "base_version_id": req.base_version_id,
            "draft": {"title": title, "body": body if isinstance(body, list) else [body_text]},
            "provider_verified": False,
            "human_confirmed": True,
        }
        db.execute(
            """INSERT INTO versions
               (id,entity_type,entity_id,version_no,label,snapshot,reason,author_id)
               VALUES (%s,'chapter_draft',%s,%s,'draft_human_edit',%s,'human_edit',%s)""",
            (version_id, chapter_id, version_no, encode(snapshot), user["id"]),
        )
        db.commit()
        return ok({
            "version": _draft_snapshot({
                "id": version_id, "version_no": version_no, "label": "draft_human_edit",
                "reason": "human_edit", "snapshot": snapshot,
            }),
            "char_count": char_count,
            "human_confirmed": True,
        }, message="人工修改后的正文候选已保存，原正文仍未修改")
    finally:
        db.close()


@router.get("/story-bible")
def list_story_bible(novel_id: str, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        novel = _novel(db, novel_id, user)
        rows = db.execute(
            """SELECT id,kind,title,body,meta,fact_type,approved,source_chapter,created_at,updated_at
               FROM knowledge_items WHERE project_id=%s AND (content_id=%s OR content_id IS NULL)
               AND is_deleted=FALSE ORDER BY kind,title""",
            (novel["project_id"], novel_id),
        ).fetchall()
        return ok([dict(row) for row in rows], message="故事 Bible")
    finally:
        db.close()


@router.post("/story-bible")
def create_story_bible(req: StoryBibleCreateRequest, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        novel = _novel(db, req.novel_id, user, write=True)
        item_id = new_id("bible")
        db.execute(
            """INSERT INTO knowledge_items
               (id,project_id,content_id,kind,title,body,meta,fact_type,source_chapter,approved,created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (item_id, novel["project_id"], req.novel_id, req.kind, req.title, req.body,
             encode(req.meta), req.fact_type, req.source_chapter, req.approved, user["id"]),
        )
        db.commit()
        return ok({"id": item_id, "novel_id": req.novel_id, "kind": req.kind, "title": req.title,
                   "body": req.body, "fact_type": req.fact_type, "approved": req.approved}, message="故事 Bible 条目已保存")
    finally:
        db.close()


@router.put("/story-bible/{item_id}")
def update_story_bible(item_id: str, req: StoryBibleUpdateRequest, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        row = db.execute("SELECT * FROM knowledge_items WHERE id=%s AND is_deleted=FALSE", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="story Bible item not found")
        item = dict(row)
        require_member(db, str(item["project_id"]), user, write=True)
        updates: list[str] = []
        values: list[Any] = []
        for key, value in (("title", req.title), ("body", req.body), ("fact_type", req.fact_type), ("source_chapter", req.source_chapter)):
            if value is not None:
                updates.append(f"{key}=%s")
                values.append(value)
        if req.meta is not None:
            updates.append("meta=%s")
            values.append(encode(req.meta))
        if not updates:
            return ok({"id": item_id, "changed": False}, message="没有需要更新的内容")
        values.append(item_id)
        db.execute(f"UPDATE knowledge_items SET {', '.join(updates)}, updated_at=now() WHERE id=%s", tuple(values))
        db.commit()
        return ok({"id": item_id, "changed": True}, message="故事 Bible 条目已更新")
    finally:
        db.close()


@router.post("/story-bible/{item_id}/confirm")
def confirm_story_bible(item_id: str, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        row = db.execute("SELECT * FROM knowledge_items WHERE id=%s AND is_deleted=FALSE", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="story Bible item not found")
        item = dict(row)
        require_member(db, str(item["project_id"]), user, write=True)
        db.execute("UPDATE knowledge_items SET approved=TRUE, updated_at=now() WHERE id=%s", (item_id,))
        db.execute(
            """INSERT INTO versions (id,entity_type,entity_id,label,snapshot,reason,author_id)
               VALUES (%s,'knowledge_item',%s,'bible_confirm',%s,'human_confirmed',%s)""",
            (new_id("ver"), item_id, encode({"kind": item.get("kind"), "title": item.get("title"), "body": item.get("body")}), user["id"]),
        )
        db.commit()
        return ok({"id": item_id, "approved": True, "confirmed_by": user.get("email", "")}, message="故事 Bible 已人工确认")
    finally:
        db.close()


@router.get("/story-bible/{item_id}/impact")
def story_bible_impact(item_id: str, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        row = db.execute("SELECT * FROM knowledge_items WHERE id=%s AND is_deleted=FALSE", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="story Bible item not found")
        item = dict(row)
        require_member(db, str(item["project_id"]), user)
        needle = str(item.get("title") or "")
        novel_id = item.get("content_id")
        chapters = db.execute(
            "SELECT id,title,meta,body FROM contents WHERE parent_id=%s AND type='chapter' AND is_deleted=FALSE ORDER BY created_at",
            (novel_id,),
        ).fetchall() if novel_id else []
        affected = []
        for chapter in chapters:
            text = _text_from_body(decode(chapter.get("body"), {}))
            if needle and (needle in text or needle in str(chapter.get("title") or "")):
                affected.append({"id": str(chapter["id"]), "title": chapter.get("title"), "seq": decode(chapter.get("meta"), {}).get("seq")})
        return ok({"item_id": item_id, "detector": "literal_reference_scan", "affected_chapters": affected,
                   "affected_count": len(affected), "requires_human_review": True}, message="影响分析完成")
    finally:
        db.close()


@router.get("/provider-roles")
def list_provider_roles(project_id: str, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        ensure_project_member(db, project_id, user)
        rows = db.execute(
            "SELECT task_type,provider,model,params,is_active,updated_at FROM model_routes WHERE task_type LIKE %s ORDER BY task_type",
            ("authoring_%",),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["role_key"] = str(item["task_type"])[len("authoring_"):]
            item["params"] = decode(item.get("params"), {})
            item["provider_status"] = _provider_status(str(item["provider"]))
            items.append(item)
        return ok({"project_id": project_id, "roles": items}, message="AI 角色路由")
    finally:
        db.close()


@router.put("/provider-roles")
def update_provider_role(req: ProviderRoleRequest, project_id: str, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        ensure_project_member(db, project_id, user, {"owner", "editor"})
        task_type = f"authoring_{req.role_key}"
        db.execute(
            """INSERT INTO model_routes (id,task_type,provider,model,params,fallback_json,is_active)
               VALUES (%s,%s,%s,%s,%s,'[]'::jsonb,TRUE)
               ON CONFLICT(task_type) DO UPDATE SET provider=EXCLUDED.provider,model=EXCLUDED.model,
                 params=EXCLUDED.params,is_active=TRUE,updated_at=now()""",
            (new_id("route"), task_type, req.provider, req.model, encode(req.params)),
        )
        db.commit()
        return ok({"project_id": project_id, "role_key": req.role_key, "task_type": task_type,
                   "provider": req.provider, "model": req.model, "provider_status": _provider_status(req.provider)}, message="AI 角色路由已保存")
    finally:
        db.close()


@router.post("/writing-events")
def record_writing_event(req: WritingEventRequest, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        item = _content(db, req.content_id, user, write=True)
        event_id = new_id("writing_event")
        db.execute(
            """INSERT INTO writing_events
               (id,project_id,novel_id,content_id,user_id,event_type,source,started_at,ended_at,
                duration_ms,chars_added,chars_removed,payload,client_event_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (user_id,client_event_id) WHERE client_event_id IS NOT NULL DO NOTHING""",
            (event_id, item["project_id"], item.get("parent_id") if item.get("type") == "chapter" else item["id"],
             req.content_id, user["id"], req.event_type, req.source, req.started_at, req.ended_at,
             req.duration_ms, req.chars_added, req.chars_removed, encode(req.payload), req.client_event_id),
        )
        db.commit()
        return ok({"event_id": event_id, "content_id": req.content_id, "event_type": req.event_type}, message="码字记录已保存")
    finally:
        db.close()


@router.get("/writing-events")
def list_writing_events(content_id: str, user: dict = Depends(get_current_user), limit: int = 100):
    db = connect()
    try:
        _content(db, content_id, user)
        rows = db.execute(
            "SELECT * FROM writing_events WHERE content_id=%s ORDER BY created_at DESC LIMIT %s",
            (content_id, min(max(limit, 1), 500)),
        ).fetchall()
        return ok([dict(row) for row in rows], message="码字记录")
    finally:
        db.close()


@router.post("/runs/clean-three-chapters/prepare")
def prepare_clean_run(req: CleanRunPrepareRequest, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        novel = _novel(db, req.novel_id, user, write=True)
        count = db.execute(
            "SELECT COUNT(*) AS count FROM contents WHERE parent_id=%s AND type='chapter' AND is_deleted=FALSE",
            (req.novel_id,),
        ).fetchone()["count"]
        run_id = new_id("authoring_run")
        db.execute(
            """INSERT INTO authoring_runs
               (id,project_id,novel_id,target_chapters,active_chapters_before,created_by)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (run_id, novel["project_id"], req.novel_id, req.target_chapters, count, user["id"]),
        )
        db.commit()
        return ok({"run_id": run_id, "status": "planned", "active_chapters_before": count,
                   "target_chapters": req.target_chapters, "requires_explicit_clean": True}, message="三章长跑已登记，等待清空历史")
    finally:
        db.close()


@router.post("/runs/{run_id}/clean")
def clean_run_history(run_id: str, req: CleanRunCleanRequest, user: dict = Depends(get_current_user)):
    if not req.confirm_clean:
        raise HTTPException(status_code=428, detail="必须明确 confirm_clean=true 才能清空历史章节")
    db = connect()
    try:
        row = db.execute("SELECT * FROM authoring_runs WHERE id=%s", (run_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="authoring run not found")
        run = dict(row)
        ensure_project_member(db, str(run["project_id"]), user, {"owner", "editor"})
        if run["status"] not in {"planned", "cleaned"}:
            raise HTTPException(status_code=409, detail="authoring run is not cleanable in current state")
        db.execute(
            "UPDATE contents SET is_deleted=TRUE,updated_at=now() WHERE parent_id=%s AND type='chapter' AND is_deleted=FALSE",
            (run["novel_id"],),
        )
        db.execute(
            "UPDATE authoring_runs SET status='cleaned',active_chapters_after=0,updated_at=now() WHERE id=%s",
            (run_id,),
        )
        db.commit()
        return ok({"run_id": run_id, "status": "cleaned", "active_chapters_after": 0,
                   "versions_retained": True, "provider_run_started": False}, message="历史章节已清空，版本记录保留")
    finally:
        db.close()


@router.get("/runs/{run_id}")
def get_authoring_run(run_id: str, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        row = db.execute("SELECT * FROM authoring_runs WHERE id=%s", (run_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="authoring run not found")
        run = dict(row)
        ensure_project_member(db, str(run["project_id"]), user)
        run["provider_evidence"] = decode(run.get("provider_evidence"), {})
        run["blind_reviews"] = decode(run.get("blind_reviews"), [])
        return ok(run, message="三章长跑状态")
    finally:
        db.close()


@router.post("/publication-variants/{variant_id}/human-receipt")
def record_human_receipt(variant_id: str, req: HumanReceiptRequest, user: dict = Depends(get_current_user)):
    db = connect()
    try:
        row = db.execute(
            """SELECT v.id,v.novel_id,v.platform,n.project_id
               FROM publication_variants v JOIN contents n ON n.id=v.novel_id
               WHERE v.id=%s AND n.is_deleted=FALSE""",
            (variant_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="publication variant not found")
        variant = dict(row)
        ensure_project_member(db, str(variant["project_id"]), user, {"owner", "editor"})
        receipt_id = new_id("receipt")
        db.execute(
            """INSERT INTO publication_human_receipts
               (id,variant_id,publish_record_id,platform,status,external_url,external_id,receipt_text,submitted_by,metadata)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (receipt_id, variant_id, req.publish_record_id, req.platform, req.status, req.external_url,
             req.external_id, req.receipt_text, user["id"], encode(req.metadata)),
        )
        if req.status == "accepted":
            db.execute(
                "UPDATE publication_variants SET publication_status='published',published_at=now(),published_url=%s,updated_at=now() WHERE id=%s",
                (req.external_url or None, variant_id),
            )
        db.commit()
        return ok({"receipt_id": receipt_id, "variant_id": variant_id, "status": req.status,
                   "publication_status": "published" if req.status == "accepted" else "unchanged"}, message="人工发布回执已记录")
    finally:
        db.close()
