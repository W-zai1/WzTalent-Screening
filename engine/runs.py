"""
评审运行管理（互斥、取消、事件重放）。

修的两个坑
----------
1. **重复点击 = 双倍烧钱**：原实现每次 POST /run 都新建一条队列 + 起一个线程，
   只靠前端 `state.running` 布尔遮挡。刷新页面/开两个标签页就能绕过，
   两条流水线的事件混进同一个直播间，积分花两倍，结果互相覆盖。
   → 现在用 `runs` 表 + 部分唯一索引 `WHERE status='running'` 做**数据库级互斥**，
     第二个请求拿到 409，而不是悄悄再跑一遍。

2. **刷新页面就丢事件**：SSE 只推给"当时连着的那一条连接"，
   关掉标签页再打开，辩论过程就没了。
   → 现在每个事件都落 `run_events`，SSE 支持 `Last-Event-ID` 重放，
     重连即从断点续上。事件本身不加密（不含简历正文，只有 agent 发言与进度）。

取消
----
合作式取消：`cancel_requested` 置位后，worker 在每个候选人边界检查并停止。
不强行 kill 线程（Python 线程无法安全强杀，且会把"已花掉的调用"变成脏数据）。
已完成的候选人保留结果，未开始的置回 pending。
"""
import json
import queue
import secrets
import threading

import audit
import db
import events

# 进程内事件广播（跨线程唤醒 SSE 连接）；持久化在 run_events 里
_subscribers = {}
_sub_lock = threading.Lock()

STATUS = ("running", "done", "cancelled", "failed")


class RunBusy(RuntimeError):
    """该岗位已有评审在跑。"""


def _new_id():
    return "run_" + secrets.token_hex(6)


# ---------------------------------------------------------------------------
# 订阅（SSE 用）
# ---------------------------------------------------------------------------
def subscribe(run_id):
    """为一条 SSE 连接开一个队列。"""
    q = queue.Queue()
    with _sub_lock:
        _subscribers.setdefault(run_id, []).append(q)
    return q


def unsubscribe(run_id, q):
    with _sub_lock:
        lst = _subscribers.get(run_id)
        if lst and q in lst:
            lst.remove(q)
        if lst is not None and not lst:
            _subscribers.pop(run_id, None)
    return True


def _broadcast(run_id, event):
    with _sub_lock:
        for q in list(_subscribers.get(run_id) or []):
            try:
                q.put_nowait(event)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
def start(job_id, candidate_ids, actor="system", rubric_version="", job_title=""):
    """登记一次运行。已有 running 的岗位直接抛 RunBusy（数据库唯一索引兜底）。"""
    db.init()
    conn = db.connect()
    run_id = _new_id()
    now = db.now_iso()

    # 先清理"进程崩了但状态还挂着 running"的僵尸运行，否则岗位会被永久锁死
    _reap_stale(conn, job_id)

    try:
        with db.tx(conn) as c:
            c.execute(
                "INSERT INTO runs(id, job_id, status, candidate_ids_json, started_by,"
                " started_at, total, rubric_version, job_title)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, job_id, "running", db.jdump(list(candidate_ids)), actor, now,
                 len(candidate_ids or []), rubric_version, job_title or ""),
            )
            audit.record(c, "run.start", actor=actor, target_type="job", target_id=job_id,
                         detail={"run_id": run_id, "candidates": len(candidate_ids or []),
                                 "rubric_version": rubric_version})
    except Exception as e:
        if "UNIQUE" in str(e).upper() or "constraint" in str(e).lower():
            raise RunBusy("该岗位已有评审正在进行，请等待完成或先取消。")
        raise
    return get_run(run_id)


def _reap_stale(conn, job_id):
    """把超时未动的 running 运行标为 failed（进程被杀/断电留下的）。"""
    rows = conn.execute(
        "SELECT id FROM runs WHERE job_id=? AND status='running'", (job_id,)).fetchall()
    for r in rows:
        last = conn.execute(
            "SELECT MAX(ts) AS t FROM run_events WHERE run_id=?", (r["id"],)).fetchone()["t"]
        from datetime import datetime, timedelta
        try:
            ref = last or conn.execute(
                "SELECT started_at AS t FROM runs WHERE id=?", (r["id"],)).fetchone()["t"]
            age = datetime.now() - datetime.fromisoformat(ref)
        except Exception:
            age = timedelta(0)
        if age > timedelta(hours=2):
            with db.tx(conn) as c:
                c.execute("UPDATE runs SET status='failed', finished_at=? WHERE id=?",
                          (db.now_iso(), r["id"]))
    return True


def reap_stale(job_id=None):
    """清理僵尸运行。

    进程被杀/断电时 `runs.status` 会永远停在 running，而部分唯一索引
    `WHERE status='running'` 会让该岗位**永久无法再启动评审**。
    所以启动时无条件扫一遍（不传 job_id 即全库），而不只是在 start() 里按岗位清。
    """
    conn = db.connect()
    if job_id:
        return _reap_stale(conn, job_id)
    job_ids = [r["job_id"] for r in conn.execute(
        "SELECT DISTINCT job_id FROM runs WHERE status='running'").fetchall()]
    for jid in job_ids:
        _reap_stale(conn, jid)
    return True


def get_run(run_id):
    conn = db.connect()
    r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _run_dict(r) if r else None


