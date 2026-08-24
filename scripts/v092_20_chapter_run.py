#!/usr/bin/env python3
"""v0.9.2 真实 Provider 20 章长跑验收。

脚本只接受显式的作品、项目和用户 ID，并从数据库读取平台规则和作品元数据。
它不会伪造规则、content hash 或门禁证据；生成、正文读取、门禁运行、证据写入任一失败都会以非零状态结束。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import traceback
from datetime import datetime, timezone
from typing import Any


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="v0.9.2真实Provider 20章长跑验收")
    parser.add_argument("--novel-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--platform", default="fanqie")
    parser.add_argument(
        "--acceptance-scope",
        choices=("generation", "publication"),
        default="generation",
        help=(
            "验收范围：generation只验证真实生成/质量/连续性，不调用发布语义评估；"
            "publication才运行七道发布门禁并要求publish_ready"
        ),
    )
    parser.add_argument("--start-chapter", type=int, default=None)
    parser.add_argument("--target-chapters", type=int, default=20)
    parser.add_argument("--output", default="")
    return parser.parse_args()


def _fetchone(sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    from app.db import connect

    conn = connect()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def load_novel_metadata(novel_id: str) -> dict[str, Any]:
    """读取真实作品元数据，不使用验收脚本内置书名或标签。"""
    row = _fetchone(
        "SELECT title, meta FROM contents WHERE id=%s AND type='novel' AND is_deleted=FALSE",
        (novel_id,),
    )
    if not row:
        raise RuntimeError(f"小说不存在或已删除: {novel_id}")
    meta = row.get("meta") or {}
    if not isinstance(meta, dict):
        raise RuntimeError("小说meta不是对象，拒绝使用不完整元数据验收")
    return {
        "title": row.get("title") or meta.get("title", ""),
        "synopsis": meta.get("synopsis") or meta.get("description", ""),
        "tags": meta.get("tags") or [],
        "category": meta.get("category") or meta.get("genre", ""),
    }


def load_platform_profile(project_id: str, platform: str) -> dict[str, Any]:
    """读取已配置的平台规则，缺失时明确失败。"""
    row = _fetchone(
        """
        SELECT platform, policy_status, policy_version, ai_usage_policy,
               chapter_word_min, chapter_word_max
        FROM platform_publication_profiles
        WHERE project_id=%s AND platform=%s AND is_active=TRUE
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (project_id, platform),
    )
    if not row:
        raise RuntimeError(f"项目未配置平台规则: project_id={project_id}, platform={platform}")
    return row


def resolve_start_chapter(novel_id: str, requested: int | None) -> int:
    if requested is not None:
        if requested < 1:
            raise ValueError("--start-chapter必须大于0")
        return requested
    row = _fetchone(
        """
        SELECT COALESCE(
                   MAX(COALESCE(
                       NULLIF(seq, 0),
                       CASE WHEN meta->>'seq' ~ '^[0-9]+$'
                            THEN (meta->>'seq')::int END,
                       CASE WHEN meta->>'chapter_number' ~ '^[0-9]+$'
                            THEN (meta->>'chapter_number')::int END,
                       0
                   )),
                   0
               ) AS max_chapter
        FROM contents WHERE parent_id=%s AND type='chapter'
        """,
        (novel_id,),
    )
    return int(row["max_chapter"]) + 1 if row else 1


def chapter_status(novel_id: str, chapter_number: int) -> str | None:
    row = _fetchone(
        """
        SELECT status FROM contents
        WHERE parent_id=%s AND type='chapter'
          AND COALESCE(
                  NULLIF(seq, 0),
                  CASE WHEN meta->>'seq' ~ '^[0-9]+$'
                       THEN (meta->>'seq')::int END,
                  CASE WHEN meta->>'chapter_number' ~ '^[0-9]+$'
                       THEN (meta->>'chapter_number')::int END,
                  0
              )=%s
        ORDER BY updated_at DESC LIMIT 1
        """,
        (novel_id, chapter_number),
    )
    return str(row["status"]) if row else None


