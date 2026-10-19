"""P1 回归：说话人归属三层 + 问答配对 + 段持久化。

零依赖、不联网——LLM 调用打桩（`llm_client.complete`），
验证的是归属逻辑与配对逻辑，不是模型本身。

跑法：
    cd tests && python test_diarization.py
"""
import json
import os
import re
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["PORT"] = "8255"

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "engine"))

import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "diar.db")
db.reset_init_flag()
import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP

import diarization
import interview_analysis
import llm_client
import store

_REAL_COMPLETE = llm_client.complete

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  ← " + str(detail)) if (detail and not cond) else ""))


def section(t):
    print("\n" + "=" * 62)
    print(" " + t)
    print("=" * 62)


# ---------------------------------------------------------------- 测试桩
def install_stub(judge):
    """把 llm_client.complete 换成按段文本判归属的桩。

    judge(text) -> (speaker, confidence, reason, asker)
    """
    orig = llm_client.complete

    def stub(messages, **kw):
        user = messages[-1]["content"]
        items = []
        for m in re.finditer(r"【(\d+)】([^\n]+)", user):
            idx, txt = int(m.group(1)), m.group(2)
            try:
                sp, conf, reason, asker = judge(txt)
            except _Skip:
                continue          # 模型漏输出这一段
            items.append({"idx": idx, "speaker": sp, "confidence": conf,
                          "reason": reason, "asker": asker})
        return {"text": json.dumps({"segments": items}, ensure_ascii=False),
                "usage": {"total_tokens": 10}}

    llm_client.complete = stub
    return orig


def restore(orig):
    llm_client.complete = orig


# ---------------------------------------------------------------- fixture
# 面试通知问题清单（interview.py 的产出形状）
QUESTIONS = [
    {"q": "请讲讲你做过的高并发支付网关，峰值是多少？", "intent": "深挖", "why": "争议点：峰值数据"},
    {"q": "主从延迟那次故障，你具体改了什么？", "intent": "核实", "why": "被剔除论断"},
    {"q": "你带的团队有几个人，怎么分工？", "intent": "边界", "why": "带团队证据不足"},
    {"q": "为什么想离开上一家公司？", "intent": "风险", "why": "红线相关"},
    {"q": "你对我们公司有什么了解？", "intent": "核实", "why": "动机核验"},
]

# 双人对话（无前缀，真实 ASR 形态）。ground truth：
#   0/2/6/8 面试官照单提问（层 1 命中）；4/9/10 面试官即兴/过渡（层 2）；
#   1/3/5/7/11 候选人（5 是反问——反问也是候选人）。
DUAL = [
    "请讲讲你做过的高并发支付网关，峰值是多少？",
    "我在上一家公司负责支付网关，QPS 峰值两万，做了三年。",
    "主从延迟那次故障，你具体改了什么？",
    "我把读请求按延迟动态路由，主库只写，改造后延迟降了八成。",
    "好，这边差不多了，你还有什么想问我们的？",
    "想问一下这个岗位的团队规模和技术栈。",
    "你带的团队有几个人，怎么分工？",
    "带过四个人，两个后端一个前端一个测试，按模块分。",
    "为什么想离开上一家公司？",
    "好，那今天就到这里，后续 HR 会联系你。",
    "你老家是哪里的，过来面试方便吗？",
    "我住望京，过来一个小时。",
]

# 三人 Panel（多个面试官 × 一个候选人）：归属仍是同一个二元问题，
# 面试官身份（asker）尽力为准——0/2 有自我介绍可区分，4 没有 → 未区分。
PANEL = [
    "我是负责技术的四面，先我来问。请讲讲支付网关的容量规划。",
    "峰值两万 QPS，按三倍冗余设计的。",
    "我是 HR，想了解一下你的薪资期望。",
    "期望在现任基础上浮动两成。",
    "我这边补充一个，灰度发布你是怎么做的？",
    "按百分之一放量，看错误率再扩。",
]


def default_judge(txt):
    """按言语行为的桩：问句/过渡/结束语 → 面试官；自述/反问 → 候选人。

    桩模仿"一个靠谱 LLM 会怎么判"——Panel 的提问不带问号结尾
    （"先我来问。""想了解一下你的薪资期望。"），所以判据是提问性
    言语行为，不是标点。
    """
    if any(h in txt for h in ("面试官", "？", "?", "今天就到这里", "我来问",
                              "想了解一下", "补充一个", "想问我们")):
        return "interviewer", 0.9, "提问或推进流程", ""
    return "candidate", 0.85, "自述经历或表达意愿", ""


