"""
ingest.py —— skills 与 engine 之间的胶水层。
把原始素材目录（简历/测评/转写/音视频）装配成 run_candidate 所需的 candidate dict。

命名约定（inputs/ 下，用「__」分隔姓名与岗位）：
  <姓名>__<岗位>.resume.txt|.md|.pdf       简历（.pdf 走 resume-parse skill）
  <姓名>__<岗位>.assessment.txt            测评结果
  <姓名>__<岗位>.transcript.txt|.json      转写稿（.json 为 asr 输出结构）
  <姓名>__<岗位>.audio.mp3|.wav|.m4a       音视频（走 asr-transcribe skill）

本模块是候选人的唯一来源：引擎不再内置样例数据，必须由素材目录装配。
"""
import os
import sys
import re
import glob
import json

from docparse import AUDIO_EXTS  # 音频/视频扩展名与 docparse、api 共用一份名单

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills")


def _load_mod(dirname, filename):
    """动态加载 skills/<dirname>/<filename> 模块（不假设为包）。"""
    p = os.path.join(SKILLS_DIR, dirname, filename)
    if not os.path.exists(p):
        return None
    sys.path.insert(0, os.path.join(SKILLS_DIR, dirname))
    import importlib.util
    spec = importlib.util.spec_from_file_location(dirname, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def build_candidates_from_dir(inputs_dir, job):
    """扫描 inputs_dir，按姓名分组，装配标准化 candidate 列表。"""
    asr = _load_mod("asr-transcribe", "transcribe.py")
    rparse = _load_mod("resume-parse", "parse.py")

    buckets = {}
    for path in sorted(glob.glob(os.path.join(inputs_dir, "*"))):
        if os.path.isdir(path):
            continue
        base = os.path.basename(path)
        stem, ext = os.path.splitext(base)
        if "__" in stem:
            name, rest = stem.split("__", 1)
            # 去掉文件名中附带的类型标记，还原真实岗位名
            # 例：林晚__高级后端工程师.resume.md -> name=林晚, position=高级后端工程师
            position = re.sub(r"\.(resume|assessment|transcript|audio|video)(?=\.|$)", "", rest)
        else:
            name, position = stem, ""
        b = buckets.setdefault(name, {"position": position})
        low = base.lower()

        if ".resume" in stem:
            if ext.lower() == ".pdf" and rparse and hasattr(rparse, "parse_resume"):
                b["resume"] = rparse.parse_resume(path)
            else:
                b["resume"] = _read(path)
        elif ".assessment" in stem:
            b["assessment"] = _read(path)
        elif ".transcript" in stem:
            if ext.lower() == ".json":
                try:
                    with open(path, encoding="utf-8") as f:
                        b["interview_transcript"] = json.load(f).get("transcript", "")
                except Exception:
                    b["interview_transcript"] = _read(path)
            else:
                b["interview_transcript"] = _read(path)
        elif ext.lower() in AUDIO_EXTS and asr and hasattr(asr, "transcribe"):
            try:
                out = asr.transcribe(path)
            except Exception as e:
                # 转写失败（未装 faster-whisper / 引擎未配置 / 空结果）必须显式失败：
                # 假转写稿会污染整场评审，宁可在这里停下。
                raise SystemExit("[ingest] %s 转写失败：%s" % (base, e))
            b["interview_transcript"] = out.get("text", "") if isinstance(out, dict) else str(out)

    candidates = []
    for i, (name, b) in enumerate(buckets.items(), 1):
        candidates.append({
            "id": f"in{i}",
            "name": name,
            "position": b.get("position", ""),
            "resume": b.get("resume", ""),
            "assessment": b.get("assessment", ""),
            "interview_transcript": b.get("interview_transcript", ""),
            "job": job,
        })
    return candidates


if __name__ == "__main__":
    # 简易自检：无真实素材时给出空列表提示
    if len(sys.argv) < 2:
        print("用法: python ingest.py <inputs_dir>")
    else:
        out = build_candidates_from_dir(sys.argv[1], {})
        print(json.dumps(out, ensure_ascii=False, indent=2))
