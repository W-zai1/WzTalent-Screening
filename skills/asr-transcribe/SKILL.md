---
name: asr-transcribe
description: 将面试音视频（mp3/wav/mp4/m4a/ogg/webm）转写为带段时间戳的文字稿，供人才筛选引擎装配上下文。引擎为本地 faster-whisper（可选依赖）；未配置引擎时显式报错，绝不返回占位文本。
version: 0.2.0
type: skill
---

# asr-transcribe · 音视频转写独立 Skill

## 用途
人才筛选流水线中，把面试的音视频转成文字稿，作为「上下文」喂给多 Agent 评判引擎
（正方/反方/裁判/评委）。属于 Layer A 的上下文装配环节。
段级时间戳（`segments`）是后续说话人归属（谁在说）的地基——纯文本无法回放到
"第几分钟谁说了什么"。

## 调用方式
```bash
# 命令行
python transcribe.py <输入音视频路径> [--engine whisper] [--model small] [--no-vad]

# 代码（engine/ingest.py 与 engine/audio_ingest.py 都是这么调的）
from transcribe import transcribe, TranscriptionUnavailable
out = transcribe(path)   # engine 取 ASR_ENGINE，未配置则抛 TranscriptionUnavailable
```

输出 JSON：
```json
{
  "text": "全文（按段换行，保留段边界）",
  "engine": "whisper",
  "duration_sec": 1830.5,
  "source": "<绝对路径>",
  "language": "zh",
  "segments": [{"start": 0.0, "end": 4.2, "text": "……"}, ...]
}
```

## 引擎
- `whisper`：本地 faster-whisper。**全项目唯一允许引入的第三方依赖**，
  只在需要转写录音时安装：`pip install faster-whisper`（自带 ctranslate2/PyAV，
  不需要系统装 ffmpeg）。不装不影响其他功能——可改用 .txt/.md 转写稿，
  或让面试平台直接导出文字稿。
- 其它引擎（外部 ASR API 等）：自行实现后在 `transcribe()` 里加分支，保持返回结构不变。

**没有 mock 引擎，这是刻意的**：本 skill 曾经返回硬编码的假转写稿，
导致 ingest 把从未存在的"高并发治理经验"喂给三位评委打分。
所以未配置引擎时抛 `TranscriptionUnavailable`，让评审在入口处失败，
而不是在一份编造的面试稿上给出 8.2 分。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ASR_ENGINE` | 空（=显式失败） | `whisper` |
| `ASR_MODEL` | `base` | tiny/base/small/medium/large-v3；中文面试建议 `small` 起 |
| `ASR_DEVICE` | `auto` | 有 CUDA 自动用 cuda，否则 cpu。**有驱动≠有运行时**：缺 cublas 等运行库时 auto 尝试 CUDA、失败后自动回退 CPU 并在返回的 `warnings` 里留痕；显式设 `cuda` 则不擅自回退，如实报错 |
| `ASR_COMPUTE_TYPE` | `auto` | cuda→float16，cpu→int8 |
| `ASR_VAD` | `1` | VAD 过滤静音；面试录音长短停顿多，收益明显 |
| `ASR_LANGUAGE` | 空=自动识别 | 固定 `zh` 可略减开销 |
| `ASR_BEAM` | `5` | beam search 宽度 |

模型实例按 (模型,设备,精度) 进程内缓存——批量转写不会重复加载。

## 在评判引擎中如何被使用
两条入口，都最终写入 candidate 的 `interview_transcript` 字段，无需改动辩论/打分逻辑：
1. **命令行**：`engine/ingest.py` 扫到 `inputs/` 下的音频文件 → 调本 skill；
2. **HTTP 上传**：`engine/audio_ingest.py` 把上传字节写临时文件 → 调本 skill →
   结果按 `transcript` 角色入库（与文本转写稿同一通道）。