def panel_judge(txt):
    sp, conf, reason, _ = default_judge(txt)
    asker = ""
    if "负责技术" in txt:
        asker = "技术官"
    elif "HR" in txt:
        asker = "HR"
    return sp, conf, reason, asker


def segs_of(texts):
    return [{"idx": i, "start": float(i), "end": float(i) + 1.0, "text": t,
             "speaker": "unassigned", "confidence": 0.0, "reason": "",
             "asker": "", "source": "engine"} for i, t in enumerate(texts)]


# ================================================================ 切分与相似度
section("切分与相似度（纯函数）")

segs = diarization.segments_from_text(
    "面试官：请先做个自我介绍。\n候选人：我做过五年后端开发。\n"
    "面试官B：谈谈高并发。\n\n   \n长行无标点" * 1)
check("前缀行解析出说话人（含 面试官B 变体）",
      [s["speaker"] for s in segs[:3]] == ["interviewer", "candidate", "interviewer"],
      [s["speaker"] for s in segs[:3]])
check("前缀行的置信度为 1.0 且注明来源",
      segs[0]["confidence"] == 1.0 and segs[0]["reason"] == "原文自带说话人前缀",
      segs[0])
check("前缀被剥离，正文干净",
      segs[0]["text"] == "请先做个自我介绍。" and segs[2]["text"] == "谈谈高并发。",
      [s["text"] for s in segs[:3]])
check("空行被丢弃", len(segs) == 4, len(segs))

long_line = "甲" * 60 + "。" + "乙" * 60 + "。" + "丙" * 60
parts = diarization.segments_from_text(long_line, max_chars=50)
check("超长行按句末标点再切", len(parts) == 3, len(parts))
check("无标点超长行不硬切（整段保留）",
      len(diarization.segments_from_text("甲" * 200, max_chars=50)) == 1)

check("相似度：同文为 1", diarization.similarity("主从延迟故障", "主从延迟故障") == 1.0)
check("相似度：无关近 0", diarization.similarity("主从延迟故障", "薪资期望与期权") < 0.2)
check("相似度：包含关系介于 0 和 1 之间",
      0.5 < diarization.similarity("面试官请讲讲高并发支付网关", "请讲讲高并发支付网关") < 1.0)

# ================================================================ 层 1：问题对齐
section("层 1：问题清单文本对齐")

orig = install_stub(default_judge)
try:
    out = diarization.attribute(segs_of(DUAL), questions=QUESTIONS)
    by_idx = {s["idx"]: s for s in out["segments"]}
    l1_hits = [i for i in (0, 2, 6, 8) if by_idx[i]["speaker"] == "interviewer"
               and "面试通知" in by_idx[i]["reason"]]
    check("照单提问的 4 段全部命中层 1", len(l1_hits) == 4,
          {i: by_idx[i]["reason"] for i in (0, 2, 6, 8)})
    check("层 1 命中带高置信", all(by_idx[i]["confidence"] >= 0.6 for i in (0, 2, 6, 8)))
    check("层 1 未命中候选段（回答里引用问题不算提问）",
          by_idx[1]["speaker"] == "candidate" and "面试通知" not in by_idx[1]["reason"])
finally:
    restore(orig)

# ================================================================ 层 2：LLM 归因
section("层 2：LLM 言语行为归因（打桩）")

orig = install_stub(default_judge)
try:
    out = diarization.attribute(segs_of(DUAL), questions=QUESTIONS)
    sp = {s["idx"]: s["speaker"] for s in out["segments"]}
    check("双人对话角色分离全对",
          [sp[i] for i in range(12)] ==
          ["interviewer", "candidate", "interviewer", "candidate", "interviewer",
           "candidate", "interviewer", "candidate", "interviewer", "interviewer",
           "interviewer", "candidate"], sp)
    check("反问判为候选人（层 2 的易错点）", sp[5] == "candidate")
    check("打断/结束语判为面试官", sp[9] == "interviewer")
    st = out["stats"]
    check("覆盖率 100%（桩全量采信）", st["coverage"] == 1.0, st)
    check("unassigned 为 0", st["unassigned"] == 0)
    check("统计口径：interviewer=7 / candidate=5",
          (st["interviewer"], st["candidate"]) == (7, 5), st)

    # Panel：二元归属不变，面试官身份尽力为准
    orig_p = install_stub(panel_judge)
    try:
        out = diarization.attribute(segs_of(PANEL), questions=[])
    finally:
        restore(orig_p)
    by_idx = {s["idx"]: s for s in out["segments"]}
    check("Panel 角色分离同样全对",
          [by_idx[i]["speaker"] for i in range(6)] ==
          ["interviewer", "candidate", "interviewer", "candidate",
           "interviewer", "candidate"],
          {i: by_idx[i]["speaker"] for i in range(6)})
    check("有自我介绍的面试官身份被区分（技术官/HR）",
          by_idx[0]["asker"] == "技术官" and by_idx[2]["asker"] == "HR",
          [by_idx[i]["asker"] for i in range(6)])
    check("无信号段归'未区分'（asker 空，不阻断）",
          by_idx[4]["asker"] == "" and by_idx[4]["speaker"] == "interviewer")
    check("候选人段永不带 asker", all(not by_idx[i]["asker"] for i in (1, 3, 5)))
    check("asker 统计=2", out["stats"]["asker_assigned"] == 2)
