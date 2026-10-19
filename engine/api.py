"""
业务 API 层：路由表 + 各接口实现。

路由表是**显式**的（path pattern → 处理函数），不再是一连串 `if path.startswith(...)`。
好处很具体：每个接口的权限点在注册时一眼可见（`auth="candidate.write"`），
漏加权限会立刻在注册表里显形，而不是藏在 300 行 if 的某个分支里。

约定
----
· 所有写接口都需要 `auth="xxx.write"`，读接口 `"xxx.read"`；`auth=None` 仅用于登录/引导。
· 返回值直接是 dict，由 http_base 序列化。
· 权限失败抛 ApiError(401/403)，业务失败抛 ApiError(400/404/409)。
"""
import json
import os
import time

import audit
import align
import auth
import backup
import cost_estimate
import crypto
import db
import http_base
import identity
import llm_client
import outcome
import privacy
import runs
import store
import talent
import usage
from docparse import SUPPORTED_EXTS, ParseError, is_supported, parse_bytes
import audio_ingest
from http_base import ApiError

# ---------------------------------------------------------------------------
# 素材归类
# ---------------------------------------------------------------------------
RESUME_HINTS = ("简历", "resume", "cv", "履历")
ASSESS_HINTS = ("测评", "评测", "笔试", "机试", "assessment", "test", "exam", "logic")
TRANSCRIPT_HINTS = ("转写", "面试", "逐字稿", "transcript", "interview")
AUDIO_HINTS = ("录音", "音频", "视频", "audio", "video", "record")


def guess_role(filename):
    """按文件名猜素材类别（HR 的命名习惯）。猜不到就当简历——最常见。"""
    low = (filename or "").lower()
    if any(h in low for h in TRANSCRIPT_HINTS):
        return "transcript"
    if any(h in low for h in AUDIO_HINTS):
        return "transcript"  # 音频的内容就是转写稿，角色唯一
    if any(h in low for h in ASSESS_HINTS):
        return "assessment"
    if any(h in low for h in RESUME_HINTS):
        return "resume"
    return "resume"


def guess_name(filename):
    """从文件名猜候选人姓名：去掉扩展名与常见后缀。"""
    base = os.path.splitext(os.path.basename(filename or ""))[0]
    for sep in ("-", "_", "—", "（", "("):
        if sep in base:
            head = base.split(sep)[0].strip()
            if 1 < len(head) <= 12:
                base = head
                break
    for noise in ("简历", "resume", "cv", "测评", "面试", "转写"):
        base = base.replace(noise, "").replace(noise.upper(), "")
    return base.strip(" _-—") or "未命名候选人"


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------
ROUTES = []


def route(method, pattern, auth_action=None):
    """注册路由。pattern 用 `:name` 表示路径参数。"""
    def deco(fn):
        ROUTES.append({"method": method, "pattern": pattern,
                       "auth": auth_action, "fn": fn})
        return fn
    return deco


def match(method, path):
    """把请求路径匹配到 (route, params)。"""
    parts = [p for p in path.split("/") if p]
    best = None
    for r in ROUTES:
        if r["method"] != method:
            continue
        rp = [p for p in r["pattern"].split("/") if p]
        if len(rp) != len(parts):
            continue
        params, ok = {}, True
        for rp_seg, actual in zip(rp, parts):
            if rp_seg.startswith(":"):
                params[rp_seg[1:]] = actual
            elif rp_seg != actual:
                ok = False
                break
        if ok:
            # 静态段更多的优先（避免 :id 抢走固定路径）
            score = sum(1 for s in rp if not s.startswith(":"))
            if best is None or score > best[0]:
                best = (score, r, params)
    return (best[1], best[2]) if best else (None, None)


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
@route("GET", "/api/health", None)
def h_health(ctx):
    return {"ok": True, "version": db.SCHEMA_VERSION,
            "schema_version": db.SCHEMA_VERSION,
            "needs_setup": auth.needs_setup(),
            "user": (ctx["user"] or {}).get("username") if ctx["user"] else None}


@route("GET", "/api/bootstrap", None)
def h_bootstrap(ctx):
    """首次启动引导状态：是否需要创建管理员。"""
    need = auth.needs_setup()
    return {"ok": True, "needs_setup": need,
            "user": ctx["user"],
            "roles": [{"id": r, "label": auth.ROLE_LABEL[r]} for r in auth.ROLES],
            "retention_default_days": store.DEFAULT_RETENTION_DAYS}


# ---------------------------------------------------------------------------
# 启动自检
#
# demo 现场最怕的不是"功能不好用"，而是"配好了但跑不起来"，且不知道卡在哪。
# 这个接口把四件事一次说清：数据库能不能写、模型能不能连、额度还剩多少、
# 备份新不新鲜。
#
# 花钱的边界：连通性探测与额度查询都会真的打一次网络（前者还会消耗一次
# 最小调用）。所以它们**默认不执行**，只有显式 `?probe=1` 才跑，
# 且结果缓存 10 分钟——否则光打开首页就在烧钱。
# ---------------------------------------------------------------------------
_SELFCACHE = {}
_SELF_TTL = 600          # 秒；探测结果的复用窗口


def _cache_get(key):
    item = _SELFCACHE.get(key)
    if not item:
        return None, 0
    ts, val = item
    age = time.time() - ts
    if age >= _SELF_TTL:
        return None, 0
    return val, int(_SELF_TTL - age)


def _cache_put(key, val):
    _SELFCACHE[key] = (time.time(), val)


def _db_check():
    """能不能读、能不能写。只读打开是发现不了"磁盘满/只读挂载"的。"""
    import sqlite3
    out = {"ok": False, "path": db.DB_PATH, "schema_version": db.SCHEMA_VERSION,
           "error": ""}
    try:
        conn = db.connect()
        conn.execute("SELECT 1").fetchone()
        # 写测试必须回滚：这里只是探活，不该留下任何东西
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        out["ok"] = True
        out["writable"] = True
    except sqlite3.Error as e:
        out["error"] = str(e)
        out["writable"] = False
    except Exception as e:
        out["error"] = str(e)
        out["writable"] = False
    return out


def _backup_check():
    import backup
    out = {"dir": backup.backup_dir(), "count": 0, "latest": "",
           "age_hours": None, "stale": True, "keep": backup.DEFAULT_KEEP,
           "auto_hours": backup.AUTO_HOURS, "note": backup.MANUAL_COPY_WARNING}
    try:
        files = backup.list_files()
        out["count"] = len(files)
        if files:
            newest = files[0]
            out["latest"] = newest["file"]
            p = os.path.join(out["dir"], newest["file"])
            age = (time.time() - os.path.getmtime(p)) / 3600.0
            out["age_hours"] = round(age, 1)
            out["stale"] = age > backup.AUTO_HOURS
    except Exception as e:
        out["error"] = str(e)
    return out


def _provider_check(ctx):
    """模型配置是否齐全（不发请求）。"""
    try:
        cfg = _model_api(ctx)
    except Exception as e:
        return {"configured": False, "error": str(e)}
    import llm_client
    provider = cfg.get("provider") or ""
    key = llm_client._provider_key(provider, cfg.get("base_url"))
    if key == "workbuddy":
        try:
            import workbuddy_client as wb
            ok = wb.is_available()
        except Exception:
            ok = False
        return {"configured": bool(ok), "provider": "workbuddy",
                "model": cfg.get("model") or "", "needs_key": False,
                "note": "复用 WorkBuddy 本机登录态" if ok
                        else "未检测到 WorkBuddy 登录态"}
    has_key = bool(cfg.get("api_key") or os.getenv("OPENAI_API_KEY"))
    return {"configured": has_key, "provider": key or "openai",
            "model": cfg.get("model") or "", "needs_key": True,
            "note": "" if has_key else "未填写 API Key"}


