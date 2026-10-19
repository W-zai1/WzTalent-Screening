"""
岗位与候选人仓储层（SQLite）。

对外保持与原文件存储版本**相同的数据形状**（dict/list），
所以 agents / run / 前端不需要跟着改；内部从 JSON 文件换成 SQLite。

三个关键改动
------------
1. **PII 加密**：resume/assessment/transcript 三类正文落库前加密（crypto.py），
   读出后解密。库里永远是密文。
2. **身份消歧**：候选人不再"同名即合并"。上传时按 identity.py 多信号比对，
   只有 verdict=same 才自动挂到同一个 person；uncertain 会标记出来等人工确认
   （`person_decision='pending'`），界面上必须给出选择。
3. **审计**：所有写操作同时落 audit（同一事务，不会出现"数据写了日志没写"）。

保留期
------
每个岗位有 `retention_days`（默认 180）。候选人入库时算出 `retention_until`，
到达后可被 `privacy.purge_expired()` 清理——"存多久"从口号变成可计算的字段。
"""
import json
import os
import secrets
import shutil
from datetime import datetime, timedelta

import audit
import crypto
import db
import identity
from util import DIMENSIONS, normalize_weights, safe_filename

JOBS = "jobs"
CAND_STATUSES = ("pending", "running", "done", "failed")
JOB_STATUSES = ("draft", "collecting", "screening", "done")
DEFAULT_RETENTION_DAYS = 180

# update_candidate() 接受的字段。**内部调用方**（jobs_runner / runs /
# confirm_identity / align.decide）直接用它，可以写状态机字段。
_CAND_WRITABLE = ("name", "status", "error", "result", "interview",
                  "resume_text", "assessment_text", "transcript_text",
                  "rubric_version", "person_id", "identity", "person_decision")

# 仅通过 `PUT /api/candidates/:cid` 暴露的字段。**故意不含状态机字段**
# （status / result / rubric_version / person_id / identity / person_decision）：
#   · 这些字段各有专用端点（评审由 jobs_runner 写、决策走 /decide、
#     身份走 /identity），专用端点才带参数校验、状态迁移检查和审计；
#   · 通用 PATCH 能写它们，等于开了一条绕过状态机的后门——
#     一条 PUT 就能把 running 改成 done，审计里却看不出发生了什么；
#   · 收窄它不影响任何内部流程，因为内部调用方走的是 _CAND_WRITABLE。
CAND_HTTP_PATCHABLE = ("name", "error", "interview",
                       "resume_text", "assessment_text", "transcript_text")


def _new_id(prefix):
    return "%s_%s" % (prefix, secrets.token_hex(5))


def project_root():
    return db.PROJECT_ROOT


def _now():
    return db.now_iso()


