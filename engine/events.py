"""事件的规范化字段：`stage` / `duration_ms` / `error_type`。

为什么需要这一层
----------------
前端和日志里现在是一堆具体事件名（`agent_start`、`judge_scored`、`candidate_failed`…），
它们描述"发生了什么"，但不方便回答"跑到哪一步了、卡了多久、为什么失败"。
一旦要做事后复盘（"这批 20 人里 3 个失败，都是什么原因"），
就得靠人去读每一条的 `type` 和 `error` 文案。

这里**不改动 `type`**（前端和已经落库的历史事件都不受影响），
只给每个事件附加三个稳定字段：

    stage        归一化的阶段枚举（queued/debate/adjudication/judge/persisting/
                 completed/failed/cancelled）
    duration_ms  该候选人该阶段的耗时；没有明确的起止点时为 None
    error_type   失败原因的分类（auth/rate_limit/network/parse/config/internal）

耗时怎么算
----------
`runs.emit` 是逐条调用的，跨候选人并发，所以计时状态按
`(run_id, candidate_id, stage)` 记账，放在进程内字典里，run 结束时清掉。
进程重启会丢在跑的计时状态——这是可接受的：重启后那些 run 本来就作废了
（`runs.reap_stale()` 会把它们标成中断）。**不为了这个去写库**，
因为每来一条事件就写一次计时表，会把写入放大一倍。

错误分类宁可粗糙也不要猜
------------------------
分类只依据异常类型与已知的错误文案特征；认不出来就归到 `internal`，
并把原始文案保留在 `error` 里。**绝不为了"看起来整齐"而把未知错误
塞进某个具体类别**——那会让复盘时把环境问题当成代码问题。
"""
import threading
import time

# 具体 agent key -> 归一化 stage
_STAGE_BY_KEY = {
    "pro_open": "debate",
    "con_open": "debate",
    "adjudication": "adjudication",
}
# 事件 type -> 归一化 stage（agent 事件之外的）
_STAGE_BY_TYPE = {
    "candidate_queue": "queued",
    "candidate_start": "persisting",
    "candidate_done": "completed",
    "candidate_failed": "failed",
    "run_done": "completed",
    "run_cancelled": "cancelled",
    "cancelling": "cancelled",
}

# 这些 stage 表示"候选人已经收尾"，可以顺手清掉它的计时状态
_TERMINAL = ("completed", "failed", "cancelled")

_lock = threading.Lock()
# (run_id, candidate_id, stage) -> 首次出现的时间戳
_open_since = {}


def stage_of(event):
    """把一条事件映射到归一化阶段。认不出来的返回 None，而不是瞎猜一个。"""
    key = event.get("key") or ""
    if key in _STAGE_BY_KEY:
        return _STAGE_BY_KEY[key]
    # 互驳回合的 key 带序号：pro_rebut_1 / con_rebut_2
    if key.startswith("pro_rebut") or key.startswith("con_rebut"):
        return "debate"
    if key.startswith("judge"):
        return "judge"
    etype = event.get("type") or ""
    if etype in _STAGE_BY_TYPE:
        return _STAGE_BY_TYPE[etype]
    if etype in ("agent_start", "agent_message", "agent_delta"):
        # 上面没命中说明是新加的 agent，归到 debate 会误导，留空更诚实
        return None
    return None


def classify_error(err):
    """把错误分类。只认明确的信号，认不出来归 internal。

    判定顺序是有讲究的：**auth 要在 config 之前**。
    "invalid api key" 与 "未配置 API Key" 都含 "api key"，
    但前者是凭证被拒、后者是压根没填——处理动作完全不同（换个 key vs 去填一个）。
    所以 auth 先吃掉 "invalid/unauthorized"，config 只认"未配置/缺少"。
    """
    if not err:
        return None
    e = err
    name = type(e).__name__.lower()
    text = (str(e) or "").lower()

    def has(*words):
        return any(w in text for w in words)

    # 顺序即优先级，改动前先想清楚谁该先被认领
    if "insufficientcredit" in name or has("积分不足", "credit"):
        return "config"
    if "ratelimit" in name or has("rate limit", "too many requests", "429", "quota"):
        return "rate_limit"
    if "auth" in name or has("401", "403", "unauthorized", "invalid api key",
                             "permission denied", "会话已失效", "鉴权"):
        return "auth"
    if "llmconfig" in name or has("未配置", "没有配置", "缺少凭证", "not configured",
                                  "请在「模型设置」"):
        return "config"
    if ("timeout" in name or has("timed out", "超时", "connection", "网络",
                                 "network", "10060", "10061", "ssl",
                                 "reset by peer", "urlopen")):
        return "network"
    if ("jsondecode" in name
            or has("不是 json", "json 对象", "缺少维度", "解析失败", "无法解析",
                   "parse error", "不是数字")):
        return "parse"
    return "internal"


def annotate(run_id, event):
    """给一条事件补上 stage / duration_ms / error_type（就地返回新 dict）。"""
    out = dict(event)
    stage = stage_of(event)
    if stage:
        out["stage"] = stage

    # 失败事件一律带上分类，哪怕原事件没写 error
    if event.get("type") in ("candidate_failed", "error") or event.get("error"):
        out["error_type"] = classify_error(event.get("error"))

    cand_id = event.get("candidate_id") or ""
    now = time.time()

    if not stage or not cand_id:
        return out

    key = (run_id, cand_id, stage)
    with _lock:
        started = _open_since.get(key)
        if started is None:
            _open_since[key] = now
            out["duration_ms"] = None       # 首次出现 = 阶段开始
        else:
            out["duration_ms"] = int((now - started) * 1000)

        if stage in _TERMINAL:
            # 候选人收尾：清掉这个人的所有计时
            for k in [k for k in _open_since
                      if k[0] == run_id and k[1] == cand_id]:
                _open_since.pop(k, None)
    return out


def clear(run_id=None):
    """清理计时状态。run_id 为空时清全部（测试/退出时用）。"""
    with _lock:
        if run_id is None:
            _open_since.clear()
        else:
            for k in [k for k in _open_since if k[0] == run_id]:
                _open_since.pop(k, None)


def snapshot():
    """未收尾的阶段计数（诊断用）。"""
    with _lock:
        return dict(_open_since)