@route("GET", "/api/selfcheck", None)
def h_selfcheck(ctx):
    """环境自检。未登录也能看，但**隐去本地路径与模型细节**。

    auth=None 是有意的：这个面板的价值就在于"登录之前先知道环境行不行"
    （demo 现场最常见的卡点是没配 Key、或备份早过期）。
    代价是它对外可见，所以匿名时只给结论、不给细节。
    """
    probe = str((ctx.get("query") or {}).get("probe") or "") == "1"
    out = {"ok": True, "db": _db_check(), "backup": _backup_check(),
           "provider": _provider_check(ctx),
           "concurrency": llm_client.concurrency_snapshot(),
           "schema_version": db.SCHEMA_VERSION,
           "needs_setup": auth.needs_setup(),
           "probed": probe, "cache_ttl_seconds": _SELF_TTL,
           "connectivity": None, "credits": None,
           "note": "连通性探测会真实发起一次最小调用（消耗极少额度），"
                   "因此默认不执行；结果缓存 10 分钟。"}

    if not ctx.get("user"):
        # 匿名：抹掉本机路径、模型名与额度明细，只留"能不能跑"
        out["db"]["path"] = os.path.basename(out["db"].get("path") or "")
        out["backup"]["dir"] = ""
        out["provider"] = {"configured": out["provider"].get("configured"),
                           "needs_key": out["provider"].get("needs_key")}
        out["concurrency"] = {}
        out["anonymous"] = True

    if probe:
        import provider_probe
        cfg = {}
        try:
            cfg = _model_api(ctx)
        except Exception:
            pass
        cached, left = _cache_get("probe")
        if cached is None:
            try:
                cached = provider_probe.test_connection(
                    cfg.get("provider"), cfg.get("base_url"),
                    cfg.get("api_key"), cfg.get("model"))
            except Exception as e:
                cached = {"ok": False, "error": str(e), "kind": "unknown"}
            _cache_put("probe", cached)
            left = _SELF_TTL
        out["connectivity"] = cached
        out["connectivity_cached_for"] = left

        provider = (cfg.get("provider") or "").lower()
        if provider == "workbuddy" or "workbuddy" in (cfg.get("base_url") or "").lower():
            cred, left2 = _cache_get("credits")
            if cred is None:
                try:
                    import workbuddy_client as wb
                    cred = wb.fetch_credits()
                except Exception as e:
                    cred = {"ok": False, "error": str(e)}
                _cache_put("credits", cred)
                left2 = _SELF_TTL
            out["credits"] = cred
            out["credits_cached_for"] = left2

    return out


@route("POST", "/api/setup", None)
def h_setup(ctx):
    """创建第一个管理员。库中已有用户则拒绝——防止被劫持改密。"""
    if not auth.needs_setup():
        raise ApiError("系统已初始化，不能重复创建管理员", 409)
    b = ctx["body"]
    user = auth.create_user(
        b.get("username") or "admin",
        b.get("password") or "",
        display_name=b.get("display_name") or "管理员",
        role="admin", actor="setup")
    out = auth.login(user["username"], b.get("password") or "",
                     ip=ctx["ip"], ua=ctx["ua"])
    return {"ok": True, "user": out["user"],
            "cookies": auth_cookies(out["token"], out["csrf"])}


def auth_cookies(token, csrf):
    return [
        "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
        % (auth.SESSION_COOKIE, token, auth.SESSION_HOURS * 3600),
        # CSRF cookie 必须能被 JS 读到（前端要放进请求头），所以不是 HttpOnly
        "%s=%s; Path=/; SameSite=Lax; Max-Age=%d"
        % (auth.CSRF_COOKIE, csrf, auth.SESSION_HOURS * 3600),
    ]


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
@route("POST", "/api/auth/login", None)
def h_login(ctx):
    b = ctx["body"]
    out = auth.login(b.get("username") or "", b.get("password") or "",
                     ip=ctx["ip"], ua=ctx["ua"])
    return {"ok": True, "user": out["user"],
            "cookies": auth_cookies(out["token"], out["csrf"])}


@route("POST", "/api/auth/logout", None)
def h_logout(ctx):
    tok = ctx["cookies"].get(auth.SESSION_COOKIE)
    auth.logout(tok)
    return {"ok": True, "cookies": [
        "%s=; Path=/; HttpOnly; Max-Age=0" % auth.SESSION_COOKIE,
        "%s=; Path=/; Max-Age=0" % auth.CSRF_COOKIE,
    ]}


@route("GET", "/api/auth/me", None)
def h_me(ctx):
    if not ctx["user"]:
        raise ApiError("未登录", 401)
    u = dict(ctx["user"])
    u["permissions"] = sorted(auth._PERMISSIONS.get(u.get("role"), set()))
    s = auth.public_settings(u["id"])
    u["model"] = s
    return {"ok": True, "user": u}


@route("POST", "/api/auth/password", "settings.write")
def h_password(ctx):
    b = ctx["body"]
    old = b.get("old_password") or ""
    new = b.get("new_password") or ""
    if not auth.verify_password(ctx["user"]["id"], old):
        raise ApiError("原口令不正确", 400)
    strength = auth.password_strength(new)
    if not strength.get("ok"):
        raise ApiError("新口令太短（至少 8 位，建议 12 位以上并混合大小写/数字/符号）", 400)
    auth.set_password(ctx["user"]["id"], new, actor=ctx["user"]["id"])
    return {"ok": True, "message": "口令已更新，所有会话已失效，请重新登录。",
            "cookies": ["%s=; Path=/; HttpOnly; Max-Age=0" % auth.SESSION_COOKIE,
                        "%s=; Path=/; Max-Age=0" % auth.CSRF_COOKIE]}


# ---------------------------------------------------------------------------
# 用户管理（仅 admin）
# ---------------------------------------------------------------------------
@route("GET", "/api/users", "settings.write")
def h_users(ctx):
    return {"ok": True, "users": auth.list_users()}


@route("POST", "/api/users", "settings.write")
def h_user_create(ctx):
    b = ctx["body"]
    u = auth.create_user(b.get("username") or "", b.get("password") or "",
                         display_name=b.get("display_name") or "", role=b.get("role") or "hr",
                         actor=ctx["user"]["id"])
    return {"ok": True, "user": u}


@route("POST", "/api/users/:uid/disable", "settings.write")
def h_user_disable(ctx):
    auth.set_disabled(ctx["params"]["uid"], bool(ctx["body"].get("disabled", True)),
                     actor=ctx["user"]["id"])
    return {"ok": True}


@route("POST", "/api/users/:uid/password", "settings.write")
def h_user_password(ctx):
    auth.set_password(ctx["params"]["uid"], ctx["body"].get("password") or "",
                     actor=ctx["user"]["id"])
    return {"ok": True}


# ---------------------------------------------------------------------------
# 岗位
# ---------------------------------------------------------------------------
@route("GET", "/api/jobs", "job.read")
def h_jobs(ctx):
    return {"ok": True, "jobs": store.list_jobs(), "stats": store.job_stats()}