def _retention_until(days):
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = DEFAULT_RETENTION_DAYS
    if d <= 0:
        return ""
    return (datetime.now() + timedelta(days=d)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 加密小工具（读写出错时不静默返回空——那会变成"材料消失"）
# ---------------------------------------------------------------------------
def _enc(v):
    return crypto.encrypt_text(v or "")


def _dec(v):
    if not v:
        return ""
    try:
        return crypto.decrypt_text(v)
    except crypto.PIICryptoError as e:
        # 解不开就如实标记，不要当成"没有材料"——那会让评审在空上下文上跑
        raise crypto.PIICryptoError(
            "候选人材料解密失败（%s）。通常是 PII 密钥被更换或文件被篡改；"
            "请恢复 data/.secret_key 或重新上传材料。" % e
        )


# ---------------------------------------------------------------------------
# 岗位
# ---------------------------------------------------------------------------
def list_jobs():
    """岗位列表（含统计）。一条 SQL 聚合，不再逐个岗位读文件。"""
    db.init()
    conn = db.connect()
    rows = conn.execute("""
        SELECT j.*,
               (SELECT COUNT(*) FROM candidates c WHERE c.job_id = j.id) AS cand_count,
               (SELECT COUNT(*) FROM candidates c WHERE c.job_id = j.id AND c.status='done') AS done_count
        FROM jobs j
        ORDER BY j.created_at DESC
    """).fetchall()
    out = []
    for r in rows:
        jd = db.jload(r["jd_json"], {})
        out.append({
            "id": r["id"],
            "title": r["title"] or "未命名岗位",
            "status": r["status"] or "draft",
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "candidate_count": r["cand_count"],
            "scored_count": r["done_count"],
            "requirement_count": len(jd.get("requirements") or []),
            "retention_days": r["retention_days"],
        })
    return out


def get_job(job_id):
    db.init()
    conn = db.connect()
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        return None
    jd = db.jload(r["jd_json"], {})
    weights = normalize_weights(jd.get("weights"))
    # 评审细则存在 jd_json 里（不需要新表）。旧岗位没有这份数据，
    # 用 base_weights 兜底，保证"显示的权重"就是"算分用的权重"。
    import rubric as _rubric
    rubric_cfg = _rubric.normalize_config(jd.get("rubric"), base_weights=weights)
    # 附一个人类可读的预设名，省得前端再维护一份映射表
    rubric_cfg["preset_label"] = _rubric.PRESETS[rubric_cfg["preset"]]["label"]
    rubric_cfg["preset_why"] = _rubric.PRESETS[rubric_cfg["preset"]].get("why", "")
    # jd 里也放一份**规范化后**的：前端岗位卡片读的是 jd.rubric。
    # 不这么做会出现两种说法不一致——界面上写"评委将按通用描述打分"，
    # 而实际提示词里塞的是默认预设的细则（normalize_config 会用预设补齐）。
    # 要么两边都承认用的是预设，要么就别用；现在是前者。
    jd["rubric"] = rubric_cfg
    return {
        "id": r["id"],
        "title": r["title"],
        "status": r["status"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "created_by": r["created_by"],
        "retention_days": r["retention_days"],
        "jd": jd,
        # 顶层也铺一份，历史代码（run.py/context.py）按扁平键读
        "requirements": jd.get("requirements") or [],
        "preferred": jd.get("preferred") or [],
        "red_flags": jd.get("red_flags") or [],
        "weights": weights,
        "rubric": rubric_cfg,
        "summary": jd.get("summary") or "",
    }


def create_job(title="未命名岗位", jd=None, status="draft", actor="system",
               retention_days=DEFAULT_RETENTION_DAYS):
    db.init()
    conn = db.connect()
    jid = _new_id("job")
    now = _now()
    # 允许直接把 JD 原文当字符串传进来（CLI/脚本里最常见的写法）。
    # 早期这里假定 jd 一定是 dict，传字符串会在 get_job 里崩成
    # AttributeError: 'str' object has no attribute 'get'，
    # 报错点离真正的错误很远，很难查。
    if isinstance(jd, str):
        jd = {"raw_text": jd} if jd.strip() else {}
    jd = jd or {}
    if not isinstance(jd, dict):
        raise ValueError("jd 必须是 dict 或字符串，收到 %r" % type(jd).__name__)
    if "weights" in jd:
        jd["weights"] = normalize_weights(jd["weights"])
    with db.tx(conn) as c:
        c.execute(
            "INSERT INTO jobs(id, title, status, jd_json, created_at, updated_at,"
            " created_by, retention_days) VALUES (?,?,?,?,?,?,?,?)",
            (jid, (title or "未命名岗位").strip()[:120], status, db.jdump(jd),
             now, now, actor, int(retention_days or DEFAULT_RETENTION_DAYS)),
        )
        audit.record(c, "job.create", actor=actor, target_type="job", target_id=jid,
                     detail={"title": title})
    return get_job(jid)


def update_job(job_id, patch, actor="system"):
    db.init()
    conn = db.connect()
    cur = get_job(job_id)
    if not cur:
        raise KeyError("岗位不存在：%s" % job_id)

    title = patch.get("title", cur["title"])
    status = patch.get("status", cur["status"])
    retention = patch.get("retention_days", cur["retention_days"])
    jd = dict(cur["jd"])
    if isinstance(patch.get("jd"), dict):
        jd.update(patch["jd"])
        if "weights" in patch["jd"]:
            jd["weights"] = normalize_weights(patch["jd"]["weights"])
    for key in ("requirements", "preferred", "red_flags", "summary", "raw_text"):
        if key in patch:
            jd[key] = patch[key]
    if "weights" in patch:
        jd["weights"] = normalize_weights(patch["weights"])
    # 评审细则与权重必须同源：细则里带着权重，写细则时要把权重同步到
    # jd.weights，否则会出现"界面显示 45%"而算分用 40% 的双份真相。
    if isinstance(patch.get("rubric"), dict):
        import rubric as _rubric
        cfg = _rubric.normalize_config(patch["rubric"],
                                       base_weights=normalize_weights(jd.get("weights")))
        jd["rubric"] = cfg
        jd["weights"] = cfg["weights"]

    now = _now()
    with db.tx(conn) as c:
        c.execute(
            "UPDATE jobs SET title=?, status=?, jd_json=?, updated_at=?, retention_days=?"
            " WHERE id=?",
            ((title or "未命名岗位").strip()[:120], status, db.jdump(jd), now,
             int(retention or DEFAULT_RETENTION_DAYS), job_id),
        )
        audit.record(c, "job.update", actor=actor, target_type="job", target_id=job_id,
                     detail={"keys": sorted(patch.keys())})
    return get_job(job_id)


def delete_job(job_id, actor="system"):
    db.init()
    conn = db.connect()
    with db.tx(conn) as c:
        cur = c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        # 原始文件（可能在磁盘上）一并删掉：岗位没了，简历不该还留着
        d = job_dir(job_id)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        audit.record(c, "job.delete", actor=actor, target_type="job", target_id=job_id)
    return cur.rowcount > 0


def job_dir(job_id):
    return os.path.join(db.DATA_DIR, "jobs", job_id)


# ---------------------------------------------------------------------------
# 候选人
# ---------------------------------------------------------------------------
def _row_to_candidate(row, files=None, with_text=True):
    if row is None:
        return None
    d = {
        "id": row["id"],
        "job_id": row["job_id"],
        "person_id": row["person_id"],
        "name": row["name"],
        "identity": db.jload(row["identity_json"], {}),
        "identity_state": row["identity_state"],
        "person_decision": row["person_decision"] or "auto",
        "status": row["status"],
        "error": row["error"] or "",
        "result": db.jload(row["result_json"], {}) or None,
        "interview": db.jload(row["interview_json"], {}) or None,
        "rubric_version": row["rubric_version"] or "",
        "retention_until": row["retention_until"] or "",
        # 人工决策：谁、什么时候、基于什么理由推/拒。
        # 这三个字段之前没有序列化，前端拿不到，等于决策记录了也看不见。
        "decision": row["decision"] or "",
        "decision_reason": row["decision_reason"] or "",
        "decided_by": row["decided_by"] or "",
        "decided_at": row["decided_at"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "files": files if files is not None else [],
    }
    if with_text:
        d["resume_text"] = _dec(row["resume_enc"])
        d["assessment_text"] = _dec(row["assessment_enc"])
        d["transcript_text"] = _dec(row["transcript_enc"])
    return d


def _files_for(conn, cand_id):
    out = []
    for r in conn.execute(
            "SELECT * FROM candidate_files WHERE candidate_id=? ORDER BY id ASC", (cand_id,)):
        out.append({
            "filename": r["filename"],
            "stored": r["stored"],
            "role": r["role"],
            "kind": r["kind"],
            "chars": r["chars"],
            "warnings": db.jload(r["warnings_json"], []),
            "imported_from": bool(r["imported_from"]),
        })
    return out


def get_candidate(cand_id, with_text=True):
    db.init()
    conn = db.connect()
    row = conn.execute("SELECT * FROM candidates WHERE id=?", (cand_id,)).fetchone()
    return _row_to_candidate(row, _files_for(conn, cand_id) if row else [],
                             with_text=with_text)


def list_candidates(job_id, with_text=True):
    db.init()
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM candidates WHERE job_id=? ORDER BY created_at ASC, id ASC", (job_id,)
    ).fetchall()
    files = {}
    for f in conn.execute("SELECT * FROM candidate_files WHERE job_id=? ORDER BY id ASC", (job_id,)):
        files.setdefault(f["candidate_id"], []).append({
            "filename": f["filename"], "stored": f["stored"], "role": f["role"],
            "kind": f["kind"], "chars": f["chars"],
            "warnings": db.jload(f["warnings_json"], []),
            "imported_from": bool(f["imported_from"]),
        })
    return [_row_to_candidate(r, files.get(r["id"], []), with_text=with_text) for r in rows]


def get_job_detail(job_id):
    """岗位 + 候选人（前端一次性拿全，减少往返）。"""
    job = get_job(job_id)
    if not job:
        return None, []
    return job, list_candidates(job_id)


# ---------------------------------------------------------------------------
# 上传 / 新增候选人（含身份消歧）
# ---------------------------------------------------------------------------
def add_candidate(job_id, name, files=None, resume_text="", assessment_text="",
                  transcript_text="", actor="system", identity_override=None,
                  decide="auto", force_new_person=False):
    """新增候选人。

    身份处理顺序：
      1. 抽取身份信号（identity.extract_profile）
      2. 在该岗位已有候选人里比对
      3. verdict=same → 复用同一个 person_id
         verdict=different → 生成新 person
         verdict=uncertain → **仍然生成新 person**，但 `person_decision='pending'`，
                             前端必须让 HR 确认"是同一个人吗"
      4. force_new_person=True 时跳过比对（HR 明确说"这是另一个人"）
    """
    db.init()
    job = get_job(job_id)
    if not job:
        raise KeyError("岗位不存在：%s" % job_id)

    files = files or []
    # 素材合并（可能一个是 .docx 简历、一个是 .pdf 测评）
    segs = []          # 音频/转写稿带来的段级时间戳（说话人归属的地基）
    for f in files:
        role = f.get("role") or "resume"
        txt = f.get("text") or ""
        if not txt:
            continue
        if role == "assessment":
            assessment_text = (assessment_text + "\n\n" + txt).strip()
        elif role == "transcript":
            transcript_text = (transcript_text + "\n\n" + txt).strip()
            # 粘贴的转写稿没有段；音频经 ASR 才有。两类可以同批，
            # 段只覆盖音频部分——归属时整体重切，见 diarization。
            segs.extend(f.get("segments") or [])
        else:
            resume_text = (resume_text + "\n\n" + txt).strip()

    prof = identity_override or identity.extract_profile(name, resume_text, assessment_text)
    decision = {"verdict": "new", "score": None, "matched": [], "conflicts": [],
                "signals_compared": 0, "reasons": ["新建档案（未做比对）"]}
    person_id = None
    identity_state = "auto"
    suggestion = None

    if not force_new_person:
        person_id, decision = resolve_person(job_id, prof, actor=actor)
        if decision["verdict"] == "same":
            identity_state = "auto"          # 多信号一致，可以自动挂靠
        elif decision["verdict"] == "uncertain":
            # ⚠ 不确定时**绝不自动挂靠**：先给人建独立档案，把"疑似同一人"
            # 作为建议挂上，等 HR 确认。宁可人才库里多一张待确认的卡片，
            # 也不要把两个人的材料混成一份评审结论。
            identity_state = "uncertain"
            suggestion = {"person_id": person_id,
                          "score": decision.get("score"),
                          "reasons": decision.get("reasons")}
            person_id = None
        else:                                # different / new
            person_id = None

    conn = db.connect()
    cid = _new_id("cand")
    now = _now()
    retain = _retention_until(job.get("retention_days"))
    if person_id is None:
        person_id = ensure_person(prof, actor=actor)

    ident_blob = identity.signature(prof)
    if suggestion:
        ident_blob["suggested_person_id"] = suggestion["person_id"]
        ident_blob["suggestion"] = suggestion

    with db.tx(conn) as c:
        c.execute(
            "INSERT INTO candidates(id, job_id, person_id, name, identity_json, identity_state,"
            " resume_enc, assessment_enc, transcript_enc, status, created_at, updated_at,"
            " retention_until, person_decision)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, job_id, person_id, (name or "未命名候选人").strip()[:80],
             db.jdump(ident_blob), identity_state,
             _enc(resume_text), _enc(assessment_text), _enc(transcript_text),
             "pending", now, now, retain, decide if identity_state == "auto" else "pending"),
        )
        for f in files:
            stored = f.get("stored") or ""
            if f.get("data"):
                stored, _ = save_upload(job_id, cid, f.get("filename") or "file", f["data"])
            c.execute(
                "INSERT INTO candidate_files(candidate_id, job_id, filename, stored, role,"
                " kind, chars, warnings_json, imported_from, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, job_id, f.get("filename") or "file", stored, f.get("role") or "resume",
                 f.get("kind") or "", int(f.get("chars") or len(f.get("text") or "")),
                 db.jdump(f.get("warnings") or []), 1 if f.get("imported_from") else 0, now),
            )
        _record_identity(c, person_id, cid, decision, decide=decide, actor=actor)
        _insert_segments(c, cid, job_id, segs, now)
        audit.record(c, "candidate.upload", actor=actor, target_type="candidate", target_id=cid,
                     detail={"job_id": job_id, "files": len(files),
                             "identity_verdict": decision.get("verdict")})
    return get_candidate(cid)


