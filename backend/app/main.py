import json
import os
import re as _re
import secrets
import uuid
from typing import Any

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import psycopg2.pool
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .core.security import get_current_user
from .core.byok import BYOKUnavailableError, stash_byok_key
from .core.errors import public_message
from .db import connect, decode, encode, init_db, new_id, row_to_dict
from .gateway import (
    BudgetExceeded,
    OutputValidationError,
    ProviderError,
    ProviderRateLimitError,
    complete,
)
from .config import settings
from .v7.quality.web_research import normalize_web_research_mode
from .core.authz import ensure_project_member, ok
from .services.novel_export import extract_body_text
from .schemas import (
    AiEditRequest,
    AiOperation,
    ApiResponse,
    ContentUpdate,
    HumanConfirm,
    NovelCreate,
    NovelGenerationSettingsUpdate,
    ShortStoryCreate,
    TitleRegenerateRequest,
    VersionRestore,
)
from .workers.tasks import confirm_human, create_run
from .api.v1.auth import router as auth_router
from .api.v1.config import router as config_router
from .api.v1.short_story import router as short_story_router
from .api.v1.dag_exec import router as dag_exec_router
from .api.v1.knowledge import router as knowledge_router
from .api.v1.hotspots import router as hotspots_router
from .api.v1.imitation import router as imitation_router
from .api.v1.author_style import router as author_style_router
from .api.v1.authoring import router as authoring_router
from .api.v1.scenes import router as scenes_router
from .api.v1.repairs import router as repairs_router
from .api.v1.platform_connections import router as platform_connections_router
from .api.v1.publish_schedule import router as publish_schedule_router
from .api.v1.overseas import router as overseas_router
from .api.v1.batch_endpoints import router as batch_router
from .api.v1.complete_api import router as complete_router
from .api.v1.ranking import library_router, router as ranking_router
from .api.v1.fusion import router as fusion_router
from .api.v1.deai import router as deai_router
from .api.v1.quality import router as quality_router
from .api.v1.publishing import router as publishing_router
from .api.v1.billing import router as billing_router
from .engine import router as engine_router
from .api.v1.skills import router as skills_router
from .api.v1.agents import router as agents_router
from .api.v1.chapter_scope import router as chapter_scope_router
from .apps.novel.router import router as novel_app_router
from .v7.api.router import router as v7_router
from .core.logging_config import setup_logging, get_logger
from .core.rate_limit import install_rate_limiter, limiter

setup_logging()
logger = get_logger(__name__)

from .core.observability import init_metrics, init_sentry  # noqa: E402

init_sentry("fastapi")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Seed built-in Skills & Agents on startup
    try:
        from .platform.skills.manager import SkillManager
        SkillManager.seed_builtin()
    except Exception:
        pass
    try:
        from .platform.agents.manager import AgentManager
        AgentManager.seed_builtin()
    except Exception:
        pass
    yield


app = FastAPI(title="星禾AI工作台 API", version="2.2.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(config_router)
app.include_router(short_story_router)
app.include_router(dag_exec_router)
app.include_router(knowledge_router)
app.include_router(hotspots_router)
app.include_router(imitation_router)
app.include_router(author_style_router)
app.include_router(authoring_router)
app.include_router(scenes_router)
app.include_router(repairs_router)
app.include_router(platform_connections_router)
app.include_router(publish_schedule_router)
app.include_router(overseas_router)
app.include_router(library_router)
app.include_router(batch_router)
app.include_router(complete_router)
app.include_router(ranking_router)
app.include_router(fusion_router, prefix="/api/v1")
app.include_router(deai_router)
app.include_router(quality_router)
app.include_router(publishing_router)
app.include_router(billing_router)
app.include_router(engine_router)
app.include_router(skills_router)
app.include_router(agents_router)
app.include_router(chapter_scope_router)
app.include_router(novel_app_router)
app.include_router(v7_router)
init_metrics(app)
install_rate_limiter(app)


@app.exception_handler(psycopg2.pool.PoolError)
async def database_pool_exhausted(_request: Request, exc: psycopg2.pool.PoolError):
    logger.error("database connection pool exhausted: %s", exc)
    return JSONResponse(status_code=503, content={
        "code": 503,
        "message": "database connection pool exhausted",
        "data": {"retryable": True},
    })


@app.exception_handler(BYOKUnavailableError)
async def handle_byok_unavailable(_request: Request, exc: BYOKUnavailableError):
    logger.error("BYOK request cannot be safely dispatched: %s", exc)
    return JSONResponse(status_code=503, content={
        "code": "BYOK_UNAVAILABLE",
        "message": "用户密钥暂时不可用，请稍后重试",
        "data": {"retryable": True},
    })

@app.exception_handler(BudgetExceeded)
async def handle_budget_exceeded(_request: Request, exc: BudgetExceeded):
    logger.warning("budget exceeded: %s", exc)
    return JSONResponse(status_code=402, content={
        "code": 402,
        "message": str(exc),
        "data": {"retryable": False},
    })


@app.exception_handler(ProviderRateLimitError)
async def handle_provider_rate_limit(_request: Request, exc: ProviderRateLimitError):
    data: dict[str, Any] = {"retryable": True}
    if exc.retry_after is not None:
        data["retry_after"] = exc.retry_after
    return JSONResponse(status_code=429, content={
        "code": 429,
        "message": str(exc) or "请求过于频繁，请稍后重试",
        "data": data,
    })


@app.exception_handler(OutputValidationError)
async def handle_output_validation(_request: Request, exc: OutputValidationError):
    logger.error("provider output invalid: %s", exc)
    return JSONResponse(status_code=502, content={
        "code": 502,
        "message": str(exc),
        "data": {"code": "PROVIDER_OUTPUT_INVALID", "retryable": False},
    })


@app.exception_handler(ProviderError)
async def handle_provider_error(_request: Request, exc: ProviderError):
    logger.error("provider error: %s", exc)
    return JSONResponse(status_code=502, content={
        "code": 502,
        "message": "AI 服务商暂时不可用，请稍后重试",
        "data": {"retryable": True, "detail": str(exc)},
    })


@app.exception_handler(HTTPException)
async def handle_http_exception(_request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("code", exc.status_code)
        message = detail.get("message", str(detail))
        extras = {k: v for k, v in detail.items() if k not in ("code", "message", "data")}
        explicit_data = detail.get("data")
        if explicit_data is not None and not extras:
            data = explicit_data
        elif isinstance(explicit_data, dict):
            data = {**explicit_data, **extras}
        else:
            data = extras or explicit_data
    else:
        code = exc.status_code
        message = str(detail)
        data = None
    return JSONResponse(status_code=exc.status_code, content={
        "code": code,
        "message": message,
        "data": data,
    })


@app.exception_handler(Exception)
async def handle_unhandled(_request: Request, exc: Exception):
    # P1-T5: never leak stack traces; return a sanitized 500 with a trace_id.
    trace_id = uuid.uuid4().hex
    logger.exception("unhandled exception trace_id=%s", trace_id)
    return JSONResponse(status_code=500, content={
        "code": 500,
        "message": "服务内部错误，请稍后重试",
        "data": {"trace_id": trace_id},
    })


app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """Protect unsafe requests authenticated by the refresh cookie."""
    public_auth_paths = {"/api/v1/auth/login", "/api/v1/auth/register"}
    if (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and request.url.path not in public_auth_paths
        and request.cookies.get("refresh_token")
    ):
        cookie_token = request.cookies.get("csrf_token")
        header_token = request.headers.get("X-CSRF-Token")
        if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
            return JSONResponse({"code": "CSRF_FAILED", "message": "CSRF 校验失败", "data": None}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def metrics_guard(request: Request, call_next):
    """Keep operational metrics private even when explicitly enabled."""
    if request.url.path == "/metrics":
        expected = os.getenv("METRICS_TOKEN", "").strip()
        supplied = request.headers.get("Authorization", "")
        if not expected or not secrets.compare_digest(supplied, f"Bearer {expected}"):
            return JSONResponse({"detail": "not found"}, status_code=404)
    return await call_next(request)

# Middleware: capture X-Api-* headers + resolved user for this request
@app.middleware("http")
async def capture_api_key(request: Request, call_next):
    from app.gateway import _request_api_key, _request_api_base_url, _request_model, _request_user_id
    from app.core.url_security import validate_ai_base_url
    from app.core.security import decode_token_payload
    from app.core.billing import enforce_quota
    tokens = []
    # Resolve the authenticated user from the Bearer token (soft-decode; no 401
    # here — the route dependency enforces auth). The result scopes the AI cost
    # ledger + plan quota to the real user instead of a shared project budget.
    auth = request.headers.get("Authorization", "")
    user_id = None
    if auth.lower().startswith("bearer "):
        payload = decode_token_payload(auth[7:].strip(), expected_type="access")
        if payload:
            user_id = payload.get("sub")
    if user_id:
        tokens.append((_request_user_id, _request_user_id.set(user_id)))
    key = request.headers.get("X-Api-Key")
    if key:
        tokens.append((_request_api_key, _request_api_key.set(key)))
    base_url = request.headers.get("X-Api-Base-Url")
    if base_url:
        if not key:
            return JSONResponse({"detail": "X-Api-Base-Url requires request-scoped X-Api-Key"}, status_code=400)
        try:
            base_url = validate_ai_base_url(base_url)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        tokens.append((_request_api_base_url, _request_api_base_url.set(base_url)))
    model = request.headers.get("X-Model")
    if model:
        # Plan gate: only enforce model quota on AI generation endpoints.
        _path = request.url.path
        _ai_paths = any(p in _path for p in ("/ai/", "/deai", "/bootstrap", "/continue", "/generate", "/prompts/"))
        if _ai_paths and user_id:
            try:
                enforce_quota(user_id, None, "model", model)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
                return JSONResponse(
                    status_code=exc.status_code,
                    content={
                        "code": detail.get("code", "FORBIDDEN"),
                        "message": detail.get("message", "forbidden"),
                        "data": {k: v for k, v in detail.items() if k not in ("code", "message")},
                    },
                )
        tokens.append((_request_model, _request_model.set(model)))
    try:
        return await call_next(request)
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def parse_content(row: dict[str, Any]) -> dict[str, Any]:
    row["body"] = decode(row["body"], {})
    row["meta"] = decode(row["meta"], {})
    return row


class BatchChapterRequest(BaseModel):
    chapter_count: int = Field(default=10, ge=1, le=50)


class ProjectCreate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=2000)


class AgentExecuteRequest(BaseModel):
    project_id: str
    variables: dict[str, Any] = Field(default_factory=dict)
    client_mutation_id: str | None = Field(default=None, max_length=100)


class ChapterReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str = Field(default="", max_length=1000)


class ExternalEvaluationRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    scope: str = Field(min_length=1, max_length=200)
    input_hash: str = Field(min_length=64, max_length=128)
    status: str = Field(default="completed", max_length=40)
    human_score: float | None = Field(default=None, ge=0, le=100)
    suspected_ai_score: float | None = Field(default=None, ge=0, le=100)
    ai_feature_score: float | None = Field(default=None, ge=0, le=100)
    scores: dict[str, Any] = Field(default_factory=dict)
    flagged_segments: list[dict[str, Any]] = Field(default_factory=list)


def load_content_for_user(content_id: str, user: dict, roles: set[str] | None = None) -> tuple[Any, dict]:
    conn = connect()
    content = row_to_dict(conn.execute("SELECT * FROM contents WHERE id = %s", (content_id,)).fetchone())
    if content is None:
        conn.close()
        raise HTTPException(status_code=404, detail="content not found")
    try:
        ensure_project_member(conn, content["project_id"], user, roles)
    except Exception:
        conn.close()
        raise
    return conn, content


def _require_v7_chapter_scope(conn: Any, content: dict[str, Any], operation: str) -> None:
    """Reject unscoped chapter AI operations before quota/provider work."""
    from .services.chapter_scope import ChapterScopeError, require_canonical_v7_chapter

    try:
        require_canonical_v7_chapter(conn, content, operation=operation)
    except ChapterScopeError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, **exc.details},
        ) from exc


def load_run_for_user(run_id: str, user: dict, roles: set[str] | None = None) -> tuple[Any, dict]:
    conn = connect()
    run = row_to_dict(conn.execute("SELECT * FROM workflow_runs WHERE id = %s", (run_id,)).fetchone())
    if run is None:
        conn.close()
        raise HTTPException(status_code=404, detail="run not found")
    try:
        ensure_project_member(conn, run["project_id"], user, roles)
    except Exception:
        conn.close()
        raise
    return conn, run


@app.get("/api/v1/healthz")
def healthz() -> ApiResponse:
    checks = {"status": "ok", "ai_provider": settings.ai_provider,
              # BUG-07: lets the UI warn before a keyless bootstrap fails.
              # Boolean only — never the key material.
              "ai_key_configured": bool(settings.deepseek_api_key),
              "web_research_provider": settings.web_research_provider,
              "web_research_key_configured": bool(
                  settings.web_research_provider == "tavily" and settings.tavily_api_key
              )}
    try:
        conn = connect()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"
    try:
        import redis
        r = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            socket_connect_timeout=2,
        )
        r.ping()
        checks["redis"] = "ok"
        # QA-001 follow-up: a dead Celery worker previously looked healthy here
        # while every async run sat pending forever. Surface worker liveness
        # (heartbeat keys kept by celery's redis transport) and queue depth.
        try:
            queue_depth = int(r.llen("celery"))
            checks["queue_depth"] = queue_depth
            worker_keys = r.keys("_kombu.binding.celery*")
            from .workers.celery_app import celery_app as _celery
            replies = _celery.control.inspect(timeout=1.0).ping() or {}
            if replies:
                checks["worker"] = f"ok: {len(replies)} online"
            elif queue_depth > 0 or worker_keys:
                checks["worker"] = "error: no worker responding (queue exists but nothing consumes it)"
            else:
                checks["worker"] = "error: no worker responding"
        except Exception as worker_exc:  # inspection is best-effort, never 500s healthz
            checks["worker"] = f"error: {worker_exc}"
        r.close()
    except Exception as e:
        checks["redis"] = f"error: {e}"
    return ok(checks)


@app.get("/api/v1/projects")
def list_projects(user: dict = Depends(get_current_user)) -> ApiResponse:
    conn = connect()
    rows = [dict(row) for row in conn.execute(
        "SELECT p.* FROM projects p JOIN project_members pm ON p.id = pm.project_id "
        "WHERE pm.user_id = %s AND p.is_deleted = FALSE ORDER BY p.created_at DESC",
        (user["id"],),
    ).fetchall()]
    conn.close()
    return ok(rows)


