"""ASR 与音频上传链路测试：不启服务、不联网、不装 faster-whisper。

覆盖"错了很难发现、发现时已经造成误判"的点：
- 没有真实引擎时**必须显式失败**（假转写稿曾污染整场评审）；
- whisper 引擎的返回结构（text/segments/duration）是 P1 说话人归属的地基；
- 模型缓存（批量转写不能每次重新加载）；
- 音频上传走 errors 通道、不阻断同批其它文件；
- CLI 侧 ingest 遇音频转写失败必须 fail-fast。

跑法：
    cd tests && python test_asr.py
"""
import base64
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "engine")
sys.path.insert(0, ENGINE)

TMP = tempfile.mkdtemp()
import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "asr.db")
db.reset_init_flag()
import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP

import audio_ingest
import ingest
import api  # noqa: F401  （guess_role 在 api 里；db 已指向 TMP）

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  ← " + str(detail)) if (detail and not cond) else ""))


def sec(t):
    print("\n" + "-" * 58)
    print(" " + t)
    print("-" * 58)


class EnvGuard:
    """ASR_* 环境变量的保存/恢复——测试改完必须还回去。"""

    KEYS = ("ASR_ENGINE", "ASR_MODEL", "ASR_DEVICE", "ASR_COMPUTE_TYPE",
            "ASR_VAD", "ASR_LANGUAGE", "ASR_BEAM")

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def load_asr_module():
    return audio_ingest._load_asr()


class FakeSegment:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class FakeInfo:
    duration = 1830.5
    language = "zh"


class FakeWhisperModel:
    """记录初始化参数与转写参数的假 faster-whisper 模型。"""

    instances = []

    def __init__(self, model, device=None, compute_type=None):
        self.model, self.device, self.compute_type = model, device, compute_type
        self.transcribe_calls = []
        FakeWhisperModel.instances.append(self)

    def transcribe(self, path, **kwargs):
        self.transcribe_calls.append({"path": path, "kwargs": kwargs})
        segs = [FakeSegment(0.0, 4.2, "  面试官：请先做个自我介绍。 "),
                FakeSegment(4.5, 22.8, " 候选人：我做过五年后端开发。 "),
                FakeSegment(23.0, 30.0, "   ")]  # 空段必须被跳过
        return iter(segs), FakeInfo()


def install_fake_whisper():
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeWhisperModel
    sys.modules["faster_whisper"] = mod
    return mod


def uninstall_fake_whisper():
    sys.modules.pop("faster_whisper", None)


# =============================================== 引擎可用性语义
sec("1. 没有真实引擎时必须显式失败（不是返回占位文本）")

asr = load_asr_module()
check("skill 模块可加载", asr is not None and hasattr(asr, "transcribe"))

with EnvGuard():
    os.environ.pop("ASR_ENGINE", None)
    try:
        asr.transcribe("/tmp/whatever.mp3", engine="unavailable")
        check("未配置引擎 → 抛 TranscriptionUnavailable", False, "竟然返回了")
    except asr.TranscriptionUnavailable as e:
        check("未配置引擎 → 抛 TranscriptionUnavailable",
              "faster-whisper" in str(e), e)
    try:
        asr.transcribe("/tmp/whatever.mp3", engine="mock")
        check("mock 引擎已移除 → 抛错", False, "竟然返回了")
    except asr.TranscriptionUnavailable:
        check("mock 引擎已移除 → 抛错", True)
    except Exception as e:
        check("mock 引擎已移除 → 抛错", False, type(e).__name__)

# 真实环境状态：未安装 faster-whisper 时 whisper 分支必须给出安装指引；
# 已安装的环境（如用户本机）则跳过这条，不误报。
with EnvGuard():
    os.environ["ASR_ENGINE"] = "whisper"
    if not asr._whisper_installed():
        try:
            asr.transcribe("/tmp/whatever.mp3")
            check("whisper 但依赖缺失 → 抛错并指引安装", False, "竟然返回了")
        except asr.TranscriptionUnavailable as e:
            check("whisper 但依赖缺失 → 抛错并指引安装",
                  "pip install faster-whisper" in str(e), e)
    else:
        print("  - 本机已装 faster-whisper，跳过“依赖缺失”分支")

d = asr.describe()
check("describe() 返回配置摘要（不加载模型）",
      d.get("engine") in ("whisper", "unavailable") and "model" in d, d)

# =============================================== whisper 引擎结构（假模块注入）
sec("2. whisper 引擎：返回结构 / 段级时间戳 / 模型缓存 / 配置透传")

