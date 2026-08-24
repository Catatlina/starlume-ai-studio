from __future__ import annotations

import json
import os
import uuid
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

DB_URL = os.getenv("DATABASE_URL", "postgresql://genius@localhost/novelcraft_dev")

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=4, maxconn=40, dsn=DB_URL,
        )
    return _pool


def connect() -> DB:
    """Get a connection from the pool.

    Raises RuntimeError when pool is exhausted (all connections in use) instead
    of blocking indefinitely in production Traefik / gunicorn-worker scenarios.
    """
    try:
        conn = _get_pool().getconn()
    except psycopg2.pool.PoolError:
        raise RuntimeError("db pool exhausted — retry later")
    conn.autocommit = False
    db = DB(conn)
    db._pool = _get_pool()
    return db


def putconn(db: DB) -> None:
    """Return connection to pool."""
    if hasattr(db, '_conn') and hasattr(db, '_pool'):
        db._pool.putconn(db._conn)
    elif hasattr(db, '_cur'):
        db._cur.connection.close()


def close_pool() -> None:
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None


def new_id(_legacy_prefix: str = "") -> str:
    """Return a database-compatible UUID; the legacy prefix is intentionally ignored."""
    return str(uuid.uuid4())


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def decode(value: Any, default: Any = None) -> Any:
    """Decode JSON string or return value as-is (psycopg2 auto-decodes JSONB to dicts)."""
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


class DB:
    """Thin psycopg2 wrapper matching sqlite3 Connection interface used by app code."""

    def __init__(self, conn: psycopg2.extensions.connection):
        self._conn = conn
        self._cur = conn.cursor()

    def __enter__(self) -> DB:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self.close()
        return False

    def execute(self, sql: str, params: tuple = ()) -> DB:
        self._cur.execute(sql, params)
        return self

    @property
    def rowcount(self) -> int:
        """Rows affected by the last execute (execute returns self, so callers
        can read `db.execute(...).rowcount` like a DB-API cursor)."""
        return self._cur.rowcount

    def fetchone(self) -> dict[str, Any] | None:
        row = self._cur.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        # RealDictRow or tuple fallback
        cols = [d[0] for d in self._cur.description] if self._cur.description else []
        return dict(zip(cols, row)) if cols else dict(row)

    def fetchall(self) -> list[dict[str, Any]]:
        rows = self._cur.fetchall()
        if not rows:
            return []
        first = rows[0]
        if isinstance(first, dict):
            return list(rows)
        cols = [d[0] for d in self._cur.description] if self._cur.description else []
        return [dict(zip(cols, r)) for r in rows]

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._cur.close()
        self._conn.rollback()
        try:
            putconn(self)
        except Exception:
            self._conn.close()

    def executescript(self, sql: str) -> None:
        """For compatibility only — split on semicolons and execute each."""
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                self._cur.execute(stmt)