finally:
    restore(orig)

# 低置信 → unassigned；未覆盖 → unassigned
def lowconf_judge(txt):
    if "面试官" in txt or "？" in txt:
        return "interviewer", 0.9, "提问", ""
    if "峰值两万" in txt:
        return "candidate", 0.45, "像自述但没把握", ""   # 低于采信线
    return "candidate", 0.85, "自述经历", ""


def partial_judge(txt):
    # 最后一段被模型整体跳过（不输出该 idx）——模拟漏覆盖
    if "我住望京" in txt:
        raise _Skip()
    if "面试官" in txt or "？" in txt:
        return "interviewer", 0.9, "提问", ""
    return "candidate", 0.85, "自述经历", ""


class _Skip(Exception):
    """桩内部用：这一段不产出任何判定项。"""


orig = install_stub(lowconf_judge)
try:
    out = diarization.attribute(segs_of(DUAL), questions=QUESTIONS)
    by_idx = {s["idx"]: s for s in out["segments"]}
    check("低置信度落 unassigned 且理由留痕",
          by_idx[1]["speaker"] == "unassigned" and "置信度" in by_idx[1]["reason"],
          by_idx[1])
finally:
    restore(orig)

orig = install_stub(partial_judge)
try:
    out = diarization.attribute(segs_of(DUAL), questions=QUESTIONS)
    by_idx = {s["idx"]: s for s in out["segments"]}
    check("模型未输出的段落 unassigned 且注明",
          by_idx[11]["speaker"] == "unassigned"
          and by_idx[11]["reason"] == "模型未输出该段的判断", by_idx[11])
    check("覆盖率反映真实边界（非 0 非 1）",
          out["stats"]["coverage"] == round(11 / 12, 3), out["stats"])
finally:
    restore(orig)

# LLM 失败必须上抛，不静默全 unassigned
def boom(messages, **kw):
    raise llm_client.LLMError("模型网关 500")


llm_client.complete = boom
try:
    raised = False
    try:
        diarization.attribute(segs_of(DUAL), questions=QUESTIONS)
    except llm_client.LLMError:
        raised = True
    check("LLM 失败向上抛（不谎报覆盖率 0）", raised)
finally:
    llm_client.complete = _REAL_COMPLETE

# 层 1 全命中时不再调用 LLM（省钱且更快）
calls = []
def counting(messages, **kw):
    calls.append(1)
    return {"text": '{"segments":[]}', "usage": {}}


prefixed = diarization.segments_from_text(
    "\n".join("面试官：%s" % q["q"] for q in QUESTIONS[:2])
    + "\n候选人：答过。")
llm_client.complete = counting
try:
    out = diarization.attribute(prefixed, questions=QUESTIONS)
    check("前缀已全部定归属时不调 LLM", not calls, calls)
    check("前缀段直接进统计", out["stats"]["interviewer"] == 2
          and out["stats"]["candidate"] == 1, out["stats"])
finally:
    llm_client.complete = _REAL_COMPLETE

# ================================================================ 人工修正保留
section("重跑不冲掉人工修正")

