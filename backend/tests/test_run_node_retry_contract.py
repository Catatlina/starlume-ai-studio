import asyncio

import pytest
from fastapi import HTTPException


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, node):
        self.node = node
        self.closed = False

    def execute(self, sql, _params=()):
        if "SELECT status, output FROM run_nodes" in sql:
            return _Cursor(self.node)
        return _Cursor()

    def commit(self):
        pass

    def close(self):
        self.closed = True


def test_quality_hold_cannot_be_retried_as_a_provider_failure(monkeypatch):
    from app import main

    conn = _Conn({"status": "needs_review", "output": {"retryable": False}})
    monkeypatch.setattr(main, "load_run_for_user", lambda *_args, **_kwargs: (conn, {"id": "run-1"}))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.retry_node("run-1", "write_chapter_draft", {"id": "user-1"}))

    assert caught.value.status_code == 409
    assert "不能原样重跑" in caught.value.detail
    assert conn.closed is True


def test_non_retryable_failed_node_is_blocked_by_persisted_failure_metadata(monkeypatch):
    from app import main

    conn = _Conn({
        "status": "failed",
        "output": {"retryable": False, "failure_kind": "generation_contract"},
    })
    monkeypatch.setattr(main, "load_run_for_user", lambda *_args, **_kwargs: (conn, {"id": "run-1"}))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.retry_node("run-1", "write_chapter_draft", {"id": "user-1"}))

    assert caught.value.status_code == 409
    assert "确定性契约问题" in caught.value.detail


def test_quality_hold_run_cannot_bypass_node_guard_through_restart(monkeypatch):
    from app import main

    conn = _Conn({"status": "needs_review", "output": {"retryable": False}})
    monkeypatch.setattr(
        main,
        "load_run_for_user",
        lambda *_args, **_kwargs: (conn, {"id": "run-1", "status": "needs_review"}),
    )

    class _Request:
        headers = {}

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.restart_run("run-1", _Request(), {"id": "user-1"}))

    assert caught.value.status_code == 409
    assert "不能原样重启" in caught.value.detail
