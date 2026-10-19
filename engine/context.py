"""
上下文管理结构（v2）
- ContextStore：按来源分区的结构化原始上下文（简历/测评/转写稿），带 provenance。
  公平性：所有 agent 共享同一份「原始事实」，立场差异只由 system prompt 体现。
- DebateMemory：回合制辩论记忆体，独立存取 pro/con/rounds/裁判结论，供裁判与评委增量消费。
- 轻量 token 预算守卫：超预算时**显式截断并留痕**（`fit_budget()`），
  不再只是打一行 warning 然后把超长材料原样发出去。
"""


# 来源 -> 展示标签（provenance 可读）
SECTION_LABELS = {
    "resume": "简历",
    "assessment": "测评",
    "transcript": "面试转写稿",
}

# 岗位 JD 字段（匹配度锚点）—— 不同岗位的配置，不是代码逻辑
JOB_FIELDS = [
    ("requirements", "岗位硬性要求"),
    ("preferred", "优先项"),
    ("red_flags", "风险红线"),
]

# 中文粗略按 1.5 字/token 估算
CHARS_PER_TOKEN = 1.5

# 上下文 token 预算：材料部分。超出会被 fit_budget() 显式截断并留痕。
TOKEN_BUDGET = 6000

# 辩论历史的预算。为什么必须单独设一个：
# 每轮互驳都会把「材料全文 + 完整辩论历史」重新塞一遍，历史随轮次线性增长。
# 材料 6000 + 2 轮互驳共 6 段（每段 800–1500 字）≈ 1.2–1.5 万 token，
# 超出窗口的模型会被供应商**静默截断**——截断位置由供应商定，
# 可能正好砍掉反方最关键的质疑，而评审结论看不出发生过。
# 2500 token ≈ 3750 字，够放"双方立论 + 最近两轮"的要点。
HISTORY_TOKEN_BUDGET = 2500


def _estimate_tokens(text):
    """与 ContextStore.estimate_tokens 同一套口径（1.5 字/token）。"""
    return int(len(text or "") / CHARS_PER_TOKEN)


# 截断时头部与尾部的保留比例。60/40 的理由：
# 简历头部通常是姓名/学历/技能概要（准入判断要用），
# 尾部常常是最近一段经历或补充说明（能力判断要用），
# 中间最容易是重复的项目流水账。
TRUNC_HEAD_RATIO = 0.6


def _truncate_head_tail(text, room):
    """把超长文本压到 room 字以内，保留头尾、省略中间。

    只砍尾巴会让"关键信息写在末尾"的简历被系统性低估——
    评分结果就取决于候选人的排版习惯，这不合理。
    """
    text = text or ""
    if len(text) <= room:
        return text
    marker = "\n…（材料此处过长，中段已省略）\n"
    avail = max(0, room - len(marker))
    if avail <= 0:
        return text[:max(0, room)]
    head = int(avail * TRUNC_HEAD_RATIO)
    tail = avail - head
    return text[:head] + marker + (text[-tail:] if tail > 0 else "")


