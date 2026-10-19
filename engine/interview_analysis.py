"""
interview_analysis.py —— 面试问答配对（P1），以及后续面试证据分析（P2a）的落点。

P1 只做问答配对
--------------
输入：问题清单（interview.py 生成、已持久化到 candidates.interview_json）
      + 已归属的转写段（diarization.py + store.interview_segments）。
输出：四分类 + 提问人 + 覆盖率。

  asked_answered  问题问了，且直到下一个面试官开口前有候选人段
  asked_dodged    问题问了，但直到下一个面试官开口都没有候选人段
                  （打断、转移话题、沉默——"答了但没答"是候选人的信号，
                    "根本没答"常常是面试流程的信号，所以单列不混入 answered）
  not_asked       通知里的问题没问。**面试官的遗漏，不是候选人的缺陷**——
                  UI 与后续分析必须分开呈现，否则等于拿面试官没问
                  来扣候选人分。
  ad_hoc          面试官即兴追加的问题（不在通知清单里）——面试深挖的
                  现场应变，是问题清单质量的反馈，不是异常。

设计原则
--------
· 只消费"已确认"的段（interviewer/candidate）；unassigned 段不参与
  配对——归属都没搞清就配对，等于把猜测当事实。
· 每条附 asker（层 2 区分成功时）。记错提问人污染的是评估者元数据，
  不影响对候选人的判断，所以 asker 缺失不阻断配对。
· 匹配用 diarization 的同款相似度（字符二元语法）：面试官照单提问
  的近原文复述能稳定命中； paraphrases 落到 ad_hoc，如实呈现。
"""
import diarization

# 面试官段与问题文本的配对阈值。低于它 → ad_hoc（即兴追问），
# 不硬凑——"看起来像问过"比"明确没问"更难排查。
PAIR_MATCH_MIN = 0.5


def _questions_of(interview):
    """从 candidates.interview_json 取问题清单。形状不对时返回空。"""
    if not isinstance(interview, dict):
        return []
    qs = interview.get("questions")
    if not isinstance(qs, list):
        return []
    out = []
    for q in qs:
        if isinstance(q, dict):
            t = (q.get("q") or q.get("question") or "").strip()
            if t:
                out.append({"q": t,
                            "intent": (q.get("intent") or "核实").strip(),
                            "why": (q.get("why") or "").strip()})
        elif isinstance(q, str) and q.strip():
            out.append({"q": q.strip(), "intent": "核实", "why": ""})
    return out


def _answer_after(segs, qseg):
    """提问段之后、下一个面试官段之前的候选人段（= 对它的回答）。"""
    started = False
    out = []
    for s in segs:
        if s is qseg:
            started = True
            continue
        if not started:
            continue
        if s["speaker"] == "interviewer":
            break
        if s["speaker"] == "candidate":
            out.append(s)
    return out


def pair_qa(segments, interview=None, match_min=PAIR_MATCH_MIN):
    """问答配对。`interview` 是 candidates.interview_json（含 questions）。

    没有 interview（还没生成追问清单）时，所有面试官段都是 ad_hoc——
    如实报告"没有可对照的问题清单"，不假装覆盖度 100%。
    """
    segs = sorted((s for s in (segments or []) if isinstance(s, dict)
                   and (s.get("text") or "").strip()),
                  key=lambda s: s.get("idx", 0))
    questions = _questions_of(interview)
    interviewers = [s for s in segs if s.get("speaker") == "interviewer"]

    # 每个问题找最佳面试官段（一个问题只认一次；重复提问归 ad_hoc）
    q_hit = {}
    for s in interviewers:
        best, bi = 0.0, None
        for i, q in enumerate(questions):
            sim = diarization.similarity(s["text"], q["q"])
            if sim > best:
                best, bi = sim, i
        if bi is not None and best >= match_min and bi not in q_hit:
            q_hit[bi] = (s, best)

    pairs = []
    for i, q in enumerate(questions):
        if i in q_hit:
            qseg, sim = q_hit[i]
            ans = _answer_after(segs, qseg)
            pairs.append({
                "question": q["q"], "intent": q["intent"], "why": q["why"],
                "status": "asked_answered" if ans else "asked_dodged",
                "asker": qseg.get("asker") or "",
                "asked_idx": qseg.get("idx"),
                "asked_text": qseg["text"],
                "match": round(sim, 2),
                "answer": "\n".join(a["text"] for a in ans),
                "answer_idx": [a["idx"] for a in ans],
                "answer_secs": round(sum(
                    max(0.0, float(a.get("end") or 0) - float(a.get("start") or 0))
                    for a in ans), 1),
                "attribution": "",
            })
        else:
            pairs.append({
                "question": q["q"], "intent": q["intent"], "why": q["why"],
                "status": "not_asked",
                "asker": "", "asked_idx": None, "asked_text": "", "match": 0.0,
                "answer": "", "answer_idx": [], "answer_secs": 0.0,
                "attribution": "面试官的遗漏（不计入候选人）",
            })

    hit_segs = {id(v[0]) for v in q_hit.values()}
    for s in interviewers:
        if id(s) in hit_segs:
            continue
        ans = _answer_after(segs, s)
        pairs.append({
            "question": s["text"], "intent": "", "why": "",
            "status": "ad_hoc",
            "asker": s.get("asker") or "",
            "asked_idx": s.get("idx"), "asked_text": s["text"], "match": 0.0,
            "answer": "\n".join(a["text"] for a in ans),
            "answer_idx": [a["idx"] for a in ans],
            "answer_secs": round(sum(
                max(0.0, float(a.get("end") or 0) - float(a.get("start") or 0))
                for a in ans), 1),
            "attribution": "",
        })

    pairs.sort(key=lambda p: (p["asked_idx"] is None, p["asked_idx"] or 0))

    counts = {"asked_answered": 0, "asked_dodged": 0, "not_asked": 0, "ad_hoc": 0}
    for p in pairs:
        counts[p["status"]] += 1
    confirmed = sum(1 for s in segs if s.get("speaker") in ("interviewer", "candidate"))
    stats = {
        "questions": len(questions),
        "pairs": len(pairs),
        "asked_answered": counts["asked_answered"],
        "asked_dodged": counts["asked_dodged"],
        "not_asked": counts["not_asked"],
        "ad_hoc": counts["ad_hoc"],
        "asker_assigned": sum(1 for p in pairs if p["asker"]),
        "segments_confirmed": confirmed,
        "segments_total": len(segs),
        "has_question_list": bool(questions),
    }
    return {"pairs": pairs, "stats": stats}


STATUS_META = {
    "asked_answered": ("问了且答了", "#7bd88f"),
    "asked_dodged": ("问了没答", "#ffd97a"),
    "not_asked": ("没问（面试官遗漏）", "#8ab4ff"),
    "ad_hoc": ("即兴追问", "#b79aff"),
}