def init_db() -> None:
    """Ensure seed data exists (tables already created by Alembic)."""
    from .config import settings

    db = connect()
    # Development databases are often created from an older Alembic snapshot.
    # Keep the shared V6/V7 execution ledger available before seed calls run;
    # production still receives the same object through Alembic.
    from .services.ai_runtime import ensure_shared_ledger_schema
    ensure_shared_ledger_schema(db)
    existing = db.execute("SELECT id FROM projects LIMIT 1").fetchone()
    if existing is None and settings.environment.lower() == "development":
        # Development convenience only. Production must never get a known password.
        user_id = new_id()
        from .core.security import hash_password
        db.execute(
            "INSERT INTO users (id, email, password_hash, display_name) VALUES (%s, %s, %s, %s)",
            (user_id, "admin@novelcraft.local", hash_password("admin123"), "管理员"),
        )
        project_id = new_id()
        db.execute(
            "INSERT INTO projects (id, name, description, owner_id) VALUES (%s, %s, %s, %s)",
            (project_id, "NovelCraft Studio", "默认创作项目", user_id),
        )
        db.execute(
            "INSERT INTO budgets (id, project_id, scope, limit_cny, spent_cny) VALUES (%s, %s, %s, %s, %s)",
            (new_id("bdg"), project_id, "default", float(settings.default_monthly_budget_cny), 0),
        )
        # Give the dev admin a Free subscription so billing queries return a real row.
        db.execute(
            """
            INSERT INTO subscriptions (id, user_id, plan_id, status, started_at, expires_at, auto_renew)
            SELECT %s, %s, 'plan_free', 'active', now(), NULL, TRUE
            WHERE NOT EXISTS (SELECT 1 FROM subscriptions WHERE user_id = %s)
            """,
            (new_id(), user_id, user_id),
        )
    # Keep project ownership and authorization membership consistent, including
    # databases created by older builds that omitted the owner membership row.
    missing_owners = db.execute(
        """
        SELECT p.id AS project_id, p.owner_id
        FROM projects p
        LEFT JOIN project_members pm
          ON pm.project_id = p.id AND pm.user_id = p.owner_id
        WHERE p.owner_id IS NOT NULL AND pm.id IS NULL
        """
    ).fetchall()
    for owner in missing_owners:
        db.execute(
            """
            INSERT INTO project_members (id, project_id, user_id, role)
            VALUES (%s, %s, %s, 'owner')
            ON CONFLICT(project_id, user_id) DO NOTHING
            """,
            (new_id(), owner["project_id"], owner["owner_id"]),
        )
    from .prompt_registry import PROMPT_SEEDS
    GOLDEN_CASES = {
        "bootstrap.gen_titles": [{"idea":"AI觉醒","genre":"科幻","expected_titles":3},{"idea":"重生80年代","genre":"都市","expected_titles":3},{"idea":"修仙废柴逆袭","genre":"仙侠","expected_titles":3}],
        "bootstrap.gen_synopsis": [{"title":"测试","genre":"科幻","expected_length":200}]*3,
        "bootstrap.gen_worldview": [{"title":"测试","genre":"科幻","expected_elements":["科技体系"]}]*3,
        "bootstrap.gen_characters": [{"title":"测试","genre":"科幻","min_characters":3}]*3,
        "bootstrap.gen_outline": [{"title":"测试","genre":"科幻","expected_volumes":3}]*3,
        "bootstrap.gen_chapter1": [{"title":"测试","style":"硬核","min_words":500}]*3,
        "bootstrap.review_7dim": [{"expected_dimensions":7,"min_score":60}]*3,
        "editor.polish": [{"input":"他走在街上。","preserves_meaning":True}]*3,
    }
    for name, version, model, template in PROMPT_SEEDS:
        cases = GOLDEN_CASES.get(name, [{"input": {}, "expected_shape": "json"}])
        db.execute(
            """
            INSERT INTO prompts (id, name, version, model, template, golden_cases)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(name, version, model) DO UPDATE
            SET template = EXCLUDED.template,
                golden_cases = EXCLUDED.golden_cases,
                updated_at = now()
            """,
            (new_id("prm"), name, version, model, template, encode(cases)),
        )
    task_types = [
        "gen_titles", "gen_synopsis", "gen_worldview", "gen_characters",
        "gen_outline", "gen_chapter1", "review_7dim",
        "review_ooc", "review_consistency", "review_rhythm",
        "editor_polish", "editor_rewrite", "editor_continue",
        "editor_expand", "editor_condense", "editor_deai",
        "summarize_chapter", "summarize_volume", "summarize_book",
        "gen_next_chapter", "extract_entities", "extract_foreshadowing", "expand_outline",
        "extract_timeline", "extract_arcs",
        "chapter_skeleton", "chapter_draft",
        "gen_short_titles", "gen_short_story", "review_short",
        "gen_video_script", "fetch_hotspots", "gen_daily_brief",
        "hm_daily_brief", "hm_title_variants", "hm_material_suggestions",
        "translate_segment", "cultural_localize", "ranking_market_analysis", "book_analysis",
        "performance_feedback", "localize_names",
        # v1.1 publishing: real provider disclosure and semantic payoff review
        "publishing_ai_disclosure", "publishing_payoff_semantic",
        # V2 four-stage bootstrap (18 agent nodes) — every node must resolve a
        # real model route or the flagship flow fails at its first node.
        "plan_idea", "repair_planning_contract", "expand_creative_bible", "audit_plan_fidelity", "regenerate_titles", "plan_market_fit", "plan_story_pattern", "plan_core_gameplay",
        "plan_world_architecture", "plan_character_system", "plan_conflict_map",
        "blueprint_volume_plan", "generate_story_arc", "blueprint_chapter_outline", "blueprint_scene_beat",
        "write_chapter_draft", "write_self_review", "write_polish",
        "write_length_check", "write_fact_reconcile",
        "final_consistency_check", "final_continuity_audit", "final_humanize",
        "repair_local", "replan_chapter",
        # V6.1.2 chapter closed loop
        "review_7dim_structured",
        "relearn_style",
        "extract_story_facts",
        "extract_ledger",
        "engine_chat",
        "reader_simulation",
    ]
    # Creative long-form nodes get a slightly higher temperature; structured
    # planning/audit nodes stay at 0.7 default.
    CREATIVE_TASKS = {"write_chapter_draft", "chapter_draft", "write_polish", "final_humanize",
                      "gen_chapter1", "gen_next_chapter"}
    for task_type in task_types:
        temperature = 1.0 if task_type in CREATIVE_TASKS else 0.7
        db.execute(
            """
            INSERT INTO model_routes (id, task_type, provider, model, params, fallback_json)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(task_type) DO NOTHING
            """,
            (new_id(), task_type, "deepseek", "deepseek-chat", encode({"temperature": temperature}), encode([])),
        )
    # Repair any routes that were seeded or edited to models not available on
    # the current DeepSeek account tier (deepseek-v4-pro / deepseek-v4-flash).
    # This is a one-way safety fix; once routes are on deepseek-chat they stay.
    db.execute(
        "UPDATE model_routes SET model = 'deepseek-chat' "
        "WHERE provider = 'deepseek' AND model IN ('deepseek-v4-pro','deepseek-v4-flash','deepseek-reasoner')"
    )
    # Seed sensitive word list
    SENSITIVE_WORDS = ["政治敏感", "色情", "暴力恐怖", "赌博", "毒品", "枪支", "诈骗", "传销", "邪教", "违禁内容",
                       "分裂国家", "颠覆政权", "民族仇恨", "宗教极端", "淫秽", "凶杀", "校园暴力", "自杀",
                       "假币", "假发票", "人体器官", "间谍器材", "非法集资", "高利贷", "套路贷",
                       "迷药", "催情", "窃听", "偷拍", "考试作弊", "代孕", "代写论文",
                       "刷单", "刷粉", "删帖", "水军", "网络攻击", "木马", "病毒"]
    for word in SENSITIVE_WORDS:
        db.execute(
            "INSERT INTO sensitive_words (id, word, category) VALUES (%s, %s, %s) ON CONFLICT(word) DO NOTHING",
            (new_id(), word, "通用"),
        )
    # V3 web-novel strategy library MVP (§6): create table (idempotent safety
    # net in case Alembic has not run) and seed built-in strategies.
    db.execute("""
        CREATE TABLE IF NOT EXISTS strategy (
            id VARCHAR(36) PRIMARY KEY,
            category VARCHAR(50) NOT NULL,
            name VARCHAR(200) NOT NULL,
            description TEXT,
            stages JSONB NOT NULL DEFAULT '[]',
            applicable_conditions JSONB NOT NULL DEFAULT '[]',
            is_builtin BOOLEAN DEFAULT TRUE,
            status VARCHAR(20) DEFAULT 'active',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    STRATEGY_SEEDS = [
        {
            "id": "stg_golden_three",
            "category": "开篇",
            "name": "黄金三章",
            "description": "前3章必须立人设、抛悬念、给甜头，禁止拖剧情。",
            "stages": ["立人设", "抛悬念", "给甜头", "埋长线"],
            "applicable_conditions": ["章节序号 <= 3", "或 function_type 为 开篇/立规矩"],
        },
        {
            "id": "stg_face_slap",
            "category": "爽点",
            "name": "打脸策略",
            "description": "先压后扬的爽点结构：隐藏实力被误解轻视，事件触发后能力展示，群体态度反转。",
            "stages": ["压制", "误解", "隐藏实力", "事件触发", "能力展示", "群体态度变化", "新的目标"],
            "applicable_conditions": ["function_type 为 爽点/冲突/打脸", "存在隐藏实力类人设"],
        },
        {
            "id": "stg_identity_reversal",
            "category": "爽点",
            "name": "身份反转",
            "description": "铺垫平凡被轻视，线索浮现后引爆真实身份，重构人物关系。",
            "stages": ["铺垫平凡", "误会轻视", "线索浮现", "真相引爆", "关系重构"],
            "applicable_conditions": ["function_type 为 身份反转", "或本章含反转转折"],
        },
    ]
    for s in STRATEGY_SEEDS:
        db.execute(
            """INSERT INTO strategy (id, category, name, description, stages, applicable_conditions, is_builtin, status)
               SELECT %s, %s, %s, %s, %s, %s, TRUE, 'active'
               WHERE NOT EXISTS (SELECT 1 FROM strategy WHERE name = %s)""",
            (s["id"], s["category"], s["name"], s["description"],
             encode(s["stages"]), encode(s["applicable_conditions"]), s["name"]),
        )
    db.commit()
    db.close()


def row_to_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return row