class ContextStore:
    """候选人的结构化原始上下文（含岗位 JD，匹配度锚点）。"""

    def __init__(self, sections: dict, meta: dict, job: dict = None):
        # sections: {"resume":..., "assessment":..., "transcript":...}
        self.sections = {k: (sections.get(k) or "") for k in SECTION_LABELS}
        self.meta = {
            "id": meta.get("id"),
            "name": meta.get("name", ""),
            "position": meta.get("position", ""),
        }
        self.job = job or {}

    @classmethod
    def from_candidate(cls, c: dict) -> "ContextStore":
        """从 candidate dict 装配上下文。

        兼容三种历史形状，任何一种读错都会导致"材料为空"的静默失效：
          · 仓储层（store.py）：resume_text / assessment_text / transcript_text
          · 旧 ingest.py：resume / assessment / interview_transcript
          · 嵌套：{"sections": {...}}
        早期版本只认第二种，结果接上 SQLite 仓储后所有候选人都被判定
        "没有可用材料"而中止评审——这是读字段名不一致造成的假阴性。
        """
        nested = c.get("sections") if isinstance(c.get("sections"), dict) else {}

        def pick(*keys):
            for k in keys:
                v = c.get(k)
                if isinstance(v, str) and v.strip():
                    return v
            for k in keys:
                v = nested.get(k)
                if isinstance(v, str) and v.strip():
                    return v
            return ""

        return cls(
            sections={
                "resume": pick("resume_text", "resume"),
                "assessment": pick("assessment_text", "assessment"),
                "transcript": pick("transcript_text", "interview_transcript", "transcript"),
            },
            meta={"id": c.get("id"), "name": c.get("name"), "position": c.get("position")},
            job=c.get("job"),
        )

    def has_material(self) -> bool:
        """是否拿到了任何候选人原始材料（简历/测评/转写）。

        没有任何材料时必须让评审**失败**，而不是让 agent 凭空编造——
        否则会产出看起来完整、实则臆造的评审结论。
        """
        return any((self.sections.get(k) or "").strip() for k in SECTION_LABELS)

    def header_text(self) -> str:
        """姓名 + 岗位 + JD：这些是匹配锚点，不参与裁剪，但要占预算。"""
        lines = [
            f"姓名：{self.meta.get('name','')}",
            f"应聘岗位：{self.meta.get('position','')}",
        ]
        if self.job:
            if self.job.get("title"):
                lines.append(f"【应聘岗位JD】{self.job.get('title')}")
            for key, lab in JOB_FIELDS:
                vals = self.job.get(key)
                if vals:
                    lines.append(f"【{lab}】" + "；".join(vals))
        return "\n".join(lines)

    def shared_facts_text(self) -> str:
        """同一份原始事实，供所有 agent 消费（对抗公平：不裁剪、不偏向）。"""
        lines = [self.header_text()]
        for key, lab in SECTION_LABELS.items():
            val = self.sections.get(key, "")
            if val:
                lines.append(f"【{lab}】{val}")
        return "\n".join(lines)

    def raw_material_text(self) -> str:
        """只有候选人材料正文（不含姓名/JD），用于注入检测。

        检测要针对"候选人写的字"，而不是我们自己的模板头——
        否则模板里的"应聘岗位"之类字样会干扰命中率。
        """
        return "\n\n".join(v for v in self.sections.values() if v)

    def estimate_tokens(self) -> int:
        return int(len(self.shared_facts_text()) / CHARS_PER_TOKEN)

    def is_over_budget(self) -> bool:
        return self.estimate_tokens() > TOKEN_BUDGET

    def fit_budget(self, budget=None):
        """把材料压到预算内，并**记录做了什么**。

        原来只有一个 `is_over_budget()` 判断，然后 `print("[warn] 超阈值")`，
        材料照原样发出去。这是最坏的一种处理：**警告了，但没管**。

        后果不是"报错"，而是更隐蔽的东西：
          · 超出上下文窗口时，供应商可能直接截断——截在哪由 tokenizer 决定，
            可能正好砍掉简历后半段的学历/项目，而评审结论看不出这件事发生过；
          · 没超窗口时，超长材料里越靠后的内容权重越低，
            于是"简历写得多的人"和"写得少的人"被系统性区别对待。
        两种情况下，分数都还在正常区间，没人知道它已经不可比。

        现在改成显式截断：按 section 顺序保留，总预算耗尽后停止，
        并把"截掉了哪些 section、截掉多少字"写进返回值和结果里。
        这样至少**评审结论的适用边界是已知的**，而不是假的。

        **单份材料内部按"首尾保留"截断，而不是只砍尾部。**
        踩过的坑：原来是从前往后装、装不下就砍尾巴。但简历末尾常常放着
        最近一段工作经历或补充说明（"补充：最近在 X 负责接入层，日均 1.5 亿请求"），
        而中间是项目流水账。于是"关键经历写在哪"直接决定了它会不会被看到——
        评分结果取决于候选人的排版习惯，这是不该发生的事。
        现在头部保留 60%、尾部保留 40%，中间省略并显式标注。

        JD 与 section 标签不计入裁剪（它们很短，且是匹配锚点）。
        """
        budget = int(budget or TOKEN_BUDGET)
        info = {"budget_tokens": budget, "original_tokens": self.estimate_tokens(),
                "truncated": False, "dropped_sections": [],
                "dropped_chars": 0, "kept_sections": [],
                "truncated_sections": []}
        if info["original_tokens"] <= budget:
            info["kept_sections"] = [k for k, v in self.sections.items() if (v or "").strip()]
            info["final_tokens"] = info["original_tokens"]
            return info

        # 逐 section 装填，保留顺序（简历/评估/转录的顺序有意义）
        # 可用字数要扣掉 JD 部分的长度：estimate_tokens() 统计的是
        # shared_facts_text()（含 JD），JD 不参与裁剪但占预算。
        # 不扣的话会出现"每个 section 都没超，但合起来超预算"——
        # 预算守卫自己超预算，那就成了笑话。
        jd_chars = len(self.header_text() or "")
        # 再留一点给 section 标签（【简历】）与换行符，
        # 否则会出现"每个 section 都没超、合起来却超几个 token"的尴尬。
        char_budget = max(400, int(budget * 1.5) - jd_chars - 120)
        used = 0
        for key, lab in SECTION_LABELS.items():
            val = (self.sections.get(key) or "").strip()
            if not val:
                continue
            room = char_budget - used
            if room <= 200:
                # 剩余空间太小，装了也读不出有效信息，不如不装并如实记录
                info["dropped_sections"].append(lab)
                info["dropped_chars"] += len(val)
                self.sections[key] = ""
                continue
            if len(val) <= room:
                used += len(val)
                info["kept_sections"].append(lab)
            else:
                self.sections[key] = _truncate_head_tail(val, room)
                info["dropped_sections"].append(lab)
                info["truncated_sections"].append(lab)
                info["dropped_chars"] += len(val) - len(self.sections[key])
                used += len(self.sections[key])
                info["truncated"] = True

        info["truncated"] = True
        info["final_tokens"] = self.estimate_tokens()
        return info

    def to_dict(self) -> dict:
        return {"meta": self.meta, "sections": self.sections, "job": self.job}