@route("POST", "/api/jobs", "job.write")
def h_job_create(ctx):
    b = ctx["body"]
    job = store.create_job(title=b.get("title") or "未命名岗位",
                           jd={"raw_text": b.get("raw_text")} if b.get("raw_text") else None,
                           actor=ctx["user"]["id"],
                           retention_days=b.get("retention_days") or store.DEFAULT_RETENTION_DAYS)
    return {"ok": True, "job": job}


@route("GET", "/api/jobs/:jid", "job.read")
def h_job_get(ctx):
    job, cands = store.get_job_detail(ctx["params"]["jid"])
    if not job:
        raise ApiError("岗位不存在", 404)
    active = runs.active_run(job["id"])
    return {"ok": True, "job": job, "candidates": cands, "active_run": active}


@route("PUT", "/api/jobs/:jid", "job.write")
def h_job_update(ctx):
    job = store.update_job(ctx["params"]["jid"], ctx["body"], actor=ctx["user"]["id"])
    return {"ok": True, "job": job}


@route("DELETE", "/api/jobs/:jid", "job.write")
def h_job_delete(ctx):
    ok = store.delete_job(ctx["params"]["jid"], actor=ctx["user"]["id"])
    return {"ok": ok}


# ---------------------------------------------------------------------------
# 候选人
# ---------------------------------------------------------------------------
@route("POST", "/api/jobs/:jid/candidates", "candidate.write")
def h_upload(ctx):
    """上传候选人素材。

    body: {"candidates":[{"name":"", "files":[{"filename","role","data_base64"}],
                          "force_new_person": false}]}
    name 为空时从文件名猜。
    """
    job_id = ctx["params"]["jid"]
    if not store.get_job(job_id):
        raise ApiError("岗位不存在", 404)
    groups = ctx["body"].get("candidates")
    if not groups:
        files = ctx["body"].get("files") or []
        groups = [{"name": ctx["body"].get("name") or "", "files": files}]
    if not groups:
        raise ApiError("没有收到任何文件", 400)

    added, errors = [], []
    for g in groups:
        name = (g.get("name") or "").strip()
        files = g.get("files") or []
        by_role = {"resume": [], "assessment": [], "transcript": []}
        stored = []

        for f in files:
            fname = f.get("filename") or "file"
            if not is_supported(fname):
                errors.append("%s：不支持的文件类型（支持 %s）"
                              % (fname, "、".join(sorted(SUPPORTED_EXTS))))
                continue
            try:
                data = http_base.b64decode_strict(f.get("data_base64") or "")
            except ApiError as e:
                errors.append("%s：%s" % (fname, e.message))
                continue
            role = (f.get("role") or "").strip() or guess_role(fname)
            if role not in by_role:
                role = "resume"
            if audio_ingest.is_audio(fname):
                # 音频/视频：不走文本抽取，经 ASR 转写成文字稿。
                # 角色强制为 transcript——音频的内容就是转写稿，没有第二种解读。
                # 失败走 errors 通道（和其它解析失败同一形态），不影响同批其它文件。
                role = "transcript"
                try:
                    parsed = audio_ingest.transcribe_audio(fname, data)
                except ParseError as e:
                    errors.append("%s：%s" % (fname, e))
                    continue
                except Exception as e:
                    errors.append("%s：转写异常 %s" % (fname, e))
                    continue
                parsed["kind"] = "asr-transcript"
            else:
                try:
                    parsed = parse_bytes(fname, data)
                except ParseError as e:
                    errors.append("%s：%s" % (fname, e))
                    continue
                except Exception as e:
                    errors.append("%s：解析异常 %s" % (fname, e))
                    continue
            if not parsed.get("text"):
                warn = "；".join(parsed.get("warnings") or []) or "抽不出文字"
                errors.append("%s：%s" % (fname, warn))
                continue
            # 文件先转成"待落盘"形式，由 store 在事务里落盘 + 建条目
            stored.append({
                "filename": fname, "role": role, "kind": parsed.get("kind"),
                "chars": len(parsed["text"]), "warnings": parsed.get("warnings") or [],
                "text": parsed["text"], "data": data,
                # ASR 的段级时间戳一起带下去——P1 说话人归属的最小单位就是段，
                # 只存合并文本的话"谁在说"在数据层就不存在。
                "segments": parsed.get("segments") or [],
            })
            by_role[role].append(parsed["text"])
            name = name or guess_name(fname)

        if not stored:
            continue
        cand = store.add_candidate(
            job_id, name=name or "未命名候选人", files=stored,
            resume_text="\n\n".join(by_role["resume"]),
            assessment_text="\n\n".join(by_role["assessment"]),
            transcript_text="\n\n".join(by_role["transcript"]),
            actor=ctx["user"]["id"],
            force_new_person=bool(g.get("force_new_person")),
        )
        added.append({"id": cand["id"], "name": cand["name"],
                      "person_id": cand["person_id"],
                      "identity_state": cand["identity_state"],
                      "person_decision": cand["person_decision"],
                      "identity": cand["identity"],
                      "suggestion": (cand["identity"] or {}).get("suggestion")})

    job = store.get_job(job_id)
    if job and job.get("status") == "draft" and store.list_candidates(job_id):
        store.update_job(job_id, {"status": "collecting"}, actor=ctx["user"]["id"])
    return {"ok": True, "added": added, "errors": errors,
            "candidates": store.list_candidates(job_id),
            "pending_identity": talent.pending_identity_list()}


@route("PUT", "/api/candidates/:cid", "candidate.write")
def h_cand_update(ctx):
    """只改内容字段。状态 / 结果 / 身份必须走各自的专用端点。

    这条接口原来用 update_candidate 的完整白名单，等于给状态机开了后门：
    一条 PUT 就能把「评审中」改成「已完成」，或者直接把别人评的结果写进去。
    界面上没有任何地方调它（在 test_viewer.py 的死接口基线里），
    所以收窄它不会破坏任何前端流程。
    """
    patch = {k: v for k, v in ctx["body"].items()
             if k in store.CAND_HTTP_PATCHABLE}
    if not patch:
        raise ApiError("没有可更新的字段。状态请用 /decide，结果由评审写入，"
                       "身份请用 /identity。", 400)
    cand = store.update_candidate(ctx["params"]["cid"], patch,
                                  actor=ctx["user"]["id"])
    return {"ok": True, "candidate": cand}


@route("DELETE", "/api/candidates/:cid", "candidate.write")
def h_cand_delete(ctx):
    ok = store.delete_candidate(ctx["params"]["cid"], actor=ctx["user"]["id"])
    return {"ok": ok}


@route("POST", "/api/candidates/:cid/identity", "candidate.write")
def h_identity_confirm(ctx):
    """HR 对"疑似同一人"做确认。这是 P0-4 的人工闸门，不能省。"""
    b = ctx["body"]
    if "is_same" not in b:
        raise ApiError("缺少 is_same（true=同一人 / false=不同的人）", 400)
    cand = store.confirm_identity(
        ctx["params"]["cid"], bool(b.get("is_same")),
        merge_into_person_id=b.get("merge_into_person_id"),
        actor=ctx["user"]["id"])
    return {"ok": True, "candidate": cand,
            "pending_identity": talent.pending_identity_list()}


@route("GET", "/api/identity/pending", "talent.read")
def h_identity_pending(ctx):
    return {"ok": True, "items": talent.pending_identity_list()}


