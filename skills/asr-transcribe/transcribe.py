"""asr-transcribe · 音视频转写（未接真实引擎时**必须报错**）。

为什么不再返回占位文本
----------------------
这里曾经返回一段硬编码的假转写稿（"候选人：我曾负责一个高并发系统的稳定性治理…"）。
后果是：ingest 扫到 .mp3 → 拿到假文本 → 塞进 interview_transcript →
ContextStore.has_material() 检查通过（字符串非空）→ 评照跑 →
三位评委拿着这段**从未存在过的**"高并发治理经验"给"硬实力门槛"打分。

整个项目其他部分都在拼命防"看起来像样的假结论"，这个函数是唯一能合法产出它的入口。
所以现在：没有真实 ASR 引擎就抛 TranscriptionUnavailable，让评审在入口处失败，
而不是在一份编造的转写稿上给出 8.2 分。

引擎
----
- ``whisper``：本地 faster-whisper（**唯一允许引入的第三方依赖**，
  只在你需要转写录音时 `pip install faster-whisper`；不装也不影响其他功能）。
  输出段级时间戳——``segments`` 是后续说话人归属（谁在说）的地基，
  没有时间戳的纯文本无法回放到"第几分钟谁说了什么"。
- 其它引擎：自行实现后在这里加分支。

配置（环境变量，均有默认）
--------------------------
- ``ASR_ENGINE``    whisper（不配置则显式失败，见上）
- ``ASR_MODEL``     base（tiny/base/small/medium/large-v3；中文面试建议 small 起）
- ``ASR_DEVICE``    auto（有 CUDA 用 cuda，否则 cpu）
- ``ASR_COMPUTE_TYPE`` auto（cuda→float16，cpu→int8）
- ``ASR_VAD``       1（开 VAD 过滤静音，面试录音长短停顿多，收益明显）
- ``ASR_LANGUAGE``  空=自动识别（面试基本是中文，但自动识别更稳）
- ``ASR_BEAM``      5

返回结构（保持稳定，调用方依赖）::

    {"text": str,              # 全文（按段换行拼接，保留段边界）
     "engine": "whisper",
     "duration_sec": float,
     "source": str,            # 绝对路径
     "language": str,          # 识别出的语言
     "segments": [{"start": float, "end": float, "text": str}, ...]}

text 为空时必须抛错（空转写稿与假转写稿一样有害）。
"""
import argparse
import json
import os
import sys


class TranscriptionUnavailable(RuntimeError):
    """没有可用的 ASR 引擎——这是"未配置"，不是"可以糊过去"。"""


# 模型实例缓存：WhisperModel 加载一次要几秒到几十秒，
# 批量转写时每次调用都重新加载是不可接受的。按 (模型, 设备, 精度) 缓存。
_MODEL_CACHE = {}


def _cfg():
    return {
        "model": os.getenv("ASR_MODEL") or "base",
        "device": (os.getenv("ASR_DEVICE") or "auto").strip(),
        "compute_type": (os.getenv("ASR_COMPUTE_TYPE") or "auto").strip(),
        "vad": (os.getenv("ASR_VAD") or "1").strip() != "0",
        "language": (os.getenv("ASR_LANGUAGE") or "").strip() or None,
        "beam_size": int(os.getenv("ASR_BEAM") or 5),
    }


def _resolve_device(device):
    if device != "auto":
        return device
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _resolve_compute(device, compute_type):
    if compute_type != "auto":
        return compute_type
    return "float16" if device == "cuda" else "int8"


def _get_model(cfg):
    from faster_whisper import WhisperModel  # 可选依赖，延迟导入
    key = (cfg["model"], cfg["device"], cfg["compute_type"])
    m = _MODEL_CACHE.get(key)
    if m is None:
        m = WhisperModel(cfg["model"], device=cfg["device"],
                         compute_type=cfg["compute_type"])
        _MODEL_CACHE[key] = m
    return m


def _whisper_install_hint():
    return ("ASR_ENGINE=whisper 但未安装 faster-whisper。请执行：pip install faster-whisper"
            "（这是全项目唯一允许的第三方依赖，只在你需要转写录音时安装；"
            "不装也不影响其他功能——可以改用 .txt/.md 转写稿，"
            "或让面试平台直接导出文字稿。）")


def _whisper_once(model, path, cfg):
    """跑一次转写，返回 (segments, parts, info)。"""
    segments, info = model.transcribe(
        path,
        language=cfg["language"],
        beam_size=cfg["beam_size"],
        vad_filter=cfg["vad"],
    )
    segs, parts = [], []
    for s in segments:
        t = (getattr(s, "text", "") or "").strip()
        if not t:
            continue
        segs.append({
            "start": round(float(getattr(s, "start", 0.0) or 0.0), 2),
            "end": round(float(getattr(s, "end", 0.0) or 0.0), 2),
            "text": t,
        })
        parts.append(t)
    return segs, parts, info


