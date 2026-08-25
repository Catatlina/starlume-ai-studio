"""TASK-008: Workflow engine tests — non-Celery tests only."""
import os, uuid
os.environ["NOVELCRAFT_ENV"] = "dev"

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.core.rate_limit import limiter


@pytest.fixture
def client():
    limiter.reset()
    return TestClient(app)


def _auth(client):
    e = f"wf-nc-{uuid.uuid4().hex[:6]}@nc.dev"
    r = client.post("/api/v1/auth/register", json={"email": e, "password": "test1234"})
    return r.json()["data"]["access_token"]


def test_bootstrap_creates_run(client):
    """TASK-008: Bootstrap creates a run with nodes (skips Celery wait)."""
    token = _auth(client)
    pid = client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"}).json()["data"][0]["id"]
    r = client.post(f"/api/v1/projects/{pid}/novels", headers={"Authorization": f"Bearer {token}"},
                    json={"idea": "test flow", "genre": "test", "style": "t", "target_words": 5000})
    nid = r.json()["data"]["id"]
    r2 = client.post(f"/api/v1/novels/{nid}/bootstrap", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert "run_id" in r2.json()["data"]


def test_run_requires_auth(client):
    r = client.get("/api/v1/runs/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 401


def test_run_404_for_unknown(client):
    token = _auth(client)
    r = client.get("/api/v1/runs/00000000-0000-0000-0000-000000000000",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404


def test_latest_run_restores_newest_project_workflow(client):
    token = _auth(client)
    headers = {"Authorization": f"Bearer {token}"}
    pid = client.get("/api/v1/projects", headers=headers).json()["data"][0]["id"]
    novel = client.post(
        f"/api/v1/projects/{pid}/novels",
        headers=headers,
        json={"idea": "restore latest run", "genre": "test", "style": "t", "target_words": 5000},
    ).json()["data"]
    created = client.post(f"/api/v1/novels/{novel['id']}/bootstrap", headers=headers).json()["data"]

    response = client.get(f"/api/v1/runs/latest?project_id={pid}", headers=headers)

    assert response.status_code == 200
    restored = response.json()["data"]
    assert restored["id"] == created["run_id"]
    assert restored["project_id"] == pid
    assert restored["novel_id"] == novel["id"]
    assert restored["nodes"]


def test_latest_run_does_not_leak_other_users_workflow(client):
    first_token = _auth(client)
    first_headers = {"Authorization": f"Bearer {first_token}"}
    first_pid = client.get("/api/v1/projects", headers=first_headers).json()["data"][0]["id"]
    novel = client.post(
        f"/api/v1/projects/{first_pid}/novels",
        headers=first_headers,
        json={"idea": "private run", "genre": "test", "style": "t", "target_words": 5000},
    ).json()["data"]
    client.post(f"/api/v1/novels/{novel['id']}/bootstrap", headers=first_headers)

    second_token = _auth(client)
    second_headers = {"Authorization": f"Bearer {second_token}"}
    response = client.get(f"/api/v1/runs/latest?project_id={first_pid}", headers=second_headers)

    assert response.status_code == 404


def test_human_confirm_requires_auth(client):
    r = client.post("/api/v1/runs/00000000-0000-0000-0000-000000000000/nodes/n2/confirm",
                    json={"selected_title": "test"})
    assert r.status_code == 401


def test_node_retry_requires_auth(client):
    r = client.post("/api/v1/runs/00000000-0000-0000-0000-000000000000/nodes/n1/retry")
    assert r.status_code in [401, 404]


def test_retry_after_generation_fix_only_matches_versioned_false_truncation_defects():
    from app.main import _retry_after_generation_fix

    old_false_rejection = {
        "retryable": False,
        "failure_kind": "generation_contract",
        "error": (
            "single-pass chapter generation contract violation after bounded retry: "
            "first_candidate=3540,final_candidate=2803,minimum=2200,maximum=3000,"
            "retry_mode=compress_recover,provider_truncated=True"
        ),
    }
    recovery = _retry_after_generation_fix("write_chapter_draft", "failed", old_false_rejection)

    assert recovery is not None
    assert recovery["to_version"] == "2.53.0"
    assert recovery["final_chars"] == 2803

    unrelated_252_failure = {
        **old_false_rejection,
        "generation_version": "2.52.0",
        "error": old_false_rejection["error"] + ",terminal_complete=False,terminal_reason=unfinished_sentence",
    }
    assert _retry_after_generation_fix("write_chapter_draft", "failed", unrelated_252_failure) is None

    in_range_tail_regression = {
        "retryable": False,
        "failure_kind": "generation_contract",
        "generation_version": "2.52.0",
        "error": (
            "single-pass chapter generation contract violation after bounded retry: "
            "first_candidate=2524,final_candidate=2749,minimum=2200,maximum=3000,"
            "retry_mode=compress_recover,provider_truncated=True,terminal_complete=False,"
            "terminal_reason=non_terminal_punctuation,initial_max_tokens=1834,"
            "final_max_tokens=2000,chars_per_token=1.3752"
        ),
    }
    tail_recovery = _retry_after_generation_fix(
        "write_chapter_draft", "failed", in_range_tail_regression
    )
    assert tail_recovery is not None
    assert tail_recovery["from_version"] == "2.52.0"
    assert tail_recovery["to_version"] == "2.53.0"
    assert tail_recovery["first_chars"] == 2524

    current_253_failure = {**in_range_tail_regression, "generation_version": "2.53.0"}
    assert _retry_after_generation_fix("write_chapter_draft", "failed", current_253_failure) is None

    genuinely_overlong = {
        **old_false_rejection,
        "error": old_false_rejection["error"].replace("final_candidate=2803", "final_candidate=3803"),
    }
    assert _retry_after_generation_fix("write_chapter_draft", "failed", genuinely_overlong) is None

    quality_failure = {**old_false_rejection, "failure_kind": "quality_contract"}
    assert _retry_after_generation_fix("write_chapter_draft", "failed", quality_failure) is None


def test_expand_outline_endpoint(client):
    token = _auth(client)
    pid = client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"}).json()["data"][0]["id"]
    r = client.post(f"/api/v1/projects/{pid}/novels", headers={"Authorization": f"Bearer {token}"},
                    json={"idea": "expand test", "genre": "test", "style": "t", "target_words": 5000})
    nid = r.json()["data"]["id"]
    r2 = client.post(f"/api/v1/novels/{nid}/expand-outline", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code in [200, 400, 404]


def test_workflow_nodes_structure():
    """TASK-008/V2: Bootstrap node structure is correct (four stages + human gate)."""
    from app.workers.tasks import BOOTSTRAP_NODES
    assert len(BOOTSTRAP_NODES) == 20
    kinds = [n[1] for n in BOOTSTRAP_NODES]
    assert "human" in kinds
    assert "agent" in kinds
    assert any(node[0] == "generate_story_arc" for node in BOOTSTRAP_NODES)


def _make_run(client, token):
    headers = {"Authorization": f"Bearer {token}"}
    pid = client.get("/api/v1/projects", headers=headers).json()["data"][0]["id"]
    r = client.post(
        f"/api/v1/projects/{pid}/novels", headers=headers,
        json={"idea": "restart seed", "genre": "test", "style": "t", "target_words": 5000},
    )
    nid = r.json()["data"]["id"]
    r2 = client.post(f"/api/v1/novels/{nid}/bootstrap", headers=headers)
    return r2.json()["data"]["run_id"], nid, pid


def test_restart_requires_auth(client):
    r = client.post("/api/v1/runs/00000000-0000-0000-0000-000000000000/restart", json={})
    assert r.status_code == 401


def test_retry_after_fix_reuses_run_and_records_audit(monkeypatch):
    import asyncio
    import json
    from starlette.requests import Request
    import app.main as main_mod
    import app.workers.tasks as tasks_mod

    run_id = "run-old-contract"
    old_error = (
        "single-pass chapter generation contract violation after bounded retry: "
        "first_candidate=3540,final_candidate=2803,minimum=2200,maximum=3000,"
        "retry_mode=compress_recover,provider_truncated=True"
    )

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConnection:
        def __init__(self):
            self.statements = []
            self.committed = False
            self.closed = False

        def execute(self, sql, params):
            self.statements.append((" ".join(sql.split()), params))
            if "SELECT status, output, error FROM run_nodes" in sql:
                return Result({
                    "status": "failed",
                    "output": {
                        "retryable": False,
                        "failure_kind": "generation_contract",
                        "error": old_error,
                    },
                    "error": old_error,
                })
            return Result()

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(
        main_mod,
        "load_run_for_user",
        lambda *_args, **_kwargs: (connection, {"context": {"preserved": True}}),
    )

    dispatched = []
    monkeypatch.setattr(tasks_mod, "dispatch_bootstrap_run", lambda *args: dispatched.append(args))
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/retry",
        "headers": [
            (b"x-api-base-url", b"https://provider.test/v1"),
            (b"x-model", b"writer-model"),
        ],
        "query_string": b"",
    })
    response = asyncio.run(
        main_mod.retry_node(
            run_id,
            "write_chapter_draft",
            request,
            user={"id": "user-1"},
        )
    )

    assert response.data["retry_mode"] == "after_code_fix"
    assert connection.committed and connection.closed
    node_update = next(params for sql, params in connection.statements if sql.startswith("UPDATE run_nodes"))
    assert node_update == (run_id, "write_chapter_draft")
    run_update = next(params for sql, params in connection.statements if sql.startswith("UPDATE workflow_runs"))
    saved_context = json.loads(run_update[1])
    assert saved_context["preserved"] is True
    assert saved_context["code_fix_retries"][-1]["to_version"] == "2.53.0"
    assert dispatched == [(run_id, "write_chapter_draft", "", "https://provider.test/v1", "writer-model")]


def test_restart_resets_non_succeeded_nodes_keeps_run_id(client, monkeypatch):
    """Restart resets every non-succeeded node to pending and re-dispatches from
    the earliest non-succeeded node (DAG order), preserving the run_id and any
    already-succeeded node. Succeeded run is NOT touched by restart (that is the
    full re-execute path)."""
    from app.main import connect
    import app.workers.tasks as tasks_mod
    token = _auth(client)
    run_id, _nid, _pid = _make_run(client, token)

    # Seed: all nodes succeeded except plan_market_fit failed.
    conn = connect()
    conn.execute("UPDATE run_nodes SET status='succeeded', output='{\"x\":1}', error=NULL WHERE run_id=%s", (run_id,))
    conn.execute(
        "UPDATE run_nodes SET status='failed', output='{}', error='模型超时' WHERE run_id=%s AND node_key='plan_market_fit'",
        (run_id,),
    )
    conn.execute("UPDATE workflow_runs SET status='failed', current_node_key='plan_market_fit' WHERE id=%s", (run_id,))
    conn.commit()
    conn.close()

    dispatched = []
    monkeypatch.setattr(tasks_mod.execute_bootstrap, "delay", lambda *a, **k: dispatched.append(a))

    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(f"/api/v1/runs/{run_id}/restart", headers=headers)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["run_id"] == run_id
    assert body["start_key"] == "plan_market_fit"
    assert body["status"] == "running"

    conn = connect()
    failed = conn.execute(
        "SELECT status, error FROM run_nodes WHERE run_id=%s AND node_key='plan_market_fit'", (run_id,)
    ).fetchone()
    succeeded = conn.execute(
        "SELECT status FROM run_nodes WHERE run_id=%s AND node_key='plan_idea'", (run_id,)
    ).fetchone()
    run_row = conn.execute(
        "SELECT status, current_node_key FROM workflow_runs WHERE id=%s", (run_id,)
    ).fetchone()
    conn.close()

    assert failed["status"] == "pending"
    assert failed["error"] is None
    assert succeeded["status"] == "succeeded"  # preserved
    assert run_row["status"] == "running"
    assert run_row["current_node_key"] == "plan_market_fit"
    # execute_bootstrap.delay 前两个位置参数必须是 (run_id, start_key)
    assert dispatched and dispatched[0][0] == run_id and dispatched[0][1] == "plan_market_fit"


def test_restart_rejects_succeeded_run(client):
    from app.main import connect
    token = _auth(client)
    run_id, _nid, _pid = _make_run(client, token)
    conn = connect()
    conn.execute("UPDATE run_nodes SET status='succeeded', output='{\"x\":1}' WHERE run_id=%s", (run_id,))
    conn.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=%s", (run_id,))
    conn.commit()
    conn.close()
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(f"/api/v1/runs/{run_id}/restart", headers=headers)
    assert resp.status_code == 409


def test_restart_forwards_byok_headers(client, monkeypatch):
    """§7 #3 X-Model 鉴权范围：restart 必须像 bootstrap/continue 等 AI 端点一样，
    从重启请求透传 BYOK 头（X-Api-Key / X-Api-Base-Url / X-Model）。否则带自定义
    model/key 的 run 重启后会静默回退到服务端默认配置。"""
    from app.main import connect
    import app.workers.tasks as tasks_mod
    token = _auth(client)
    run_id, _nid, _pid = _make_run(client, token)

    conn = connect()
    conn.execute("UPDATE run_nodes SET status='succeeded', output='{\"x\":1}' WHERE run_id=%s", (run_id,))
    conn.execute(
        "UPDATE run_nodes SET status='failed', output='{}', error='x' WHERE run_id=%s AND node_key='plan_market_fit'",
        (run_id,),
    )
    conn.execute("UPDATE workflow_runs SET status='failed', current_node_key='plan_market_fit' WHERE id=%s", (run_id,))
    conn.commit()
    conn.close()

    captured = {}
    def fake_delay(*a, **k):
        captured["args"] = a
        captured["kwargs"] = k
    monkeypatch.setattr(tasks_mod.execute_bootstrap, "delay", fake_delay)

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Api-Key": "sk-test-byok",
        "X-Api-Base-Url": "https://api.openai.com",
        "X-Model": "claude-sonnet-4",
    }
    resp = client.post(f"/api/v1/runs/{run_id}/restart", headers=headers)
    assert resp.status_code == 200
    args = captured.get("args", ())
    # execute_bootstrap.delay(run_id, start_key, "", api_url, model, api_key_ref=...)
    assert args[0] == run_id
    assert args[3] == "https://api.openai.com"  # X-Api-Base-Url 透传
    assert args[4] == "claude-sonnet-4"          # X-Model 透传
    assert captured.get("kwargs", {}).get("api_key_ref"), "X-Api-Key 应经 stash_byok_key 生成非空 ref 透传"
