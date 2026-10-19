"""audio_ingest.py —— HTTP 上传链路的音频转写桥接。

为什么单独一个模块
----------------
skills/asr-transcribe 的 transcribe(path) 只接受**文件路径**（faster-whisper 的契约），
而 HTTP 上传拿到的是 base64 解码后的**字节**。命令行侧的 ingest.py 天然有路径，
不需要这层；服务侧需要，且 api.py 不该知道 skills 目录的存在——所以抽出来。

职责边界
--------
· 字节 → 临时文件（保留原扩展名，PyAV 靠后缀探容器格式）→ skill → 清理临时文件；
· 把 skill 的 TranscriptionUnavailable 翻成 ParseError（api.py 的上传错误通道）；
· 不加载模型、不判空转写之外的事——那些在 skill 里。
"""
import os
import tempfile

from docparse import AUDIO_EXTS, ParseError

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills")


def _load_asr():
    """动态加载 skills/asr-transcribe/transcribe.py（不假设为包，ingest 同款做法）。"""
    import importlib.util
    p = os.path.join(SKILLS_DIR, "asr-transcribe", "transcribe.py")
    if not os.path.exists(p):
        raise ParseError("asr-transcribe skill 缺失：%s" % p)
    spec = importlib.util.spec_from_file_location("asr-transcribe", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def is_audio(filename):
    return os.path.splitext(filename or "")[1].lower() in AUDIO_EXTS


def transcribe_audio(fname, data, engine=None):
    """把上传的音频字节转成文字稿。

    返回 {"text", "warnings", "engine", "duration_sec", "language", "segments"}。
    任何失败都抛 ParseError——调用方（api 上传链路）把它并进入 `errors` 列表，
    和其它"解析失败"同一通道，不影响同批其它文件入库。
    """
    if not data:
        raise ParseError("%s：文件内容为空" % (fname or "音频"))
    ext = os.path.splitext(fname or "")[1].lower()
    if ext not in AUDIO_EXTS:
        raise ParseError("%s：不支持的音频格式（支持 %s）"
                         % (fname or "音频", "、".join(sorted(AUDIO_EXTS))))
    try:
        asr = _load_asr()
    except ParseError:
        raise
    except Exception as e:
        raise ParseError("%s：ASR 组件加载失败：%s" % (fname or "音频", e))

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext or ".audio", delete=False) as f:
            tmp = f.name
            f.write(data)
        try:
            out = asr.transcribe(tmp, engine=engine)
        except Exception as e:
            # skill 的 TranscriptionUnavailable / faster-whisper 的异常都走这里：
            # 对用户呈现同一形态的"这份音频没能转成文字"，且说明原因。
            raise ParseError("%s：%s" % (fname or "音频", e))
        if not isinstance(out, dict) or not (out.get("text") or "").strip():
            raise ParseError("%s：转写结果为空（纯静音或非人声）" % (fname or "音频"))
        return {
            "text": out["text"],
            "warnings": [],
            "engine": out.get("engine", ""),
            "duration_sec": out.get("duration_sec", 0),
            "language": out.get("language", ""),
            "segments": out.get("segments") or [],
        }
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
