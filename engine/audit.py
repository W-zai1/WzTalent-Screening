"""
审计日志 —— 只追加、不可改、哈希链。

为什么必须要它
--------------
HR 场景的决策会被追问：
  · 这个人为什么被判 C？
  · 是谁把状态从「待定」改成「已淘汰」的？
  · 我们是否对某类候选人存在系统性偏差？

没有日志，这些问题全部无法回答。而"事后能编辑的日志"在争议场景里没有证明力，
所以这里是 **append-only + 哈希链**：

  hash(n) = SHA256( hash(n-1) || ts || actor || action || target || detail )

任何一条被改动或删除，后续所有 hash 都对不上，`verify_chain()` 会指出来。
这不是防"有数据库写权限的人"（那种人能重建整条链），
而是防"事后悄悄补一条/改一条"——成本被显著抬高，且普通误操作一定被发现。

PII 与日志
----------
detail 里**不写简历原文**，只写摘要与计数。审计日志本身不该成为第二份简历库。
"""
import hashlib
import json

import db

GENESIS = "0" * 64

# 需要留痕的动作（写全，方便查询时枚举）。
# 校验是**故意严格**的：写错动作名会在开发期立刻报错，
# 而不是悄悄产生一条搜不到的审计记录。
ACTIONS = (
    "auth.login", "auth.login_failed", "auth.logout",
    "job.create", "job.update", "job.delete",
    "jd.structure",
    "candidate.upload", "candidate.delete", "candidate.identity_link",
    "candidate.identity_confirm", "candidate.identity_split",
    "run.start", "run.cancel", "run.finish",
    "talent.update", "talent.import", "talent.delete",
    "outcome.record", "outcome.delete",
    "interview.generate",
    # 面试录音分析（P1）：归属一次运行（含保留了哪几条人工修正）、
    # 以及逐段人工修正。修正改的是"谁在说"——错了会污染整场分析，
    # 必须能查到是谁在什么时候把哪一段从什么改成了什么。
    "interview.diarize", "segment.correct",
    "calibration.record",
    # 回测对齐：把"HR 的真实决定"作为标准答案导入，以及基于它做的决策。
    # 单独登记动作，是为了出事时能一条查询捞全"谁在什么依据下推/拒了谁"。
    "align.create", "align.import", "align.delete",
    "candidate.decide",
    # 备份/恢复：恢复是破坏性操作，必须能被事后追查
    # （"谁在什么时候用哪个文件覆盖了生产库"）。
    "backup.create", "backup.restore",
    "align.bind",
    "privacy.export", "privacy.erase", "privacy.purge", "privacy.rotate_key",
    "user.create", "user.disable", "user.password",
    "model.config",
)


def _canon(obj):
    """键序稳定的 JSON，保证同一内容 hash 一致。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _last_hash(conn):
    row = conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
    return row["hash"] if row else GENESIS


def record(conn, action, actor="system", target_type="", target_id="",
           detail=None, ts=None):
    """追加一条审计记录，返回记录行。

    连接由调用方提供，以便与业务写入落在同一个事务里——
    否则会出现"数据写进去了但日志没写"的空窗。
    """
    if action not in ACTIONS:
        # 不静默通过：动作名写错会让后续审计查询漏掉整类事件
        raise ValueError("未登记的审计动作：%s" % action)

    prev = _last_hash(conn)
    ts = ts or db.now_iso()
    payload = {
        "ts": ts,
        "actor": actor or "system",
        "action": action,
        "target_type": target_type or "",
        "target_id": target_id or "",
        "detail": detail or {},
    }
    h = hashlib.sha256((prev + _canon(payload)).encode("utf-8")).hexdigest()

    cur = conn.execute(
        "INSERT INTO audit(ts, actor, action, target_type, target_id, detail_json, prev_hash, hash)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (ts, payload["actor"], action, payload["target_type"], payload["target_id"],
         _canon(payload["detail"]), prev, h),
    )
    return {"seq": cur.lastrowid, "hash": h, **payload}


def query(conn, limit=200, action=None, target_type=None, target_id=None,
          actor=None, since=None):
    sql = "SELECT * FROM audit WHERE 1=1"
    args = []
    if action:
        sql += " AND action = ?"
        args.append(action)
    if target_type:
        sql += " AND target_type = ?"
        args.append(target_type)
    if target_id:
        sql += " AND target_id = ?"
        args.append(target_id)
    if actor:
        sql += " AND actor = ?"
        args.append(actor)
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(max(1, min(int(limit or 200), 2000)))

    rows = []
    for r in conn.execute(sql, args):
        d = dict(r)
        try:
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
        except Exception:
            d["detail"] = {}
        rows.append(d)
    return rows


def verify_chain(conn, limit=None):
    """校验哈希链完整性。

    返回 {"ok": bool, "checked": n, "broken_at": seq|None, "error": str|None}
    """
    sql = "SELECT seq, ts, actor, action, target_type, target_id, detail_json, prev_hash, hash" \
          " FROM audit ORDER BY seq ASC"
    prev = GENESIS
    checked = 0
    for r in conn.execute(sql):
        payload = {
            "ts": r["ts"],
            "actor": r["actor"],
            "action": r["action"],
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "detail": json.loads(r["detail_json"] or "{}"),
        }
        expect = hashlib.sha256((prev + _canon(payload)).encode("utf-8")).hexdigest()
        if r["prev_hash"] != prev:
            return {"ok": False, "checked": checked, "broken_at": r["seq"],
                    "error": "第 %d 条的 prev_hash 与上一条不衔接" % r["seq"]}
        if r["hash"] != expect:
            return {"ok": False, "checked": checked, "broken_at": r["seq"],
                    "error": "第 %d 条内容与哈希不符（记录可能被修改）" % r["seq"]}
        prev = r["hash"]
        checked += 1
    return {"ok": True, "checked": checked, "broken_at": None, "error": None}


def stats(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last FROM audit"
    ).fetchone()
    return {"count": row["n"], "first": row["first"], "last": row["last"]}