def resolve_person(job_id, prof, actor="system"):
    """在某岗位内比对是否已有同一个人。

    返回 (person_id|None, decision_dict)。
    同一岗位内的候选人是"最可能重复上传"的场景，所以先比岗位内，
    再退到全局（跨岗位重复投递）。全局命中会复用 person_id，从而让人才库自动合并。
    """
    conn = db.connect()
    candidates = []
    seen = set()

    for scope, sql in (
        ("job", "SELECT id, name, person_id, identity_json FROM candidates"
                " WHERE job_id=? AND person_id IS NOT NULL"),
        ("all", "SELECT id, name, person_id, identity_json FROM candidates"
                " WHERE person_id IS NOT NULL"),
    ):
        for r in conn.execute(sql, (job_id,) if scope == "job" else ()):
            if r["person_id"] in seen:
                continue
            seen.add(r["person_id"])
            candidates.append((r["person_id"], db.jload(r["identity_json"], {}), r["name"]))

    best = None
    for pid, other_prof, other_name in candidates:
        cmp = identity.compare_profiles(prof, dict(other_prof, name=other_name))
        if cmp["verdict"] == "different":
            continue
        if best is None or cmp["score"] > best[1]["score"]:
            best = (pid, cmp)

    if best is None:
        return None, {"verdict": "new", "score": 0.0, "matched": [], "conflicts": [],
                      "signals_compared": 0, "reasons": ["岗位内没有可比的既有候选人"]}
    return best[0], best[1]