@app.post("/api/v1/projects")
def create_project(payload: ProjectCreate | None = Body(default=None), name: str = "新项目",
                   user: dict = Depends(get_current_user)) -> ApiResponse:
    # Prefer the request body's name; fall back to the ?name= query param so older
    # callers keep working. Ignoring a provided name was BUG-006.
    body_name = payload.name.strip() if payload and payload.name and payload.name.strip() else ""
    project_name = body_name or name
    description = payload.description if payload else ""
    # Plan gate: block project creation once the user's plan project limit is hit.
    from .core.billing import enforce_quota
    enforce_quota(user["id"], None, "max_projects")
    conn = connect()
    pid = new_id()
    conn.execute(
        "INSERT INTO projects (id, name, description, owner_id) VALUES (%s, %s, %s, %s)",
        (pid, project_name, description, user["id"]),
    )
    conn.execute(
        "INSERT INTO project_members (id, project_id, user_id, role) VALUES (%s, %s, %s, 'owner') ON CONFLICT DO NOTHING",
        (new_id(), pid, user["id"]),
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM projects WHERE id = %s", (pid,)).fetchone())
    conn.close()
    return ok(row)


@app.post("/api/v1/projects/{project_id}/novels")
def create_novel(project_id: str, payload: NovelCreate, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn = connect()
    project = row_to_dict(conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone())
    if project is None:
        conn.close()
        raise HTTPException(status_code=404, detail="project not found")
    ensure_project_member(conn, project_id, user, {"owner", "editor"})
    novel_id = new_id("cnt")
    # The title is a human gate. Preserve the raw idea in meta and keep the
    # library label neutral until the user chooses a generated/custom title.
    title = "待命名作品"
    body = {"type": "doc", "content": []}
    meta = payload.model_dump()
    genre_id = str(meta.get("genre_id") or "").strip()
    if genre_id:
        try:
            genre_id = str(uuid.UUID(genre_id))
        except ValueError:
            conn.close()
            raise HTTPException(status_code=422, detail="genre_id must be a valid genre pack id")
        genre_pack = row_to_dict(conn.execute(
            "SELECT id, name, slug, is_active FROM v7_genre_packs WHERE id = %s",
            (genre_id,),
        ).fetchone())
        if genre_pack is None:
            conn.close()
            raise HTTPException(status_code=422, detail="selected genre pack does not exist")
        if not genre_pack.get("is_active", True):
            conn.close()
            raise HTTPException(status_code=422, detail="selected genre pack is inactive")
        # Keep the id authoritative while also preserving a readable snapshot
        # for older planning nodes and audit/debug views.
        meta["genre_id"] = str(genre_pack["id"])
        meta["genre_name"] = genre_pack.get("name") or meta.get("genre")
        meta["genre_slug"] = genre_pack.get("slug") or ""
    conn.execute(
        """
        INSERT INTO contents (id, project_id, type, title, body, meta, status)
        VALUES (%s, %s, 'novel', %s ,%s, %s, 'draft')
        """,
        (novel_id, project_id, title, encode(body), encode(meta)),
    )
    conn.execute(
        "INSERT INTO versions (id, entity_type, entity_id, label, snapshot) VALUES (%s, 'content', %s, 'initial_idea', %s)",
        (new_id("ver"), novel_id, encode({"title": title, "body": body, "meta": meta})),
    )
    conn.commit()
    novel = parse_content(dict(conn.execute("SELECT * FROM contents WHERE id = %s", (novel_id,)).fetchone()))
    conn.close()
    return ok(novel)


class WorkImportRequest(BaseModel):
    """Import a full novel setting (outline / world / power system / rules / characters)
    from pasted text or an uploaded .md/.txt file. The raw text is preserved verbatim in
    meta.idea (灵感) and copied into meta.creative_bible + knowledge_items(kind='creative_bible')
    so the writing engine reads it as-is. No AI bootstrap is triggered, so the user's text is
    never rewritten."""
    text: str = Field(min_length=1, max_length=1_000_000)
    name: str = Field(default="", max_length=200)
    genre: str = Field(default="东方玄幻", max_length=80)
    platform: str = Field(default="fanqie", max_length=40)
    subgenre: str = Field(default="", max_length=80)
    style: str = Field(default="克制、悬疑、强画面感", max_length=160)
    target_words: int = Field(default=1000000, ge=1000, le=3000000)
    # Imported manuscripts are protected source material; live research stays
    # off until the user explicitly enables it for this book.
    web_research_mode: str = Field(default="off", pattern="^(off|required)$")


@app.post("/api/v1/projects/{project_id}/import")
def import_work(project_id: str, payload: WorkImportRequest, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn = connect()
    project = row_to_dict(conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone())
    if project is None:
        conn.close()
        raise HTTPException(status_code=404, detail="project not found")
    ensure_project_member(conn, project_id, user, {"owner", "editor"})
    novel_id = new_id("cnt")
    title = payload.name.strip() or "待命名作品"
    body = {"type": "doc", "content": []}
    # Verbatim preservation: the full imported text lives in idea and creative_bible.
    meta = {
        "idea": payload.text,
        "genre": payload.genre,
        "platform": payload.platform,
        "subgenre": payload.subgenre,
        "style": payload.style,
        "target_words": payload.target_words,
        "web_research_mode": payload.web_research_mode,
        "creative_bible": payload.text,
        "outline": payload.text,
        "imported": True,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    conn.execute(
        """
        INSERT INTO contents (id, project_id, type, title, body, meta, status)
        VALUES (%s, %s, 'novel', %s, %s, %s, 'draft')
        """,
        (novel_id, project_id, title, encode(body), encode(meta)),
    )
    conn.execute(
        "INSERT INTO versions (id, entity_type, entity_id, label, snapshot) VALUES (%s, 'content', %s, 'imported_setting', %s)",
        (new_id("ver"), novel_id, encode({"title": title, "body": body, "meta": meta})),
    )
    # Mirror the setting into knowledge_items so the writing engine reads it verbatim
    # instead of requiring an AI bootstrap that would rewrite the user's text.
    conn.execute(
        """
        INSERT INTO knowledge_items (id, project_id, content_id, kind, title, body, meta)
        VALUES (%s, %s, %s, 'creative_bible', '创作圣经（导入）', %s, %s)
        """,
        (new_id(), project_id, novel_id, payload.text, encode({})),
    )
    conn.commit()
    novel = parse_content(dict(conn.execute("SELECT * FROM contents WHERE id = %s", (novel_id,)).fetchone()))
    conn.close()
    return ok(novel)


def _generation_settings_payload(novel_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    mode = normalize_web_research_mode(meta.get("web_research_mode"))
    return {
        "novel_id": novel_id,
        "web_research_mode": mode,
        "provider": settings.web_research_provider,
        "provider_configured": bool(
            settings.web_research_provider == "tavily" and settings.tavily_api_key
        ),
        "cache_ttl_seconds": settings.web_research_cache_ttl_seconds,
    }


@app.get("/api/v1/novels/{novel_id}/generation-settings")
def get_generation_settings(novel_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn, novel = load_content_for_user(novel_id, user)
    try:
        if novel.get("type") != "novel":
            raise HTTPException(status_code=422, detail="content is not a novel")
        meta = decode(novel.get("meta"), {})
        return ok(_generation_settings_payload(novel_id, meta if isinstance(meta, dict) else {}))
    finally:
        conn.close()


@app.put("/api/v1/novels/{novel_id}/generation-settings")
def update_generation_settings(
    novel_id: str,
    payload: NovelGenerationSettingsUpdate,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    conn, novel = load_content_for_user(novel_id, user, {"owner", "editor"})
    try:
        if novel.get("type") != "novel":
            raise HTTPException(status_code=422, detail="content is not a novel")
        mode = normalize_web_research_mode(payload.web_research_mode)
        row = conn.execute("SELECT * FROM contents WHERE id = %s FOR UPDATE", (novel_id,)).fetchone()
        meta = decode(row.get("meta"), {})
        meta = dict(meta) if isinstance(meta, dict) else {}
        snapshot = {"title": row.get("title"), "body": decode(row.get("body"), {}), "meta": meta}
        conn.execute(
            "INSERT INTO versions (id, entity_type, entity_id, label, snapshot, reason, author_id) "
            "VALUES (%s, 'content', %s, 'before_generation_settings', %s, 'generation_settings', %s)",
            (new_id("ver"), novel_id, encode(snapshot), user["id"]),
        )
        meta["web_research_mode"] = mode
        conn.execute(
            "UPDATE contents SET meta=%s, updated_at=now() WHERE id=%s",
            (encode(meta), novel_id),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, entity_type, entity_id, action, details, created_at) "
            "VALUES (%s, 'content', %s, 'generation_settings.updated', %s, now())",
            (new_id("audit"), novel_id, encode({"web_research_mode": mode, "actor_id": user["id"]})),
        )
        conn.commit()
        return ok(_generation_settings_payload(novel_id, meta))
    finally:
        conn.close()


@app.get("/api/v1/contents")
def list_contents(project_id: str = Query(...), parent_id: str | None = None,
                  limit: int = Query(50, le=200), offset: int = Query(0, ge=0),
                  user: dict = Depends(get_current_user)) -> ApiResponse:
    conn = connect()
    # Verify project membership
    member = conn.execute(
        "SELECT 1 FROM project_members WHERE project_id = %s AND user_id = %s",
        (project_id, user["id"]),
    ).fetchone()
    if not member:
        conn.close()
        raise HTTPException(status_code=403, detail="not a project member")
    if parent_id is None:
        rows = conn.execute(
            """SELECT * FROM contents
               WHERE project_id = %s AND is_deleted = FALSE AND parent_id IS NULL
                 AND type <> 'chapter'
               ORDER BY created_at DESC LIMIT %s OFFSET %s""",
            (project_id, limit, offset),
    ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM contents WHERE project_id = %s AND is_deleted = FALSE AND parent_id = %s ORDER BY created_at ASC LIMIT %s OFFSET %s",
            (project_id, parent_id, limit, offset),
        ).fetchall()
    items = [parse_content(dict(row)) for row in rows]
    conn.close()
    return ok(items)


@app.get("/api/v1/contents/{content_id}")
def get_content(content_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn, row = load_content_for_user(content_id, user)
    conn.close()
    return ok(parse_content(row))


@app.put("/api/v1/contents/{content_id}")
def update_content(content_id: str, payload: ContentUpdate, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn, row = load_content_for_user(content_id, user, {"owner", "editor"})
    try:
        _require_v7_chapter_scope(conn, row, "content_update")
    except Exception:
        conn.rollback()
        conn.close()
        raise
    row = conn.execute("SELECT * FROM contents WHERE id = %s FOR UPDATE", (content_id,)).fetchone()
    snapshot = {"title": row["title"], "body": decode(row["body"], {}), "meta": decode(row["meta"], {})}
    if payload.client_mutation_id:
        existing = conn.execute(
            "SELECT id, reason FROM versions WHERE client_mutation_id = %s",
            (payload.client_mutation_id,),
        ).fetchone()
        if existing:
            current = parse_content(dict(row))
            current["sync_status"] = "conflict" if existing["reason"] == "offline_conflict" else "applied"
            current["mutation_replayed"] = True
            current["conflict_version_id"] = existing["id"] if existing["reason"] == "offline_conflict" else None
            conn.close()
            return ok(current)

    latest = conn.execute(
        "SELECT id, reason FROM versions WHERE entity_type = 'content' AND entity_id = %s ORDER BY created_at DESC LIMIT 1",
        (content_id,),
    ).fetchone()
    parent_version_id = latest["id"] if latest else None
    if payload.base_updated_at and payload.base_updated_at != row["updated_at"]:
        conflict_version_id = new_id("ver")
        incoming = {
            "title": payload.title if payload.title is not None else snapshot["title"],
            "body": payload.body if payload.body is not None else snapshot["body"],
            "meta": payload.meta if payload.meta is not None else snapshot["meta"],
        }
        conn.execute(
            """
            INSERT INTO versions (
                id, entity_type, entity_id, parent_version_id, label, snapshot,
                reason, author_id, client_mutation_id
            ) VALUES (%s, 'content', %s, %s, 'offline_conflict', %s, 'offline_conflict', %s, %s)
            """,
            (conflict_version_id, content_id, parent_version_id, encode(incoming), user["id"], payload.client_mutation_id),
        )
        conn.commit()
        current = parse_content(dict(row))
        current["sync_status"] = "conflict"
        current["conflict_version_id"] = conflict_version_id
        conn.close()
        return ok(current)

    conn.execute(
        """
        INSERT INTO versions (
            id, entity_type, entity_id, parent_version_id, label, snapshot,
            reason, author_id, client_mutation_id
        ) VALUES (%s, 'content', %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            new_id("ver"), content_id, parent_version_id, payload.label, encode(snapshot),
            "offline_sync" if payload.client_mutation_id else "manual", user["id"], payload.client_mutation_id,
        ),
    )
    if latest and latest.get("reason") == "offline_conflict":
        conn.execute("UPDATE versions SET reason = 'offline_conflict_resolved' WHERE id = %s", (latest["id"],))
    title = payload.title if payload.title is not None else row["title"]
    body = payload.body if payload.body is not None else snapshot["body"]
    meta = payload.meta if payload.meta is not None else snapshot["meta"]
    conn.execute(
        "UPDATE contents SET title = %s, body = %s, meta = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (title, encode(body), encode(meta), content_id),
    )
    conn.commit()
    updated = parse_content(dict(conn.execute("SELECT * FROM contents WHERE id = %s", (content_id,)).fetchone()))
    if payload.client_mutation_id:
        updated["sync_status"] = "applied"
    conn.close()
    return ok(updated)


@app.post("/api/v1/contents/{content_id}/synopsis")
@limiter.limit("10/minute")
def generate_reader_synopsis(
    request: Request,
    content_id: str,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """Generate and persist storefront synopsis without changing the idea.

    ``meta.idea`` is the author's source brief; ``meta.synopsis`` is reader-
    facing copy.  Keeping this as an explicit operation lets older books be
    repaired without silently spending Provider quota while browsing the
    library.
    """
    conn, content = load_content_for_user(content_id, user, {"owner", "editor"})
    conn.close()
    if content.get("type") != "novel":
        raise HTTPException(status_code=400, detail="content is not a novel")
    meta = content.get("meta") if isinstance(content.get("meta"), dict) else {}
    idea = str(meta.get("idea") or "").strip()
    if len(idea) < 4:
        raise HTTPException(status_code=422, detail="作品没有可用于生成简介的创作灵感")
    # Standalone package repair must use the same V7 platform/genre strategy
    # as the main planning chain; a static one-line hint would otherwise make
    # old books use a different synopsis policy from newly planned books.
    from .services.quality_profiles import compile_quality_directive, profile_from_context
    from .v7.quality.market_snapshot import market_benchmark_directive

    synopsis_profile = profile_from_context({
        **meta,
        "idea": idea,
        "genre": meta.get("genre") or meta.get("source_type") or "都市",
    })
    synopsis_strategy = synopsis_profile.get("quality_strategy") or {}
    synopsis_benchmark = synopsis_strategy.get("market_benchmark") or {}
    output = complete(
        run_id=None,
        node_key=None,
        project_id=content["project_id"],
        task_type="gen_synopsis",
        prompt_name="bootstrap.gen_synopsis",
        variables={
            "selected_title": str(content.get("title") or "待命名作品"),
            "genre": str(meta.get("genre") or meta.get("source_type") or "网文"),
            "style": str(meta.get("style") or "第三人称、冲突前置、节奏明快"),
            "idea": idea[:8000],
            "quality_profile_directive": (
                compile_quality_directive(synopsis_profile, chapter_number=1)
                + "\n简介独立于灵感和创作圣经，只写读者能看到的故事冲突、主角处境、行动目标与悬念。"
            ),
            "market_benchmark": market_benchmark_directive(synopsis_benchmark),
        },
        client_mutation_id=f"synopsis:{content_id}:{uuid.uuid4()}",
    )
    synopsis = str(output.get("synopsis") or "").strip()
    planning_markers = ("小说灵感", "项目完整设定", "核心设定", "金手指", "爽点设计", "主角:", "主角：")
    looks_like_planning_text = len(synopsis) > 220 and any(marker in synopsis for marker in planning_markers)
    if len(synopsis) < 40 or len(synopsis) > 240 or synopsis == idea or looks_like_planning_text:
        raise HTTPException(status_code=422, detail="AI 未返回合格的读者简介，请重试")
    points = output.get("selling_points") if isinstance(output.get("selling_points"), list) else []
    updated_meta = dict(meta)
    updated_meta["synopsis"] = synopsis
    updated_meta["selling_points"] = [str(item).strip() for item in points if str(item).strip()][:5]
    updated_meta["synopsis_source"] = "standalone_ai"
    conn = connect()
    conn.execute(
        """INSERT INTO versions (id, entity_type, entity_id, label, snapshot, reason, author_id)
           VALUES (%s, 'content', %s, 'synopsis_generated', %s, 'reader_synopsis', %s)""",
        (new_id("ver"), content_id,
         encode({"title": content.get("title"), "body": decode(content.get("body"), {}), "meta": meta}),
         user["id"]),
    )
    conn.execute(
        "UPDATE contents SET meta=%s, updated_at=now() WHERE id=%s",
        (encode(updated_meta), content_id),
    )
    conn.commit()
    updated = parse_content(dict(conn.execute("SELECT * FROM contents WHERE id=%s", (content_id,)).fetchone()))
    conn.close()
    return ok({"content": updated, "synopsis": synopsis, "selling_points": updated_meta["selling_points"]})


class BootstrapNovelRequest(BaseModel):
    class Config:
        extra = "allow"


@app.post("/api/v1/novels/{novel_id}/bootstrap")
@limiter.limit("10/minute")
async def bootstrap_novel(request: Request, novel_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn, novel = load_content_for_user(novel_id, user, {"owner", "editor"})
    conn.close()
    if novel["type"] != "novel":
        raise HTTPException(status_code=400, detail="content is not a novel")
    # P2-T10: validate the request body shape (permissive) instead of raw request.json()
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raw = {}
        BootstrapNovelRequest(**raw)
    except Exception:
        raw = {}
    run_id = create_run(novel["project_id"], novel_id,
                        api_key=request.headers.get("X-Api-Key", ""),
                        api_url=request.headers.get("X-Api-Base-Url", ""),
                        model=request.headers.get("X-Model", ""),
                        auto_confirm_title=False)
    return ok({"run_id": run_id})


@app.post("/api/v1/novels/{novel_id}/continue")
@limiter.limit("10/minute")
async def continue_novel(request: Request, novel_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """M2: Generate next chapter for an existing novel."""
    conn, novel = load_content_for_user(novel_id, user, {"owner", "editor"})
    conn.close()
    if novel["type"] != "novel":
        raise HTTPException(status_code=400, detail="content is not a novel")
    from .workers.tasks import gen_next_chapter_task
    result = gen_next_chapter_task.delay(novel_id, novel["project_id"],
                                         api_key_ref=stash_byok_key(request.headers.get("X-Api-Key", "")),
                                         api_url=request.headers.get("X-Api-Base-Url", ""),
                                         model=request.headers.get("X-Model", ""),
                                         canonical=True)
    return ok({"task_id": result.id, "novel_id": novel_id, "status": "dispatched"})


@app.post("/api/v1/chapters/{chapter_id}/manual-review")
@limiter.limit("20/minute")
async def manual_review_chapter(
    request: Request,
    chapter_id: str,
    payload: ChapterReviewRequest,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """Human quality gate for generated chapters.

    approve: marks the chapter as reviewed/accepted and therefore usable in the library.
    reject: marks it needs_rewrite and dispatches an in-place regeneration task.
    """
    conn, chapter = load_content_for_user(chapter_id, user, {"owner", "editor"})
    if chapter["type"] != "chapter":
        conn.close()
        raise HTTPException(status_code=400, detail="content is not a chapter")
    try:
        _require_v7_chapter_scope(conn, chapter, "manual_review")
    except Exception:
        conn.rollback()
        conn.close()
        raise
    now_iso = datetime.now(timezone.utc).isoformat()
    if payload.decision == "approve":
        from .v7.quality.review_gate import reviewed_gate_failures
        gate_failures = reviewed_gate_failures({
            "canonical_engine": "v7",
            **(chapter.get("meta") if isinstance(chapter.get("meta"), dict) else {}),
        })
        if gate_failures:
            conn.rollback()
            conn.close()
            raise HTTPException(
                status_code=409,
                detail={"code": "V7_REVIEW_GATE_FAILED", "message": "连续性或审阅证据未通过，不能人工批准", "failures": gate_failures},
            )
        conn.execute(
            """UPDATE contents
               SET status='reviewed',
                   meta=meta || %s,
                   updated_at=now()
               WHERE id=%s""",
            (encode({
                "quality_status": "accepted",
                "manual_review": {
                    "status": "approved",
                    "reviewed_by": user["id"],
                    "reviewed_at": now_iso,
                    "reason": payload.reason,
                },
            }), chapter_id),
        )
        conn.execute(
            """INSERT INTO audit_logs (id, entity_type, entity_id, action, details, created_at)
               VALUES (%s,'content',%s,'manual_review.approved',%s,now())""",
            (new_id(), chapter_id, encode({"reason": payload.reason, "user_id": user["id"]})),
        )
        conn.commit()
        updated = parse_content(dict(conn.execute("SELECT * FROM contents WHERE id=%s", (chapter_id,)).fetchone()))
        conn.close()
        return ok({"chapter": updated, "status": "reviewed"})

    conn.execute(
        """UPDATE contents
           SET status='needs_rewrite',
               meta=meta || %s,
               updated_at=now()
           WHERE id=%s""",
        (encode({
            "quality_status": "needs_review",
            "manual_review": {
                "status": "rejected",
                "reviewed_by": user["id"],
                "reviewed_at": now_iso,
                "reason": payload.reason,
            },
        }), chapter_id),
    )
    conn.execute(
        """INSERT INTO audit_logs (id, entity_type, entity_id, action, details, created_at)
           VALUES (%s,'content',%s,'manual_review.rejected',%s,now())""",
        (new_id(), chapter_id, encode({"reason": payload.reason, "user_id": user["id"]})),
    )
    conn.commit()
    conn.close()

    from .workers.tasks import regenerate_chapter_task
    task = regenerate_chapter_task.delay(
        chapter_id,
        payload.reason,
        api_key_ref=stash_byok_key(request.headers.get("X-Api-Key", "")),
        api_url=request.headers.get("X-Api-Base-Url", ""),
        model=request.headers.get("X-Model", ""),
        canonical=True,
    )
    tracking_conn = connect()
    tracking_conn.execute(
        "UPDATE contents SET meta=meta || %s, updated_at=now() WHERE id=%s",
        (encode({
            "manual_review": {
                "status": "regenerating",
                "reviewed_by": user["id"],
                "reviewed_at": now_iso,
                "reason": payload.reason,
                "task_id": task.id,
            },
        }), chapter_id),
    )
    tracking_conn.commit()
    tracking_conn.close()
    return ok({"chapter_id": chapter_id, "status": "regenerating", "task_id": task.id})


@app.post("/api/v1/chapters/{chapter_id}/external-evaluation")
@limiter.limit("20/minute")
async def register_external_evaluation(
    request: Request,
    chapter_id: str,
    payload: ExternalEvaluationRequest,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """Bind a real external detector/editor report to the exact chapter text.

    The endpoint records evidence only. It never calls a detector and never
    manufactures an AI/human score when the external report is absent.
    """
    conn, chapter = load_content_for_user(chapter_id, user, {"owner", "editor"})
    if chapter["type"] != "chapter":
        conn.close()
        raise HTTPException(status_code=400, detail="content is not a chapter")
    try:
        _require_v7_chapter_scope(conn, chapter, "external_evaluation")
        body = decode(chapter.get("body"), {})
        chapter_text = extract_body_text(body).strip()
        from .v7.quality.writing_methodology import (
            register_external_evaluation,
            transition_workflow_status,
        )
        raw_payload = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        try:
            evaluation = register_external_evaluation(chapter_text, raw_payload)
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(status_code=409, detail={
                "code": "EXTERNAL_EVALUATION_INVALID",
                "message": str(exc),
            }) from exc
        meta = decode(chapter.get("meta"), {})
        meta = meta if isinstance(meta, dict) else {}
        workflow = meta.get("writing_workflow")
        workflow = dict(workflow) if isinstance(workflow, dict) else {
            "schema_version": "writing-workflow-v1",
            "methodology_version": "1.0.0",
            "status": "external_pending",
            "legacy_evaluation_binding": True,
        }
        workflow["external_evaluation"] = evaluation
        next_status = evaluation["status"] if evaluation["status"] in {"external_95_5_0", "external_failed"} else "external_pending"
        try:
            if next_status in {"external_95_5_0", "external_failed"} and workflow.get("status") not in {"external_pending", next_status}:
                transition_workflow_status(workflow, "external_pending")
            transition_workflow_status(workflow, next_status)
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(status_code=409, detail={
                "code": "WRITING_WORKFLOW_TRANSITION_INVALID",
                "message": str(exc),
            }) from exc
        meta_update = {
            "external_evaluation": evaluation,
            "writing_workflow": workflow,
        }
        conn.execute(
            "UPDATE contents SET meta=meta || %s, updated_at=now() WHERE id=%s",
            (encode(meta_update), chapter_id),
        )
        conn.execute(
            "INSERT INTO versions (id, entity_type, entity_id, label, snapshot, reason, author_id) "
            "VALUES (%s, 'content', %s, 'external_evaluation', %s, 'external_evaluation_registered', %s)",
            (new_id("ver"), chapter_id, encode({"meta": meta_update}), user["id"]),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, entity_type, entity_id, action, details, created_at) "
            "VALUES (%s, 'content', %s, 'external_evaluation.registered', %s, now())",
            (new_id(), chapter_id, encode({"provider": evaluation["provider"], "scope": evaluation["scope"], "input_hash": evaluation["input_hash"], "user_id": user["id"]})),
        )
        conn.commit()
        updated = parse_content(dict(conn.execute("SELECT * FROM contents WHERE id=%s", (chapter_id,)).fetchone()))
        return ok({"chapter": updated, "external_evaluation": evaluation, "status": workflow["status"]})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class ChapterRewriteRequest(BaseModel):
    instructions: str = Field(default="", max_length=2000)
    mode: str = Field(default="full", pattern="^(full|partial)$")


@app.post("/api/v1/chapters/{chapter_id}/rewrite")
@limiter.limit("5/minute")
async def rewrite_chapter(
    request: Request,
    chapter_id: str,
    payload: ChapterRewriteRequest,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """主动重写已入库的章节。

    触发 V7 引擎重新生成指定章节，支持传入修改意见。
    重写过程是异步的，通过 /api/v1/chapters/{chapter_id}/regeneration 查询进度。
    """
    conn, chapter = load_content_for_user(chapter_id, user, {"owner", "editor"})
    if chapter["type"] != "chapter":
        conn.close()
        raise HTTPException(status_code=400, detail="content is not a chapter")
    try:
        _require_v7_chapter_scope(conn, chapter, "manual_rewrite")
    except Exception:
        conn.rollback()
        conn.close()
        raise

    now_iso = datetime.now(timezone.utc).isoformat()

    # 更新章节状态为 needs_rewrite
    conn.execute(
        """UPDATE contents
           SET status='needs_rewrite',
               meta=meta || %s,
               updated_at=now()
           WHERE id=%s""",
        (encode({
            "quality_status": "needs_rewrite",
            "manual_rewrite": {
                "status": "regenerating",
                "requested_by": user["id"],
                "requested_at": now_iso,
                "instructions": payload.instructions,
                "mode": payload.mode,
            },
        }), chapter_id),
    )
    conn.execute(
        """INSERT INTO audit_logs (id, entity_type, entity_id, action, details, created_at)
           VALUES (%s,'content',%s,'manual_rewrite.requested',%s,now())""",
        (new_id(), chapter_id, encode({"instructions": payload.instructions, "user_id": user["id"], "mode": payload.mode})),
    )
    conn.commit()
    conn.close()

    # 触发重写任务
    from .workers.tasks import regenerate_chapter_task
    task = regenerate_chapter_task.delay(
        chapter_id,
        payload.instructions,
        api_key_ref=stash_byok_key(request.headers.get("X-Api-Key", "")),
        api_url=request.headers.get("X-Api-Base-Url", ""),
        model=request.headers.get("X-Model", ""),
        canonical=True,
    )

    # 记录 task_id
    tracking_conn = connect()
    tracking_conn.execute(
        "UPDATE contents SET meta=meta || %s, updated_at=now() WHERE id=%s",
        (encode({
            "manual_rewrite": {
                "status": "regenerating",
                "requested_by": user["id"],
                "requested_at": now_iso,
                "instructions": payload.instructions,
                "mode": payload.mode,
                "task_id": task.id,
            },
        }), chapter_id),
    )
    tracking_conn.commit()
    tracking_conn.close()

    return ok({"chapter_id": chapter_id, "status": "regenerating", "task_id": task.id})


@app.get("/api/v1/chapters/{chapter_id}/regeneration")
def chapter_regeneration_status(
    chapter_id: str,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """Return the real Celery/result state for one authorized chapter rewrite."""
    conn, chapter = load_content_for_user(chapter_id, user)
    conn.close()
    if chapter["type"] != "chapter":
        raise HTTPException(status_code=400, detail="content is not a chapter")
    meta = chapter.get("meta") if isinstance(chapter.get("meta"), dict) else dict()

    # 优先检查 manual_rewrite（主动重写），然后检查 manual_review（审核拒绝重写）
    manual_rewrite = meta.get("manual_rewrite") if isinstance(meta.get("manual_rewrite"), dict) else dict()
    manual_review = meta.get("manual_review") if isinstance(meta.get("manual_review"), dict) else dict()

    task_id = str(manual_rewrite.get("task_id") or manual_review.get("task_id") or "")

    # 如果是主动重写且已完成
    if chapter.get("status") == "pending_review" and manual_rewrite.get("status") == "regenerated":
        return ok({"status": "completed", "chapter": chapter, "task_id": task_id, "source": "manual_rewrite"})
    # 如果是审核拒绝重写且已完成
    if chapter.get("status") == "pending_review" and manual_review.get("status") == "regenerated":
        return ok({"status": "pending_review", "chapter": chapter, "task_id": task_id, "source": "manual_review"})

    if not task_id:
        raise HTTPException(status_code=404, detail="chapter has no regeneration task")
    if not task_id:
        raise HTTPException(status_code=404, detail="chapter has no regeneration task")

    from .workers.celery_app import celery_app
    task = celery_app.AsyncResult(task_id)
    if task.state == "SUCCESS":
        refreshed_conn, refreshed = load_content_for_user(chapter_id, user)
        refreshed_conn.close()
        result = task.result if isinstance(task.result, dict) else dict()
        if refreshed.get("status") == "pending_review" and result.get("status") == "pending_review":
            return ok({"status": "pending_review", "chapter": refreshed, "task_id": task_id})
        return ok({"status": "failed", "task_id": task_id, "message": "重写任务未产出可复审章节，原文未被覆盖"})
    if task.state == "FAILURE":
        return ok({"status": "failed", "task_id": task_id, "message": "重写任务失败，原文保持未覆盖，可修改要求后重试"})
    return ok({"status": "regenerating", "task_id": task_id})


@app.post("/api/v1/novels/{novel_id}/expand-outline")
@limiter.limit("10/minute")
async def expand_outline(request: Request, novel_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """M2: Expand volume outline into chapter-level outlines."""
    conn, novel = load_content_for_user(novel_id, user, {"owner", "editor"})
    conn.close()
    if novel["type"] != "novel":
        raise HTTPException(status_code=400, detail="content is not a novel")
    from .workers.tasks import expand_outline_task
    result = expand_outline_task.delay(novel_id, novel["project_id"])
    return ok({"task_id": result.id, "novel_id": novel_id, "status": "dispatched"})


@app.post("/api/v1/novels/{novel_id}/chapters/batch")
@limiter.limit("5/minute")
async def batch_generate_chapters(
    request: Request,
    novel_id: str,
    payload: BatchChapterRequest,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """Queue 1-50 chapters and persist cancellation/progress state."""
    conn, novel = load_content_for_user(novel_id, user, {"owner", "editor"})
    if novel["type"] != "novel":
        conn.close()
        raise HTTPException(status_code=400, detail="content is not a novel")
    batch_id = new_id()
    start_row = conn.execute("""SELECT COALESCE(MAX((meta->>'seq')::int),0)+1 AS start_seq FROM contents
                                WHERE parent_id=%s AND type='chapter' AND is_deleted=FALSE""",
                             (novel_id,)).fetchone()
    start_seq = int((start_row or {}).get("start_seq") or 1)
    # 卷级门禁：if the batch starts a new planned volume and the previous volume's
    # gate was run and FAILED, block until the gate passes (or is re-run clean).
    novel_meta = decode(novel.get("meta"), {}) or {}
    volume_plan = novel_meta.get("volume_plan") or []
    if volume_plan:
        from app.services.narrative_engine import volume_for_chapter
        current_vol = volume_for_chapter(volume_plan, start_seq)
        if current_vol and int(current_vol.get("number", 0) or 0) > 1 and start_seq == int(current_vol.get("start_chapter", 0) or 0):
            prev_num = int(current_vol["number"]) - 1
            prev_gate = (novel_meta.get("volume_gates") or {}).get(str(prev_num))
            if prev_gate and not prev_gate.get("passed"):
                conn.close()
                raise HTTPException(status_code=409, detail={
                    "code": "VOLUME_GATE_FAILED",
                    "message": f"第{prev_num}卷卷级门禁未通过，先解决阻断项再生成第{current_vol['number']}卷",
                    "blockers": prev_gate.get("blockers", []),
                })
    conn.execute(
        """INSERT INTO generation_batches
           (id,project_id,novel_id,requested_count,start_seq,quality_status)
           VALUES (%s,%s,%s,%s,%s,'in_progress')""",
        (batch_id, novel["project_id"], novel_id, payload.chapter_count, start_seq),
    )
    conn.commit()
    conn.close()
    from .workers.tasks import batch_generate_chapters_task
    try:
        task = batch_generate_chapters_task.delay(
            batch_id,
            api_key_ref=stash_byok_key(request.headers.get("X-Api-Key", "")),
            api_url=request.headers.get("X-Api-Base-Url", ""),
            model=request.headers.get("X-Model", ""),
        )
    except Exception as exc:
        conn = connect()
        conn.execute(
            "UPDATE generation_batches SET status = 'failed', updated_at = now() WHERE id = %s",
            (batch_id,),
        )
        conn.commit()
        conn.close()
        raise HTTPException(status_code=503, detail="generation queue unavailable") from exc
    conn = connect()
    conn.execute("UPDATE generation_batches SET celery_task_id = %s WHERE id = %s", (task.id, batch_id))
    conn.commit()
    conn.close()
    return ok({"batch_id": batch_id, "task_id": task.id, "status": "pending"})


@app.get("/api/v1/novels/{novel_id}/generation-batches")
def list_novel_generation_batches(novel_id: str, limit: int = 20, offset: int = 0,
                                  user: dict = Depends(get_current_user)) -> ApiResponse:
    conn = None
    try:
        conn, novel = load_content_for_user(novel_id, user)
        batches = conn.execute("""SELECT * FROM generation_batches WHERE novel_id=%s
                                  ORDER BY created_at DESC LIMIT %s OFFSET %s""",
                               (novel_id, min(max(limit, 1), 100), max(offset, 0))).fetchall()
    finally:
        if conn is not None:
            conn.close()
    items = []
    for batch in batches:
        requested = max(int(batch.get("requested_count") or 0), 1)
        generated = int(batch.get("generated_count") or 0)
        reviewed = int(batch.get("reviewed_count") or 0)
        accepted = int(batch.get("accepted_count") or 0)
        needs_review = int(batch.get("needs_review_count") or 0)
        legacy = batch.get("status") == "succeeded" and generated == 0 and reviewed == 0 \
            and int(batch.get("completed_count") or 0) > 0
        quality_status = "legacy_unverified" if legacy else (batch.get("quality_status") or "in_progress")
        items.append({**dict(batch), "terminal_count": accepted + needs_review,
                      "generation_percent": round(generated / requested * 100),
                      "review_percent": round(reviewed / generated * 100) if generated else 0,
                      "acceptance_percent": round(accepted / requested * 100),
                      "recoverable": batch.get("status") in {"failed", "dispatch_failed"},
                      "quality_status": quality_status})
    return ok({"items": items, "count": len(items)})


@app.get("/api/v1/generation-batches/{batch_id}")
def get_generation_batch(batch_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn = connect()
    batch = row_to_dict(conn.execute("SELECT * FROM generation_batches WHERE id = %s", (batch_id,)).fetchone())
    if not batch:
        conn.close()
        raise HTTPException(status_code=404, detail="batch not found")
    ensure_project_member(conn, batch["project_id"], user)
    conn.close()
    return ok(batch)


@app.post("/api/v1/generation-batches/{batch_id}/cancel")
def cancel_generation_batch(batch_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn = connect()
    batch = row_to_dict(conn.execute("SELECT * FROM generation_batches WHERE id = %s", (batch_id,)).fetchone())
    if not batch:
        conn.close()
        raise HTTPException(status_code=404, detail="batch not found")
    ensure_project_member(conn, batch["project_id"], user, {"owner", "editor"})
    if batch["status"] in {"succeeded", "failed", "cancelled"}:
        conn.close()
        return ok({"batch_id": batch_id, "status": batch["status"]})
    conn.execute(
        "UPDATE generation_batches SET cancel_requested = TRUE, status = 'cancelled', updated_at = now() WHERE id = %s",
        (batch_id,),
    )
    conn.commit()
    conn.close()
    current_ordinal = batch.get("current_ordinal")
    return ok({"batch_id": batch_id, "status": "cancelled", "in_flight": current_ordinal is not None,
               "current_ordinal": current_ordinal,
               "message": "已停止后续槽位；当前正在执行的章节可能完成后才停止" if current_ordinal is not None
                          else "已取消，尚无正在执行的槽位"})


@app.post("/api/v1/generation-batches/{batch_id}/resume")
def resume_generation_batch(request: Request, batch_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """Re-dispatch an interrupted batch; it continues from completed_count."""
    conn = connect()
    batch = row_to_dict(conn.execute("SELECT * FROM generation_batches WHERE id = %s", (batch_id,)).fetchone())
    if not batch:
        conn.close()
        raise HTTPException(status_code=404, detail="batch not found")
    ensure_project_member(conn, batch["project_id"], user, {"owner", "editor"})
    if batch["status"] != "failed":
        conn.close()
        raise HTTPException(status_code=409, detail=f"batch is {batch['status']}, only failed can resume")
    conn.execute(
        "UPDATE generation_batches SET status = 'pending', cancel_requested = FALSE, updated_at = now() WHERE id = %s",
        (batch_id,),
    )
    conn.commit()
    conn.close()
    from .workers.tasks import batch_generate_chapters_task
    try:
        task = batch_generate_chapters_task.delay(
            batch_id,
            api_key_ref=stash_byok_key(request.headers.get("X-Api-Key", "")),
            api_url=request.headers.get("X-Api-Base-Url", ""),
            model=request.headers.get("X-Model", ""),
        )
    except Exception as exc:
        conn = connect()
        conn.execute(
            "UPDATE generation_batches SET status = %s, error = %s, updated_at = now() WHERE id = %s",
            (batch["status"], f"resume dispatch failed: {exc}", batch_id),
        )
        conn.commit()
        conn.close()
        raise HTTPException(status_code=503, detail="generation queue unavailable") from exc
    conn = connect()
    conn.execute("UPDATE generation_batches SET celery_task_id = %s WHERE id = %s", (task.id, batch_id))
    conn.commit()
    conn.close()
    return ok({"batch_id": batch_id, "task_id": task.id, "status": "pending",
               "completed_count": batch["completed_count"], "requested_count": batch["requested_count"]})


@app.post("/api/v1/projects/{project_id}/short-stories")
@limiter.limit("10/minute")
async def create_short_story(request: Request, project_id: str, payload: ShortStoryCreate,
                             user: dict = Depends(get_current_user)) -> ApiResponse:
    """M3: Create and bootstrap a short story."""
    conn = connect()
    project = row_to_dict(conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone())
    if project is None:
        conn.close()
        raise HTTPException(status_code=404, detail="project not found")
    ensure_project_member(conn, project_id, user, {"owner", "editor"})
    sid = new_id()
    conn.execute(
        "INSERT INTO contents (id, project_id, type, title, body, meta, status) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (sid, project_id, "short_story", payload.idea[:26], encode({"type":"doc","content":[]}),
         encode(payload.model_dump()), "draft"),
    )
    conn.commit()
    conn.close()
    from .workers.tasks import bootstrap_short_story_task
    result = bootstrap_short_story_task.delay(project_id, sid)
    return ok({"short_id": sid, "task_id": result.id, "status": "dispatched"})


@app.post("/api/v1/contents/{content_id}/fanout")
async def fanout_content(
    content_id: str,
    platforms: str = "wechat,toutiao,xiaohongshu",
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """M3: Fan-out source content to multiple social platforms."""
    from app.services.social_media import PLATFORMS
    conn, source = load_content_for_user(content_id, user, {"owner", "editor"})

    # Extract source text
    src_body = source.get("body", {})
    src_text = ""
    if isinstance(src_body, dict) and src_body.get("content"):
        src_text = "\n".join(c.get("text", "") for c in src_body["content"] if isinstance(c, dict))

    platform_list = [p.strip() for p in platforms.split(",") if p.strip() in PLATFORMS]
    results = []
    for pkey in platform_list:
        p = PLATFORMS[pkey]
        derived_id = new_id()
        # Generate platform-specific content via AI
        try:
            output = complete(
                run_id=None, node_key=None, project_id=source["project_id"],
                task_type=f"fanout_{pkey}", prompt_name="editor.rewrite",
                variables={"selection": src_text[:3000], "instruction": f"改写为{p['name']}格式: {p['style']}"},
            )
            body = {"type": "doc", "content": [{"type": "paragraph", "text": output.get("text", src_text[:500])}]}
        except (ProviderError, BudgetExceeded) as exc:
            results.append({"platform": pkey, "status": "failed", "error": str(exc)})
            continue

        conn.execute(
            "INSERT INTO contents (id, project_id, parent_id, type, title, body, meta, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (derived_id, source["project_id"], content_id, p["type"],
             source["title"] + f" ({p['name']}版)", encode(body),
             encode({"platform": pkey, "source_id": content_id, "style": p["style"]}), "draft"),
        )
        conn.execute(
            "INSERT INTO derivations (id, source_content_id, derived_content_id) VALUES (%s,%s,%s)",
            (new_id(), content_id, derived_id),
        )
        results.append({"platform": pkey, "type": p["type"], "derived_id": derived_id,
                        "status": "succeeded"})
    conn.commit()
    conn.close()
    return ok({"fanout_count": len(results), "items": results})


@app.post("/api/v1/contents/{content_id}/video-script")
@limiter.limit("20/minute")
async def generate_video_script(
    request: Request,
    content_id: str,
    platform: str = "douyin",
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """M3: Generate short video script from content."""
    from app.services.social_media import VIDEO_PLATFORMS
    if platform not in VIDEO_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"unknown platform: {platform}")
    p = VIDEO_PLATFORMS[platform]
    conn, source = load_content_for_user(content_id, user, {"owner", "editor"})
    conn.close()
    # Generate via AI
    from .gateway import complete
    body_text = ""
    if isinstance(source.get("body"), dict):
        body_text = "\n".join(c.get("text","") for c in source["body"].get("content",[]))
    output = complete(run_id=None, node_key=None, project_id=source["project_id"],
                      task_type="gen_video_script", prompt_name="social.gen_video",
                      variables={"body": body_text[:3000], "platform": p["name"], "style": p["style"], "max_duration": p["max_duration"]})
    return ok({"platform": platform, "script": output})


def _retry_after_generation_fix(node_key: str, status: str, output: Any) -> dict[str, Any] | None:
    """Expose a one-time recovery path for the pre-2.52 false truncation rejection.

    Version 2.51 rejected a compressed chapter whenever the provider response
    reached its token ceiling, even when the recovered prose was inside the
    requested character range and ended naturally.  Version 2.52 records
    ``terminal_complete=...`` and accepts that exact safe case.  Keep this
    recognizer deliberately narrow so unrelated deterministic contract or
    quality failures cannot be force-retried.
    """
    if node_key != "write_chapter_draft" or status != "failed" or not isinstance(output, dict):
        return None
    if output.get("retryable") is not False or output.get("failure_kind") != "generation_contract":
        return None
    error = str(output.get("error") or "")
    if "terminal_complete=" in error:
        return None
    match = _re.search(
        r"final_candidate=(\d+),minimum=(\d+),maximum=(\d+),"
        r"retry_mode=compress_recover,provider_truncated=True(?:,|$)",
        error,
    )
    if not match:
        return None
    final_chars, minimum_chars, maximum_chars = (int(value) for value in match.groups())
    if not minimum_chars <= final_chars <= maximum_chars:
        return None
    from .v7.generation.generation_engine import CHAPTER_SINGLE_PASS_GENERATION_VERSION

    if CHAPTER_SINGLE_PASS_GENERATION_VERSION != "2.52.0":
        return None
    return {
        "available": bool(match and minimum_chars <= final_chars <= maximum_chars),
        "from_version": "pre-2.52.0",
        "to_version": CHAPTER_SINGLE_PASS_GENERATION_VERSION,
        "reason": "旧版将字数合格且已收束的截断恢复稿误判为失败；2.52 已加入终止完整性判断。",
        "final_chars": final_chars,
        "minimum_chars": minimum_chars,
        "maximum_chars": maximum_chars,
    }


def _hydrate_run(conn, run: dict) -> dict:
    run = dict(run)
    run_id = str(run["id"])
    nodes = [dict(row) for row in conn.execute("SELECT * FROM run_nodes WHERE run_id = %s ORDER BY node_key", (run_id,)).fetchall()]
    for node in nodes:
        node["output"] = decode(node["output"], {})
        retry_after_fix = _retry_after_generation_fix(
            str(node.get("node_key") or ""),
            str(node.get("status") or ""),
            node["output"],
        )
        if retry_after_fix:
            node["output"] = {**node["output"], "retry_after_fix": retry_after_fix}
    run["context"] = decode(run["context"], {})
    run["nodes"] = nodes
    return run


@app.get("/api/v1/runs/latest")
def get_latest_run(project_id: str | None = None, novel_id: str | None = None, user: dict = Depends(get_current_user)) -> ApiResponse:
    """Restore the user's newest workflow after a browser reload or device switch."""
    conn = connect()
    params: list[str] = [user["id"]]
    extra_filters = ""
    if project_id:
        extra_filters += " AND wr.project_id = %s"
        params.append(project_id)
    if novel_id:
        extra_filters += " AND wr.novel_id = %s"
        params.append(novel_id)
    run = conn.execute(
        """SELECT wr.*
           FROM workflow_runs wr
           JOIN project_members pm ON pm.project_id = wr.project_id
           WHERE pm.user_id = %s""" + extra_filters + """
           ORDER BY wr.created_at DESC, wr.id DESC
           LIMIT 1""",
        tuple(params),
    ).fetchone()
    if not run:
        conn.close()
        raise HTTPException(status_code=404, detail="workflow run not found")
    hydrated = _hydrate_run(conn, dict(run))
    conn.close()
    return ok(hydrated)


@app.get("/api/v1/history")
def list_generation_history(
    project_id: str = Query(...),
    novel_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """Return one paginated history across the legacy V6 and canonical V7 runs.

    V6 ``workflow_runs`` is the blueprint/bootstrap progress contract, while
    V7 ``v7_agent_runs`` is the canonical chapter-generation trace.  They are
    intentionally kept in their source tables, but the product must expose a
    single read model so a project never appears to have lost its history
    merely because a run was created by the other engine.
    """
    conn = connect()
    member = conn.execute(
        "SELECT 1 FROM project_members WHERE project_id=%s AND user_id=%s",
        (project_id, user["id"]),
    ).fetchone()
    if not member:
        conn.close()
        raise HTTPException(status_code=403, detail="not a project member")

    v6_filters = ["wr.project_id=%s"]
    v6_params: list[str] = [project_id]
    v7_filters = [
        "n.project_id=%s",
        "n.type='novel'",
        "n.is_deleted=FALSE",
    ]
    v7_params: list[str] = [project_id]
    if novel_id:
        v6_filters.append("wr.novel_id=%s")
        v6_params.append(novel_id)
        v7_filters.append("ar.novel_id=%s")
        v7_params.append(novel_id)

    v6_sql = f"""
        SELECT
            wr.id::text AS id,
            wr.project_id::text AS project_id,
            wr.novel_id::text AS novel_id,
            COALESCE(n.title, '未命名作品') AS novel_title,
            'v6'::text AS engine,
            COALESCE(wr.workflow_key, 'bootstrap') AS run_type,
            wr.status::text AS status,
            NULL::integer AS chapter_number,
            (
                SELECT COUNT(*)::integer FROM run_nodes rn WHERE rn.run_id=wr.id
            ) AS step_count,
            NULL::integer AS total_tokens,
            NULL::double precision AS total_cost,
            COALESCE(wr.created_at, wr.started_at, wr.updated_at) AS created_at,
            COALESCE(wr.updated_at, wr.finished_at, wr.created_at) AS updated_at
        FROM workflow_runs wr
        LEFT JOIN contents n ON n.id=wr.novel_id
        WHERE {' AND '.join(v6_filters)}
    """
    v7_sql = f"""
        SELECT
            ar.id::text AS id,
            n.project_id::text AS project_id,
            ar.novel_id::text AS novel_id,
            COALESCE(n.title, '未命名作品') AS novel_title,
            'v7'::text AS engine,
            ar.run_type::text AS run_type,
            ar.status::text AS status,
            ar.chapter_number,
            COALESCE(
                ar.step_count,
                (SELECT COUNT(*)::integer FROM v7_agent_traces at WHERE at.run_id=ar.id),
                0
            ) AS step_count,
            ar.total_tokens,
            ar.total_cost,
            COALESCE(ar.created_at, ar.started_at) AS created_at,
            COALESCE(ar.updated_at, ar.completed_at, ar.created_at, ar.started_at) AS updated_at
        FROM v7_agent_runs ar
        JOIN contents n ON n.id=ar.novel_id
        WHERE {' AND '.join(v7_filters)}
    """
    params = tuple(v6_params + v7_params)
    history_cte = f"WITH history AS ({v6_sql} UNION ALL {v7_sql})"
    total = int(conn.execute(f"{history_cte} SELECT COUNT(*) AS total FROM history", params).fetchone()["total"])
    rows = conn.execute(
        f"""{history_cte}
            SELECT * FROM history
            ORDER BY created_at DESC NULLS LAST, id DESC
            LIMIT %s OFFSET %s
        """,
        params + (limit, offset),
    ).fetchall()
    conn.close()
    return ok({
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@app.get("/api/v1/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn, run = load_run_for_user(run_id, user)
    hydrated = _hydrate_run(conn, run)
    conn.close()
    return ok(hydrated)


@app.get("/api/v1/runs/{run_id}/ledger")
def get_run_ledger(run_id: str, limit: int = 50, user: dict = Depends(get_current_user)) -> ApiResponse:
    """denova 融合：run 级不可变事件账本（audit_logs）。"""
    conn, _run = load_run_for_user(run_id, user)
    conn.close()
    from app.services.fusion_deep_workflow import get_event_ledger
    return ok(get_event_ledger(run_id, limit=min(max(limit, 1), 200)))


@app.get("/api/v1/contents/{content_id}/fact-chain")
def get_content_fact_chain(content_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """show-me-the-story 融合：章节事实事务链（可回溯的 reconcile 变更记录）。"""
    conn, _content = load_content_for_user(content_id, user)
    conn.close()
    from app.services.fusion_deep_workflow import get_fact_chain
    return ok(get_fact_chain(content_id))


@app.get("/api/v1/runs/{run_id}/events")
async def run_events(run_id: str, user: dict = Depends(get_current_user)):
    conn, _run = load_run_for_user(run_id, user)
    conn.close()

    async def event_stream():
        import asyncio
        # Cap the long-poll so an abandoned or stuck run can never hold a
        # connection/worker forever; the client reconnects with EventSource.
        MAX_TICKS = int(os.getenv("SSE_RUN_EVENTS_MAX_TICKS", "600"))  # ~10 min at 1s
        seq = 0
        for _tick in range(MAX_TICKS):
            seq += 1
            yield f"id: {seq}\n\n"
            await asyncio.sleep(1)
            conn = connect()
            row = conn.execute("SELECT status FROM workflow_runs WHERE id = %s", (run_id,)).fetchone()
            nodes = conn.execute(
                "SELECT node_key, status, output, error, updated_at FROM run_nodes "
                "WHERE run_id = %s ORDER BY node_key", (run_id,),
            ).fetchall()
            conn.close()
            if row and row["status"] in (
                "succeeded",
                "failed",
                "needs_review",
                "waiting_human",
                "pending_budget",
                "pending_provider",
                "dispatch_failed",
                "cancelled",
            ):
                for n in nodes:
                    seq += 1
                    event = dict(n)
                    event["output"] = decode(event.get("output"), {})
                    event["updated_at"] = str(event.get("updated_at") or "")
                    yield f"id: {seq}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield f"id: {seq+1}\ndata: {json.dumps({'status': row['status']})}\n\n"
                return
        # Timed out without a terminal state: tell the client to reconnect.
        yield f"id: {seq+1}\ndata: {json.dumps({'status': 'timeout', 'reconnect': True})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/v1/runs/{run_id}/nodes/n2/confirm")
async def confirm_title(request: Request, run_id: str, payload: HumanConfirm, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn, _run = load_run_for_user(run_id, user, {"owner", "editor"})
    conn.close()
    confirm_human(run_id, payload.selected_title,
                  api_key=request.headers.get("X-Api-Key", ""),
                  api_url=request.headers.get("X-Api-Base-Url", ""),
                  model=request.headers.get("X-Model", ""))
    return ok({"run_id": run_id, "selected_title": payload.selected_title})


@app.post("/api/v1/runs/{run_id}/titles/regenerate")
@limiter.limit("10/minute")
def regenerate_run_titles(
    request: Request,
    run_id: str,
    payload: TitleRegenerateRequest,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    conn, run = load_run_for_user(run_id, user, {"owner", "editor"})
    human = conn.execute(
        "SELECT status FROM run_nodes WHERE run_id=%s AND node_key='human_confirm_title'",
        (run_id,),
    ).fetchone()
    context = run["context"] if isinstance(run.get("context"), dict) else {}
    conn.close()
    if not human or human["status"] != "waiting_human":
        raise HTTPException(409, "书名只能在人工选名阶段重新生成")

    from .services.quality_profiles import compile_quality_directive, profile_from_context
    from .v7.quality.market_snapshot import market_benchmark_directive

    title_profile = profile_from_context(context)
    title_strategy = title_profile.get("quality_strategy") or {}
    title_benchmark = title_strategy.get("market_benchmark") or {}

    result = complete(
        run_id=run_id,
        node_key="human_confirm_title",
        project_id=run["project_id"],
        task_type="regenerate_titles",
        prompt_name="bootstrap.regenerate_titles",
        variables={
            **context,
            "feedback": payload.feedback,
            "quality_profile_directive": compile_quality_directive(title_profile, chapter_number=1),
            "market_benchmark": market_benchmark_directive(title_benchmark),
        },
        client_mutation_id=f"title-regenerate:{run_id}:{new_id('ttl')}",
    )
    titles = result["title_candidates"]
    context["title_candidates"] = titles
    context["title_regeneration_count"] = int(context.get("title_regeneration_count") or 0) + 1
    conn = connect()
    conn.execute(
        "UPDATE workflow_runs SET context=%s,updated_at=now() WHERE id=%s",
        (encode(context), run_id),
    )
    conn.commit(); conn.close()
    return ok({"run_id": run_id, "title_candidates": titles})


@app.post("/api/v1/runs/{run_id}/nodes/{node_key}/retry")
async def retry_node(
    run_id: str,
    node_key: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    conn, run = load_run_for_user(run_id, user, {"owner", "editor"})
    node = row_to_dict(conn.execute(
        "SELECT status, output, error FROM run_nodes WHERE run_id = %s AND node_key = %s FOR UPDATE",
        (run_id, node_key),
    ).fetchone())
    if node is None:
        conn.close()
        raise HTTPException(status_code=404, detail="创作步骤不存在")
    node_output = decode(node.get("output"), {})
    if node.get("status") not in {"failed", "pending_provider"}:
        conn.close()
        raise HTTPException(
            status_code=409,
            detail="该步骤不是可恢复的模型故障；质量待重写需要先调整策划或正文，不能原样重跑。",
        )
    retry_after_fix = _retry_after_generation_fix(node_key, str(node.get("status") or ""), node_output)
    if isinstance(node_output, dict) and node_output.get("retryable") is False and not retry_after_fix:
        conn.close()
        raise HTTPException(
            status_code=409,
            detail="该失败已判定为确定性契约问题，原样重试不会成功。请先修正失败原因。",
        )
    run_context = decode(run.get("context"), {})
    if not isinstance(run_context, dict):
        run_context = {}
    if retry_after_fix:
        audit_entries = list(run_context.get("code_fix_retries") or [])
        audit_entries.append({
            "node_key": node_key,
            "retried_at": datetime.now(timezone.utc).isoformat(),
            "from_version": retry_after_fix["from_version"],
            "to_version": retry_after_fix["to_version"],
            "reason": retry_after_fix["reason"],
            "previous_error": str(node.get("error") or node_output.get("error") or "")[:500],
        })
        run_context["code_fix_retries"] = audit_entries[-10:]
    conn.execute(
        "UPDATE run_nodes SET status = 'pending', output = '{}', error = NULL WHERE run_id = %s AND node_key = %s",
        (run_id, node_key),
    )
    conn.execute(
        "UPDATE workflow_runs SET status = 'running', current_node_key = %s, context = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (node_key, encode(run_context), run_id),
    )
    conn.commit()
    conn.close()
    from .workers.tasks import dispatch_bootstrap_run
    dispatch_bootstrap_run(
        run_id,
        node_key,
        request.headers.get("X-Api-Key", ""),
        request.headers.get("X-Api-Base-Url", ""),
        request.headers.get("X-Model", ""),
    )
    return ok({
        "run_id": run_id,
        "node_key": node_key,
        "retry_mode": "after_code_fix" if retry_after_fix else "transient_failure",
        "generation_version": retry_after_fix.get("to_version") if retry_after_fix else None,
    })


@app.post("/api/v1/runs/{run_id}/restart")
async def restart_run(run_id: str, request: Request, user: dict = Depends(get_current_user)) -> ApiResponse:
    """Restart a run in place: reset every non-succeeded node to pending and
    re-dispatch bootstrap from the earliest non-succeeded node (DAG order). The
    run_id and all chapters/versions are preserved — this is NOT a full
    re-execute. Succeeded runs must use the full re-execute (bootstrap) flow.

    BYOK headers (X-Api-Key / X-Api-Base-Url / X-Model) are carried from the
    restart request into the re-dispatched bootstrap, consistent with the other
    AI-generation endpoints — so a run started under a user-supplied model/key
    keeps that scope on restart instead of silently falling back to server config."""
    conn, run = load_run_for_user(run_id, user, {"owner", "editor"})
    if run["status"] == "succeeded":
        conn.close()
        raise HTTPException(
            status_code=409,
            detail="已完成的创作请用「全流程重执行」新建一次 run，旧 run 与章节、版本会保留。",
        )
    if run["status"] not in {"pending", "dispatch_failed", "failed", "pending_provider"}:
        conn.close()
        raise HTTPException(
            status_code=409,
            detail="当前流程不是可恢复的运行故障；质量待重写或人工确认状态不能原样重启。",
        )
    from .workers.tasks import execute_bootstrap, BOOTSTRAP_NODES
    rows = conn.execute(
        "SELECT node_key, status, output FROM run_nodes WHERE run_id = %s", (run_id,)
    ).fetchall()
    nodes_by_key = {r["node_key"]: r for r in rows}
    start_key = None
    for key, *_ in BOOTSTRAP_NODES:
        node_row = nodes_by_key.get(key)
        if not node_row or node_row["status"] != "succeeded":
            start_key = key
            break
    if start_key is None:
        start_key = BOOTSTRAP_NODES[0][0] if BOOTSTRAP_NODES else "plan_idea"
    start_node = nodes_by_key.get(start_key)
    start_output = decode(start_node.get("output"), {}) if start_node else {}
    if start_node and (
        start_node.get("status") in {"needs_review", "pending_budget"}
        or (isinstance(start_output, dict) and start_output.get("retryable") is False)
    ):
        conn.close()
        raise HTTPException(
            status_code=409,
            detail="首个未完成步骤是确定性质量/预算问题，原样重启不会成功。请先修正失败原因。",
        )
    conn.execute(
        """UPDATE run_nodes SET status = 'pending', output = '{}', error = NULL,
               started_at = NULL, finished_at = NULL
           WHERE run_id = %s AND status <> 'succeeded'""",
        (run_id,),
    )
    conn.execute(
        "UPDATE workflow_runs SET status = 'running', current_node_key = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (start_key, run_id),
    )
    conn.commit()
    conn.close()
    # 透传 BYOK：与 bootstrap / continue 等 AI 端点一致，从重启请求取 X-Model/X-Api-Key/X-Api-Base-Url
    execute_bootstrap.delay(
        run_id, start_key,
        "",  # 遗留明文 api_key 不再使用，统一走 api_key_ref
        request.headers.get("X-Api-Base-Url", ""),
        request.headers.get("X-Model", ""),
        api_key_ref=stash_byok_key(request.headers.get("X-Api-Key", "")),
    )
    return ok({"run_id": run_id, "start_key": start_key, "status": "running"})


@app.delete("/api/v1/contents/{content_id}")
def delete_content(content_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """QA-004: soft-delete a content row; deleting a novel cascades to its
    chapters and knowledge items. Versions are retained for recovery."""
    conn, content = load_content_for_user(content_id, user, {"owner", "editor"})
    deleted_children = 0
    if content["type"] == "novel":
        cur = conn.execute(
            "UPDATE contents SET is_deleted = TRUE, updated_at = now() WHERE parent_id = %s AND is_deleted = FALSE",
            (content_id,),
        )
        deleted_children = getattr(cur, "rowcount", 0) or 0
        conn.execute(
            "UPDATE knowledge_items SET is_deleted = TRUE, updated_at = now() WHERE content_id = %s AND is_deleted = FALSE",
            (content_id,),
        )
    conn.execute(
        "UPDATE contents SET is_deleted = TRUE, updated_at = now() WHERE id = %s",
        (content_id,),
    )
    conn.commit()
    conn.close()
    return ok({"deleted": content_id, "type": content["type"], "children_deleted": deleted_children})


@app.delete("/api/v1/novels/{novel_id}")
def delete_novel(novel_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """QA-004 alias: novels are contents of type 'novel'."""
    conn, content = load_content_for_user(novel_id, user, {"owner", "editor"})
    conn.close()
    if content["type"] != "novel":
        raise HTTPException(status_code=404, detail="novel not found")
    return delete_content(novel_id, user)


@app.get("/api/v1/contents/{content_id}/versions")
def list_versions(content_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn, _content = load_content_for_user(content_id, user)
    rows = [dict(row) for row in conn.execute("SELECT * FROM versions WHERE entity_id = %s ORDER BY created_at DESC", (content_id,)).fetchall()]
    for row in rows:
        row["snapshot"] = decode(row["snapshot"], {})
    conn.close()
    return ok(rows)


@app.post("/api/v1/contents/{content_id}/versions/restore")
def restore_version(content_id: str, payload: VersionRestore, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn, _content = load_content_for_user(content_id, user, {"owner", "editor"})
    version = row_to_dict(
        conn.execute("SELECT * FROM versions WHERE id = %s AND entity_id = %s", (payload.version_id, content_id)).fetchone()
    )
    if version is None:
        conn.close()
        raise HTTPException(status_code=404, detail="version not found")
    current = row_to_dict(conn.execute("SELECT * FROM contents WHERE id = %s", (content_id,)).fetchone())
    if current is not None:
        snapshot = {"title": current["title"], "body": decode(current["body"], {}), "meta": decode(current["meta"], {})}
        conn.execute(
            "INSERT INTO versions (id, entity_type, entity_id, label, snapshot) VALUES (%s, 'content', %s, 'before_restore', %s)",
            (new_id("ver"), content_id, encode(snapshot)),
        )
    restored = decode(version["snapshot"], {})
    conn.execute(
        "UPDATE contents SET title = %s, body = %s, meta = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (restored.get("title", "未命名"), encode(restored.get("body", {})), encode(restored.get("meta", {})), content_id),
    )
    conn.commit()
    row = parse_content(dict(conn.execute("SELECT * FROM contents WHERE id = %s", (content_id,)).fetchone()))
    conn.close()
    return ok(row)


def _editor_provenance(source: str, candidate: str) -> dict[str, Any]:
    """Detect a catastrophic unrelated editor candidate before preview.

    A rewrite may change wording, but a full-chapter polish/rewrite must retain
    enough exact local phrases to prove it is editing the submitted chapter.
    This is deliberately a low bar: it only blocks a completely different
    story, not legitimate paraphrase or de-AI rewriting.
    """
    import re as _re2

    def compact(value: str) -> str:
        return _re2.sub(r"[\s\W_]+", "", str(value or ""))

    source_compact = compact(source)
    candidate_compact = compact(candidate)
    if len(source_compact) < 180 or len(candidate_compact) < 120:
        return {"passed": True, "shared_ngrams": 0, "candidate_ngrams": 0, "reason": "short_text_exempt"}
    source_ngrams = {source_compact[i:i + 4] for i in range(max(0, len(source_compact) - 3))}
    candidate_ngrams = {candidate_compact[i:i + 4] for i in range(max(0, len(candidate_compact) - 3))}
    shared = len(source_ngrams & candidate_ngrams)
    # Zero shared four-character phrases is the signature of a wholly unrelated
    # provider answer. Three shared phrases is intentionally enough for a real
    # paraphrase with changed sentence structure.
    passed = shared >= 3
    return {
        "passed": passed,
        "shared_ngrams": shared,
        "candidate_ngrams": len(candidate_ngrams),
        "reason": "ok" if passed else "candidate_has_no_source_evidence",
    }


def _run_v7_editor(
    content: dict[str, Any],
    selection: str,
    operation: str,
    *,
    instruction: str,
    request: Request,
    client_mutation_id: str | None,
    role_key: str | None = None,
) -> dict[str, Any]:
    """Call the canonical V7 editor for a real chapter row.

    This is deliberately a small boundary so both the normal and SSE editor
    endpoints share exactly one V7 generation contract.  V6 ``contents`` is
    still written by the caller as a storage/version branch after this call;
    it is never an AI fallback for a chapter attached to a novel.
    """
    from .v7.editor_service import edit_chapter_v7_sync

    return edit_chapter_v7_sync(
        content,
        selection,
        operation,
        instruction=instruction,
        api_key=request.headers.get("X-Api-Key", ""),
        api_url=request.headers.get("X-Api-Base-Url", ""),
        model=request.headers.get("X-Model", ""),
        client_mutation_id=client_mutation_id,
        role_key=role_key,
    )


def _legacy_editor_compat_candidate(
    content: dict[str, Any],
    selection: str,
    operation: str,
    *,
    instruction: str,
    client_mutation_id: str | None,
) -> dict[str, Any]:
    """Keep malformed pre-V7 rows usable without weakening real chapters.

    Old test/import rows can lack ``parent_id`` and therefore cannot provide a
    V7 Novel Brain scope.  They are not product chapters.  This explicit seam
    is the only editor generation compatibility path and is never selected for
    a real chapter row.
    """
    task_op = "rewrite" if str(operation) == "rewrite_chapter" else str(operation)
    return complete(
        run_id=None,
        node_key=None,
        project_id=content["project_id"],
        task_type=f"editor_{task_op}",
        prompt_name=f"editor.{task_op}",
        variables={
            "selection": selection,
            "instruction": instruction,
            "chapter_title": content.get("title", ""),
            "editing_contract": "只编辑提交的当前章节，保留人物、地点、物品、时间线和事件事实；不得换成另一篇故事。",
        },
        client_mutation_id=client_mutation_id,
    )


def _raise_v7_editor_http_error(exc: Exception) -> None:
    """Convert a V7 editor failure into a safe, non-success API response."""
    code = str(getattr(exc, "code", "V7_EDITOR_FAILED") or "V7_EDITOR_FAILED")
    message = _v7_editor_public_message(exc, code)
    raise HTTPException(
        status_code=422 if code.startswith("EDITOR_") else 502,
        detail={
            "code": code,
            "message": message,
            "canonical_engine": "v7",
            "version_written": False,
        },
    ) from exc


def _v7_editor_public_message(exc: Exception, code: str) -> str:
    """Keep provider/budget details out of both JSON and SSE editor responses."""
    if code == "V7_EDITOR_BUDGET":
        return public_message(exc, "V7 编辑额度已达上限，请稍后重试")
    if code == "V7_EDITOR_PROVIDER_FAILED":
        return public_message(exc, "V7 编辑服务暂时不可用，请稍后重试")
    return public_message(exc, "V7 编辑失败，请稍后重试")


def _load_ai_edit_replay(content_id: str, client_mutation_id: str | None) -> dict[str, Any] | None:
    """Return a previously accepted editor result without spending Provider quota.

    The version row is the product-level idempotency record.  V7's execution
    ledger records provenance and cost, but deliberately does not store the
    full prose payload; the V6-compatible version snapshot remains the right
    replay source for the editor API.
    """
    if not client_mutation_id:
        return None
    conn = connect()
    try:
        row = conn.execute(
            """SELECT snapshot FROM versions
               WHERE entity_id=%s AND label='ai_edit' AND client_mutation_id=%s
               ORDER BY created_at DESC LIMIT 1""",
            (content_id, client_mutation_id),
        ).fetchone()
        if not row:
            return None
        snapshot = row.get("snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else decode(snapshot, {})
        output = snapshot.get("output") if isinstance(snapshot, dict) else None
        if not isinstance(output, dict):
            return None
        replay = dict(output)
        replay["mutation_replayed"] = True
        return replay
    finally:
        conn.close()


def _ensure_editor_paragraphs(text: Any) -> str:
    """保证编辑器 AI 文本带段落分隔（空行），避免应用后折叠成一大段。

    网文风格分段规则：
    - 对话（「」包裹）必须独立成段
    - 每段 1-2 句，不超过 80 字
    - 标点规范化（破折号/省略号/引号）
    """
    if isinstance(text, (dict, list)):
        # Some older provider adapters returned a TipTap document under
        # ``text``.  Coerce it through the canonical extractor instead of
        # allowing Python/JavaScript to turn nested nodes into
        # ``[object Object]``.
        from .services.novel_export import extract_body_text
        text = extract_body_text(text)
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    import re as _re2
    # 标点规范化
    text = text.replace("\r\n", "\n").strip()
    text = _re2.sub(r"\.{3,}", "……", text)
    text = text.replace("。。。", "……")
    text = _re2.sub(r"…{3}", "……", text)
    text = _re2.sub(r"—{2,}", "——", text)
    text = text.replace("\u201c", "「").replace("\u201d", "」")
    text = text.replace("\u2018", "『").replace("\u2019", "』")

    if "\n" in text:
        return text

    # 按对话边界拆分：「」包裹的内容独立成段
    parts: list[str] = []
    dialog_re = _re2.compile(r"「[^」]*」")
    last_end = 0
    for m in dialog_re.finditer(text):
        if m.start() > last_end:
            parts.append(text[last_end:m.start()])
        parts.append(m.group())
        last_end = m.end()
    if last_end < len(text):
        parts.append(text[last_end:])

    # 对每个非对话片段按句末标点切短段
    grouped: list[str] = []
    for part in parts:
        if part.startswith("「"):
            grouped.append(part)
            continue
        sentences = [s.strip() for s in _re.split(r"(?<=[\u3002\uff01\uff1f!?])", part) if s.strip()]
        buf: list[str] = []
        for s in sentences:
            buf.append(s)
            if len(buf) >= 2 or len("".join(buf)) > 80:
                grouped.append("".join(buf))
                buf = []
        if buf:
            grouped.append("".join(buf))
    return "\n\n".join(g for g in grouped if g)


@app.post("/api/v1/contents/{content_id}/ai/{op}")
@limiter.limit("30/minute")
def ai_edit(
    request: Request,
    content_id: str,
    op: AiOperation,
    payload: AiEditRequest,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    conn, content = load_content_for_user(content_id, user, {"owner", "editor"})
    try:
        _require_v7_chapter_scope(conn, content, str(op))
        replay = _load_ai_edit_replay(content_id, payload.client_mutation_id)
        if replay is not None:
            return ok(replay)
    finally:
        conn.close()
    # Plan gate: enforce the monthly word quota before any AI generation so
    # Free users cannot bypass the cap via the generic editor / continue path.
    from .core.billing import enforce_quota
    enforce_quota(user["id"], None, "max_words_per_month")
    task_op = "rewrite" if str(op) == "rewrite_chapter" else str(op)
    # V3 deai → use the new DeaiPipeline (web novel style rewrite)
    IMPROVE_OPS = {"polish", "rewrite", "rewrite_chapter"}
    EDITOR_MIN_CHARS = int(os.getenv("MIN_CHAPTER_CHARS", "2000"))
    from app.services.text_metrics import count_content_chars
    from app.v7.quality.deai_metrics import analyze_deai_patterns
    source_chars = count_content_chars(payload.selection)
    # A selected paragraph is not a chapter.  Apply the chapter floor only to
    # full-chapter operations; selection edits retain at least 75% of the
    # selected material so the editor remains useful for small local fixes.
    operation_min_chars = (
        EDITOR_MIN_CHARS
        if str(op) == "rewrite_chapter" or source_chars >= EDITOR_MIN_CHARS
        else max(80, int(source_chars * 0.75))
    )
    # An editor candidate is only quality-safe after the same product bar as
    # the canonical V7 writer.  The preview remains user-confirmed, but a
    # score in the low 80s or a material risk must trigger targeted rework.
    EDITOR_REVIEW_PASS = 85
    # Interactive editor actions must finish within the browser's operation
    # window.  One quality repair pass is enough to keep the review gate
    # meaningful while avoiding the old 4-generation/4-review worst case that
    # made "按全部建议润色" appear broken in production.  Deployments that
    # explicitly accept a slower editor may raise this to at most 3 retries.
    try:
        MAX_EDITOR_RETRIES = max(0, min(int(os.getenv("EDITOR_MAX_RETRIES", "1")), 3))
    except (TypeError, ValueError):
        MAX_EDITOR_RETRIES = 1

    editor_review_error = ""

    def _review_editor_candidate(candidate_text: str) -> dict[str, Any] | None:
        """Use V7 for real chapter rows; keep malformed legacy rows compatible.

        A provider/review outage must be visible to the editor and must not be
        replaced by a fabricated score.  Rows without a parent novel are old
        compatibility/test fixtures, so their injected legacy review adapter is
        still allowed to return its own explicitly non-canonical payload.
        """
        nonlocal editor_review_error
        from app.v7.review_service import review_chapter_v7_sync

        try:
            return review_chapter_v7_sync(
                content,
                candidate_text,
                api_key=request.headers.get("X-Api-Key", ""),
                api_url=request.headers.get("X-Api-Base-Url", ""),
                model=request.headers.get("X-Model", ""),
                use_cache=False,
            )
        except Exception as exc:
            editor_review_error = "V7 审阅暂不可用，候选未获得质量分，请稍后重新审阅"
            if content.get("parent_id"):
                from .v7.editor_service import V7EditorError
                raise V7EditorError(
                    "V7_EDITOR_REVIEW_FAILED",
                    "V7 审阅暂不可用，候选未写入版本，请稍后重试",
                    details={"error_type": type(exc).__name__},
                ) from exc
            # Compatibility-only path for rows created before chapters were
            # attached to a novel.  Do not label this result as V7.
            legacy = complete(
                run_id=None,
                node_key=None,
                project_id=content["project_id"],
                task_type="review_7dim",
                prompt_name="bootstrap.review_7dim",
                variables={"body": candidate_text, "chapter_title": content.get("title", "")},
            )
            if isinstance(legacy, dict):
                legacy.setdefault("canonical_engine", "v6_compat")
                legacy.setdefault("audit_error", editor_review_error)
                return legacy
            return None

    if str(op) == "deai" and content.get("parent_id"):
        try:
            v7_result = _run_v7_editor(
                content,
                payload.selection,
                str(op),
                instruction=payload.instruction,
                request=request,
                client_mutation_id=(
                    f"{payload.client_mutation_id}:gen:0"
                    if payload.client_mutation_id
                    else None
                ),
                role_key=payload.role_key,
            )
        except Exception as exc:
            _raise_v7_editor_http_error(exc)
        candidate_text = _ensure_editor_paragraphs(v7_result.get("text") or "")
        content_evidence = _editor_provenance(payload.selection, candidate_text)
        if not content_evidence.get("passed"):
            from .v7.editor_service import V7EditorError
            _raise_v7_editor_http_error(
                V7EditorError(
                    "EDITOR_SOURCE_EVIDENCE_FAILED",
                    "V7 编辑候选缺少当前章节内容证据，未写入版本",
                )
            )
        output = {
            "text": candidate_text,
            "deai_quality_gate": v7_result.get("quality_gate") or {
                "passed": False,
                "code": "deai_quality_gate_missing",
                "message": "去 AI 味质量门禁缺失，结果未验证",
            },
            "deai_warnings": [],
            "editor_provenance": {
                **(v7_result.get("editor_provenance") or {}),
                "content_evidence": content_evidence,
            },
            "canonical_engine": "v7",
            "usage": v7_result.get("usage") or {},
        }
    elif str(op) == "deai":
        # Compatibility-only path for malformed pre-V7 rows.  Real chapter
        # rows always take the branch above and never call the V6 pipeline.
        from app.services.deai_pipeline import DeaiPipeline
        pipeline = DeaiPipeline(
            project_id=content["project_id"],
            content_id=content_id,
            chapter_title=str(content.get("title", "")),
        )
        result = pipeline.run(payload.selection)
        deai_result = result
        candidate_text = _ensure_editor_paragraphs(result.get("final_text", payload.selection))
        if count_content_chars(candidate_text) < operation_min_chars:
            result2 = pipeline.run(
                payload.selection
                + f"\n\n【上一版去AI味字数不足（{count_content_chars(candidate_text)}字 < {operation_min_chars}字）。"
                + "请保持原文全部信息与字数，只改表达方式，不得压缩、总结或删减情节。】"
            )
            candidate2 = _ensure_editor_paragraphs(result2.get("final_text", payload.selection))
            if count_content_chars(candidate2) > count_content_chars(candidate_text):
                candidate_text = candidate2
                deai_result = result2
        if count_content_chars(candidate_text) < operation_min_chars and source_chars >= operation_min_chars:
            candidate_text = _ensure_editor_paragraphs(payload.selection)
        provenance = _editor_provenance(payload.selection, candidate_text)
        if not provenance["passed"]:
            candidate_text = _ensure_editor_paragraphs(payload.selection)
            deai_result.setdefault("warnings", []).append("候选与当前章节缺少内容证据，已保留原文")
        output = {
            "text": candidate_text,
            "deai_quality_gate": deai_result.get("quality_gate") or {
                "passed": False,
                "code": "deai_quality_gate_missing",
                "message": "去 AI 味质量门禁缺失，结果未验证",
            },
            "deai_warnings": deai_result.get("warnings") or [],
            "editor_provenance": provenance,
            "canonical_engine": "v6_compat",
        }
    else:
        instruction = payload.instruction
        best = None
        best_score = -1.0
        last_candidate_text = payload.selection
        for attempt in range(MAX_EDITOR_RETRIES + 1):
            mutation_id = (
                f"{payload.client_mutation_id}:gen:{attempt}"
                if payload.client_mutation_id
                else None
            )
            if content.get("parent_id"):
                try:
                    gen = _run_v7_editor(
                        content,
                        payload.selection,
                        str(op),
                        instruction=instruction,
                        request=request,
                        client_mutation_id=mutation_id,
                        role_key=payload.role_key,
                    )
                except Exception as exc:
                    _raise_v7_editor_http_error(exc)
            else:
                gen = _legacy_editor_compat_candidate(
                    content,
                    payload.selection,
                    str(op),
                    instruction=instruction,
                    client_mutation_id=mutation_id,
                )
            candidate_text = _ensure_editor_paragraphs(gen.get("text") or payload.selection)
            last_candidate_text = candidate_text
            if str(op) in IMPROVE_OPS:
                try:
                    review = _review_editor_candidate(candidate_text)
                except Exception as exc:
                    _raise_v7_editor_http_error(exc)
                if review is None:
                    best = {"text": candidate_text, "review_7dim": None}
                    break
                score = float(review.get("overall_score") or review.get("score") or 0)
                chars = count_content_chars(candidate_text)
                provenance = _editor_provenance(payload.selection, candidate_text)
                from app.services.quality_risks import (
                    evaluate_editor_review_gate,
                    repair_feedback,
                )
                review["deai_metrics"] = analyze_deai_patterns(candidate_text)
                review["editor_provenance"] = {
                    **(gen.get("editor_provenance") or {}),
                    "content_evidence": provenance,
                }
                if not provenance["passed"]:
                    instruction = (
                        "候选与原文缺少内容证据，必须重写当前原文而不是另起一篇故事；"
                        "保留原文中的人物、地点、物品、事件和关键四字以上短语。"
                    )
                    continue
                quality_gate = evaluate_editor_review_gate(
                    review,
                    chars=chars,
                    minimum_chars=operation_min_chars,
                    minimum_score=EDITOR_REVIEW_PASS,
                )
                review["quality_gate"] = quality_gate
                review["quality_repair_contract"] = quality_gate["quality_repair_contract"]
                if score > best_score:
                    best_score = score
                    best = {
                        "text": candidate_text,
                        "review_7dim": review,
                        "editor_provenance": review["editor_provenance"],
                        "canonical_engine": gen.get("canonical_engine", "v6_compat"),
                        "usage": gen.get("usage") or {},
                    }
                if quality_gate["passed"]:
                    best = {
                        "text": candidate_text,
                        "review_7dim": review,
                        "editor_provenance": review["editor_provenance"],
                        "canonical_engine": gen.get("canonical_engine", "v6_compat"),
                        "usage": gen.get("usage") or {},
                    }
                    break
                # 评分/字数不达标 → 用审查建议 + 去 AI 味要求重跑（默认最多 1 次）
                issues = repair_feedback(
                    quality_gate["quality_repair_contract"],
                    list(review.get("issues", [])),
                )
                if chars < operation_min_chars:
                    issues.append(f"字数不足：当前 {chars} 字，必须保留至少 {operation_min_chars} 字，不得压缩或总结情节")
                issues.append(
                    "必须去除 AI 味：按【去 AI 味改稿铁律】检查整章分布——打散同构段落与均匀句长，"
                    "用动作/对白/具体细节承载情绪，减少空泛连接词和总结腔；不设置单个词或标点的禁用清单，"
                    "只改高密度、连续重复、无语境必要的模板表达，保留人物口吻与全部事实。"
                )
                instruction = "；".join(issues)
            else:
                # continue 等非改善操作：单遍生成
                best = {
                    "text": candidate_text,
                    "editor_provenance": gen.get("editor_provenance") or {},
                    "canonical_engine": gen.get("canonical_engine", "v6_compat"),
                    "usage": gen.get("usage") or {},
                }
                break
        if str(op) in IMPROVE_OPS and best is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "EDITOR_CANDIDATE_UNSAFE",
                    "message": "AI 返回的候选与当前章节缺少内容证据，原文未改变，请重试",
                    "candidate_chars": count_content_chars(last_candidate_text),
                },
            )
        output = best or {"text": last_candidate_text}
        # 最终兜底：润色/改写重跑耗尽后仍不足 2000 → 回退原文。
        # 润色的目的是改表达，压缩/删减情节属于破坏性操作，宁可少改。
        if str(op) in IMPROVE_OPS and count_content_chars(output.get("text") or "") < operation_min_chars:
            if count_content_chars(payload.selection) >= operation_min_chars:
                if content.get("parent_id"):
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "EDITOR_LENGTH_OUTSIDE_SAFE_RANGE",
                            "message": "V7 编辑候选篇幅不足，原文未写入版本",
                            "candidate_chars": count_content_chars(output.get("text") or ""),
                            "minimum_chars": operation_min_chars,
                            "canonical_engine": "v7",
                            "version_written": False,
                        },
                    )
                output = {"text": _ensure_editor_paragraphs(payload.selection)}

    # 附七维审查（deai 单遍在此补算）。下一章规划属于实时审计/生成
    # 链路的上下文，不是编辑器预览的必要数据；这里不再同步调用，避免
    # “按全部建议润色”平白多等一次模型请求。编辑器收到的 review_7dim
    # 已足够展示问题、评分和质量门禁，应用后实时审计会按需刷新规划。
    if str(op) in {"polish", "rewrite", "rewrite_chapter", "deai"}:
        review_context = _chapter_review_context(content, output.get("text") or payload.selection)
        if output.get("review_7dim") is None and not editor_review_error:
            try:
                output["review_7dim"] = _review_editor_candidate(
                    output.get("text") or payload.selection
                )
            except Exception as exc:
                _raise_v7_editor_http_error(exc)
        if output.get("review_7dim") is None:
            # Honest degraded mode: text/version are returned, but the UI gets
            # an explicit unavailable state and no score or “passed” badge.
            output["audit_error"] = editor_review_error or "V7 审阅暂不可用，候选未获得质量分"
            output["review"] = None
            output["next_chapter_plan"] = None
        else:
            from app.services.quality_risks import evaluate_editor_review_gate
            final_chars = count_content_chars(output.get("text") or payload.selection)
            output["review_7dim"]["deai_metrics"] = analyze_deai_patterns(output.get("text") or payload.selection)
            deai_gate = output.get("deai_quality_gate") or {}
            if deai_gate.get("passed") is False:
                output["review_7dim"].setdefault("issues", []).append({
                    "dimension": "writing_quality",
                    "type": "rewrite_candidate_rejected",
                    "severity": "high",
                    "description": str(
                        deai_gate.get("message") or "去 AI 味候选未通过篇幅/重复安全校验"
                    ),
                    "suggestion": "保留原文并重新发起去 AI 味，不能把未验证结果标为完成",
                })
            final_gate = evaluate_editor_review_gate(
                output["review_7dim"],
                chars=final_chars,
                minimum_chars=operation_min_chars,
                minimum_score=EDITOR_REVIEW_PASS,
            )
            output["review_7dim"]["quality_gate"] = final_gate
            output["review_7dim"]["quality_repair_contract"] = final_gate["quality_repair_contract"]
            output["review"] = output["review_7dim"]
        if editor_review_error:
            output.setdefault("audit_error", editor_review_error)
        output["next_chapter_plan"] = None
    # C5-03: every AI edit leaves a version branch so the tree stays auditable.
    conn = connect()
    conn.execute(
        """INSERT INTO versions (id, entity_type, entity_id, label, snapshot, reason, author_id, client_mutation_id)
           VALUES (%s, 'content', %s, 'ai_edit', %s, %s, %s, %s)
           ON CONFLICT (client_mutation_id) WHERE client_mutation_id IS NOT NULL DO NOTHING""",
        (new_id("ver"), content_id,
         encode({"op": str(op), "selection": payload.selection[:2000],
                 "instruction": payload.instruction[:500], "output": output}),
         f"editor_{op}", user["id"], payload.client_mutation_id),
    )
    conn.commit()
    conn.close()
    return ok(output)


@app.post("/api/v1/contents/{content_id}/review")
@limiter.limit("40/minute")
def ai_chapter_review(
    request: Request,
    content_id: str,
    payload: AiEditRequest,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """Live audit: score the current chapter text and surface review issues.

    Analysis only — does not edit text and does not enforce the word-generation
    quota (it is a read-only audit). Each *distinct* result is recorded as an
    'ai_review' version branch so the audit trail stays traceable; identical
    consecutive results are deduped to avoid version-tree spam."""
    conn, content = load_content_for_user(content_id, user, {"owner", "editor"})
    try:
        _require_v7_chapter_scope(conn, content, "live_review")
    finally:
        conn.close()
    text = (payload.selection or "").strip()
    if len(text) < 10:
        return ok({"review_7dim": None, "next_chapter_plan": None, "skipped": "too_short"})
    review_7dim = None
    next_chapter_plan = None
    audit_error = ""
    try:
        from app.v7.review_service import review_chapter_v7_sync
        review_7dim = review_chapter_v7_sync(
            content,
            text,
            api_key=request.headers.get("X-Api-Key", ""),
            api_url=request.headers.get("X-Api-Base-Url", ""),
            model=request.headers.get("X-Model", ""),
            use_cache=True,
        )
    except Exception:
        # Live audit must never break the editor, but a silent null result makes
        # the UI look as if there was no audit at all and prevents a retry.
        # Return a stable user-facing status while keeping provider details out
        # of the response.
        audit_error = "实时审计暂不可用，请点击重新审计"
        review_7dim = review_7dim or None
        next_chapter_plan = next_chapter_plan or None
    if review_7dim is not None:
        # The live audit is read-only, but it must expose the same quality
        # contract as an AI edit; otherwise the editor can show an issue list
        # without explaining that the candidate is still below the product
        # acceptance bar.
        from app.services.quality_risks import evaluate_editor_review_gate
        from app.services.text_metrics import count_content_chars
        live_gate = evaluate_editor_review_gate(
            review_7dim,
            chars=count_content_chars(text),
            minimum_chars=int(os.getenv("MIN_CHAPTER_CHARS", "2000")),
            minimum_score=85.0,
        )
        review_7dim["quality_gate"] = live_gate
        review_7dim["quality_repair_contract"] = live_gate["quality_repair_contract"]
    # C5-03-audit: record each distinct V7 audit as a version branch (dedupe by
    # the reviewed text hash, not only by a rounded score/issues pair).
    conn = None
    try:
        conn = connect()
        last = conn.execute(
            """SELECT snapshot FROM versions WHERE entity_id=%s AND label='ai_review'
               ORDER BY created_at DESC LIMIT 1""",
            (content_id,),
        ).fetchone()
        same = False
        if last and last.get("snapshot") is not None:
            try:
                snap = decode(last["snapshot"], {}) if not isinstance(last["snapshot"], dict) else last["snapshot"]
                prev = snap.get("review", {}) if isinstance(snap, dict) else {}
                if prev.get("provenance", {}).get("text_hash") == (review_7dim or {}).get("provenance", {}).get("text_hash"):
                    same = True
            except Exception:
                same = False
        if not same and review_7dim is not None:
            conn.execute(
                """INSERT INTO versions (id, entity_type, entity_id, label, snapshot, reason, author_id)
                   VALUES (%s, 'content', %s, 'ai_review', %s, 'live_audit', %s)""",
                (new_id("ver"), content_id,
                 encode({"review": review_7dim, "next": next_chapter_plan, "text_len": len(text)}),
                 user["id"]),
            )
            conn.commit()
    finally:
        if conn is not None:
            conn.close()
    return ok({"review": review_7dim, "review_7dim": review_7dim, "next_chapter_plan": next_chapter_plan,
               "audit_error": audit_error})


def _chapter_review_context(content: dict, text: str) -> dict:
    conn = connect()
    try:
        novel_id = content.get("parent_id") or content.get("id")
        knowledge = conn.execute(
            """SELECT kind,title,body FROM knowledge_items
               WHERE content_id=%s AND is_deleted=FALSE ORDER BY kind LIMIT 40""",
            (novel_id,),
        ).fetchall()
    finally:
        conn.close()
    characters = "\n".join(f"{row.get('title','')}: {row.get('body','')}" for row in knowledge if row.get("kind") == "character")
    worldview = "\n".join(str(row.get("body", "")) for row in knowledge if row.get("kind") == "worldview")
    return {"chapter_text": text[:12000], "body": text[:12000],
            "characters": characters[:4000] or "暂无人物档案",
            "worldview": worldview[:4000] or "暂无世界观档案"}


def sse_event(payload: dict) -> str:
    """Frame a Server-Sent-Event payload as ``data: {json}\\n\\n`` (single source of truth)."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/v1/contents/{content_id}/ai/{op}/stream")
@limiter.limit("20/minute")
def ai_edit_stream(
    request: Request,
    content_id: str,
    op: AiOperation,
    payload: AiEditRequest,
    user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """SSE streaming variant of ai_edit for pure-text operations.

    Frames: {"delta": str}* then {"done": true, "text": full}. Provider/budget
    failures are surfaced as explicit error frames."""
    conn, content = load_content_for_user(content_id, user, {"owner", "editor"})
    try:
        _require_v7_chapter_scope(conn, content, str(op))
    finally:
        conn.close()
    replay = _load_ai_edit_replay(content_id, payload.client_mutation_id)
    if replay is not None:
        def replay_event_source():
            full_text = _ensure_editor_paragraphs(replay.get("text") or "")
            for offset in range(0, len(full_text), 160):
                yield sse_event({"delta": full_text[offset:offset + 160]})
            done = {"done": True, "text": full_text}
            # Preserve the historical SSE frame shape for malformed legacy
            # rows.  Real V7 rows carry the richer replay/provenance fields.
            if replay.get("canonical_engine") == "v7":
                done.update({
                    "mutation_replayed": True,
                    "canonical_engine": "v7",
                    "editor_provenance": replay.get("editor_provenance") or {},
                })
            yield sse_event(done)

        return StreamingResponse(
            replay_event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    # Plan gate: enforce the monthly word quota before streaming AI generation.
    from .core.billing import enforce_quota
    enforce_quota(user["id"], None, "max_words_per_month")
    project_id = content["project_id"]
    task_op = "rewrite" if str(op) == "rewrite_chapter" else str(op)

    if content.get("parent_id"):
        def v7_event_source():
            try:
                result = _run_v7_editor(
                    content,
                    payload.selection,
                    str(op),
                    instruction=payload.instruction,
                    request=request,
                    client_mutation_id=payload.client_mutation_id,
                    role_key=payload.role_key,
                )
                full_text = _ensure_editor_paragraphs(result.get("text") or "")
            except Exception as exc:
                code = str(getattr(exc, "code", "V7_EDITOR_FAILED") or "V7_EDITOR_FAILED")
                message = _v7_editor_public_message(exc, code)
                yield sse_event({
                    "error": message,
                    "code": code,
                    "canonical_engine": "v7",
                    "version_written": False,
                })
                return

            # The V7 provider call is buffered today because the shared async
            # gateway owns the request/provenance transaction.  Keep the SSE
            # contract for the browser while emitting deterministic chunks;
            # this avoids a second provider path and guarantees the version is
            # written only after the whole candidate passes validation.
            for offset in range(0, len(full_text), 160):
                yield sse_event({"delta": full_text[offset:offset + 160]})

            version_conn = connect()
            version_conn.execute(
                """INSERT INTO versions (id, entity_type, entity_id, label, snapshot, reason, author_id, client_mutation_id)
                   VALUES (%s, 'content', %s, 'ai_edit', %s, %s, %s, %s)
                   ON CONFLICT (client_mutation_id) WHERE client_mutation_id IS NOT NULL DO NOTHING""",
                (new_id("ver"), content_id,
                 encode({
                     "op": str(op),
                     "selection": payload.selection[:2000],
                     "instruction": payload.instruction[:500],
                     "output": {
                         **result,
                         "text": full_text,
                     },
                 }),
                 f"editor_{op}", user["id"], payload.client_mutation_id),
            )
            version_conn.commit()
            version_conn.close()
            yield sse_event({
                "done": True,
                "text": full_text,
                "canonical_engine": "v7",
                "editor_provenance": result.get("editor_provenance") or {},
            })

        return StreamingResponse(
            v7_event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Compatibility-only streaming for malformed pre-V7 rows.  Real chapters
    # return above and never enter the V6 streaming gateway.
    from .gateway import BudgetExceeded, ProviderError, complete_stream

    def event_source():
        chunks: list[str] = []
        try:
            for delta in complete_stream(
                project_id=project_id,
                task_type=f"editor_{task_op}",
                prompt_name=f"editor.{task_op}",
                variables={"selection": payload.selection, "instruction": payload.instruction},
                client_mutation_id=payload.client_mutation_id,
            ):
                chunks.append(delta)
                yield sse_event({"delta": delta})
        except (ProviderError, BudgetExceeded) as exc:
            code = "PENDING_BUDGET" if isinstance(exc, BudgetExceeded) else "PROVIDER_FAILED"
            yield sse_event({"error": public_message(exc, "AI 服务暂时不可用"), "code": code})
            return
        full_text = "".join(chunks)
        version_conn = connect()
        version_conn.execute(
            """INSERT INTO versions (id, entity_type, entity_id, label, snapshot, reason, author_id, client_mutation_id)
               VALUES (%s, 'content', %s, 'ai_edit', %s, %s, %s, %s)
               ON CONFLICT (client_mutation_id) WHERE client_mutation_id IS NOT NULL DO NOTHING""",
            (new_id("ver"), content_id,
             encode({"op": str(op), "selection": payload.selection[:2000],
                     "instruction": payload.instruction[:500], "output": {"text": full_text}}),
             f"editor_{op}", user["id"], payload.client_mutation_id),
        )
        version_conn.commit()
        version_conn.close()
        yield sse_event({"done": True, "text": full_text})

    return StreamingResponse(event_source(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/v1/agents/status")
def agents_status(user: dict = Depends(get_current_user)) -> ApiResponse:
    """Real per-agent stats from run_nodes, scoped to the user's projects."""
    conn = connect()
    rows = [dict(r) for r in conn.execute(
        """SELECT rn.agent AS name,
                  COUNT(*) AS task_count,
                  COUNT(*) FILTER (
                    WHERE rn.status = 'running'
                      AND wr.status = 'running'
                      AND wr.current_node_key = rn.node_key
                      AND rn.started_at >= now() - interval '30 minutes'
                  ) AS running_count,
                  COUNT(*) FILTER (
                    WHERE rn.status = 'running'
                      AND NOT (
                        wr.status = 'running'
                        AND wr.current_node_key = rn.node_key
                        AND rn.started_at >= now() - interval '30 minutes'
                      )
                  ) AS stale_running_count,
                  MAX(COALESCE(rn.finished_at, rn.started_at)) AS last_run
           FROM run_nodes rn
           JOIN workflow_runs wr ON wr.id = rn.run_id
           JOIN project_members pm ON pm.project_id = wr.project_id
           WHERE pm.user_id = %s AND rn.agent IS NOT NULL AND rn.agent != ''
           GROUP BY rn.agent ORDER BY rn.agent""",
        (user["id"],),
    ).fetchall()]
    conn.close()
    return ok([{"name": r["name"],
                "status": "running" if r["running_count"] else ("stale" if r["stale_running_count"] else "idle"),
                "task_count": int(r["task_count"]),
                "last_run": str(r["last_run"]) if r["last_run"] else "--"} for r in rows])


@app.get("/api/v1/ai-calls")
def list_ai_calls(run_id: str | None = None, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn = connect()
    if run_id:
        run = row_to_dict(conn.execute("SELECT project_id FROM workflow_runs WHERE id = %s", (run_id,)).fetchone())
        if run is None:
            conn.close()
            raise HTTPException(status_code=404, detail="run not found")
        ensure_project_member(conn, run["project_id"], user)
        rows = conn.execute("SELECT * FROM ai_calls WHERE run_id = %s ORDER BY created_at DESC", (run_id,)).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT ac.* FROM ai_calls ac
            LEFT JOIN workflow_runs wr ON ac.run_id = wr.id
            JOIN project_members pm ON wr.project_id = pm.project_id
            WHERE pm.user_id = %s
            ORDER BY ac.created_at DESC LIMIT 100
            """,
            (user["id"],),
        ).fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        item["input"] = decode(item["input"], {})
        item["output"] = decode(item["output"], {})
    conn.close()
    return ok(items)


@app.get("/api/v1/prompts")
def list_prompts(user: dict = Depends(get_current_user)) -> ApiResponse:
    conn = connect()
    rows = [dict(row) for row in conn.execute("SELECT * FROM prompts ORDER BY name, version").fetchall()]
    for row in rows:
        row["golden_cases"] = decode(row["golden_cases"], [])
    conn.close()
    return ok(rows)


@app.get("/api/v1/knowledge")
def list_knowledge(
    project_id: str,
    content_id: str | None = None,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    conn = connect()
    ensure_project_member(conn, project_id, user)
    if content_id:
        rows = conn.execute(
            "SELECT * FROM knowledge_items WHERE project_id = %s AND content_id = %s ORDER BY created_at",
            (project_id, content_id),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM knowledge_items WHERE project_id = %s ORDER BY created_at", (project_id,)).fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        item["meta"] = decode(item["meta"], {})
    conn.close()
    return ok(items)


@app.post("/api/v1/knowledge/search")
def search_knowledge(
    project_id: str,
    query: str = "",
    kind: str = "",
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """M3: Search knowledge hub."""
    conn = connect()
    ensure_project_member(conn, project_id, user)
    conn.close()
    from .services.knowledge_hub import search
    kinds = [kind] if kind else None
    return ok(search(query, project_id, kinds))


@app.post("/api/v1/knowledge/daily-briefing")
@limiter.limit("10/minute")
def daily_briefing(request: Request, project_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """M3: Generate daily content briefing from hotspots."""
    conn = connect()
    ensure_project_member(conn, project_id, user, {"owner", "editor"})
    conn.close()
    from .services.hotspot import generate_daily_briefing
    return ok(generate_daily_briefing(project_id, user_id=user["id"]))


class StyleLearnRequest(BaseModel):
    project_id: str = ""
    samples: list = []


@app.post("/api/v1/knowledge/style-learn")
async def style_learn(request: Request, user: dict = Depends(get_current_user)) -> ApiResponse:
    """M3: Learn style from sample texts."""
    from .services.style_learn import learn_style
    # P2-T10: validate body via Pydantic
    try:
        raw = await request.json()
        req = StyleLearnRequest.model_validate(raw if isinstance(raw, dict) else {})
    except Exception:
        req = StyleLearnRequest()
    project_id = req.project_id
    if project_id:
        conn = connect()
        ensure_project_member(conn, project_id, user, {"owner", "editor"})
        conn.close()
    return ok(learn_style(req.samples))


class CheckSimilarityRequest(BaseModel):
    project_id: str = ""
    original: str = ""
    generated: str = ""


@app.post("/api/v1/knowledge/check-similarity")
async def check_similarity(request: Request, user: dict = Depends(get_current_user)) -> ApiResponse:
    """M3: Check similarity between original and generated text."""
    from .services.style_learn import check_similarity
    # P2-T10: validate body via Pydantic
    try:
        raw = await request.json()
        req = CheckSimilarityRequest.model_validate(raw if isinstance(raw, dict) else {})
    except Exception:
        req = CheckSimilarityRequest()
    project_id = req.project_id
    if project_id:
        conn = connect()
        ensure_project_member(conn, project_id, user)
        conn.close()
    return ok(check_similarity(req.original, req.generated))


@app.post("/api/v1/prompts/lab")
@limiter.limit("20/minute")
def prompt_lab(
    request: Request,
    prompt_name: str,
    input_text: str,
    project_id: str,
    models: str = "deepseek-chat",
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """M3: Prompt lab — run same input against multiple models and compare."""
    from .gateway import complete
    conn = connect()
    ensure_project_member(conn, project_id, user, {"owner", "editor"})
    conn.close()
    model_list = [m.strip() for m in models.split(",")]
    results = []
    for model in model_list:
        try:
            output = complete(run_id=None, node_key=None, project_id=project_id,
                            task_type="prompt_lab", prompt_name=prompt_name,
                            variables={"input": input_text, "model": model})
            results.append({"model": model, "output": output, "status": "ok"})
        except Exception as e:
            results.append({"model": model, "error": str(e), "status": "error"})
    return ok({"prompt": prompt_name, "models": len(results), "results": results})


@app.post("/api/v1/publish")
@limiter.limit("20/minute")
def publish(
    request: Request,
    content_id: str,
    platform: str,
    mode: str | None = None,
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """M4: Publish content to a platform."""
    from .services.publish_gateway import publish_content, check_sensitive
    conn, content = load_content_for_user(content_id, user, {"owner", "editor"})
    row = conn.execute("SELECT body FROM contents WHERE id = %s", (content_id,)).fetchone()
    conn.close()
    if row:
        body_text = ""
        if isinstance(row.get("body"), dict):
            body_text = "\n".join(c.get("text","") for c in row["body"].get("content",[]))
        if body_text:
            safety = check_sensitive(body_text[:5000])
            if not safety["passed"]:
                return ok({"blocked": True, "words": safety["blocked_words"]})
    result = publish_content(content_id, platform, mode, user_id=user["id"])
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return ok(result)


@app.post("/api/v1/contents/{content_id}/check-sensitive")
def check_content_sensitive(content_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """Standalone sensitive-word check so the UI can validate before publishing."""
    from .services.publish_gateway import check_sensitive
    conn, content = load_content_for_user(content_id, user)
    conn.close()
    body = content.get("body")
    body = decode(body, {}) if isinstance(body, str) else (body or {})
    text = "\n".join(c.get("text", "") for c in body.get("content", [])) if isinstance(body, dict) else str(body)
    result = check_sensitive(text[:5000])
    return ok({"passed": result["passed"], "blocked_words": result["blocked_words"], "checked_chars": len(text[:5000])})


@app.get("/api/v1/novels/{novel_id}/narrative")
def get_novel_narrative(novel_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """Return one V7-first narrative read model for the review panel.

    Older novels may still have rows in ``timeline_events``/``arcs``.  New V7
    chapters persist the same facts in ``v7_story_states`` and chapter
    transition contracts, so the page must read both sources into one truthful
    response instead of showing an empty card when the legacy projection is
    absent.
    """
    conn, novel = load_content_for_user(novel_id, user)
    timeline = [dict(r) for r in conn.execute(
        """SELECT te.event_text AS event, (c.meta->>'seq')::int AS chapter_seq
           FROM timeline_events te JOIN contents c ON c.id = te.chapter_id
           WHERE c.parent_id = %s ORDER BY chapter_seq, te.event_order LIMIT 200""",
        (novel_id,),
    ).fetchall()]
    arcs = [dict(r) for r in conn.execute(
        """SELECT character_name AS character, stage, goal, status
           FROM arcs WHERE novel_id = %s ORDER BY character_name""",
        (novel_id,),
    ).fetchall()]

    timeline_source = "legacy_timeline_events" if timeline else "none"
    arcs_source = "legacy_arcs" if arcs else "none"

    # Chapter metadata is the V6 product boundary for accepted V7 results. It
    # is a real persisted summary/transition source, not a generated fallback.
    v7_chapters = conn.execute(
        """
        SELECT title, body, meta, seq
        FROM contents
        WHERE parent_id=%s AND type='chapter' AND is_deleted=FALSE
        ORDER BY COALESCE(seq, (meta->>'seq')::int, 0)
        LIMIT 500
        """,
        (novel_id,),
    ).fetchall()
    if not timeline:
        for row in v7_chapters:
            metadata = decode(row.get("meta"), {}) or {}
            if metadata.get("canonical_engine") != "v7" and metadata.get("source") != "v7":
                continue
            seq = int(row.get("seq") or metadata.get("seq") or 0)
            contract = metadata.get("transition_contract") or {}
            summary = (
                metadata.get("chapter_summary")
                or (contract.get("end_state") or {}).get("summary")
                or row.get("title")
            )
            if seq and str(summary or "").strip():
                timeline.append({"event": str(summary).strip(), "chapter_seq": seq})
        if timeline:
            timeline_source = "v7_chapter_metadata"

    # V7 Novel Brain is the canonical state store.  Use it when the old
    # extraction tables are empty, and keep the source visible to the UI.
    v7_states: list[dict[str, Any]] = []
    if not timeline or not arcs:
        try:
            v7_states = [dict(r) for r in conn.execute(
                """
                SELECT state_type, state_key, state_value, is_pending_review, updated_at
                FROM v7_story_states
                WHERE novel_id=%s AND is_active=TRUE
                  AND state_type IN ('chapter', 'plot', 'character')
                ORDER BY updated_at DESC
                LIMIT 800
                """,
                (novel_id,),
            ).fetchall()]
        except Exception:
            # Deployments that have not run V7 migrations may not have this
            # table.  Existing V6 data remains usable; do not fabricate rows.
            v7_states = []

    if not timeline:
        seen_events: set[tuple[int, str]] = set()
        for row in v7_states:
            if row.get("state_type") not in {"chapter", "plot"}:
                continue
            value = decode(row.get("state_value"), {}) or {}
            seq = int(value.get("chapter_number") or 0)
            if not seq:
                match = re.search(r"(\d+)", str(row.get("state_key") or ""))
                seq = int(match.group(1)) if match else 0
            events = value.get("events") if isinstance(value.get("events"), list) else []
            candidates = events or [{
                "summary": value.get("summary")
                or value.get("chapter_summary")
                or (value.get("transition_contract") or {}).get("end_state", {}).get("summary")
                or value.get("title")
            }]
            for event in candidates:
                summary = str(event.get("summary") if isinstance(event, dict) else event or "").strip()
                if not summary:
                    continue
                key = (seq, summary)
                if key in seen_events:
                    continue
                seen_events.add(key)
                timeline.append({"event": summary, "chapter_seq": seq})
        if timeline:
            timeline_source = "v7_story_states"

    if not arcs:
        grouped_arcs: dict[str, dict[str, Any]] = {}
        for row in v7_states:
            if row.get("state_type") != "character":
                continue
            value = decode(row.get("state_value"), {}) or {}
            raw_key = str(row.get("state_key") or "").strip()
            character = str(
                value.get("character")
                or value.get("name")
                or value.get("title")
                or raw_key.replace("v6:character:", "").split(".", 1)[0]
            ).strip()
            if not character:
                continue
            grouped_arcs[character] = {
                "character": character,
                "stage": value.get("stage") or value.get("arc_stage") or "已建立",
                "goal": value.get("goal") or value.get("summary") or value.get("detail") or "人物状态已由 V7 真相状态记录",
                "status": value.get("status") or ("pending_review" if row.get("is_pending_review") else "tracked"),
            }
        if grouped_arcs:
            arcs = list(grouped_arcs.values())
            arcs_source = "v7_story_states"

    timeline.sort(key=lambda item: (int(item.get("chapter_seq") or 0), str(item.get("event") or "")))
    arcs.sort(key=lambda item: str(item.get("character") or ""))
    conn.close()
    return ok({
        "timeline": timeline[:200],
        "arcs": arcs[:200],
        "evidence": {
            "engine": "v7",
            "timeline_source": timeline_source,
            "arcs_source": arcs_source,
            "timeline_count": len(timeline),
            "arcs_count": len(arcs),
        },
    })


@app.get("/api/v1/stats/overview")
def stats_overview(user: dict = Depends(get_current_user)) -> ApiResponse:
    """Real workspace statistics for the settings page (scoped to the user's projects)."""
    conn = connect()
    row = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM ai_calls a JOIN project_members pm ON pm.project_id = a.project_id
              WHERE pm.user_id = %s) AS ai_calls,
             (SELECT COUNT(*) FROM contents c JOIN project_members pm ON pm.project_id = c.project_id
              WHERE pm.user_id = %s AND c.is_deleted = FALSE) AS contents,
             pg_size_pretty(pg_database_size(current_database())) AS db_size""",
        (user["id"], user["id"]),
    ).fetchone()
    conn.close()
    return ok({"ai_calls": int(row["ai_calls"] or 0), "contents": int(row["contents"] or 0),
               "db_size": row["db_size"]})


@app.post("/api/v1/overseas/translate")
@limiter.limit("20/minute")
def overseas_translate(
    request: Request,
    content_id: str,
    target_lang: str = "en",
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """M4: Translate content for overseas publishing."""
    from app.gateway import BudgetExceeded, ProviderError
    from .services.overseas import translate_chapter
    conn, content = load_content_for_user(content_id, user, {"owner", "editor"})
    row = conn.execute("SELECT body FROM contents WHERE id = %s", (content_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="content not found")
    body_text = ""
    if isinstance(row.get("body"), dict):
        body_text = "\n".join(c.get("text","") for c in row["body"].get("content",[]))
    try:
        return ok(translate_chapter(body_text[:8000], target_lang, content["project_id"]))
    except (ProviderError, BudgetExceeded) as exc:
        raise HTTPException(status_code=502, detail={"code": "AI_PROVIDER_FAILED", "detail": str(exc)}) from exc


@app.get("/api/v1/publish/records")
def publish_records(content_id: str | None = None, user: dict = Depends(get_current_user)) -> ApiResponse:
    """M4: List publish records."""
    from .services.publish_gateway import list_publish_records
    if content_id:
        conn, _content = load_content_for_user(content_id, user)
        conn.close()
        return ok(list_publish_records(content_id))
    conn = connect()
    project_ids = [
        row["project_id"]
        for row in conn.execute("SELECT project_id FROM project_members WHERE user_id = %s", (user["id"],)).fetchall()
    ]
    conn.close()
    return ok(list_publish_records(project_ids=project_ids))


@app.post("/api/v1/collaboration/invite")
def invite_member(
    project_id: str,
    email: str,
    role: str = "editor",
    user: dict = Depends(get_current_user),
) -> ApiResponse:
    """M5: Invite a user to collaborate on a project."""
    from .services.collaboration import invite_user
    conn = connect()
    ensure_project_member(conn, project_id, user, {"owner"})
    conn.close()
    return ok(invite_user(project_id, email, role, invited_by=user["id"]))


@app.get("/api/v1/collaboration/members")
def collaboration_members(project_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """M5: List project members."""
    from .services.collaboration import list_members
    conn = connect()
    ensure_project_member(conn, project_id, user)
    conn.close()
    return ok(list_members(project_id))


@app.get("/api/v1/collaboration/logs")
def collaboration_logs(project_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """M5: Get operation logs."""
    from .services.collaboration import get_operation_logs
    conn = connect()
    ensure_project_member(conn, project_id, user)
    conn.close()
    return ok(get_operation_logs(project_id))


@app.get("/api/v1/novels/{novel_id}/foreshadowings")
def list_foreshadowings(novel_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """TASK-018: List foreshadowings for a novel."""
    conn, _ = load_content_for_user(novel_id, user)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM foreshadowings WHERE chapter_id IN (SELECT id FROM contents WHERE parent_id = %s) ORDER BY created_at DESC",
        (novel_id,)
    ).fetchall()]
    conn.close()
    return ok(rows)


@app.post("/api/v1/novels/{novel_id}/volume-gate/{volume_num}")
def run_volume_gate(novel_id: str, volume_num: int, user: dict = Depends(get_current_user)) -> ApiResponse:
    """卷级门禁：完成度/伏笔回收/实体矛盾检查，结果持久化到 meta.volume_gates。"""
    conn, novel = load_content_for_user(novel_id, user, {"owner", "editor"})
    conn.close()
    if novel["type"] != "novel":
        raise HTTPException(status_code=400, detail="content is not a novel")
    from app.services.narrative_engine import volume_gate
    return ok(volume_gate(novel_id, volume_num))


@app.get("/api/v1/novels/{novel_id}/volume-gates")
def list_volume_gates(novel_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    conn, novel = load_content_for_user(novel_id, user)
    conn.close()
    meta = decode(novel.get("meta"), {}) or {}
    return ok({"volume_plan": meta.get("volume_plan", []), "volume_gates": meta.get("volume_gates", {})})


# --- C3: Agent registry ---

@app.get("/api/v1/agents")
def list_agents_endpoint(user: dict = Depends(get_current_user)) -> ApiResponse:
    """List all registered AI agents with their contracts."""
    from app.services.agent_registry import list_agents
    agents = list_agents()
    return ok({"agents": agents, "count": len(agents)})


@app.get("/api/v1/agents/{agent_id}")
def get_agent_endpoint(agent_id: str, user: dict = Depends(get_current_user)) -> ApiResponse:
    """Get agent definition by ID."""
    from app.services.agent_registry import get_agent
    agent = get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    return ok({"id": agent_id, **agent})


@app.post("/api/v1/agents/{agent_id}/execute")
@limiter.limit("20/minute")
def execute_agent_endpoint(request: Request, agent_id: str, payload: AgentExecuteRequest,
                           user: dict = Depends(get_current_user)) -> ApiResponse:
    """Execute an Agent contract through the real gateway with project isolation."""
    conn = connect()
    ensure_project_member(conn, payload.project_id, user, {"owner", "editor"})
    conn.close()
    from app.services.agent_registry import execute_agent
    try:
        result = execute_agent(agent_id, payload.project_id, payload.variables,
                               payload.client_mutation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ok(result)



# ---- Feedback ----

class FeedbackPayload(BaseModel):
    type: str = "bug"
    title: str
    body: str = ""
    page: str = ""
    metadata: dict | None = None


@app.post("/api/v1/feedback")
def submit_feedback(payload: FeedbackPayload, user: dict = Depends(get_current_user)) -> ApiResponse:
    """User feedback endpoint — bugs, feature requests, suggestions."""
    conn = connect()
    conn.execute(
        "INSERT INTO operation_logs (id, user_id, action, target_type, target_id, details, project_id, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
        (
            new_id("fb"),
            user["id"],
            f"feedback_{payload.type}",
            payload.title[:200],
            payload.page or "",
            encode({"body": payload.body[:2000], "type": payload.type, "meta": payload.metadata or {}}),
            user.get("active_project_id"),
        ),
    )
    conn.commit()
    conn.close()
    return ok({"submitted": True})


# ---- Feature Flags ----

from app.api.v1.config import require_admin_reads


@app.get("/api/v1/admin/feature-flags")
def list_feature_flags(user: dict = Depends(require_admin_reads)) -> ApiResponse:
    from app.core.feature_flags import all_flags as get_all_flags
    return ok({"flags": get_all_flags()})


# ---- SSE Progress ----
try:
    from sse_starlette.sse import EventSourceResponse

    @app.get("/api/v1/runs/{run_id}/stream")
    async def stream_run_progress(run_id: str, user: dict = Depends(get_current_user)):
        from app.api.v1.sse_progress import run_progress_stream
        return EventSourceResponse(run_progress_stream(run_id))
except ImportError:
    pass  # sse-starlette not installed — SSE unavailable

# ---- Module Marketplace ----
from app.api.v1.modules import router as modules_router
app.include_router(modules_router)

from app.api.v1.checkpoint import router as checkpoint_router
app.include_router(checkpoint_router, prefix="/api/v1")