# 批量确认的上限。合并身份是**不可逆**的（confirm_identity 会改写 person_id），
# 一次点错 200 条无法逐条核对，所以人为设一个上限，
# 逼使用者分组确认而不是"全选然后回车"。
IDENTITY_BATCH_MAX = 50


@route("POST", "/api/identity/batch-confirm", "candidate.write")
def h_identity_batch(ctx):
    """批量确认/否认"疑似同一人"。

    body: {"items": [{"cid": "...", "is_same": true/false, "merge_into_person_id": "..."}]}

    设计取舍：
      · **部分成功也照实返回**，不整体回滚——批量操作里一条坏数据不该让
        另外 49 条白做；但每条失败原因都要出现在结果里。
      · 每条都单独写审计（confirm_identity 内部负责），所以审计链上能还原
        "哪一批、谁、把谁合并到了谁"。
      · 超过上限直接拒绝，不静默截断。
    """
    b = ctx.get("body") or {}
    items = b.get("items")
    if not isinstance(items, list) or not items:
        raise ApiError("需要 items 列表", 400)
    if len(items) > IDENTITY_BATCH_MAX:
        raise ApiError("一次最多确认 %d 条（当前 %d 条）。批量合并不可逆，"
                       "请分组确认。" % (IDENTITY_BATCH_MAX, len(items)), 400)

    actor = (ctx.get("user") or {}).get("id")
    ok_n, results = 0, []
    for it in items:
        cid = (it.get("cid") or "").strip()
        if not cid:
            results.append({"cid": "", "ok": False, "error": "缺少 cid"})
            continue
        if "is_same" not in it:
            results.append({"cid": cid, "ok": False, "error": "缺少 is_same"})
            continue
        try:
            cand = store.confirm_identity(
                cid, bool(it.get("is_same")),
                merge_into_person_id=it.get("merge_into_person_id"),
                actor=actor)
            ok_n += 1
            results.append({"cid": cid, "ok": True,
                            "person_id": (cand or {}).get("person_id"),
                            "identity_state": (cand or {}).get("identity_state")})
        except Exception as e:
            results.append({"cid": cid, "ok": False, "error": str(e)})

    return {"ok": ok_n > 0, "succeeded": ok_n,
            "failed": len(results) - ok_n, "results": results,
            "pending_identity": talent.pending_identity_list()}


# ---------------------------------------------------------------------------
# 岗位级评审细则（权重 + 每个维度"看什么"）
#
# 允许在评审开始前随意改：权重和细则本来就是用人方的判断，不该被系统
# 冻结。但要守住两条：① 评审进行中不能改（否则同一批人用了两把尺子，
# 而且改动会落在跑了一半的那批人身上）；② 改完版本号必变，界面要提示
# "与已有结果不可比"——否则用户会拿两种口径的分数直接比大小。
# ---------------------------------------------------------------------------
def _job_rubric_view(job, api_cfg=None):
    import rubric
    api_cfg = api_cfg or {}
    model, provider = api_cfg.get("model"), api_cfg.get("provider")
    cfg = job["rubric"]
    sig = rubric.signature(job.get("weights"), model=model, provider=provider,
                           rubric_config=cfg)
    # 已有结果用的是哪个版本？如果和当前不一致，说明这个岗位存在跨口径的结果
    conn = db.connect()
    rows = conn.execute(
        "SELECT rubric_version, COUNT(*) n FROM candidates"
        " WHERE job_id=? AND rubric_version IS NOT NULL AND rubric_version<>''"
        " GROUP BY rubric_version", (job["id"],)).fetchall()
    versions = [{"version": r["rubric_version"], "count": r["n"]} for r in rows]
    stale = [v for v in versions if v["version"] != sig["version"]]

    run = runs.active_run(job["id"])
    can_edit, why = True, ""
    if run:
        can_edit = False
        why = "该岗位有评审正在进行（%s）。等它结束再改，否则同一批人会被两种口径混合评分。" % run["id"]

    return {
        "ok": True,
        "job_id": job["id"],
        "config": cfg,
        "presets": [rubric.preset_payload(k) for k in rubric.PRESETS],
        "dimensions": list(rubric.DIMENSIONS),
        "version": sig["version"],
        "parts": sig["parts"],
        "can_edit": can_edit,
        "blocked_reason": why,
        "scored_with_other_version": stale,
        "scored_versions": versions,
        "note": ("权重没有正确答案，它表达的是你的优先级。改完版本号会变，"
                 "新结果与该岗位已有的旧结果不可直接比较。"),
    }


@route("GET", "/api/jobs/:jid/rubric", "job.read")
def h_job_rubric(ctx):
    jid = ctx["params"]["jid"]
    job = store.get_job(jid)
    if not job:
        raise ApiError("岗位不存在", 404)
    try:
        api_cfg = _model_api(ctx)
    except Exception:
        api_cfg = {}
    return _job_rubric_view(job, api_cfg)


@route("PUT", "/api/jobs/:jid/rubric", "job.write")
def h_job_rubric_save(ctx):
    """保存评审细则。weights / criteria / preset / notes 都可单独或一起改。"""
    import rubric
    jid = ctx["params"]["jid"]
    job = store.get_job(jid)
    if not job:
        raise ApiError("岗位不存在", 404)

    run = runs.active_run(jid)
    if run:
        raise ApiError("该岗位有评审正在进行（%s），结束后再改口径。" % run["id"], 409)

    b = ctx.get("body") or {}
    cur = job["rubric"]
    nxt = {
        "preset": b.get("preset", cur["preset"]),
        "weights": b.get("weights", cur["weights"]),
        "criteria": b.get("criteria", cur["criteria"]),
        "notes": b.get("notes", cur["notes"]),
    }
    # 只传 preset 时，连带套用该预设的权重与细则——否则"选了预设但没生效"
    # 会让人以为预设坏了。
    if b.get("preset") and "weights" not in b and "criteria" not in b:
        p = rubric.preset_payload(b["preset"])
        nxt["weights"] = p["weights"]
        nxt["criteria"] = p["criteria"]

    cfg = rubric.normalize_config(nxt, base_weights=job["weights"])
    actor = (ctx.get("user") or {}).get("username") or "system"
    store.update_job(jid, {"rubric": cfg}, actor=actor)

    fresh = store.get_job(jid)
    try:
        api_cfg = _model_api(ctx)
    except Exception:
        api_cfg = {}
    view = _job_rubric_view(fresh, api_cfg)
    view["changed"] = (rubric.config_fingerprint(cfg)
                       != rubric.config_fingerprint(cur))
    return view


# ---------------------------------------------------------------------------
# JD 结构化 / 文档解析
# ---------------------------------------------------------------------------
@route("POST", "/api/parse", "candidate.write")
def h_parse(ctx):
    """通用文档解析：上传 → 抽文本。供"粘贴 or 上传 JD"使用。"""
    results = []
    for f in (ctx["body"].get("files") or []):
        name = f.get("filename") or "file"
        try:
            data = http_base.b64decode_strict(f.get("data_base64") or "")
            results.append(parse_bytes(name, data))
        except ParseError as e:
            results.append({"filename": name, "error": str(e)})
        except Exception as e:
            results.append({"filename": name, "error": "解析异常：%s" % e})
    return {"ok": any(r.get("text") for r in results), "results": results,
            "supported": sorted(SUPPORTED_EXTS)}