def _run_dict(r):
    if r is None:
        return None
    return {
        "id": r["id"], "job_id": r["job_id"], "status": r["status"],
        "candidate_ids": db.jload(r["candidate_ids_json"], []),
        "started_by": r["started_by"], "started_at": r["started_at"],
        "finished_at": r["finished_at"],
        "total": r["total"], "succeeded": r["succeeded"], "failed": r["failed"],
        "cancel_requested": bool(r["cancel_requested"]),
        "rubric_version": r["rubric_version"], "job_title": r["job_title"],
    }


def active_run(job_id):
    conn = db.connect()
    r = conn.execute("SELECT * FROM runs WHERE job_id=? AND status='running'"
                     " ORDER BY started_at DESC LIMIT 1", (job_id,)).fetchone()
    return _run_dict(r) if r else None


def recent_runs(job_id=None, limit=20):
    conn = db.connect()
    if job_id:
        rows = conn.execute("SELECT * FROM runs WHERE job_id=? ORDER BY started_at DESC LIMIT ?",
                            (job_id, int(limit))).fetchall()
    else:
        rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
                            (int(limit),)).fetchall()
    return [_run_dict(r) for r in rows]


def finish(run_id, status="done", succeeded=0, failed=0, actor="system"):
    conn = db.connect()
    run = get_run(run_id)
    with db.tx(conn) as c:
        c.execute("UPDATE runs SET status=?, finished_at=?, succeeded=?, failed=? WHERE id=?",
                  (status, db.now_iso(), int(succeeded), int(failed), run_id))
        audit.record(c, "run.finish", actor=actor,
                     target_type="job", target_id=(run or {}).get("job_id", ""),
                     detail={"run_id": run_id, "status": status,
                             "succeeded": succeeded, "failed": failed})
    _broadcast(run_id, {"type": "__end__", "run_id": run_id, "status": status})
    # 叫醒所有 SSE 连接，让它们收尾退出
    with _sub_lock:
        for q in list(_subscribers.get(run_id) or []):
            try:
                q.put_nowait(None)
            except Exception:
                pass
    return get_run(run_id)


def request_cancel(run_id, actor="system"):
    conn = db.connect()
    run = get_run(run_id)
    if not run or run["status"] != "running":
        return False
    with db.tx(conn) as c:
        c.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
        audit.record(c, "run.cancel", actor=actor, target_type="job",
                     target_id=run["job_id"], detail={"run_id": run_id})
    _broadcast(run_id, {"type": "cancelling", "run_id": run_id})
    return True


def cancel_requested(run_id):
    conn = db.connect()
    r = conn.execute("SELECT cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()
    return bool(r and r["cancel_requested"])


# ---------------------------------------------------------------------------
# 事件（持久化 + 广播）
# ---------------------------------------------------------------------------
def emit(run_id, event):
    """记录一个事件并广播给在线连接。返回事件序号。

    `ephemeral=True` 的事件**只广播、不落库**，返回 0。用于流式增量：
    一次 800 字的立论如果逐字落库就是 800 行，200 人 × 10 次调用会到百万行级别，
    而断点续传根本不需要逐字重放——重连时拿到最终的 agent_message 全文就够了。
    所以增量走内存广播，全文仍按普通事件落库。

    并发安全：`seq` 是"该 run 内第几条事件"，靠 `MAX(seq)+1` 算出。
    原来 SELECT 在事务外，两个线程可能读到同一个 MAX 再去插入，
    撞上 UNIQUE(run_id, seq) 直接抛 IntegrityError，
    表现是"并发跑候选人时偶发事件丢失/报错"。
    现在把 SELECT 和 INSERT 放进同一个 BEGIN IMMEDIATE 事务里，
    SQLite 的写锁保证两者原子——拿到写锁之后读到的 MAX 就是最终值。
    """
    if event.get("ephemeral"):
        return emit_delta(run_id, event)

    # 归一化字段（stage / duration_ms / error_type）在落库前补上：
    # 这样断点重放出来的事件也带这些字段，而不是只有实时广播的那份才有。
    # 只在内存里记账，不额外写库（见 events.py 的说明）。
    event = events.annotate(run_id, event)

    conn = db.connect()
    with db.tx(conn) as c:
        seq = c.execute(
            "SELECT IFNULL(MAX(seq),0)+1 AS s FROM run_events WHERE run_id=?", (run_id,)
        ).fetchone()["s"]
        payload = dict(event)
        payload["seq"] = seq
        payload["run_id"] = run_id
        c.execute("INSERT INTO run_events(run_id, seq, ts, payload_json) VALUES (?,?,?,?)",
                  (run_id, seq, db.now_iso(), db.jdump(payload)))
    _broadcast(run_id, payload)
    return seq


def emit_delta(run_id, event):
    """只广播、不落库（流式增量）。返回 0 表示"没有占用事件序号"。

    没有 seq 是有意的：前端拿 seq 做断点续传游标，增量不该推进它，
    否则重连时会以为"已经读到 N 了"而跳过真正需要补发的持久事件。
    """
    payload = dict(event)
    payload["seq"] = 0
    payload["run_id"] = run_id
    payload["ephemeral"] = True
    _broadcast(run_id, payload)
    return 0


def events_since(run_id, after_seq=0, limit=500):
    """取断点之后的事件（SSE 重放）。"""
    conn = db.connect()
    rows = conn.execute(
        "SELECT seq, payload_json FROM run_events WHERE run_id=? AND seq>? ORDER BY seq ASC"
        " LIMIT ?", (run_id, int(after_seq or 0), int(limit))
    ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload_json"]))
        except Exception:
            continue
    return out


def event_count(run_id):
    conn = db.connect()
    return conn.execute("SELECT COUNT(*) AS n FROM run_events WHERE run_id=?",
                        (run_id,)).fetchone()["n"]