with EnvGuard():
    os.environ["ASR_ENGINE"] = "whisper"
    os.environ["ASR_MODEL"] = "small"
    os.environ["ASR_DEVICE"] = "cpu"
    os.environ["ASR_COMPUTE_TYPE"] = "int8"
    os.environ["ASR_VAD"] = "1"
    os.environ["ASR_BEAM"] = "3"
    install_fake_whisper()
    asr._MODEL_CACHE.clear()
    FakeWhisperModel.instances.clear()
    try:
        out = asr.transcribe("面试验证.mp3")
        check("返回 text（空段被跳过、按段换行）",
              out["text"] == "面试官：请先做个自我介绍。\n候选人：我做过五年后端开发。",
              repr(out.get("text")))
        check("返回 segments（P1 说话人归属的地基）",
              len(out["segments"]) == 2
              and out["segments"][0] == {"start": 0.0, "end": 4.2,
                                         "text": "面试官：请先做个自我介绍。"},
              out.get("segments"))
        check("返回 duration/language/engine/source",
              out["duration_sec"] == 1830.5 and out["language"] == "zh"
              and out["engine"] == "whisper" and out["source"].endswith("面试验证.mp3"),
              {k: out.get(k) for k in ("duration_sec", "language", "engine", "source")})
        check("模型按配置构造（small/cpu/int8）",
              len(FakeWhisperModel.instances) == 1
              and FakeWhisperModel.instances[0].model == "small"
              and FakeWhisperModel.instances[0].device == "cpu"
              and FakeWhisperModel.instances[0].compute_type == "int8",
              [(i.model, i.device, i.compute_type) for i in FakeWhisperModel.instances])
        kw = FakeWhisperModel.instances[0].transcribe_calls[0]["kwargs"]
        check("配置透传给 transcribe（语言自动/beam/VAD）",
              kw.get("language") is None and kw.get("beam_size") == 3
              and kw.get("vad_filter") is True, kw)

        # 第二次调用：模型必须复用（批量转写不能每次重新加载）
        asr.transcribe("第二段.mp3")
        check("模型实例缓存：二次转写不重新构造",
              len(FakeWhisperModel.instances) == 1, len(FakeWhisperModel.instances))

        # 固定语言
        os.environ["ASR_LANGUAGE"] = "zh"
        asr.transcribe("第三段.mp3")
        kw2 = FakeWhisperModel.instances[0].transcribe_calls[-1]["kwargs"]
        check("ASR_LANGUAGE=zh 透传", kw2.get("language") == "zh", kw2)

        # 关 VAD
        os.environ["ASR_VAD"] = "0"
        asr.transcribe("第四段.mp3")
        kw3 = FakeWhisperModel.instances[0].transcribe_calls[-1]["kwargs"]
        check("ASR_VAD=0 → vad_filter=False", kw3.get("vad_filter") is False, kw3)

        # 空转写必须抛错（空稿与假稿一样有害）
        class EmptyModel(FakeWhisperModel):
            def transcribe(self, path, **kwargs):
                return iter([]), FakeInfo()
        sys.modules["faster_whisper"].WhisperModel = EmptyModel
        asr._MODEL_CACHE.clear()
        try:
            asr.transcribe("静音.mp3")
            check("空转写结果 → 抛错", False, "竟然返回了")
        except asr.TranscriptionUnavailable as e:
            check("空转写结果 → 抛错", "为空" in str(e), e)

        # 引擎内部异常必须包装成可读错误，不能裸 traceback 逃逸
        class BoomModel(FakeWhisperModel):
            def transcribe(self, path, **kwargs):
                raise RuntimeError("CUDA out of memory")
        sys.modules["faster_whisper"].WhisperModel = BoomModel
        asr._MODEL_CACHE.clear()
        try:
            asr.transcribe("崩溃.mp3")
            check("转写内部异常 → 包装为 TranscriptionUnavailable", False, "竟然返回了")
        except asr.TranscriptionUnavailable as e:
            check("转写内部异常 → 包装为 TranscriptionUnavailable",
                  "CUDA out of memory" in str(e) and "ASR_MODEL" in str(e), e)
    finally:
        uninstall_fake_whisper()
        asr._MODEL_CACHE.clear()