def ensure_person(prof, actor="system"):
    """为一份材料新建人物档案，返回 person_id。

    ⚠ 这里**故意不做姓名查重**。
    早期版本按 `name_key` 复用已有档案，结果"重名不同人"又被合成了同一个：
    即便 resolve_person 已判定 different，落库时仍被 ensure_person 按姓名合并回去。
    姓名不是标识——只有 phone/email 或多信号一致才算同一人。
    因此：新人 = 新档案；要挂到既有档案必须显式传 `merge_into_person_id`。
    """
    conn = db.connect()
    name = (prof or {}).get("name") or "未命名"
    name_key = identity.norm_name(name)
    pid = "p_" + secrets.token_hex(8)
    now = _now()
    with db.tx(conn) as c:
        c.execute(
            "INSERT INTO people(id, name, name_key, status, identity_json, created_at, updated_at,"
            " retention_until) VALUES (?,?,?,?,?,?,?,?)",
            (pid, name, name_key, "new", db.jdump(identity.signature(prof)), now, now,
             _retention_until(DEFAULT_RETENTION_DAYS)),
        )
    return pid


def _record_identity(conn, person_id, candidate_id, decision, decide="auto", actor="system"):
    """把一次身份判断留痕（谁在什么时候、依据什么、把谁和谁关联起来）。"""
    conn.execute(
        "INSERT INTO identity_decisions(id, person_id, candidate_id, decision, score,"
        " signals_json, decide, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (_new_id("id"), person_id, candidate_id, decision.get("verdict") or "new",
         decision.get("score"), db.jdump(decision), decide, actor, _now()),
    )


