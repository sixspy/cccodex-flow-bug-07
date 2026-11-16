from __future__ import annotations

# 每个编排流固定切出的支路（段）计划
BRANCH_PLAN = ("A", "B", "C")
# 收口失败口径：整回滚——单事务原子清掉该任务全部已抽段，不保留残留、不混用保留策略
STRATEGY_NAME = "整回滚"
STRATEGY_DESC = "收口失败时在同一事务内清掉该任务全部已抽段，回滚后领段列表进度归零，不留残留段"
LIST_LABEL = "领段列表"


def ensure(conn):
    """建表并补齐唯一约束。唯一索引是同支路只留一枚号的最终防线，前端禁用不算数。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS branches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          flow_id INTEGER NOT NULL,
          branch_no TEXT NOT NULL,
          owner TEXT,
          ok INTEGER NOT NULL DEFAULT 0,
          chunks INTEGER NOT NULL DEFAULT 0
        )"""
    )
    # 旧库可能没有约束：先按 (flow_id, branch_no) 去重，再建唯一索引
    conn.execute(
        """DELETE FROM branches
           WHERE id NOT IN (
             SELECT MIN(id) FROM branches GROUP BY flow_id, branch_no
           )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_branches_flow_branch
           ON branches(flow_id, branch_no)"""
    )


def _flow_id(args):
    raw = args.get("flow_id")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw in (None, ""):
        return None
    return int(raw)


def _ensure_plan(conn, flow_id):
    """保证该流的 A/B/C 支路行都在；owner 为空即未领。"""
    for branch_no in BRANCH_PLAN:
        conn.execute(
            "INSERT OR IGNORE INTO branches(flow_id, branch_no, owner, ok, chunks) VALUES (?,?,NULL,0,0)",
            (flow_id, branch_no),
        )


def _branch_rows(conn, flow_id):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM branches WHERE flow_id=? ORDER BY branch_no",
            (flow_id,),
        )
    ]


def panel_payload(conn, args):
    ensure(conn)
    flow_id = _flow_id(args)
    if flow_id is None:
        # 未指定流时给全量列表（保持面板可打开），不计算可拼段状态
        items = [dict(r) for r in conn.execute("SELECT * FROM branches ORDER BY flow_id, branch_no")]
        return {
            "items": items,
            "strategy_name": STRATEGY_NAME,
            "strategy_desc": STRATEGY_DESC,
            "list_label": LIST_LABEL,
            "plan": list(BRANCH_PLAN),
        }

    _ensure_plan(conn, flow_id)
    items = _branch_rows(conn, flow_id)
    claimed = [r for r in items if r["owner"]]
    confirmed = [r for r in items if r["ok"]]
    missing = [r["branch_no"] for r in items if not r["owner"]]
    all_claimed = not missing and len(items) == len(BRANCH_PLAN)
    return {
        "flow_id": flow_id,
        "items": items,
        "strategy_name": STRATEGY_NAME,
        "strategy_desc": STRATEGY_DESC,
        "list_label": LIST_LABEL,
        "plan": list(BRANCH_PLAN),
        "total": len(BRANCH_PLAN),
        "claimed": len(claimed),
        "confirmed": len(confirmed),
        "missing": missing,
        "all_claimed": all_claimed,
        # 已确认（拼过段）后不允许重复拼段
        "can_merge": all_claimed and not confirmed,
        "merged": bool(confirmed),
    }


def _conflict(error, **extra):
    out = {"ok": False, "error": error}
    out.update(extra)
    return out


def mutate_action(conn, body):
    ensure(conn)
    action = str(body.get("action") or "claim")
    try:
        flow_id = int(body.get("flow_id") or 1)
    except (TypeError, ValueError):
        return _conflict("flow_id 非法")
    branch_no = str(body.get("branch_no") or "A")
    who = str(body.get("who") or "").strip()

    if action == "claim":
        if branch_no not in BRANCH_PLAN:
            return _conflict("支路不存在", branch_no=branch_no)
        if not who:
            return _conflict("领段工号不能为空")
        # BEGIN IMMEDIATE 立即拿写锁：并发抢领在此排队，杜绝两人同时读到“无人领”
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_plan(conn, flow_id)
            row = conn.execute(
                "SELECT owner FROM branches WHERE flow_id=? AND branch_no=?",
                (flow_id, branch_no),
            ).fetchone()
            if row is not None and row["owner"]:
                conn.execute("ROLLBACK")
                return _conflict("该支路已被领取", owner=row["owner"], branch_no=branch_no)
            # 计划行已存在（owner 为 NULL）：原子 UPDATE 占位，
            # 并发事务排队后只有一个 UPDATE 命中行，另一个 rowcount=0 让位
            cur = conn.execute(
                "UPDATE branches SET owner=?, ok=0, chunks=3 "
                "WHERE flow_id=? AND branch_no=? AND owner IS NULL",
                (who, flow_id, branch_no),
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                holder = conn.execute(
                    "SELECT owner FROM branches WHERE flow_id=? AND branch_no=?",
                    (flow_id, branch_no),
                ).fetchone()
                return _conflict(
                    "该支路已被领取",
                    owner=holder["owner"] if holder else None,
                    branch_no=branch_no,
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"ok": True, "owner": who, "branch_no": branch_no, "flow_id": flow_id}

    if action == "merge":
        # 拼段落库：未领齐一律后端拒绝，前端按钮禁用只是体验层
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_plan(conn, flow_id)
            rows = _branch_rows(conn, flow_id)
            missing = [r["branch_no"] for r in rows if not r["owner"]]
            if missing:
                conn.execute("ROLLBACK")
                return _conflict("支路未领齐，禁止拼段", missing=missing)
            if any(r["ok"] for r in rows):
                conn.execute("ROLLBACK")
                return _conflict("该任务已拼段落库，请勿重复操作")
            conn.execute(
                "UPDATE branches SET ok=1 WHERE flow_id=? AND ok=0",
                (flow_id,),
            )
            conn.execute(
                "UPDATE flows SET status=?, landed=landed+10 WHERE id=?",
                ("已落库", flow_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"ok": True, "merged": True, "flow_id": flow_id}

    if action == "rollback":
        # 整回滚口径：单事务原子清掉该任务全部已抽段（含已确认段），不留残留
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_plan(conn, flow_id)
            cur = conn.execute("DELETE FROM branches WHERE flow_id=?", (flow_id,))
            cleared = cur.rowcount
            flow = conn.execute(
                "SELECT status FROM flows WHERE id=?", (flow_id,)
            ).fetchone()
            if flow is not None and flow["status"] == "已落库":
                conn.execute(
                    "UPDATE flows SET status=?, landed=0 WHERE id=?",
                    ("已抽取待落库", flow_id),
                )
            # 清完后重建未领计划行，保证领段列表进度与回滚后的状态一致
            _ensure_plan(conn, flow_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"ok": True, "rolled": True, "cleared": cleared, "strategy": STRATEGY_NAME}

    return _conflict("未知操作")