orig = install_stub(default_judge)
try:
    first = diarization.attribute(segs_of(DUAL), questions=QUESTIONS)["segments"]
    # 人工把第 5 段（反问）改成面试官——模拟看错了现场
    manual = [dict(s) for s in first]
    manual[5].update(speaker="interviewer", confidence=1.0,
                     reason="人工修正", source="manual")
    second = diarization.attribute(manual, questions=QUESTIONS)["segments"]
    check("manual 段原样保留（不被模型覆盖）",
          second[5]["speaker"] == "interviewer" and second[5]["source"] == "manual",
          second[5])
    check("其余段照常重新归属", second[1]["speaker"] == "candidate")

    # prefix 段（确定性信号，切段时前缀已剥离、无法重建）同样不被覆盖
    prefixed = diarization.segments_from_text(
        "面试官：请讲讲你做过的高并发支付网关，峰值是多少？")
    kept_run = diarization.attribute(prefixed, questions=QUESTIONS)["segments"]
    check("prefix 段重跑后保留且不调 LLM 改写",
          kept_run[0]["source"] == "prefix" and kept_run[0]["speaker"] == "interviewer"
          and kept_run[0]["confidence"] == 1.0, kept_run[0])

    # 人工把一段标成"不确定"是终审：两层都不许翻案
    locked = [dict(s) for s in first]
    locked[1].update(speaker="unassigned", confidence=1.0,
                     reason="人工修正：现场听不清", source="manual")
    after = diarization.attribute(locked, questions=QUESTIONS)["segments"]
    check("人工标 unassigned 的段不被层 1/层 2 翻案",
          after[1]["speaker"] == "unassigned" and after[1]["source"] == "manual"
          and after[1]["reason"] == "人工修正：现场听不清", after[1])
    check("被锁段不进 pending（其余段照常归属）",
          after[3]["speaker"] == "candidate" and after[3]["source"] == "engine", after[3])

    # engine 段重跑时重置重推（否则"重跑"对已归属段是死的）
    eng = [dict(s, source="engine", speaker="candidate", confidence=0.9,
                reason="上一轮的旧判断") for s in first]
    rederived = diarization.attribute(eng, questions=QUESTIONS)["segments"]
    check("engine 旧归属被重置后由层 1 重新推导",
          rederived[0]["speaker"] == "interviewer"
          and "面试通知" in rederived[0]["reason"], rederived[0])
finally:
    restore(orig)

# ================================================================ 问答配对
section("问答配对：四分类 + 提问人 + 覆盖声明")

orig = install_stub(default_judge)
try:
    attributed = diarization.attribute(segs_of(DUAL), questions=QUESTIONS)["segments"]
finally:
    restore(orig)

paired = interview_analysis.pair_qa(attributed, {"questions": QUESTIONS})
by_status = {}
for p in paired["pairs"]:
    by_status.setdefault(p["status"], []).append(p)
check("问了且答了 = 3", len(by_status.get("asked_answered", [])) == 3,
      [p["question"] for p in by_status.get("asked_answered", [])])
check("问了没答 = 1（下一问前无候选人段）",
      len(by_status.get("asked_dodged", [])) == 1
      and by_status["asked_dodged"][0]["question"] == "为什么想离开上一家公司？")
check("没问 = 1 且注明归属面试官",
      len(by_status.get("not_asked", [])) == 1
      and "面试官" in by_status["not_asked"][0]["attribution"]
      and by_status["not_asked"][0]["question"] == "你对我们公司有什么了解？")
check("即兴追问 = 3（过渡语/结束语/闲聊）",
      len(by_status.get("ad_hoc", [])) == 3,
      [p["question"] for p in by_status.get("ad_hoc", [])])
ans1 = next(p for p in by_status["asked_answered"] if p["question"] == QUESTIONS[0]["q"])
check("回答内容被挂到问题上", "QPS 峰值两万" in ans1["answer"], ans1["answer"])
check("回答段号可追溯", ans1["answer_idx"] == [1], ans1["answer_idx"])
check("配对带匹配相似度", ans1["match"] >= 0.6, ans1["match"])
check("ad_hoc 也带回答（面试深挖的现场应变）",
      any("团队规模" in p["answer"] for p in by_status.get("ad_hoc", [])))
st = paired["stats"]
check("配对统计口径", (st["questions"], st["asked_answered"], st["asked_dodged"],
                   st["not_asked"], st["ad_hoc"]) == (5, 3, 1, 1, 3), st)
check("配对暴露确认段边界", (st["segments_confirmed"], st["segments_total"]) == (12, 12), st)

# 没有问题清单：全部 ad_hoc，如实报告
paired2 = interview_analysis.pair_qa(attributed, None)
check("无问题清单时全部为 ad_hoc 且不假装覆盖",
      paired2["stats"]["has_question_list"] is False
      and all(p["status"] == "ad_hoc" for p in paired2["pairs"])
      and paired2["stats"]["ad_hoc"] == 7, paired2["stats"])

# 未确认段不参与配对（吃 Percent 确认的段）
partial = [dict(s) for s in attributed]
partial[1]["speaker"] = "unassigned"
paired3 = interview_analysis.pair_qa(partial, {"questions": QUESTIONS})
q1 = next(p for p in paired3["pairs"] if p["question"] == QUESTIONS[0]["q"])
check("回答段被置未确认后，该问降级为'问了没答'", q1["status"] == "asked_dodged", q1["status"])
check("配对统计只数确认段", paired3["stats"]["segments_confirmed"] == 11)