def confirm_identity(candidate_id, is_same, merge_into_person_id=None, actor="system"):
    """HR 对 uncertain 的判断做确认：是同一个人 / 不是。

    is_same=True  → 把候选人挂到指定 person（默认取比对得分最高的那个）
    is_same=False → 新建独立 person，避免两人被合并
    """
    db.init()
    conn = db.connect()
    cand = get_candidate(candidate_id, with_text=False)
    if not cand:
        raise KeyError("候选人不存在：%s" % candidate_id)

    with db.tx(conn) as c:
        if is_same:
            pid = merge_into_person_id or cand["person_id"]
            old = cand["person_id"]
            c.execute("UPDATE candidates SET person_id=?, identity_state='confirmed',"
                      " person_decision='same', updated_at=? WHERE id=?",
                      (pid, _now(), candidate_id))
            if old and old != pid:
                c.execute("UPDATE people SET merged_into=? WHERE id=?", (pid, old))
            _record_identity(c, pid, candidate_id,
                             {"verdict": "same", "reasons": ["HR 确认为同一人"], "score": 1.0},
                             decide="human", actor=actor)
            audit.record(c, "candidate.identity_confirm", actor=actor,
                         target_type="candidate", target_id=candidate_id,
                         detail={"is_same": True, "person_id": pid})
        else:
            # 独立成一个人。若该候选人的档案是入库时临时建的（且没有别的候选人挂靠），
            # 直接沿用即可——不必再新生一个，否则人才库里会留下空壳档案。
            pid = cand["person_id"]
            others = c.execute(
                "SELECT COUNT(*) AS n FROM candidates WHERE person_id=? AND id<>?",
                (pid, candidate_id)).fetchone()["n"]
            if not pid or others:
                prof = identity.extract_profile(cand["name"], "", "")
                pid = ensure_person(prof, actor=actor)
            c.execute("UPDATE candidates SET person_id=?, identity_state='confirmed',"
                      " person_decision='different', updated_at=? WHERE id=?",
                      (pid, _now(), candidate_id))
            _record_identity(c, pid, candidate_id,
                             {"verdict": "different", "reasons": ["HR 确认为不同的人"],
                              "score": 0.0}, decide="human", actor=actor)
            audit.record(c, "candidate.identity_split", actor=actor,
                         target_type="candidate", target_id=candidate_id,
                         detail={"is_same": False, "person_id": pid})
    return get_candidate(candidate_id, with_text=False)