@route("POST", "/api/jobs/:jid/jd/structure", "job.write")
def h_jd_structure(ctx):
    import jd_struct
    raw = (ctx["body"].get("raw_text") or "").strip()
    for f in (ctx["body"].get("files") or []):
        try:
            data = http_base.b64decode_strict(f.get("data_base64") or "")
            r = parse_bytes(f.get("filename") or "jd", data)
            if r.get("text"):
                raw = (raw + "\n\n" + r["text"]).strip()
        except Exception as e:
            print("[api] JD 附件解析失败：%s" % e)
    if not raw:
        raise ApiError("没有可结构化的内容：请粘贴招聘要求或上传 JD 文档。", 400)
    structured = jd_struct.structure_jd(raw, api=_model_api(ctx),
                                        actor=ctx["user"]["id"])
    job = store.update_job(ctx["params"]["jid"], {"jd": structured},
                           actor=ctx["user"]["id"])
    if ctx["user"]:
        conn = db.connect()
        with db.tx(conn) as c:
            audit.record(c, "jd.structure", actor=ctx["user"]["id"], target_type="job",
                         target_id=ctx["params"]["jid"],
                         detail={"requirements": len(structured.get("requirements") or [])})
    return {"ok": True, "job": job, "jd": structured,
            "injection": structured.get("injection")}


# ---------------------------------------------------------------------------
# 模型配置 / 探测
# ---------------------------------------------------------------------------
def _model_api(ctx):
    """组装调用引擎用的 api dict。

    来源优先级：请求体里的 api 覆盖 > 当前登录用户的已保存配置。
    密钥**只在此刻从库里解密取出**，用完即丢，不进任何日志或响应。
    """
    api = {}
    uid = (ctx.get("user") or {}).get("id")
    if uid:
        s = auth.get_settings(uid)
        if s.get("provider"):
            api["provider"] = s["provider"]
        if s.get("model"):
            api["model"] = s["model"]
        if s.get("base_url"):
            api["base_url"] = s["base_url"]
        if s.get("api_key"):
            api["api_key"] = s["api_key"]
    for k in ("provider", "model", "base_url", "api_key"):
        v = (ctx.get("body") or {}).get("api", {}).get(k)
        if v:
            api[k] = v
    return api


@route("GET", "/api/model/config", "settings.write")
def h_model_get(ctx):
    return {"ok": True, "config": auth.public_settings(ctx["user"]["id"])}


@route("POST", "/api/model/config", "settings.write")
def h_model_set(ctx):
    b = ctx["body"]
    cfg = auth.save_settings(
        ctx["user"]["id"],
        provider=b.get("provider"), model=b.get("model"), base_url=b.get("base_url"),
        api_key=(b.get("api_key") if b.get("api_key") else
                 ("" if b.get("clear_key") else None)),
        actor=ctx["user"]["id"])
    return {"ok": True, "config": cfg}


@route("POST", "/api/model/probe", "settings.write")
def h_probe(ctx):
    import provider_probe
    b = ctx["body"]
    action = (b.get("action") or "test").strip()
    if action == "models":
        res = provider_probe.list_models(b.get("provider"), b.get("base_url"),
                                         b.get("api_key"))
    else:
        res = provider_probe.test_connection(b.get("provider"), b.get("base_url"),
                                             b.get("api_key"), b.get("model"))
    return {"ok": True, "action": action, "result": res}


@route("GET", "/api/model/workbuddy", "settings.write")
def h_workbuddy(ctx):
    import workbuddy_client as wb
    return {"ok": True, "status": wb.is_available(),
            "models": wb.describe_models() if wb.is_available().get("ok") else [],
            "credits": wb.fetch_credits() if wb.is_available().get("ok") else None,
            "notice": "WorkBuddy 通道复用本机桌面端登录态，仅供个人本机试用；"
                      "对外部署请改用自备 API Key 的供应商。"}


# ---------------------------------------------------------------------------
# 评审运行
# ---------------------------------------------------------------------------
@route("POST", "/api/jobs/:jid/run", "run.write")
def h_run_start(ctx):
    """启动评审。已有运行则返回 409（**不再**默默再跑一遍）。"""
    job_id = ctx["params"]["jid"]
    job = store.get_job(job_id)
    if not job:
        raise ApiError("岗位不存在", 404)
    cands = store.list_candidates(job_id)
    ids = ctx["body"].get("candidate_ids")
    if ids:
        cands = [c for c in cands if c["id"] in ids]
    cands = [c for c in cands if c.get("status") != "done"] or cands
    if not cands:
        raise ApiError("该岗位还没有候选人素材", 400)

    api = _model_api(ctx)
    import rubric
    rv = rubric.signature(job.get("weights"), model=api.get("model"),
                          provider=api.get("provider"),
                          rubric_config=job.get("rubric"))["version"]
    try:
        run = runs.start(job_id, [c["id"] for c in cands], actor=ctx["user"]["id"],
                         rubric_version=rv, job_title=job.get("title") or "")
    except runs.RunBusy as e:
        raise ApiError(str(e), 409)

    import jobs_runner
    jobs_runner.spawn(run["id"], api)
    return {"ok": True, "run": run}


@route("GET", "/api/jobs/:jid/run", "run.read")
def h_run_state(ctx):
    job_id = ctx["params"]["jid"]
    active = runs.active_run(job_id)
    return {"ok": True, "active": active, "recent": runs.recent_runs(job_id, 10)}


@route("POST", "/api/runs/:rid/cancel", "run.write")
def h_run_cancel(ctx):
    ok = runs.request_cancel(ctx["params"]["rid"], actor=ctx["user"]["id"])
    if not ok:
        raise ApiError("该评审不在运行中", 409)
    return {"ok": True, "message": "已请求取消，当前候选人完成后停止。"}


@route("GET", "/api/runs/:rid/events", "run.read")
def h_run_events(ctx):
    """断线重放：SSE 连不上时前端可退化为轮询。"""
    after = int(ctx["query"].get("after") or 0)
    rid = ctx["params"]["rid"]
    return {"ok": True, "events": runs.events_since(rid, after),
            "run": runs.get_run(rid)}


@route("POST", "/api/jobs/:jid/estimate", "run.read")
def h_estimate(ctx):
    job_id = ctx["params"]["jid"]
    cands = store.list_candidates(job_id, with_text=False)
    ids = ctx["body"].get("candidate_ids")
    todo = [c for c in cands if (not ids or c["id"] in ids) and c.get("status") != "done"]
    api = _model_api(ctx)
    est = cost_estimate.estimate(len(todo), provider=api.get("provider") or "",
                                 model=api.get("model"), job_id=job_id)
    rem = cost_estimate.remaining_credits()
    if rem and rem.get("total_remain") is not None and est.get("estimated_credits"):
        est["remaining_before"] = rem["total_remain"]
        est["remaining_after"] = round(rem["total_remain"] - est["estimated_credits"], 2)
        est["enough"] = est["remaining_after"] >= 0
    return {"ok": True, "estimate": est, "pending": len(todo),
            "actual": cost_estimate.actual_vs_estimate(job_id)}


