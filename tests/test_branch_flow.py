"""flow-bug-07 验收：抢领排队发号 / 未领齐禁拼 / 整回滚口径 / 列表进度一致。"""
import sqlite3
import threading

import pytest

from app.branch import BRANCH_PLAN, STRATEGY_NAME, ensure, mutate_action, panel_payload
from app.db import get_db, init_db


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    db = tmp_path / "branch.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    init_db(str(db))
    return str(db)


def _claim(conn, who, branch_no="A", flow_id=1):
    return mutate_action(conn, {
        "action": "claim", "flow_id": flow_id, "branch_no": branch_no, "who": who,
    })


def test_claim_twice_same_branch_only_one_owner(db_env):
    """同支路两人顺序抢领：只留一枚号，后领者被拒，先领者不被盖掉。"""
    with get_db() as conn:
        first = _claim(conn, "u1")
        second = _claim(conn, "u2")
    assert first["ok"] is True
    assert second["ok"] is False
    assert "已被领取" in second["error"]
    assert second["owner"] == "u1"
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM branches WHERE flow_id=1 AND branch_no='A'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["owner"] == "u1"


def test_concurrent_claim_queues_single_winner(db_env):
    """两边同时下手须排队发号：只有一人成功，库里只有一枚号且无覆盖。"""
    results = []
    barrier = threading.Barrier(2)

    def claim(who):
        with get_db() as conn:
            barrier.wait()
            results.append((who, _claim(conn, who)))

    t1 = threading.Thread(target=claim, args=("u1",))
    t2 = threading.Thread(target=claim, args=("u2",))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    winners = [(who, out) for who, out in results if out["ok"]]
    losers = [out for _who, out in results if not out["ok"]]
    assert len(winners) == 1
    assert len(losers) == 1
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM branches WHERE flow_id=1 AND branch_no='A'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["owner"] == winners[0][0]
    assert losers[0]["owner"] == winners[0][0]


def test_unique_index_is_hard_database_constraint(db_env):
    """唯一索引兜底：绕过业务层直插两条同支路记录必须被数据库拒绝。"""
    with get_db() as conn:
        ensure(conn)
        conn.execute(
            "INSERT INTO branches(flow_id, branch_no, owner) VALUES (1,'A','u1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO branches(flow_id, branch_no, owner) VALUES (1,'A','u2')"
            )


def test_merge_rejected_until_all_branches_claimed(db_env):
    """未领齐禁止拼段：后端强校验，而非仅前端禁用。"""
    with get_db() as conn:
        blocked = mutate_action(conn, {"action": "merge", "flow_id": 1})
        assert blocked["ok"] is False
        assert "未领齐" in blocked["error"]
        assert set(blocked["missing"]) == set(BRANCH_PLAN)

        # 只领两条仍禁止
        assert _claim(conn, "u1", "A")["ok"]
        assert _claim(conn, "u2", "B")["ok"]
        blocked2 = mutate_action(conn, {"action": "merge", "flow_id": 1})
        assert blocked2["ok"] is False
        assert blocked2["missing"] == ["C"]

        panel = panel_payload(conn, {"flow_id": 1})
        assert panel["all_claimed"] is False
        assert panel["can_merge"] is False

        # 领齐后允许拼段，且不可重复拼
        assert _claim(conn, "u3", "C")["ok"]
        panel = panel_payload(conn, {"flow_id": 1})
        assert panel["all_claimed"] is True
        assert panel["can_merge"] is True
        assert mutate_action(conn, {"action": "merge", "flow_id": 1})["ok"] is True
        again = mutate_action(conn, {"action": "merge", "flow_id": 1})
        assert again["ok"] is False


def test_rollback_clears_all_segments_and_resets_progress(db_env):
    """整回滚：原子清掉全部已抽段（含已确认段），列表进度归零且计划行仍在。"""
    with get_db() as conn:
        for branch_no in BRANCH_PLAN:
            assert _claim(conn, "u1", branch_no)["ok"]
        assert mutate_action(conn, {"action": "merge", "flow_id": 1})["ok"] is True
        landed = conn.execute("SELECT landed FROM flows WHERE id=1").fetchone()["landed"]
        assert landed > 0

    with get_db() as conn:
        out = mutate_action(conn, {"action": "rollback", "flow_id": 1})
        assert out["ok"] is True
        assert out["strategy"] == STRATEGY_NAME

        owned = conn.execute(
            "SELECT COUNT(*) c FROM branches WHERE flow_id=1 AND owner IS NOT NULL"
        ).fetchone()["c"]
        assert owned == 0
        confirmed = conn.execute(
            "SELECT COUNT(*) c FROM branches WHERE flow_id=1 AND ok=1"
        ).fetchone()["c"]
        assert confirmed == 0

        panel = panel_payload(conn, {"flow_id": 1})
        assert panel["claimed"] == 0
        assert panel["confirmed"] == 0
        assert panel["missing"] == list(BRANCH_PLAN)
        assert [r["branch_no"] for r in panel["items"]] == list(BRANCH_PLAN)
        assert panel["can_merge"] is False

        flow = dict(conn.execute(
            "SELECT status, landed FROM flows WHERE id=1"
        ).fetchone())
        assert flow["status"] == "已抽取待落库"
        assert flow["landed"] == 0