def update_candidate(cand_id, patch, actor="system"):
    """按白名单更新候选人字段（只写有变化的列）。"""
    db.init()
    conn = db.connect()
    cur = get_candidate(cand_id)
    if not cur:
        raise KeyError("候选人不存在：%s" % cand_id)

    sets, args = [], []
    for k in _CAND_WRITABLE:
        if k not in patch:
            continue
        v = patch[k]
        if k in ("resume_text", "assessment_text", "transcript_text"):
            col = {"resume_text": "resume_enc", "assessment_text": "assessment_enc",
                   "transcript_text": "transcript_enc"}[k]
            sets.append("%s=?" % col)
            args.append(_enc(v))
        elif k in ("result", "interview", "identity"):
            col = {"result": "result_json", "interview": "interview_json",
                   "identity": "identity_json"}[k]
            sets.append("%s=?" % col)
            args.append(db.jdump(v))
        else:
            sets.append("%s=?" % k)
            args.append(v)

    if not sets:
        return cur
    sets.append("updated_at=?")
    args.append(_now())
    args.append(cand_id)

    with db.tx(conn) as c:
        c.execute("UPDATE candidates SET %s WHERE id=?" % ", ".join(sets), args)
        # 结果类字段同时追加一条历史。**只追加不覆盖**：
        # 重跑一次如果直接覆盖，就再也无法回答"这次和上次差多少"，
        # 也无法验证评审到底可不可复现。
        if "result" in patch and patch["result"]:
            _append_evaluation(c, cand_id, cur.get("job_id") or "", patch["result"],
                               run_id=(patch.get("_run_id") or ""))
    return get_candidate(cand_id)


def _append_evaluation(conn, cand_id, job_id, res, run_id=""):
    """把一次评审结果写进 evaluations。结果形状不对时安静跳过。"""
    if not isinstance(res, dict) or res.get("total") is None:
        return
    unc = res.get("uncertainty") or {}
    parts = res.get("rubric_parts") or {}
    conn.execute(
        "INSERT INTO evaluations(id, candidate_id, job_id, run_id, total, grade,"
        " rubric_version, prompt_fingerprint, model, provider, repeats, stdev,"
        " result_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_new_id("ev"), cand_id, job_id or "", run_id or "",
         res.get("total"), res.get("grade") or "",
         res.get("rubric_version") or "",
         parts.get("prompt_fingerprint") or "",
         res.get("model") or parts.get("model") or "",
         res.get("provider") or parts.get("provider") or "",
         int(unc.get("repeats_ok") or 1), unc.get("stdev"),
         db.jdump(res), _now()))


def evaluations_of(cand_id, limit=50):
    """某个候选人的评审历史（新的在前）。"""
    db.init()
    conn = db.connect()
    return [{
        "id": r["id"], "run_id": r["run_id"], "total": r["total"],
        "grade": r["grade"], "rubric_version": r["rubric_version"],
        "prompt_fingerprint": r["prompt_fingerprint"],
        "model": r["model"], "provider": r["provider"],
        "repeats": r["repeats"], "stdev": r["stdev"],
        "created_at": r["created_at"],
    } for r in conn.execute(
        "SELECT * FROM evaluations WHERE candidate_id=? ORDER BY created_at DESC"
        " LIMIT ?", (cand_id, int(limit)))]