# ---------------------------------------------------------------------------
# 面试追问
# ---------------------------------------------------------------------------
@route("POST", "/api/candidates/:cid/interview", "candidate.write")
def h_interview(ctx):
    import interview
    cand = store.get_candidate(ctx["params"]["cid"])
    if not cand:
        raise ApiError("候选人不存在", 404)
    if not cand.get("result"):
        raise ApiError("这位候选人还没有评审结果，先跑一次评审。", 400)
    job = store.get_job(cand["job_id"])
    out = interview.generate_questions(cand, job, api=_model_api(ctx),
                                       job_id=cand["job_id"])
    store.update_candidate(cand["id"], {"interview": out}, actor=ctx["user"]["id"])
    if ctx["user"]:
        conn = db.connect()
        with db.tx(conn) as c:
            audit.record(c, "interview.generate", actor=ctx["user"]["id"],
                         target_type="candidate", target_id=cand["id"],
                         detail={"questions": len(out["questions"])})
    return {"ok": True, "interview": out}


# ---------------------------------------------------------------------------
# 面试录音：说话人归属与问答配对（P1）
# ---------------------------------------------------------------------------
def _interview_questions(cand):
    """从候选人记录取面试通知的问题清单（interview.py 的产出）。"""
    iv = cand.get("interview")
    if not isinstance(iv, dict):
        return []
    qs = iv.get("questions")
    return qs if isinstance(qs, list) else []


@route("POST", "/api/candidates/:cid/diarize", "candidate.write")
def h_diarize(ctx):
    import diarization
    cand = store.get_candidate(ctx["params"]["cid"])
    if not cand:
        raise ApiError("候选人不存在", 404)
    segs = store.get_segments(cand["id"])
    if not segs:
        # 没有 ASR 段：粘贴的转写稿切伪段（层 1 认得"面试官："前缀）。
        text = (cand.get("transcript_text") or "").strip()
        if not text:
            raise ApiError("这位候选人没有面试转写稿——先上传录音或粘贴转写稿。", 400)
        segs = diarization.segments_from_text(text)
        if not segs:
            raise ApiError("转写稿切不出任何段落（可能是空内容）。", 400)
    try:
        out = diarization.attribute(segs, questions=_interview_questions(cand),
                                    api=_model_api(ctx))
    except Exception as e:
        # 模型失败如实上抛——不静默退回"全 unassigned"：
        # 那会让覆盖率谎报成 0 而不是一次明确的失败。
        raise ApiError("说话人归属失败：%s" % e, 502)
    saved = store.save_segments(cand["id"], cand["job_id"], out["segments"])
    if ctx["user"]:
        conn = db.connect()
        with db.tx(conn) as c:
            audit.record(c, "interview.diarize", actor=ctx["user"]["id"],
                         target_type="candidate", target_id=cand["id"],
                         detail={"segments": saved["saved"],
                                 "manual_kept": saved["manual_kept"],
                                 "coverage": out["stats"]["coverage"]})
    return {"ok": True, "segments": out["segments"], "stats": out["stats"],
            "saved": saved}


@route("GET", "/api/candidates/:cid/diarization", "candidate.read")
def h_diarization(ctx):
    import diarization
    cand = store.get_candidate(ctx["params"]["cid"], with_text=False)
    if not cand:
        raise ApiError("候选人不存在", 404)
    segs = store.get_segments(cand["id"])
    return {"ok": True, "segments": segs,
            "stats": diarization.stats_of(segs) if segs else None}


@route("PUT", "/api/candidates/:cid/segments/:idx", "candidate.write")
def h_segment_correct(ctx):
    cand = store.get_candidate(ctx["params"]["cid"], with_text=False)
    if not cand:
        raise ApiError("候选人不存在", 404)
    body = ctx["body"] or {}
    try:
        store.update_segment(cand["id"], ctx["params"]["idx"], body)
    except ValueError as e:
        raise ApiError(str(e), 400)
    except KeyError:
        raise ApiError("段不存在：idx=%s" % ctx["params"]["idx"], 404)
    if ctx["user"]:
        conn = db.connect()
        with db.tx(conn) as c:
            audit.record(c, "segment.correct", actor=ctx["user"]["id"],
                         target_type="candidate", target_id=cand["id"],
                         detail={"idx": ctx["params"]["idx"],
                                 "speaker": (body.get("speaker") or "").strip(),
                                 "asker": (body.get("asker") or "").strip()[:40]})
    return {"ok": True}


@route("GET", "/api/candidates/:cid/qa-pairing", "candidate.read")
def h_qa_pairing(ctx):
    import interview_analysis
    cand = store.get_candidate(ctx["params"]["cid"], with_text=False)
    if not cand:
        raise ApiError("候选人不存在", 404)
    segs = store.get_segments(cand["id"])
    if not segs:
        raise ApiError("还没有转写段——先上传录音并跑一次说话人归属。", 400)
    out = interview_analysis.pair_qa(segs, cand.get("interview"))
    return {"ok": True, "pairs": out["pairs"], "stats": out["stats"]}


# ---------------------------------------------------------------------------
# 人才库
# ---------------------------------------------------------------------------
@route("GET", "/api/talent", "talent.read")
def h_talent(ctx):
    q = ctx["query"]
    rows = talent.query(keyword=q.get("q"), grade=q.get("grade"),
                        status=q.get("status"), tag=q.get("tag"),
                        job_id=q.get("job_id"),
                        only_evaluated=q.get("evaluated") == "1",
                        pending_identity=q.get("pending") == "1")
    return {"ok": True, "people": rows, "stats": talent.stats(),
            "statuses": [{"id": k, "label": v} for k, v in talent.TALENT_STATUSES],
            "pending_identity": talent.pending_identity_list()}


@route("GET", "/api/talent/:pid", "talent.read")
def h_talent_person(ctx):
    pid = ctx["params"]["pid"]
    p = talent.get(pid)
    if not p:
        raise ApiError("人才库里没有这个人", 404)
    p["contact"] = talent.get_contact(pid)
    p["outcomes"] = talent.outcomes_for(pid)
    p["latest_stage"] = outcome.latest_stage(pid)
    return {"ok": True, "person": p,
            "stages": [{"id": k, "label": v} for k, v in outcome.STAGES],
            "positive": list(outcome.POSITIVE)}


@route("PUT", "/api/talent/:pid", "talent.write")
def h_talent_update(ctx):
    p = talent.update_person(ctx["params"]["pid"], ctx["body"], actor=ctx["user"]["id"])
    return {"ok": True, "person": p}


@route("DELETE", "/api/talent/:pid", "talent.write")
def h_talent_delete(ctx):
    return {"ok": talent.delete_person(ctx["params"]["pid"], actor=ctx["user"]["id"])}


@route("POST", "/api/talent/:pid/import", "talent.write")
def h_talent_import(ctx):
    ok, msg, cand = talent.import_to_job(ctx["params"]["pid"],
                                         ctx["body"].get("job_id") or "",
                                         actor=ctx["user"]["id"])
    if not ok:
        raise ApiError(msg, 400)
    return {"ok": True, "message": msg, "candidate": cand}


# ---------------------------------------------------------------------------
# 结果回填 / 校准 / 漂移
# ---------------------------------------------------------------------------
@route("GET", "/api/outcomes", "outcome.read")
def h_outcomes(ctx):
    return {"ok": True, "recent": outcome.recent(50, ctx["query"].get("job_id")),
            "stages": [{"id": k, "label": v} for k, v in outcome.STAGES],
            "positive": list(outcome.POSITIVE)}


@route("POST", "/api/talent/:pid/outcome", "outcome.write")
def h_outcome_record(ctx):
    b = ctx["body"]
    o = outcome.record(ctx["params"]["pid"], b.get("stage") or "",
                       note=b.get("note") or "", job_id=b.get("job_id") or "",
                       candidate_id=b.get("candidate_id") or "",
                       actor=ctx["user"]["id"])
    return {"ok": True, "outcome": o,
            "latest_stage": outcome.latest_stage(ctx["params"]["pid"])}