class DebateMemory:
    """回合制辩论记忆体。"""

    def __init__(self):
        self.pro = ""
        self.con = ""
        self.rounds = []  # [{"pro":..., "con":...}, ...]

    def add_round(self, pro: str, con: str):
        self.rounds.append({"pro": pro, "con": con})

    def _segments(self):
        """按时间顺序返回 [(标签行, 正文), ...]。"""
        segs = []
        if self.pro:
            segs.append(("正方立论（主张录用）", self.pro))
        if self.con:
            segs.append(("反方立论（主张淘汰）", self.con))
        for i, r in enumerate(self.rounds, 1):
            segs.append((f"第{i}轮 · 正方驳斥", r.get("pro") or ""))
            segs.append((f"第{i}轮 · 反方驳斥", r.get("con") or ""))
        return [(lab, txt) for lab, txt in segs if txt]

    def history_text(self, budget=None, with_info=False):
        """完整辩论历程，供裁判/评委消费。

        budget=None 返回全文（存档、报告、导出都用这个，不允许丢东西）。
        给了 budget 就**按"最近优先"裁剪**——这一点很关键：
        原来的实现把全文直接拼进 prompt，而每轮互驳都会把"材料全文 + 完整历史"
        再塞一遍，历史随轮次线性增长。单次调用很容易过万 token，
        超出窗口的模型会被供应商**静默截断**，而截断位置由供应商决定——
        很可能正好砍掉反方最关键的质疑，而评审结论看不出发生过。

        裁剪策略：
          · 从最新一段往前装，装得下就整段保留；
          · 装不下的那一段按剩余空间截断，并标注「此处已截断」；
          · 更早的整段丢弃，只在开头留一行说明丢了多少。
        最新一段永远是完整的——辩论的价值在"最后双方到底在争什么"。
        """
        segs = self._segments()
        full = "\n\n".join("%s：\n%s" % (lab, txt) for lab, txt in segs)
        info = {
            "budget_tokens": budget,
            "original_tokens": _estimate_tokens(full),
            "final_tokens": _estimate_tokens(full),
            "truncated": False,
            "kept_segments": len(segs),
            "dropped_segments": 0,
            "dropped_chars": 0,
        }
        if not budget or info["original_tokens"] <= budget:
            return (full, info) if with_info else full

        # 需要裁剪时先把"更早的 N 段已省略"那行算进预算里。
        # 不预留的话，正文刚好占满、再加一行说明就会超出预算——
        # 而"落在预算内"正是这个函数存在的意义，差 11 个 token 也算没做到。
        reserve = 100 if info["original_tokens"] > budget else 0
        room_chars = max(0, int(budget * CHARS_PER_TOKEN) - reserve)
        kept_rev, used = [], 0
        dropped, dropped_chars = 0, 0
        for lab, txt in reversed(segs):
            item = "%s：\n%s" % (lab, txt)
            if used + len(item) + 2 <= room_chars:
                kept_rev.append(item)
                used += len(item) + 2
                continue
            room = room_chars - used - 30          # 给省略标记留位置
            if room >= 160:
                cut = "%s：\n%s\n…（此处已截断）" % (lab, txt[:room])
                kept_rev.append(cut)
                used += len(cut)
                dropped_chars += max(0, len(txt) - room)
            else:
                dropped_chars += len(txt)
            dropped += 1

        kept_rev.reverse()
        body = "\n\n".join(kept_rev)
        if dropped:
            body = ("…（更早的 %d 段辩论已省略，共 %d 字）\n\n"
                    % (dropped, dropped_chars)) + body
        info["truncated"] = True
        info["final_tokens"] = _estimate_tokens(body)
        info["kept_segments"] = len(kept_rev)
        info["dropped_segments"] = dropped
        info["dropped_chars"] = dropped_chars
        return (body, info) if with_info else body

    def to_dict(self) -> dict:
        return {
            "pro": self.pro,
            "con": self.con,
            "rounds": self.rounds,
        }