def _looks_like_cuda_runtime_error(e):
    """有驱动≠有 CUDA 运行时：cublas/cudnn DLL 缺失时报的是加载类错误。"""
    s = str(e).lower()
    return any(k in s for k in ("cublas", "cudnn", "cuda", "nvidia",
                                "not found or cannot be loaded", "dll"))


def transcribe(path, engine=None):
    """把音视频转成文字稿。没有真实引擎就显式失败，不返回占位文本。"""
    engine = engine or os.getenv("ASR_ENGINE") or "unavailable"

    if engine in ("unavailable", "mock", ""):
        raise TranscriptionUnavailable(
            "未配置 ASR 引擎，无法转写音频「%s」。"
            "请改用 .txt/.md 转写稿，或安装 faster-whisper 后设置 ASR_ENGINE=whisper。"
            "（本项目不提供 mock 转写——编造的面试稿会直接污染评审结论。）"
            % (os.path.basename(path or "") or "未提供文件")
        )

    if engine == "whisper":
        try:
            from faster_whisper import WhisperModel  # noqa: F401  可选依赖
        except ImportError:
            raise TranscriptionUnavailable(_whisper_install_hint())
        cfg = _cfg()
        cfg["device"] = _resolve_device(cfg["device"])
        cfg["compute_type"] = _resolve_compute(cfg["device"], cfg["compute_type"])
        # auto 解析到 cuda 的，允许加载失败时回退 CPU；显式指定的不擅自改
        auto_cuda = (os.getenv("ASR_DEVICE") or "auto").strip() == "auto" and cfg["device"] == "cuda"
        warnings = []
        try:
            try:
                model = _get_model(cfg)
                segs, parts, info = _whisper_once(model, path, cfg)
            except Exception as e:
                if not (auto_cuda and _looks_like_cuda_runtime_error(e)):
                    raise
                warnings.append(
                    "CUDA 运行时不可用（%s），已自动回退 CPU 完成转写。"
                    "要手动控制：ASR_DEVICE=cpu；要用 CUDA：安装 CUDA 12 运行库。"
                    % str(e)[:150])
                print("[asr] %s" % warnings[-1], file=sys.stderr)
                cfg["device"], cfg["compute_type"] = "cpu", "int8"
                model = _get_model(cfg)
                segs, parts, info = _whisper_once(model, path, cfg)
        except TranscriptionUnavailable:
            raise
        except Exception as e:
            raise TranscriptionUnavailable(
                "whisper 转写失败（%s）：%s。常见原因：① 文件损坏或格式不支持；"
                "② 显存/内存不足（换更小的 ASR_MODEL）；"
                "③ faster-whisper 与 PyAV 版本不匹配（如 av.open 报 unexpected keyword "
                "argument——重装对齐版本：pip install -U faster-whisper av）；"
                "④ CUDA 运行时缺失（设 ASR_DEVICE=cpu 回退）。"
                % (os.path.basename(path or ""), e)
            )
        text = "\n".join(parts)
        if not text:
            raise TranscriptionUnavailable(
                "whisper 转写结果为空：%s（可能是纯静音/损坏文件，"
                "或语音不是人声——空转写稿与假转写稿一样有害，故拒绝放行）"
                % os.path.basename(path or "")
            )
        out = {
            "text": text,
            "engine": "whisper",
            "duration_sec": round(float(getattr(info, "duration", 0.0) or 0.0), 2),
            "source": os.path.abspath(path),
            "language": getattr(info, "language", "") or "",
            "segments": segs,
        }
        if warnings:
            out["warnings"] = warnings
        return out

    raise TranscriptionUnavailable(
        "未知 ASR 引擎：%s（支持 whisper；其它需自行实现）" % engine)


def describe():
    """返回当前 ASR 配置摘要（不加载模型，供自检/界面显示）。"""
    cfg = _cfg()
    return {
        "engine": os.getenv("ASR_ENGINE") or "unavailable",
        "model": cfg["model"],
        "device": _resolve_device(cfg["device"]),
        "compute_type": _resolve_compute(_resolve_device(cfg["device"]), cfg["compute_type"]),
        "vad": cfg["vad"],
        "language": cfg["language"] or "auto",
        "installed": _whisper_installed(),
    }


def _whisper_installed():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="音视频转写")
    ap.add_argument("path", nargs="?", default="", help="音视频文件路径")
    ap.add_argument("--engine", default=os.getenv("ASR_ENGINE", "unavailable"))
    ap.add_argument("--model", default="", help="覆盖 ASR_MODEL")
    ap.add_argument("--no-vad", action="store_true", help="关闭 VAD 过滤")
    args = ap.parse_args()
    if args.model:
        os.environ["ASR_MODEL"] = args.model
    if args.no_vad:
        os.environ["ASR_VAD"] = "0"
    try:
        print(json.dumps(transcribe(args.path, args.engine), ensure_ascii=False, indent=2))
    except TranscriptionUnavailable as e:
        print("[error] %s" % e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