@route("DELETE", "/api/outcomes/:oid", "outcome.write")
def h_outcome_delete(ctx):
    return {"ok": outcome.delete(ctx["params"]["oid"], actor=ctx["user"]["id"])}


@route("GET", "/api/analytics/hitrate", "outcome.read")
def h_hitrate(ctx):
    days = ctx["query"].get("days")
    return {"ok": True, "report": outcome.hit_rate(
        job_id=ctx["query"].get("job_id"),
        since_days=int(days) if days else None)}


@route("GET", "/api/analytics/drift", "outcome.read")
def h_drift(ctx):
    return {"ok": True, "report": outcome.drift()}


@route("GET", "/api/calibration", "calibration.read")
def h_calibration(ctx):
    sid = ctx["query"].get("set_id")
    return {"ok": True, "sets": outcome.list_sets(),
            "report": outcome.calibration_report(sid),
            "grades": ["A", "B", "C"]}


@route("POST", "/api/calibration/sets", "calibration.write")
def h_calib_set(ctx):
    b = ctx["body"]
    return {"ok": True, "set": outcome.create_set(b.get("name") or "标定集",
                                                  b.get("description") or "")}


@route("POST", "/api/calibration/items", "calibration.write")
def h_calib_item(ctx):
    b = ctx["body"]
    item = outcome.add_item(b.get("set_id") or "", b.get("human_label") or "",
                            human_score=b.get("human_score"),
                            candidate_id=b.get("candidate_id") or "",
                            note=b.get("note") or "", actor=ctx["user"]["id"])
    if ctx["user"]:
        conn = db.connect()
        with db.tx(conn) as c:
            audit.record(c, "calibration.record", actor=ctx["user"]["id"],
                         target_type="candidate", target_id=b.get("candidate_id") or "",
                         detail={"label": b.get("human_label")})
    return {"ok": True, "item": item}


# ---------------------------------------------------------------------------
# 用量 / 审计 / 隐私
# ---------------------------------------------------------------------------
@route("GET", "/api/usage", "usage.read")
def h_usage(ctx):
    job_id = ctx["query"].get("job_id")
    return {"ok": True, "summary": usage.summary(job_id=job_id),
            "per_candidate": usage.per_candidate(job_id) if job_id else [],
            "recent": usage.recent(80)}


@route("GET", "/api/audit", "audit.read")
def h_audit(ctx):
    conn = db.connect()
    return {"ok": True,
            "stats": audit.stats(conn),
            "verify": audit.verify_chain(conn),
            "actions": list(audit.ACTIONS),
            "rows": audit.query(conn, limit=int(ctx["query"].get("limit") or 100),
                                action=ctx["query"].get("action"),
                                actor=ctx["query"].get("actor"),
                                target_id=ctx["query"].get("target_id"))}


@route("GET", "/api/privacy", "settings.write")
def h_privacy(ctx):
    return {"ok": True, "overview": privacy.overview(),
            "retention": privacy.retention_status()}


@route("POST", "/api/privacy/purge", "settings.write")
def h_privacy_purge(ctx):
    dry = bool(ctx["body"].get("dry_run", True))
    res = privacy.purge_expired(dry_run=dry, actor=ctx["user"]["id"])
    return {"ok": True, "result": res}


@route("POST", "/api/privacy/rotate", "settings.write")
def h_privacy_rotate(ctx):
    return {"ok": True, "result": privacy.rotate_key(actor=ctx["user"]["id"])}


@route("GET", "/api/privacy/export/:pid", "settings.write")
def h_privacy_export(ctx):
    """导出某个人的全部数据。admin 才能做，且不缓存。"""
    data = privacy.export_person(ctx["params"]["pid"], actor=ctx["user"]["id"])
    return {"ok": True, "export": data,
            "warning": "导出物含明文个人信息，请按内部数据管理规定保存，勿随意转发。"}


@route("POST", "/api/privacy/erase/:pid", "settings.write")
def h_privacy_erase(ctx):
    res = privacy.erase_person(ctx["params"]["pid"], actor=ctx["user"]["id"])
    return {"ok": True, "result": res}


@route("GET", "/api/privacy/purge", "settings.write")
def h_privacy_purge_preview(ctx):
    """预览清理：先给 HR 看"会删多少"，再让人确认。

    不提供"一键直接删"——不可逆操作必须先看见影响面。
    """
    return {"ok": True, "preview": privacy.purge_expired(dry_run=True,
                                                         actor=ctx["user"]["id"])}


# ---------------------------------------------------------------------------
# 身份比对自检（给人看的，不参与业务）
# ---------------------------------------------------------------------------
@route("POST", "/api/identity/compare", "talent.read")
def h_identity_compare(ctx):
    """手工比对两份材料是否为同一人——用于解释系统的判断依据。

    这是可解释性要求（PIPL 第 24 条）的一部分：HR 必须能看到
    "为什么系统认为是同一个人"，并能否决它。
    """
    b = ctx["body"]
    a = identity.extract_profile(b.get("name_a") or "", b.get("text_a") or "", "")
    c = identity.extract_profile(b.get("name_b") or "", b.get("text_b") or "", "")
    cmp = identity.compare_profiles(a, c)
    return {"ok": True, "result": cmp, "profile_a": identity.signature(a),
            "profile_b": identity.signature(c)}

# ---------------------------------------------------------------------------
# 回测对齐：把 HR 的真实决定变成标准答案，回答"它排的前 N 名有几个是我真会推的"
# ---------------------------------------------------------------------------
@route("POST", "/api/jobs/:jid/align", "align.write")
def h_align_create(ctx):
    """为一个岗位建对齐批次。"""
    b = ctx["body"]
    jid = ctx["params"]["jid"]
    out = align.create_batch(
        jid,
        label=b.get("label") or "",
        source=b.get("source") or "manual",
        note=b.get("note") or "",
        actor=(ctx.get("user") or {}).get("username") or "")
    audit.record(db.connect(), "align.create",
                 actor=(ctx.get("user") or {}).get("username") or "system",
                 target_type="job", target_id=jid,
                 detail={"align_id": out["id"], "label": out["label"]})
    return {"ok": True, "batch": out}


@route("GET", "/api/jobs/:jid/align", "align.read")
def h_align_list(ctx):
    """列出该岗位的对齐批次。"""
    jid = ctx["params"]["jid"]
    return {"ok": True, "batches": align.list_batches(jid),
            "decisions": align.decisions_of(jid)}


@route("GET", "/api/align/:aid", "align.read")
def h_align_get(ctx):
    d = align.get_batch(ctx["params"]["aid"])
    if not d:
        raise ApiError("对齐批次不存在", 404)
    return {"ok": True, "batch": d}


@route("POST", "/api/align/preview", "align.write")
def h_align_preview(ctx):
    """预览 CSV 解析结果：不落库，也不要求先有批次。

    早先把它挂在 /api/align/:aid/preview 上，前端为了预览得先建一个批次，
    结果用户点一次"预览"就多出一个空批次，再导入又多一个。
    解析 CSV 跟批次本来没关系，单独一条路更干净。
    """
    text = ctx["body"].get("text") or ""
    rows, errors = align.parse_csv(text)
    summary = {"advance": 0, "reject": 0, "hold": 0}
    for r in rows:
        summary[r["decision"]] = summary.get(r["decision"], 0) + 1
    return {"ok": True, "rows": rows[:200], "errors": errors,
            "summary": summary, "count": len(rows)}