def evaluation_drift(cand_id):
    """同一候选人多次评审之间的波动。

    这是"评审可复现吗"最直接的证据：同一个人的分数反复变化超过档位，
    说明分数不该被当成事实使用。样本少于 2 次时返回 None（没测就是没测）。
    """
    hist = evaluations_of(cand_id, limit=100)
    totals = [h["total"] for h in hist if h["total"] is not None]
    if len(totals) < 2:
        return None
    grades = [h["grade"] for h in hist if h["total"] is not None]
    return {
        "n": len(totals),
        "min": min(totals), "max": max(totals),
        "spread": round(max(totals) - min(totals), 2),
        "versions": sorted({h["rubric_version"] for h in hist if h["rubric_version"]}),
        "grade_changed": len(set(grades)) > 1,
        "history": hist,
    }


def mark_run_status(job_id, statuses, actor="system"):
    """批量改状态（开跑前把 pending 全置 running）。"""
    db.init()
    conn = db.connect()
    now = _now()
    with db.tx(conn) as c:
        for cid, st in (statuses or {}).items():
            c.execute("UPDATE candidates SET status=?, updated_at=? WHERE id=?",
                      (st, now, cid))
    return True


def delete_candidate(cand_id, actor="system"):
    db.init()
    conn = db.connect()
    cand = get_candidate(cand_id, with_text=False)
    if not cand:
        return False
    job_id = cand["job_id"]
    with db.tx(conn) as c:
        c.execute("DELETE FROM candidates WHERE id=?", (cand_id,))
        audit.record(c, "candidate.delete", actor=actor, target_type="candidate",
                     target_id=cand_id, detail={"job_id": job_id})
    d = os.path.join(job_dir(job_id), "files", cand_id)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    return True


# ---------------------------------------------------------------------------
# 面试转写稿的段级说话人归属（P1，diarization 的数据落地）
# ---------------------------------------------------------------------------
def _seg_row(r):
    return {
        "idx": r["idx"],
        "start": r["start_sec"],
        "end": r["end_sec"],
        "text": r["text"],
        "speaker": r["speaker"],
        "confidence": r["confidence"],
        "reason": r["reason"],
        "asker": r["asker"],
        "source": r["source"],
        "updated_at": r["updated_at"],
    }


def _insert_segments(c, cand_id, job_id, segs, now):
    """在既有事务里插段（add_candidate 用，保证候选人与段同事务）。

    段序按传入顺序重排（`idx` = 位置）：多份音频/多份转写稿拼起来时，
    "第几段"必须是全局唯一且连续的，否则人工修正按 idx 对齐会错位。
    """
    n = 0
    for i, s in enumerate(segs or []):
        if not isinstance(s, dict):
            continue
        text = (s.get("text") or "").strip()
        if not text:
            continue
        c.execute(
            "INSERT INTO interview_segments(candidate_id, job_id, idx, start_sec, end_sec,"
            " text, speaker, confidence, reason, asker, source, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cand_id, job_id, i,
             float(s.get("start") or s.get("start_sec") or 0.0),
             float(s.get("end") or s.get("end_sec") or 0.0),
             text, s.get("speaker") or "unassigned",
             float(s.get("confidence") or 0.0),
             (s.get("reason") or "")[:300], (s.get("asker") or "")[:40],
             s.get("source") or "engine", now, now))
        n += 1
    return n


