"""同名候选人靠 ID 区分事件：并发评审时两个「张伟」的气泡不能互相覆盖。

为什么单独测这一条
------------------
`identity` 模块专门处理同名候选人，`align` 报告层甚至拒绝按姓名回退匹配
（宁可少算也不猜）。存储层一直按 candidate_id 走，但**事件层**曾经只带姓名，
于是直播间的气泡定位键是「姓名::agent key」——同一个岗位下两个「张伟」
并发评审时，后一个人的 pro_open 会覆盖前一个人的气泡。

分数不会错（那是按 id 存的），但人看到的过程是错的。这类"数据对、展示错"
的 bug 最难在事后发现，所以这里从事件字段一路测到前端定位键。
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "engine")
sys.path.insert(0, ENGINE)

TMP = tempfile.mkdtemp()
import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "same.db")
db.reset_init_flag()
import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

import agents
import llm_client
import store

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


print("--- 1. 每个 agent 事件都带 candidate_id ---")
EV = []


def fake_stream(messages, model, api_key, base_url, temperature, use_json,
                timeout, on_delta):
    text = "辩" * 40
    for ch in text[:10]:
        on_delta(ch)
    return text, {"total_tokens": 5}


def fake_raw(messages, model, api_key, base_url, temperature, use_json, timeout):
    from util import DIMENSIONS as D
    sysmsg = messages[0].get("content") or ""
    if "评委" in sysmsg:
        return (json.dumps({D[0]: 8.0, D[1]: 8.0, D[2]: 8.0, "comment": "「原文」"}),
                {"total_tokens": 5})
    return (json.dumps({"pro_evidence": ["「原文」"], "con_challenges": ["「原文」"],
                        "disputes": ["争议"], "excluded": []}), {"total_tokens": 5})


import _llmstub
_llmstub.install(fake_raw)
llm_client._stream_raw = fake_stream
try:
    job = store.create_job("同名岗", {"requirements": []})
    # 两个同名候选人：这是本项目明确支持、且必须靠 ID 区分的情形
    a = store.add_candidate(job["id"], "张伟", resume_text="清华大学 本科\n字节跳动",
                            actor="t")
    b = store.add_candidate(job["id"], "张伟", resume_text="北京大学 硕士\n腾讯",
                            actor="t")
    check("两条记录姓名相同", a["name"] == b["name"] == "张伟", (a["name"], b["name"]))
    check("两条记录 ID 不同", a["id"] != b["id"], (a["id"], b["id"]))

    ej = {"id": job["id"], "title": "岗位", "requirements": [], "preferred": [],
          "red_flags": [], "weights": None, "rubric": None}
    for cand in (a, b):
        c = {"id": cand["id"], "job_id": job["id"], "name": cand["name"],
             "resume_text": "某校 本科\n某公司 3 年",
             "assessment_text": "", "transcript_text": "", "job": ej}
        agents.run_candidate(c, ej, api={"model": "m", "provider": "openai",
                                         "api_key": "k"},
                             on_event=EV.append, repeats=1)
finally:
    _llmstub.restore(fake_raw, fake_stream)

agent_events = [e for e in EV if e.get("type", "").startswith("agent_")]
check("产生了 agent 事件", len(agent_events) > 0, len(agent_events))
missing = [e.get("type") for e in agent_events if not e.get("candidate_id")]
check("所有 agent 事件都带 candidate_id", not missing,
      sorted(set(missing)))

ids_seen = {e.get("candidate_id") for e in agent_events}
check("两条同名候选人各自产生了事件", {a["id"], b["id"]} <= ids_seen, ids_seen)

print("\n--- 2. 增量事件也带 candidate_id ---")
deltas = [e for e in EV if e.get("type") == "agent_delta"]
check("有增量事件", len(deltas) > 0, len(deltas))
check("增量都带 candidate_id",
      all(d.get("candidate_id") for d in deltas), deltas[:1])
delta_ids = {d.get("candidate_id") for d in deltas}
check("两条候选人的增量能区分开", {a["id"], b["id"]} <= delta_ids, delta_ids)

print("\n--- 3. 前端的定位键必须按 ID，不能按姓名 ---")
app_js = open(os.path.join(ENGINE, "..", "viewer", "app.js"),
              encoding="utf-8").read()
m = re.search(r"function bubbleKey\(ev\) \{(.+?)\n\}", app_js, re.S)
check("找到 bubbleKey", m is not None)
body = m.group(1) if m else ""
check("bubbleKey 使用了 candidate_id", "candidate_id" in body, body[:120])
check("bubbleKey 保留姓名作为回退", "ev.candidate" in body, body[:120])

print("\n--- 4. 端到端推演：两个同名者的同一 agent key 不会撞 ---")


def bubble_key(ev):
    """与 app.js 里 bubbleKey 同构的实现，用来验证语义。"""
    who = ev.get("candidate_id") or ev.get("candidate") or ""
    return who + "::" + (ev.get("key") or "")


ev_a = next(e for e in agent_events
            if e.get("candidate_id") == a["id"] and e.get("key") == "pro_open")
ev_b = next(e for e in agent_events
            if e.get("candidate_id") == b["id"] and e.get("key") == "pro_open")
check("同名同 key 的两条事件，定位键不同",
      bubble_key(ev_a) != bubble_key(ev_b),
      (bubble_key(ev_a), bubble_key(ev_b)))
check("定位键里带的是 ID 而不是姓名",
      a["id"] in bubble_key(ev_a) and "张伟" not in bubble_key(ev_a),
      bubble_key(ev_a))

print("\n--- 5. 旧事件（无 candidate_id）仍能定位，不丢气泡 ---")
legacy = {"type": "agent_message", "candidate": "张伟", "key": "pro_open",
          "role": "pro", "text": "旧数据"}
check("旧事件回退到姓名", bubble_key(legacy) == "张伟::pro_open",
      bubble_key(legacy))

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