def assert_clean_long_run_target(novel_id: str) -> None:
    """Refuse a long run whose target carries historical prose or V7 state.

    A failed predecessor must never be treated as a valid starting point for
    a 20-chapter acceptance run.  The caller should create a dedicated blank
    acceptance copy instead of deleting an author's working manuscript.
    """
    chapter_row = _fetchone(
        """
        SELECT COUNT(*) AS count
        FROM contents
        WHERE parent_id=%s AND type='chapter' AND is_deleted=FALSE
        """,
        (novel_id,),
    )
    chapter_count = int((chapter_row or {}).get("count") or 0)
    state_count = 0
    try:
        state_row = _fetchone(
            "SELECT COUNT(*) AS count FROM v7_story_states WHERE novel_id=%s",
            (novel_id,),
        )
        state_count = int((state_row or {}).get("count") or 0)
    except Exception:
        # Older deployments may not have the V7 table yet.  The content
        # check remains mandatory; generation itself will fail closed later.
        state_count = 0
    if chapter_count or state_count:
        raise RuntimeError(
            "20章长跑必须从清空历史的验收副本开始: "
            f"active_chapters={chapter_count}, v7_states={state_count}; "
            "拒绝在旧正文上续跑"
        )


async def generate_one_chapter(
    novel_id: str, project_id: str, user_id: str, chapter_number: int
) -> dict[str, Any]:
    from app.v7.runtime import generate_v7_chapter

    started = time.time()
    log(f"开始生成第{chapter_number}章...")
    try:
        result = await generate_v7_chapter(
            novel_id=novel_id,
            project_id=project_id,
            chapter_number=chapter_number,
            user_id=user_id,
        )
        # The canonical V7 runtime returns the persisted V6 content id under
        # ``v6_content_id``; accept the legacy keys only for compatibility.
        chapter_id = (
            result.get("v6_content_id")
            or result.get("chapter_id")
            or result.get("id")
            or ""
        )
        status = result.get("status", "unknown")
        success = status == "completed" and bool(chapter_id)
        log(
            f"第{chapter_number}章生成结束: status={status}, chapter_id={chapter_id}, "
            f"耗时={time.time() - started:.1f}s"
        )
        return {
            "chapter_number": chapter_number,
            "chapter_id": chapter_id,
            "status": status,
            "success": success,
            "elapsed_seconds": round(time.time() - started, 1),
            "raw": result,
            "error": None if success else f"生成状态不可验收: {status}",
        }
    except Exception as exc:
        log(f"第{chapter_number}章生成失败: {exc}")
        traceback.print_exc()
        return {
            "chapter_number": chapter_number,
            "chapter_id": "",
            "status": "failed",
            "success": False,
            "elapsed_seconds": round(time.time() - started, 1),
            "raw": {},
            "error": str(exc),
        }


def get_chapter_text(chapter_id: str) -> str:
    from app.services.novel_export import extract_body_text

    row = _fetchone(
        "SELECT body FROM contents WHERE id=%s AND type='chapter'",
        (chapter_id,),
    )
    if not row or not row.get("body"):
        return ""
    return extract_body_text(row["body"])