def save_segments(cand_id, job_id, segments, keep_manual=True):
    """替换保存某候选人的转写段（engine 归属的一次运行结果）。

    `keep_manual=True`（默认）时按 idx 保留**不可重算**的段：
    source=manual（人工修正）与 source=prefix（原文自带前缀推定的
    归属——切段时前缀已剥离，无法重建）。重跑归属不该冲掉它们，
    那是比模型输出贵得多的数据。代价是换了一份 segmentation 不同的
    录音重传时，旧修正可能错位；所以修正记录里带 updated_at，
    UI 需要时能看出"这条修正是针对哪一版分段"。

    返回 {"saved": n, "manual_kept": m}（manual_kept 含 prefix 段——
    两者都是"非引擎"来源，对调用方是同一类：不许覆盖）。
    """
    db.init()
    conn = db.connect()
    cand = get_candidate(cand_id, with_text=False)
    if not cand:
        raise KeyError("候选人不存在：%s" % cand_id)
    job_id = job_id or cand["job_id"]
    now = _now()

    rows = []
    for i, s in enumerate(segments or []):
        if not isinstance(s, dict):
            continue
        text = (s.get("text") or "").strip()
        if not text:
            continue
        rows.append((i,
                     float(s.get("start") or s.get("start_sec") or 0.0),
                     float(s.get("end") or s.get("end_sec") or 0.0),
                     text, s.get("speaker") or "unassigned",
                     float(s.get("confidence") or 0.0),
                     (s.get("reason") or "")[:300], (s.get("asker") or "")[:40],
                     s.get("source") or "engine"))

    with db.tx(conn) as c:
        keep = {}
        if keep_manual:
            for r in c.execute(
                    "SELECT idx, speaker, asker, reason, confidence, source, updated_at"
                    " FROM interview_segments WHERE candidate_id=?"
                    " AND source IN ('manual','prefix')",
                    (cand_id,)):
                keep[r["idx"]] = dict(r)
        c.execute("DELETE FROM interview_segments WHERE candidate_id=?", (cand_id,))
        kept = 0
        for (idx, st, en, text, speaker, conf, reason, asker, source) in rows:
            m = keep.get(idx)
            if m:
                speaker, asker, reason, source = (m["speaker"], m["asker"],
                                                  m["reason"], m["source"])
                conf, upd = m["confidence"], m["updated_at"]
                kept += 1
            else:
                upd = now
            c.execute(
                "INSERT INTO interview_segments(candidate_id, job_id, idx, start_sec, end_sec,"
                " text, speaker, confidence, reason, asker, source, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cand_id, job_id, idx, st, en, text, speaker, conf, reason, asker,
                 source, now, upd))
    return {"saved": len(rows), "manual_kept": kept}


def get_segments(cand_id):
    """取某候选人的全部段（按 idx）。没有返回空列表——调用方据此决定
    是把粘贴的转写稿切伪段还是直接报"没有转写稿"。"""
    db.init()
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM interview_segments WHERE candidate_id=? ORDER BY idx",
        (cand_id,)).fetchall()
    return [_seg_row(r) for r in rows]


def update_segment(cand_id, idx, patch):
    """人工修正一段的归属（层 3 终审）。只允许 speaker/asker。

    speaker 允许 unassigned——UI 必须能表达"这段我也不确定"，
    把不确定态藏起来只会逼用户猜。confidence 置 1.0：人看过了。
    """
    db.init()
    conn = db.connect()
    cand = get_candidate(cand_id, with_text=False)
    if not cand:
        raise KeyError("候选人不存在：%s" % cand_id)
    speaker = (patch.get("speaker") or "").strip()
    if speaker not in ("interviewer", "candidate", "unassigned"):
        raise ValueError("speaker 必须是 interviewer / candidate / unassigned")
    asker = (patch.get("asker") or "").strip()[:40]
    reason = (patch.get("reason") or "").strip()[:200] or "人工修正"
    now = _now()
    with db.tx(conn) as c:
        cur = c.execute("SELECT id FROM interview_segments WHERE candidate_id=? AND idx=?",
                        (cand_id, int(idx))).fetchone()
        if not cur:
            raise KeyError("段不存在：idx=%s" % idx)
        c.execute(
            "UPDATE interview_segments SET speaker=?, asker=?, reason=?, source='manual',"
            " confidence=1.0, updated_at=? WHERE id=?",
            (speaker, asker, reason, now, cur["id"]))
    return True


# ---------------------------------------------------------------------------
# 原始文件
# ---------------------------------------------------------------------------
def save_upload(job_id, cand_id, filename, data):
    """把上传的原始文件落盘，返回 (相对项目根路径, 落盘文件名)。"""
    name = safe_filename(filename)
    d = os.path.join(job_dir(job_id), "files", cand_id)
    os.makedirs(d, exist_ok=True)
    target = os.path.join(d, name)
    if os.path.exists(target):
        stem, ext = os.path.splitext(name)
        target = os.path.join(d, "%s_%s%s" % (stem, secrets.token_hex(4), ext))
    with open(target, "wb") as f:
        f.write(data)
    rel = os.path.relpath(target, db.PROJECT_ROOT).replace("\\", "/")
    return rel, os.path.basename(target)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def job_stats(job_id=None):
    conn = db.connect()
    if job_id:
        r = conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done"
            " FROM candidates WHERE job_id=?", (job_id,)).fetchone()
        return {"candidates": r["n"], "done": r["done"] or 0}
    r = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done"
        " FROM candidates").fetchone()
    j = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    return {"jobs": j["n"], "candidates": r["n"], "done": r["done"] or 0}