def test_rollback_is_scoped_to_one_flow(db_env):
    """整回滚只清本任务已抽段，其他任务的领段不受影响。"""
    with get_db() as conn:
        assert _claim(conn, "u1", "A", flow_id=1)["ok"]
        assert _claim(conn, "u2", "A", flow_id=2)["ok"]
        assert mutate_action(conn, {"action": "rollback", "flow_id": 1})["ok"]
        rows = conn.execute(
            "SELECT flow_id, owner FROM branches WHERE branch_no='A' AND owner IS NOT NULL ORDER BY flow_id"
        ).fetchall()
    kept = [dict(r) for r in rows]
    assert len(kept) == 1
    assert kept[0] == {"flow_id": 2, "owner": "u2"}


def test_panel_exposes_single_consistent_strategy(db_env):
    """收口策略口径唯一且透出到面板，页面可见，不允许漂移混用。"""
    with get_db() as conn:
        panel = panel_payload(conn, {"flow_id": 1})
    assert panel["strategy_name"] == "整回滚"
    assert "清掉" in panel["strategy_desc"]
    assert panel["list_label"] == "领段列表"
    assert panel["plan"] == list(BRANCH_PLAN)


# ---------- HTTP 层：状态码与端到端口径 ----------

def _login(client):
    _req, resp = client.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    )
    assert resp.status == 200
    cookie = resp.cookies.get("flow_session")
    return {"Cookie": f"flow_session={cookie}"}


def test_api_conflict_returns_409_and_strategy_visible(client):
    headers = _login(client)
    _req, ok = client.post("/api/branch/action", headers=headers, json={
        "action": "claim", "flow_id": 1, "branch_no": "A", "who": "u1",
    })
    assert ok.status == 200
    _req, conflict = client.post("/api/branch/action", headers=headers, json={
        "action": "claim", "flow_id": 1, "branch_no": "A", "who": "u2",
    })
    assert conflict.status == 409
    assert conflict.json["ok"] is False
    assert conflict.json["owner"] == "u1"

    _req, panel = client.get("/api/branch/panel?flow_id=1", headers=headers)
    assert panel.status == 200
    body = panel.json
    assert body["strategy_name"] == "整回滚"
    assert body["all_claimed"] is False
    assert body["can_merge"] is False


def test_api_merge_409_when_incomplete(client):
    headers = _login(client)
    _req, resp = client.post("/api/branch/action", headers=headers, json={
        "action": "merge", "flow_id": 1,
    })
    assert resp.status == 409
    assert "未领齐" in resp.json["error"]


def test_api_full_flow_claim_merge_rollback(client):
    headers = _login(client)
    for branch_no in BRANCH_PLAN:
        _req, resp = client.post("/api/branch/action", headers=headers, json={
            "action": "claim", "flow_id": 1, "branch_no": branch_no, "who": "u1",
        })
        assert resp.status == 200
    _req, merged = client.post("/api/branch/action", headers=headers, json={
        "action": "merge", "flow_id": 1,
    })
    assert merged.status == 200
    _req, rolled = client.post("/api/branch/action", headers=headers, json={
        "action": "rollback", "flow_id": 1,
    })
    assert rolled.status == 200
    assert rolled.json["strategy"] == "整回滚"

    _req, panel = client.get("/api/branch/panel?flow_id=1", headers=headers)
    assert panel.json["claimed"] == 0
    assert panel.json["missing"] == list(BRANCH_PLAN)


def test_frontend_renders_strategy_and_guards(client):
    """收口策略在页面可见，拼段/领段按钮状态由后端口径驱动。"""
    _req, resp = client.get("/static/app.js")
    assert resp.status == 200
    text = resp.text
    assert "收口策略" in text
    assert "strategy_name" in text
    assert "can_merge" in text