# auto 设备解析的一致性（不假设 CI 有没有卡）
with EnvGuard():
    os.environ["ASR_ENGINE"] = "whisper"
    os.environ.pop("ASR_DEVICE", None)
    os.environ.pop("ASR_COMPUTE_TYPE", None)
    dev = asr._resolve_device("auto")
    comp = asr._resolve_compute(dev, "auto")
    check("auto 设备/精度解析自洽",
          dev in ("cpu", "cuda") and comp == ("float16" if dev == "cuda" else "int8"),
          (dev, comp))
    check("显式设备不被 auto 覆盖", asr._resolve_device("cpu") == "cpu")

# CUDA 运行时缺失（有驱动无 cublas）：auto 回退 CPU 并留痕；显式 cuda 不擅自改
with EnvGuard():
    os.environ["ASR_ENGINE"] = "whisper"
    os.environ["ASR_MODEL"] = "tiny"
    os.environ.pop("ASR_DEVICE", None)
    install_fake_whisper()
    asr._MODEL_CACHE.clear()
    _orig_resolve = asr._resolve_device
    asr._resolve_device = lambda d: "cuda" if d == "auto" else d

    class CudaBrokenModel:
        instances = []

        def __init__(self, model, device=None, compute_type=None):
            CudaBrokenModel.instances.append((model, device, compute_type))
            if device == "cuda":
                # 真实世界症状：有 NVIDIA 驱动但缺 CUDA 运行库
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")

        def transcribe(self, path, **kwargs):
            return iter([FakeSegment(0.0, 1.0, " 回退 CPU 后的转写 ")]), FakeInfo()

    try:
        sys.modules["faster_whisper"].WhisperModel = CudaBrokenModel
        out = asr.transcribe("cuda缺库.mp3")
        check("CUDA 运行库缺失 → auto 回退 CPU 完成转写",
              out["text"] == "回退 CPU 后的转写", out.get("text"))
        check("回退事件写入 warnings（留痕，不静默）",
              any("CUDA 运行时不可用" in w for w in out.get("warnings") or []), out.get("warnings"))
        check("回退后按 cpu/int8 重新加载模型",
              CudaBrokenModel.instances[-1] == ("tiny", "cpu", "int8"),
              CudaBrokenModel.instances)

        # 显式指定 cuda 时不擅自回退——用户显式要的，失败就如实失败
        os.environ["ASR_DEVICE"] = "cuda"
        try:
            asr.transcribe("显式cuda.mp3")
            check("显式 ASR_DEVICE=cuda 失败时不静默回退", False, "竟然回退了")
        except asr.TranscriptionUnavailable as e:
            check("显式 ASR_DEVICE=cuda 失败时不静默回退",
                  "cublas" in str(e), str(e)[:120])

        # 非 CUDA 类错误（如 OOM 文案）不应触发回退，直接报错
        class OomModel(FakeWhisperModel):
            def transcribe(self, path, **kwargs):
                raise RuntimeError("CUDA failed with error out of memory")
        sys.modules["faster_whisper"].WhisperModel = OomModel
        os.environ.pop("ASR_DEVICE", None)
        asr._MODEL_CACHE.clear()
        try:
            asr.transcribe("oom.mp3")
            check("非运行库错误不触发回退", False, "竟然回退了")
        except asr.TranscriptionUnavailable as e:
            check("非运行库错误不触发回退", "out of memory" in str(e), str(e)[:120])
    finally:
        asr._resolve_device = _orig_resolve
        uninstall_fake_whisper()
        asr._MODEL_CACHE.clear()

# =============================================== audio_ingest 桥接
sec("3. audio_ingest：字节→临时文件→skill→清理")

check("is_audio 识别音频/视频", audio_ingest.is_audio("一面录音.mp3")
      and audio_ingest.is_audio("a.MP4") and audio_ingest.is_audio("b.webm")
      and not audio_ingest.is_audio("简历.txt"))


class FakeSkill:
    """记录收到的路径；转写后临时文件必须已被删除。"""

    def __init__(self):
        self.seen_paths = []

    def transcribe(self, path, engine=None):
        self.seen_paths.append((path, engine))
        return {"text": "面试官：你好。\n候选人：你好。", "engine": "whisper",
                "duration_sec": 12.0, "language": "zh",
                "segments": [{"start": 0.0, "end": 2.0, "text": "面试官：你好。"},
                             {"start": 2.1, "end": 5.0, "text": "候选人：你好。"}]}


