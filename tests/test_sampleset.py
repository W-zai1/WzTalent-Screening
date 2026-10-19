"""合成样本集自洽性 + 超长材料的截断策略。

两个问题在这里一起盯：

1. **样本集本身要自洽**。人工预期写错（比如要求"必须提到 Go"但简历里没有 Go）
   会让评测永远失败，而人不容易发现。`sampleset.check()` 就是把预期和原文对一遍。

2. **超长材料不能因为排版而误判**。原来 `fit_budget` 从前往后装、装不下就砍尾巴，
   于是"关键经历写在末尾"的简历会被系统性低估——评分结果取决于排版习惯。
   现在头部 60% + 尾部 40%，中间显式省略。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

import context
import sampleset

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


print("--- 1. 样本集自洽 ---")
problems = sampleset.check()
check("没有自洽性问题", not problems, problems[:5])
data, samples = sampleset.load()
check("8 份样本", len(samples) == 8, len(samples))
check("id 覆盖 A–H", [s["id"] for s in samples] == list("ABCDEFGH"),
      [s["id"] for s in samples])
check("每份都有 design 说明验什么", all(s.get("design") for s in samples))
check("每份都有 must_not_claim（防止过度推断）",
      all((s["expect"].get("must_not_claim") or []) for s in samples),
      [s["id"] for s in samples if not (s["expect"].get("must_not_claim") or [])])
check("样本集带版本与冻结日期",
      data.get("version") and data.get("frozen_at"),
      (data.get("version"), data.get("frozen_at")))

print("\n--- 2. 预期与原文对得上（这是自检最该抓的） ---")
job_reqs = set(data["job"]["requirements"])
besides = set()
for s in samples:
    for it in s["expect"].get("must_support") or []:
        besides.add(it["requirement"])
check("must_support 只引用岗位要求里的条目", besides <= job_reqs,
      sorted(besides - job_reqs))

missing_kinds = {}
for s in samples:
    for m in s["expect"].get("must_flag_missing") or []:
        missing_kinds.setdefault(m["requirement"], []).append(s["id"])
check("must_flag_missing 也都能对上岗位要求",
      set(missing_kinds) <= job_reqs, sorted(set(missing_kinds) - job_reqs))
check("C（明显不匹配）几乎全部要求都判缺失",
      len(missing_kinds.get("5 年以上后端开发经验", [])) >= 2,
      missing_kinds.get("5 年以上后端开发经验"))
check("A（高匹配）没有任何缺失项",
      not (samples[0]["expect"].get("must_flag_missing") or []),
      samples[0]["expect"].get("must_flag_missing"))

print("\n--- 3. 注入样本确实含注入文本 ---")
h = next(s for s in samples if s["id"] == "H")
check("H 简历里有指令性文本",
      "系统指令" in h["resume"] and "忽略" in h["resume"], h["resume"][:60])
check("H 要求不得被指令影响",
      any("指令" in x for x in h["expect"]["must_not_claim"]),
      h["expect"]["must_not_claim"])

print("\n--- 4. 超长样本真的超预算，且关键事实不被砍掉 ---")
g = next(s for s in samples if s["id"] == "G")
raw_tokens = int(len(g["resume"]) / context.CHARS_PER_TOKEN)
check("G 展开后确实超过材料预算（6000 token）",
      raw_tokens > context.TOKEN_BUDGET, raw_tokens)

st = context.ContextStore(
    {"resume": g["resume"]},
    {"id": "g", "name": g["name"], "position": data["job"]["title"]},
    data["job"])
info = st.fit_budget(context.TOKEN_BUDGET)
text = st.shared_facts_text()
check("确实发生了截断", info["truncated"] is True, info["truncated"])
check("截断后不超预算（预算守卫自己不能超预算）",
      info["final_tokens"] <= info["budget_tokens"],
      (info["final_tokens"], info["budget_tokens"]))
for fact in g["meta_expect"]["key_fact_must_survive"]:
    check("关键事实「%s」在截断后仍可见" % fact, fact in text,
          "被截掉了")
check("中间有显式的省略标记（不是无声截断）",
      "中段已省略" in text, text[3000:3040])
check("记录里能看出哪一段被截",
      info["truncated_sections"] == ["简历"], info["truncated_sections"])

print("\n--- 5. 短材料一字不动 ---")
short = "李四\n本科\n3 年后端"
st2 = context.ContextStore({"resume": short}, {"id": "s", "name": "李四"})
info2 = st2.fit_budget(context.TOKEN_BUDGET)
check("短材料不截断", info2["truncated"] is False, info2["truncated"])
check("短材料内容完整", st2.sections["resume"] == short, st2.sections["resume"])

print("\n--- 6. 头尾保留比例符合约定 ---")
long_text = "头" * 5000 + "中" * 20000 + "尾" * 5000
tr = context._truncate_head_tail(long_text, 6000)
check("结果长度不超过给定空间", len(tr) <= 6000, len(tr))
check("保留了开头", tr.startswith("头" * 100))
check("保留了结尾", tr.endswith("尾" * 100))
check("有省略标记", "中段已省略" in tr)
head_kept = tr.count("头")
tail_kept = tr.count("尾")
check("头部保留多于尾部（60/40 约定）", head_kept > tail_kept,
      (head_kept, tail_kept))

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
