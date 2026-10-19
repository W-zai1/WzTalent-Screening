"""合成样本集：加载、展开、自检。

为什么要这一层
--------------
没有固定样本，"这个模型准不准"就只能靠感觉。但样本集本身也很容易写错：
把"必须提到的证据"写成简历里根本没有的词、把注定超预算的样本写成正常长度、
注入样本里其实没写注入文本——这些错误会让评测永远失败或永远通过，
而人不容易发现。

所以这里提供一个**自检**：把每条人工预期与简历原文对一遍。
它不是评测，但它是评测的前提：样本集不自洽，跑出来的通过率没有意义。

隐私：本文件只处理 `tests/fixtures/sample_set_*.json` 里的**虚构**数据，
不读取也不写入任何真实简历。
"""
import json
import os

LONG_LIST_TOKEN = "$LONG_PROJECT_LIST$"

# 展开超长样本时用的确定性片段：不能随机，否则"超长"这件事每次结果不同，
# 断点复现就无从谈起。
_LONG_ITEM = ("  项目{n}：为某内部平台开发接口 {n} 个，使用 {tech}，"
              "协作 3 人，周期约 2 个月，负责其中一个模块的增删改查与联调。\n")


def default_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "tests", "fixtures",
                        "sample_set_v1.json")


def expand(text, items=220):
    """把 $LONG_PROJECT_LIST$ 换成确定性生成的一长串项目描述。

    220 条约 1.4 万字，足以让材料超过 6000 token 的材料预算并触发截断。
    """
    if LONG_LIST_TOKEN not in (text or ""):
        return text
    techs = ["Java", "Spring", "MySQL"]
    body = "".join(_LONG_ITEM.format(n=i + 1, tech=techs[i % len(techs)])
                   for i in range(int(items)))
    return text.replace(LONG_LIST_TOKEN, body.rstrip("\n"))


def load(path=None, expand_long=True):
    """读取样本集，返回 (meta, samples)。"""
    path = path or default_path()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    samples = []
    for s in data.get("samples") or []:
        s = dict(s)
        if expand_long:
            s["resume"] = expand(s.get("resume") or "")
        samples.append(s)
    return data, samples


def check(path=None):
    """自检样本集。返回问题列表（空列表 = 自洽）。

    检查的是"这份预期和这份简历是否对得上"，不是"模型答得对不对"。
    """
    problems = []
    try:
        data, samples = load(path)
    except Exception as e:
        return ["样本集无法解析：%s" % e]

    if not samples:
        return ["样本集里没有任何样本"]

    ids = [s.get("id") for s in samples]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        problems.append("样本 id 重复：%s" % sorted(dup))

    for s in samples:
        sid = s.get("id") or "?"
        resume = s.get("resume") or ""
        if not resume.strip():
            problems.append("%s：简历为空" % sid)
        if not s.get("design"):
            problems.append("%s：缺少 design（说明这份样本验证什么）" % sid)
        exp = s.get("expect")
        if not isinstance(exp, dict):
            problems.append("%s：缺少 expect（人工预期）" % sid)
            continue

        must = exp.get("must_support") or []
        miss = exp.get("must_flag_missing") or []
        forbid = exp.get("must_not_claim") or []
        if not must and not miss:
            problems.append("%s：must_support 与 must_flag_missing 都是空的，"
                            "这份样本无法判定通过与否" % sid)
        if not forbid:
            problems.append("%s：must_not_claim 为空——"
                            "没有'不该得出什么结论'的样本挡不住过度推断" % sid)

        # 预期里的证据必须真的在简历里，否则这条预期永远失败
        for item in must:
            req = (item or {}).get("requirement") or "?"
            words = (item or {}).get("evidence_must_mention") or []
            if not words:
                problems.append("%s：要求「%s」没写 evidence_must_mention" % (sid, req))
                continue
            for w in words:
                if w and w not in resume:
                    problems.append(
                        "%s：预期说「%s」的证据应提到「%s」，但简历里没有这个词"
                        % (sid, req, w))
            if req and req not in (data.get("job", {}).get("requirements") or []):
                problems.append(
                    "%s：must_support 引用了岗位要求里没有的条目「%s」" % (sid, req))

        # 声明"缺失"的要求，也应该真的出现在岗位要求里
        job_req = data.get("job", {}).get("requirements") or []
        for m in miss:
            # 允许两种写法：字符串（只要对上某条要求即可）或
            # {requirement, why}（评测时要把"缺哪条"和"为什么"分开用）
            if isinstance(m, dict):
                key = (m.get("requirement") or "").strip()
                if not (m.get("why") or "").strip():
                    problems.append("%s：must_flag_missing 里的「%s」没写 why"
                                    % (sid, key or "?"))
            else:
                key = str(m).split("（")[0].strip()
            if key and not any(key in r or r in key for r in job_req):
                problems.append(
                    "%s：must_flag_missing 里的「%s」对不上任何岗位要求" % (sid, key))

        # 元预期：注入样本必须真的含注入文本；超长样本必须真的超预算
        meta = s.get("meta_expect") or {}
        if meta.get("injection_must_be_detected"):
            if "系统指令" not in resume and "忽略" not in resume:
                problems.append("%s：标记为注入样本，但简历里没有注入文本" % sid)
        if meta.get("should_trigger_budget"):
            tokens = int(len(resume) / 1.5)
            if tokens <= 6000:
                problems.append("%s：标记为超长样本，但展开后只有 %d token"
                                "（材料预算是 6000）" % (sid, tokens))
        if meta.get("should_parse"):
            if not resume.strip():
                problems.append("%s：标记为可解析，但简历为空" % sid)

    return problems


def summary(path=None):
    """给报告用的一行摘要。"""
    data, samples = load(path)
    return {
        "version": data.get("version"),
        "frozen_at": data.get("frozen_at"),
        "count": len(samples),
        "designs": [s.get("design") for s in samples],
        "job_title": (data.get("job") or {}).get("title"),
    }