@route("POST", "/api/align/:aid/import", "align.write")
def h_align_import(ctx):
    """导入真实决定。"""
    aid = ctx["params"]["aid"]
    b = ctx["body"]
    if b.get("text"):
        rows, errors = align.parse_csv(b.get("text"))
        if errors and not rows:
            raise ApiError("；".join(e["error"] for e in errors), 400)
    else:
        rows, errors = b.get("rows") or [], []
    if not rows:
        raise ApiError("没有可导入的行", 400)
    actor = (ctx.get("user") or {}).get("username") or ""
    res = align.import_truth(aid, rows, actor=actor)
    audit.record(db.connect(), "align.import", actor=actor or "system",
                 target_type="align", target_id=aid,
                 detail={"added": res["added"], "updated": res["updated"],
                         "ambiguous": len(res["ambiguous"]),
                         "unmatched": len(res["unmatched"])})
    return {"ok": True, "result": res, "errors": errors}


@route("GET", "/api/align/:aid/report", "align.read")
def h_align_report(ctx):
    """核心：precision@k / recall@k / lift。样本不足时只报不足，不给数字。"""
    aid = ctx["params"]["aid"]
    ks = None
    raw = (ctx.get("query") or {}).get("ks")
    if raw:
        try:
            ks = [int(x) for x in str(raw).split(",") if x.strip()]
        except ValueError:
            raise ApiError("ks 参数应为逗号分隔的整数，如 ks=5,10,20", 400)
    try:
        rep = align.report(aid, ks=ks)
    except ValueError as e:
        raise ApiError(str(e), 404)
    return {"ok": True, "report": rep}


@route("GET", "/api/align/:aid/pending", "align.read")
def h_align_pending(ctx):
    """还没绑到具体候选人的真实决定（同名 / 对不上人）。

    单独给一个接口，是因为这些行**不参与**命中率计算。
    如果只在报告里给个数字，用户没法把它们接走；
    而"被拒的那批"往往正是过滤器最需要被检验的部分。
    """
    aid = ctx["params"]["aid"]
    try:
        return {"ok": True, "pending": align.pending_bindings(aid)}
    except ValueError as e:
        raise ApiError(str(e), 404)


@route("POST", "/api/align/:aid/bind", "align.write")
def h_align_bind(ctx):
    """把一个真实决定手工绑到指定候选人（同名时的唯一出口）。"""
    aid = ctx["params"]["aid"]
    b = ctx.get("body") or {}
    truth_id = (b.get("truth_id") or "").strip()
    candidate_id = (b.get("candidate_id") or "").strip()
    if not truth_id or not candidate_id:
        raise ApiError("需要 truth_id 与 candidate_id", 400)
    actor = (ctx.get("user") or {}).get("username") or "system"
    try:
        return {"ok": True, "bind": align.bind_truth(truth_id, candidate_id,
                                                     actor=actor)}
    except ValueError as e:
        raise ApiError(str(e), 400)


@route("POST", "/api/align/:aid/delete", "align.write")
def h_align_delete(ctx):
    aid = ctx["params"]["aid"]
    conn = db.connect()
    with db.tx(conn) as c:
        n = c.execute("DELETE FROM align_runs WHERE id=?", (aid,)).rowcount
    if not n:
        raise ApiError("对齐批次不存在", 404)
    audit.record(conn, "align.delete",
                 actor=(ctx.get("user") or {}).get("username") or "system",
                 target_type="align", target_id=aid, detail={})
    return {"ok": True, "deleted": n}


# ---------------------------------------------------------------------------
# 候选人级决策：人做的决定，必须带理由
# ---------------------------------------------------------------------------
@route("POST", "/api/candidates/:cid/decide", "align.write")
def h_candidate_decide(ctx):
    """记录"推进 / 淘汰 / 待定"。理由必填。

    这是把系统从"自动化决策工具"拉回"辅助工具"的那颗螺丝：
    只要淘汰是人做的且理由被留下，系统就没有替任何人做决定。
    """
    b = ctx["body"]
    actor = (ctx.get("user") or {}).get("username") or ""
    try:
        out = align.decide(ctx["params"]["cid"], b.get("decision") or "",
                           b.get("reason") or "", actor=actor)
    except ValueError as e:
        raise ApiError(str(e), 400)
    return {"ok": True, "decision": out}


# ---------------------------------------------------------------------------
# 评审历史与波动
# ---------------------------------------------------------------------------
@route("GET", "/api/candidates/:cid/history", "candidate.read")
def h_candidate_history(ctx):
    """某个候选人的历次评审，以及"同一份材料重复评有多不稳定"。

    这是可复现性问题的直接证据：如果同一个人的分数在几次重跑之间
    跨了档位，那"他是 A 级"这个结论就不该被当成事实使用。
    """
    cid = ctx["params"]["cid"]
    if not store.get_candidate(cid, with_text=False):
        raise ApiError("候选人不存在", 404)
    return {"ok": True,
            "evaluations": store.evaluations_of(cid),
            "drift": store.evaluation_drift(cid)}


@route("GET", "/api/rubric", "job.read")
def h_rubric(ctx):
    """当前评分口径：版本号、各组成部分、提示词指纹。

    暴露这个，是为了让"为什么版本变了"可被回答。只给一个
    `rubric_version` 字符串的话，用户看到版本变了也不知道是改了权重、
    换了模型、还是动了一句提示词——于是只能选择忽略它。
    """
    import rubric
    api = _model_api(ctx)
    d = rubric.describe(model=api.get("model"), provider=api.get("provider"))
    d["concurrency"] = llm_client.concurrency_snapshot()
    return {"ok": True, "rubric": d}


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
@route("GET", "/api/backups", "settings.write")
def h_backup_list(ctx):
    """列出本机备份。备份目录里的数据仍是敏感数据（含明文姓名）。"""
    return {"ok": True, "files": backup.list_files(),
            "dir": backup.backup_dir(),
            "note": "备份包含明文姓名、决策理由与审计记录，请按敏感数据保管。"
                    + backup.MANUAL_COPY_WARNING}


@route("POST", "/api/backups/create", "settings.write")
def h_backup_create(ctx):
    b = ctx.get("body") or {}
    keep = b.get("keep")
    actor = (ctx.get("user") or {}).get("username") or "system"
    out = backup.create(keep=int(keep) if keep else backup.DEFAULT_KEEP,
                        actor=actor, note=(b.get("note") or "")[:200])
    if not out.get("ok"):
        raise ApiError(out.get("error") or "备份失败", 500)
    return {"ok": True, "backup": out}


@route("POST", "/api/backups/restore", "settings.write")
def h_backup_restore(ctx):
    """从备份恢复。破坏性操作，必须显式 confirm。"""
    b = ctx.get("body") or {}
    if not b.get("confirm"):
        raise ApiError("恢复会覆盖当前数据库，需要 confirm=true", 400)
    actor = (ctx.get("user") or {}).get("username") or "system"
    path = (b.get("file") or "").strip()
    if not path:
        raise ApiError("需要 file（备份文件名）", 400)
    full = os.path.join(backup.backup_dir(), os.path.basename(path))
    try:
        out = backup.restore(full, actor=actor, confirm=True)
    except ValueError as e:
        raise ApiError(str(e), 400)
    return {"ok": True, "restore": out}