def run_publishing_gates(
    chapter_id: str,
    text: str,
    project_id: str,
    user_id: str,
    platform: str,
    platform_profile: dict[str, Any],
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    from app.v7.quality.semantic_assessments import assess_payoff_semantically
    from app.v7.quality.publishing_gates import run_all_gates

    if not text:
        return {"error": "empty text", "gates": {}}, None
    semantic_payoff = assess_payoff_semantically(
        project_id=project_id,
        chapter_id=chapter_id,
        text=text,
        platform=platform,
        user_id=user_id,
    )
    report = run_all_gates(
        chapter_id=chapter_id,
        text=text,
        platform_profile=platform_profile,
        metadata=metadata,
        semantic_payoff=semantic_payoff,
    )
    return {
        "quality_candidate": report.quality_candidate,
        "overall_publish_ready": report.overall_publish_ready,
        "blocking_failures": report.blocking_failures,
        "non_blocking_warnings": report.non_blocking_warnings,
        "gates": {
            key: {
                "passed": gate.passed,
                "score": gate.score,
                "threshold": gate.threshold,
                "is_blocking": gate.is_blocking,
                "issues_count": len(gate.issues),
                "warnings_count": len(gate.warnings),
            }
            for key, gate in report.gates.items()
        },
    }, report


def persist_gate_results(chapter_id: str, text: str, report: Any) -> None:
    if report is None:
        raise ValueError("缺少门禁报告，拒绝写入不完整验收证据")
    from app.db import connect
    from app.v7.services.publishing_service import save_gate_results
    from app.v7.services.publishing_service import save_statistics_snapshot

    conn = connect()
    try:
        save_statistics_snapshot(conn, chapter_id, text, report.variant_id)
        save_gate_results(conn, report)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def validate_generation_result(result: dict[str, Any], text: str) -> dict[str, Any]:
    """Validate the generation contract without invoking another Provider.

    The V7 runtime already performs planning, prose generation, review and
    continuity/state persistence.  A generation long run must inspect that
    evidence directly; it must not call the publication payoff assessor or
    require human disclosure confirmation before the author has edited prose.
    """
    # ``generate_one_chapter`` returns an execution envelope and keeps the
    # canonical V7 payload under ``raw``.  Accept both shapes so the runner
    # cannot turn a valid V7 result into a false failure by reading only the
    # envelope fields.
    payload = result.get("raw") if isinstance(result.get("raw"), dict) else result
    failures: list[dict[str, Any]] = []
    if payload.get("status") != "completed":
        failures.append({"code": "generation_status", "actual": payload.get("status")})
    if payload.get("passed_review") is not True:
        failures.append({"code": "v7_review", "actual": payload.get("passed_review")})

    quality_gate = payload.get("quality_gate") or {}
    if quality_gate.get("passed") is not True:
        failures.append({
            "code": "v7_quality_gate",
            "failures": list(quality_gate.get("failures") or []),
        })

    continuity = payload.get("continuity") or {}
    if continuity and continuity.get("passed") is False:
        failures.append({
            "code": "continuity",
            "issues": list(continuity.get("issues") or []),
        })

    budget = payload.get("reader_chapter_budget") or {}
    generation_quality = payload.get("generation_quality") or {}
    try:
        char_count = len(text)
        minimum = int(budget.get("minimum_chars") or 0)
        maximum = int(
            generation_quality.get("generation_hard_max_chars")
            or budget.get("maximum_chars")
            or 3000
        )
    except (TypeError, ValueError):
        char_count, minimum, maximum = len(text), 0, 3000
    if not text.strip():
        failures.append({"code": "empty_body"})
    if minimum and char_count < minimum:
        failures.append({"code": "below_reader_minimum", "actual": char_count, "minimum": minimum})
    if maximum and char_count > maximum:
        failures.append({"code": "above_generation_maximum", "actual": char_count, "maximum": maximum})

    return {
        "passed": not failures,
        "scope": "generation",
        "text_length": char_count,
        "reader_minimum": minimum,
        "generation_maximum": maximum,
        "v7_status": payload.get("status"),
        "review_score": payload.get("review_score"),
        "failures": failures,
    }


async def main() -> None:
    args = parse_args()
    output_file = args.output or f"/tmp/v092_20ch_report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    results: list[dict[str, Any]] = []
    success_count = 0
    publish_ready_count = 0
    expected_count = args.target_chapters
    fatal_error: str | None = None

    try:
        if args.target_chapters < 1:
            raise ValueError("--target-chapters必须大于0")
        assert_clean_long_run_target(args.novel_id)
        start_chapter = resolve_start_chapter(args.novel_id, args.start_chapter)
        if start_chapter != 1:
            raise RuntimeError(f"清空历史后的长跑必须从第1章开始，当前起始章={start_chapter}")
        if start_chapter > args.target_chapters:
            raise ValueError("起始章节已经超过目标章节，未执行长跑")

        metadata: dict[str, Any] = {}
        platform_profile: dict[str, Any] = {}
        if args.acceptance_scope == "publication":
            metadata = load_novel_metadata(args.novel_id)
            platform_profile = load_platform_profile(args.project_id, args.platform)
        if start_chapter > 1:
            previous = chapter_status(args.novel_id, start_chapter - 1)
            if previous in {"needs_rewrite", "failed"}:
                raise RuntimeError(f"第{start_chapter - 1}章 status={previous}，必须先修复前置章节")

        expected_count = args.target_chapters - start_chapter + 1
        log("=" * 60)
        log("v0.9.2真实Provider 20章长跑验收开始")
        log(f"小说ID={args.novel_id}, 目标章节={args.target_chapters}, 起始章节={start_chapter}")
        if args.acceptance_scope == "publication":
            log(f"平台={platform_profile.get('platform')}, policy={platform_profile.get('policy_status')}")
        else:
            log("范围=generation（不运行发布语义评估，不要求AI披露确认）")
        log("=" * 60)

        for chapter_number in range(start_chapter, args.target_chapters + 1):
            result = await generate_one_chapter(
                args.novel_id, args.project_id, args.user_id, chapter_number
            )
            results.append(result)
            if not result["success"]:
                result["acceptance_success"] = False
                break

            try:
                text = get_chapter_text(result["chapter_id"])
                result["text_length"] = len(text)
                if not text:
                    raise RuntimeError("generated chapter has no persisted body")

                if args.acceptance_scope == "generation":
                    generation_validation = validate_generation_result(result, text)
                    result["generation_validation"] = generation_validation
                    result["acceptance_success"] = generation_validation["passed"]
                    if generation_validation["passed"]:
                        success_count += 1
                    log(
                        f"第{chapter_number}章正文={len(text)}字，"
                        f"生成验收={'通过' if generation_validation['passed'] else '失败'}，"
                        f"V7评分={result.get('review_score')}"
                    )
                    if not generation_validation["passed"]:
                        log(f"生成验收失败原因={generation_validation['failures']}")
                        break
                else:
                    gates_result, report = run_publishing_gates(
                        result["chapter_id"], text, args.project_id, args.user_id,
                        args.platform, platform_profile, metadata
                    )
                    result["gates"] = gates_result
                    persist_gate_results(result["chapter_id"], text, report)
                    result["acceptance_success"] = True
                    success_count += 1
                    if gates_result["overall_publish_ready"]:
                        publish_ready_count += 1

                    passed = [key for key, value in gates_result["gates"].items() if value["passed"]]
                    failed = [key for key, value in gates_result["gates"].items() if not value["passed"]]
                    log(f"第{chapter_number}章正文={len(text)}字，门禁通过={len(passed)}/7，失败={failed}")
                    log(f"publish_ready={gates_result['overall_publish_ready']}")
            except Exception as exc:
                result["acceptance_success"] = False
                result["acceptance_error"] = str(exc)
                log(f"第{chapter_number}章生成已落库，但验收步骤失败: {exc}")
                traceback.print_exc()
                break

            if chapter_number < args.target_chapters:
                await asyncio.sleep(2)
    except Exception as exc:
        fatal_error = str(exc)
        results.append({
            "chapter_number": None,
            "chapter_id": "",
            "status": "failed",
            "success": False,
            "acceptance_success": False,
            "error": fatal_error,
        })
        log(f"长跑前置或运行失败，已记录报告: {exc}")
        traceback.print_exc()

    with open(output_file, "w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2, default=str)

    log("=" * 60)
    if args.acceptance_scope == "generation":
        log(f"生成验收通过={success_count}/{expected_count}")
    else:
        log(f"生成成功={success_count}/{expected_count}, publish_ready={publish_ready_count}/{success_count}")
    log(f"完整报告={output_file}")
    log("=" * 60)

    failed_publication_scope = (
        args.acceptance_scope == "publication"
        and publish_ready_count != success_count
    )
    if fatal_error or success_count != expected_count or failed_publication_scope:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