fake_skill = FakeSkill()
audio_ingest._load_asr = lambda: fake_skill  # 注入假 skill
try:
    out = audio_ingest.transcribe_audio("一面录音.mp3", b"FAKE-AUDIO-BYTES")
    check("字节转写：text/segments/duration 透传",
          out["text"].startswith("面试官：你好") and len(out["segments"]) == 2
          and out["duration_sec"] == 12.0, out)
    p = fake_skill.seen_paths[-1][0]
    check("临时文件用后即删（不留音频副本在临时目录）",
          not os.path.exists(p), p)
    check("临时文件保留原扩展名（PyAV 靠后缀探容器）",
          p.endswith(".mp3"), p)

    # 不支持的类型
    try:
        audio_ingest.transcribe_audio("简历.txt", b"hello")
        check("非音频扩展名 → ParseError", False, "竟然返回了")
    except Exception as e:
        check("非音频扩展名 → ParseError", type(e).__name__ == "ParseError", e)

    # 空文件
    try:
        audio_ingest.transcribe_audio("空.mp3", b"")
        check("空文件 → ParseError", False, "竟然返回了")
    except Exception as e:
        check("空文件 → ParseError", type(e).__name__ == "ParseError", e)

    # skill 失败（未装 faster-whisper 等）→ ParseError，走上传 errors 通道
    class FailSkill:
        def transcribe(self, path, engine=None):
            raise RuntimeError("未配置 ASR 引擎，无法转写音频")
    audio_ingest._load_asr = lambda: FailSkill()
    try:
        audio_ingest.transcribe_audio("坏.mp3", b"x")
        check("skill 失败 → ParseError（上传 errors 通道）", False, "竟然返回了")
    except Exception as e:
        check("skill 失败 → ParseError（上传 errors 通道）",
              type(e).__name__ == "ParseError" and "未配置 ASR 引擎" in str(e), e)
finally:
    import importlib
    importlib.reload(audio_ingest)  # 还原 _load_asr

# =============================================== guess_role
sec("4. 角色识别：音频按 transcript 入库")

check("面试录音.mp3 → transcript", api.guess_role("面试录音.mp3") == "transcript")
check("recording1.mp3 → transcript（无中文提示也认）",
      api.guess_role("recording1.mp3") == "transcript")
check("一面视频.mp4 → transcript", api.guess_role("一面视频.mp4") == "transcript")
check("逐字稿.txt → transcript", api.guess_role("逐字稿.txt") == "transcript")
check("测评结果.txt → assessment", api.guess_role("测评结果.txt") == "assessment")
check("张三简历.pdf → resume", api.guess_role("张三简历.pdf") == "resume")
check("未知命名.txt → resume（最常见兜底）", api.guess_role("abc.txt") == "resume")

# =============================================== CLI ingest：音频必须 fail-fast
sec("5. CLI ingest：音频转写失败必须显式停下（不许悄悄跳过）")

in_dir = tempfile.mkdtemp(prefix="asr_in_")
with open(os.path.join(in_dir, "王五__后端工程师.audio.mp3"), "wb") as f:
    f.write(b"NOT-REAL-AUDIO")
with open(os.path.join(in_dir, "王五__后端工程师.transcript.txt"), "w", encoding="utf-8") as f:
    f.write("面试官：讲讲你的项目。\n候选人：好。")

with EnvGuard():
    os.environ.pop("ASR_ENGINE", None)
    try:
        ingest.build_candidates_from_dir(in_dir, {"id": "j1"})
        check("音频无法转写 → ingest 显式失败", False, "竟然继续跑了")
    except SystemExit as e:
        check("音频无法转写 → ingest 显式失败", "转写失败" in str(e), e)
    except Exception as e:
        check("音频无法转写 → ingest 显式失败", False, "%s: %s" % (type(e).__name__, e))

# 同一目录里只有文本转写稿时正常装配（音频缺失不是错误）
in_dir2 = tempfile.mkdtemp(prefix="asr_in2_")
with open(os.path.join(in_dir2, "赵六__后端工程师.transcript.txt"), "w", encoding="utf-8") as f:
    f.write("面试官：你好\n候选人：你好")
cands = ingest.build_candidates_from_dir(in_dir2, {"id": "j2"})
check("纯文本转写稿正常装配", len(cands) == 1
      and "面试官：你好" in cands[0]["interview_transcript"], cands)

# 位置名里的 .audio 标记要被剥掉（否则岗位名变成"后端工程师.audio"）
check("文件名 .audio 标记剥离子岗位名",
      ingest.build_candidates_from_dir(in_dir2, {"id": "j3"})[0]["position"] == "后端工程师")

# =============================================== 汇总
print("\n" + "=" * 58)
print("  通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED: %s" % f)
sys.exit(0 if not FAIL else 1)