# ================================================================ store 层
section("段持久化：入库 / 人工修正 / 重跑保留")

job = store.create_job(title="P1 测试岗位", actor="test")
jid = job["id"] if isinstance(job, dict) else job
asr_like = [{"start": 0.0, "end": 4.2, "text": t} for t in DUAL[:4]]
cand = store.add_candidate(jid, "段入库", files=[
    {"role": "transcript", "text": "\n".join(DUAL[:4]), "segments": asr_like},
    {"role": "resume", "text": "段入库\n五年后端"},
])
cid = cand["id"]
rows = store.get_segments(cid)
check("音频段随入库落 4 段", len(rows) == 4, len(rows))
check("段序 idx 连续", [r["idx"] for r in rows] == [0, 1, 2, 3])
check("入库时归属为 unassigned（不预判）",
      all(r["speaker"] == "unassigned" for r in rows))
check("时间戳被保留", rows[0]["start"] == 0.0 and rows[0]["end"] == 4.2)

# 只收 transcript 角色的段：resume 带的 segments 字段被忽略
cand2 = store.add_candidate(jid, "非转写段忽略", files=[
    {"role": "resume", "text": "只有简历", "segments": asr_like}])
check("非 transcript 角色不落段", store.get_segments(cand2["id"]) == [])

# 跑归属 → 保存 → 人工修正 → 重跑保留
orig = install_stub(default_judge)
try:
    out = diarization.attribute(store.get_segments(cid), questions=QUESTIONS)
finally:
    restore(orig)
r1 = store.save_segments(cid, jid, out["segments"])
check("保存 4 段、无人工修正可保留", (r1["saved"], r1["manual_kept"]) == (4, 0), r1)
rows = store.get_segments(cid)
check("归属已落库", [r["speaker"] for r in rows] == ["interviewer", "candidate",
                                                "interviewer", "candidate"],
      [r["speaker"] for r in rows])

store.update_segment(cid, 1, {"speaker": "interviewer", "asker": "技术官"})
rows = store.get_segments(cid)
check("人工修正：source=manual / confidence=1.0 / 理由留痕",
      rows[1]["source"] == "manual" and rows[1]["confidence"] == 1.0
      and rows[1]["speaker"] == "interviewer" and rows[1]["asker"] == "技术官", rows[1])

engine_again = [dict(s, speaker="candidate", source="engine") for s in rows]
engine_again[1].pop("source")
r2 = store.save_segments(cid, jid, engine_again)
check("重跑保留人工修正（manual_kept=1）", r2["manual_kept"] == 1, r2)
rows = store.get_segments(cid)
check("重跑后人工段仍是面试官（模型说候选人也没被覆盖）",
      rows[1]["speaker"] == "interviewer" and rows[1]["source"] == "manual", rows[1])
check("重跑后其余段用引擎结果", rows[3]["speaker"] == "candidate"
      and rows[3]["source"] == "engine", rows[3])

# 参数校验
try:
    store.update_segment(cid, 1, {"speaker": "面试官"})
    check("非法 speaker 被拒", False)
except ValueError:
    check("非法 speaker 被拒", True)
try:
    store.update_segment(cid, 99, {"speaker": "candidate"})
    check("不存在的段号被拒", False)
except KeyError:
    check("不存在的段号被拒", True)

# prefix 段在重跑后同样保留（manual_kept 计入）
pref = diarization.segments_from_text(
    "面试官：请先做个自我介绍。\n候选人：我做过五年后端。")
cand3 = store.add_candidate(jid, "前缀段保留", files=[
    {"role": "transcript", "text": "\n".join(s["text"] for s in pref),
     "segments": pref}])
store.save_segments(cand3["id"], jid, pref)
engine_reset = [dict(s, speaker="candidate", source="engine")
                for s in store.get_segments(cand3["id"])]
r3 = store.save_segments(cand3["id"], jid, engine_reset)
rows3 = store.get_segments(cand3["id"])
check("prefix 段重跑保留（manual_kept=2）", r3["manual_kept"] == 2, r3)
check("prefix 段归属未被引擎翻掉",
      [r["source"] for r in rows3] == ["prefix", "prefix"]
      and [r["speaker"] for r in rows3] == ["interviewer", "candidate"],
      [(r["source"], r["speaker"]) for r in rows3])

# ================================================================ 汇总
print("\n" + "=" * 62)
print(" 结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
