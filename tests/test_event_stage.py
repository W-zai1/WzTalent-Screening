"""事件归一化字段：stage / duration_ms / error_type。

doc1 §4.1 要求"能回答每份简历处于什么阶段、耗时多少、失败原因是什么"。
这些信息原来散在 `type` 与 `error` 的自由文本里，复盘时要靠人读。

这里守三条：
  · 归一化是**附加**的，不改原有 `type`（前端与历史事件不受影响）
  · 认不出来的阶段返回 None，不瞎归类
  · 错误的分类宁可粗糙也不猜：认不出来就是 internal，原始文案保留在 error 里
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

TMP = tempfile.mkdtemp()
import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "ev.db")
db.reset_init_flag()
import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

import events
import llm_client

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


print("--- 1. stage 映射 ---")
cases = [
    ({"type": "agent_start", "key": "pro_open"}, "debate"),
    ({"type": "agent_message", "key": "con_open"}, "debate"),
    ({"type": "agent_message", "key": "pro_rebut_1"}, "debate"),
    ({"type": "agent_message", "key": "con_rebut_2"}, "debate"),
    ({"type": "agent_start", "key": "adjudication"}, "adjudication"),
    ({"type": "agent_message", "key": "judge_1"}, "judge"),
    ({"type": "agent_message", "key": "judge_3"}, "judge"),
    ({"type": "candidate_queue"}, "queued"),
    ({"type": "candidate_done"}, "completed"),
    ({"type": "candidate_failed"}, "failed"),
    ({"type": "run_cancelled"}, "cancelled"),
]
bad = [(e, events.stage_of(e), want) for e, want in cases
       if events.stage_of(e) != want]
check("常见事件都能映射到 stage", not bad, bad)

check("认不出来的 agent 不瞎归类（返回 None）",
      events.stage_of({"type": "agent_message", "key": "brand_new_agent"}) is None,
      events.stage_of({"type": "agent_message", "key": "brand_new_agent"}))
check("完全未知的事件返回 None", events.stage_of({"type": "whatever"}) is None)

print("\n--- 2. duration_ms：首次为 None，重复出现给出耗时 ---")
events.clear()
ev = {"type": "agent_start", "candidate_id": "c1", "key": "adjudication"}
first = events.annotate("r1", ev)
check("阶段首次出现时 duration_ms 为 None", first["duration_ms"] is None,
      first["duration_ms"])
time.sleep(0.05)
second = events.annotate("r1", ev)
check("再次出现时给出耗时", isinstance(second["duration_ms"], int)
      and second["duration_ms"] >= 40, second["duration_ms"])

other = events.annotate("r1", {"type": "agent_start", "candidate_id": "c2",
                               "key": "adjudication"})
check("不同候选人的计时互不干扰", other["duration_ms"] is None, other["duration_ms"])

print("\n--- 3. 收尾后清掉计时，下一轮重新开始 ---")
events.annotate("r1", {"type": "candidate_done", "candidate_id": "c1"})
check("candidate_done 带 stage=completed",
      events.annotate("r1", {"type": "candidate_done",
                             "candidate_id": "c9"})["stage"] == "completed")
time.sleep(0.03)
again = events.annotate("r1", {"type": "agent_start", "candidate_id": "c1",
                               "key": "adjudication"})
check("重跑同一个候选人时重新计时（不是接着旧值）",
      again["duration_ms"] is None, again["duration_ms"])

print("\n--- 4. error_type 分类 ---")
def cls(err):
    return events.classify_error(err)


class RuntimeError(Exception):
    pass


class TimeoutError_(Exception):
    pass


checks = [
    # 未配置 → config；被拒 → auth。两者都含 "api key"，靠"未配置/缺少"与
    # "invalid/401" 区分——处理动作不同：去填一个 vs 换一个。
    (RuntimeError("未配置模型凭证：请在「模型设置」里填入 API Key"), "config"),
    (RuntimeError("401 Unauthorized"), "auth"),
    (RuntimeError("invalid api key"), "auth"),
    (RuntimeError("429 Too Many Requests"), "rate_limit"),
    (RuntimeError("rate limit exceeded"), "rate_limit"),
    (RuntimeError("WorkBuddy 积分不足（HTTP 402）"), "config"),
    (RuntimeError("urlopen error [WinError 10060] 连接尝试失败"), "network"),
    (RuntimeError("timed out"), "network"),
    (ValueError("评委回复不是 JSON 对象"), "parse"),
    (ValueError("评委回复缺少维度「硬实力门槛」"), "parse"),
    (ValueError("维度「岗位能力匹配度」的分数不是数字：'abc'"), "parse"),
    (ValueError("bad"), "internal"),
    (RuntimeError("某个谁也没见过的错误"), "internal"),
    (None, None),
]
bad_cls = [(str(e), cls(e), want) for e, want in checks if cls(e) != want]
check("错误分类符合预期", not bad_cls, bad_cls)
check("缺凭证归 config 而不是 internal",
      cls(RuntimeError("未配置模型凭证")) == "config")

print("\n--- 5. annotate 不改原有字段 ---")
raw = {"type": "candidate_failed", "candidate": "张三", "candidate_id": "c1",
       "error": "429 Too Many Requests", "custom": "保留"}
out = events.annotate("r1", raw)
check("type 保持不变", out["type"] == "candidate_failed")
check("自定义字段保留", out.get("custom") == "保留")
check("补上了 stage", out.get("stage") == "failed", out.get("stage"))
check("补上了 error_type", out.get("error_type") == "rate_limit",
      out.get("error_type"))
check("原始 error 文案保留", out.get("error") == "429 Too Many Requests")
check("不修改传入的 dict", "stage" not in raw, list(raw))

print("\n--- 6. 落库后重放出来的事件也带这些字段 ---")
import runs
job = None
import store
job = store.create_job("事件岗", {"requirements": []})
run = runs.start(job["id"], ["c1"], actor="t", rubric_version="rTEST")
rid = run["id"]
events.clear()
runs.emit(rid, {"type": "agent_start", "candidate": "张三",
                "candidate_id": "c1", "key": "judge_1"})
runs.emit(rid, {"type": "candidate_failed", "candidate": "张三",
                "candidate_id": "c1", "error": "429 Too Many Requests"})
runs.finish(rid, "done", succeeded=0, failed=1, actor="t")
replayed = runs.events_since(rid, 0)
stages = [e.get("stage") for e in replayed if e.get("type") == "agent_start"]
check("重放的事件带 stage", stages == ["judge"], stages)
failed = [e for e in replayed if e.get("type") == "candidate_failed"]
check("重放的失败事件带 error_type",
      failed and failed[0].get("error_type") == "rate_limit",
      failed[0].get("error_type") if failed else None)
check("落库事件仍然带 seq 与 type", all(e.get("seq") for e in replayed),
      [e.get("seq") for e in replayed])

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
