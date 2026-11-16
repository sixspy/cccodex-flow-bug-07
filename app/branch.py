from __future__ import annotations

def ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS branches (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      flow_id INTEGER NOT NULL,
      branch_no TEXT NOT NULL,
      owner TEXT,
      ok INTEGER NOT NULL DEFAULT 0,
      chunks INTEGER NOT NULL DEFAULT 0
    )""")

def panel_payload(conn, args):
    ensure(conn)
    items = [dict(r) for r in conn.execute("SELECT * FROM branches")]
    return {"items": items, "strategy_name": "整回滚", "list_label": "领段列表"}

def mutate_action(conn, body):
    ensure(conn)
    action = body.get("action") or "claim"
    flow_id = int(body.get("flow_id") or 1)
    branch_no = str(body.get("branch_no") or "A")
    who = str(body.get("who") or "u1")
    if action == "claim":
        conn.execute(
            "INSERT INTO branches(flow_id, branch_no, owner, ok, chunks) VALUES (?,?,?,1,3)",
            (flow_id, branch_no, who),
        )
        return {"ok": True, "owner": who}
    if action == "merge":
        conn.execute(
            "UPDATE flows SET status=?, landed=landed+10 WHERE id=?",
            ("已落库", flow_id),
        )
        return {"ok": True, "merged": True}
    if action == "rollback":
        conn.execute("UPDATE branches SET owner=NULL WHERE flow_id=?", (flow_id,))
        return {"ok": True, "rolled": True}
    return {"ok": False, "error": "未知"}
