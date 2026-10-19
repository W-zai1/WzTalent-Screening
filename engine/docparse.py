"""
文档抽取层：把 HR 上传的各种格式统一转成纯文本，供 LLM 读取。

支持情况（按可靠性排序）：
  .txt/.md/.json/.csv   纯文本，直接读
  .docx                 **纯标准库**：docx 就是 zip + XML，读 word/document.xml 里的 <w:t>
  .pdf                  纯标准库尽力而为：解 FlateDecode 流 + 解析 Tj/TJ 文本算子。
                        文本型 PDF（Word/WPS 导出）可用；扫描件/纯图 PDF 抽不出文字，
                        这时给出明确指引（转 Word 或粘贴文字），而不是返回空字符串让人困惑。
  .png/.jpg/.jpeg/.webp 走视觉模型 OCR（WorkBuddy 的 glm-5v-turbo 等）

设计取舍：
- **不引入第三方依赖**，docx 用标准库解，pdf 用标准库尽力解；解不出来如实告知。
- 抽取结果一律附 `warnings`，让前端能提示"这份 PDF 可能是扫描件"。
"""
import io
import json
import os
import re
import zipfile
import zlib

TEXT_EXTS = {".txt", ".md", ".markdown", ".json", ".csv", ".log"}
DOCX_EXTS = {".docx"}
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
# 音频/视频容器：不走文本抽取，走 asr-transcribe skill 转写（见 audio_ingest）。
# 与 ingest.py 的音频分支、api.py 的上传分支共用同一份名单。
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".mp4", ".webm"}
SUPPORTED_EXTS = TEXT_EXTS | DOCX_EXTS | PDF_EXTS | IMAGE_EXTS

MAX_CHARS = 60000  # 单份文档的抽取上限，防止一份超长 PDF 把上下文撑爆


class ParseError(Exception):
    """文档无法解析。"""


def _clip(text):
    text = (text or "").strip()
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + "\n\n…（内容过长，已截断）"
    return text


# ---------------------------------------------------------------------------
# 纯文本
# ---------------------------------------------------------------------------
def _read_text(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# .docx —— 标准库 zip + XML
# ---------------------------------------------------------------------------
_DOCX_PARA_RE = re.compile(rb"<w:p[ >].*?</w:p>", re.S)
_DOCX_TEXT_RE = re.compile(rb"<w:t[^>]*>(.*?)</w:t>", re.S)
_DOCX_TAB_RE = re.compile(rb"<w:tab\b[^>]*/>")
_DOCX_BR_RE = re.compile(rb"<w:br\b[^>]*/>")


def _xml_unescape(raw: bytes) -> str:
    s = raw.decode("utf-8", "replace")
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'")
             .replace("&amp;", "&"))


def parse_docx(data: bytes) -> str:
    """从 .docx 提取正文文本。

    按 <w:p> 分段、<w:t> 取文字，保留段落结构——简历的行结构对评审很关键
    （比如"【技术栈】精通 Go"这种小节标题不能被粘成一行）。
    表格里的文字同样落在 <w:p> 里，因此一并会被取到。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ParseError("不是有效的 .docx 文件（可能是改了后缀的 .doc，请另存为 .docx）")

    try:
        xml = zf.read("word/document.xml")
    except KeyError:
        raise ParseError(".docx 里找不到 word/document.xml，文件可能已损坏")

    lines = []
    for para in _DOCX_PARA_RE.findall(xml):
        buf = []
        for m in _DOCX_TEXT_RE.finditer(para):
            buf.append(_xml_unescape(m.group(1)))
            # 标签之间的 tab / 换行要保留，否则"精通 Go 与 Java"会粘成一片
        if not buf:
            continue
        # 处理段内的 tab / 手动换行（用位置近似：直接对整段做替换）
        seg = "".join(buf)
        if _DOCX_TAB_RE.search(para):
            seg += "\t"
        lines.append(seg)
    text = "\n".join(lines)
    if not text.strip():
        raise ParseError(".docx 正文为空（可能内容都在图片里，请改用截图/粘贴文字）")
    return text


# ---------------------------------------------------------------------------
# .pdf —— 标准库尽力抽取
# ---------------------------------------------------------------------------
_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_PDF_TJ_RE = re.compile(rb"\[(.*?)\]\s*TJ", re.S)
_PDF_TJ_STR_RE = re.compile(rb"\((?:\\.|[^\\()])*\)")
_PDF_TJ_NUM_RE = re.compile(rb"(-?\d+\.?\d*)")


def _pdf_unescape(raw: bytes) -> str:
    """解 PDF 字面量字符串的转义（\\( \\) \\\\ \\n \\ddd 等）。"""
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == 0x5C and i + 1 < n:  # backslash
            nxt = raw[i + 1]
            if 0x30 <= nxt <= 0x37:  # 八进制 \ddd
                j = i + 1
                oct_digits = b""
                while j < n and len(oct_digits) < 3 and 0x30 <= raw[j] <= 0x37:
                    oct_digits += bytes([raw[j]])
                    j += 1
                try:
                    out.append(int(oct_digits, 8) & 0xFF)
                except ValueError:
                    pass
                i = j
                continue
            mapping = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
            out.append(mapping.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return out.decode("latin-1")


def _hex_to_text(raw: bytes) -> str:
    """解 PDF 十六进制字符串 <48656C6C6F>。"""
    h = re.sub(rb"[^0-9A-Fa-f]", b"", raw)
    if len(h) % 2:
        h += b"0"
    try:
        return bytes.fromhex(h.decode("ascii")).decode("latin-1")
    except Exception:
        return ""


_OP_RE = re.compile(rb"(TJ|Tj|Td|TD|T\*|'|\")")


def _extract_from_content(content: bytes) -> str:
    """按出现顺序遍历内容流，抽取文字并保留换行结构。

    只靠正则各抓各的会把所有文本粘成一行（简历会变成一坨），
    所以这里按 token 顺序走：遇到文本算子取字，遇到定位算子（Td/TD/T*/'/"）
    补一个换行——这样段落结构能保留下来。
    """
    out = []
    i = 0
    n = len(content)
    while i < n:
        c = content[i]

        # 字面量字符串 (...)
        if c == 0x28:  # '('
            j = i + 1
            depth = 1
            while j < n:
                ch = content[j]
                if ch == 0x5C:      # 反斜杠转义，跳过下一个字节
                    j += 2
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:    # ')'
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(_pdf_unescape(content[i + 1:j]))
            i = j + 1
            continue

        # 十六进制字符串 <...>（排除字典起始 <<）
        if c == 0x3C and content[i:i + 2] != b"<<":
            j = content.find(b">", i)
            if j < 0:
                break
            out.append(_hex_to_text(content[i + 1:j]))
            i = j + 1
            continue

        # 算子
        m = _OP_RE.match(content, i)
        if m:
            op = m.group(1)
            if op in (b"Td", b"TD", b"T*", b"'", b'"'):
                out.append("\n")
            i = m.end()
            continue

        i += 1

    text = "".join(out)
    # TJ 数组里的大负字距表示空格，但更常见的换行已经由 Td 补上了
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def parse_pdf(data: bytes):
    """尽力从 PDF 抽取文本。

    返回 (text, warnings)。抽不出文字时 text 为空并附指引性 warning。
    只处理文本型 PDF；CID 子集字体需要 ToUnicode CMap 才能映射，
    这里不实现——那种情况会抽到乱码或空，由 warning 提示用户换格式。
    """
    warnings = []
    raw = data
    chunk_texts = []

    for m in _PDF_STREAM_RE.finditer(raw):
        blob = m.group(1)
        content = None
        try:
            content = zlib.decompress(blob)
        except zlib.error:
            # 未压缩的流：直接当内容用（少见但合法）
            if b"Tj" in blob or b"TJ" in blob:
                content = blob
        if content is None:
            continue
        try:
            t = _extract_from_content(content)
        except Exception:
            continue
        if t.strip():
            # 内容流里 \n 常被当作换行
            chunk_texts.append(t)
        if b"BT" in content and b"Tf" in content:
            pass

    text = "\n".join(chunk_texts)

    # 判断是不是"抽到了但全是乱码"：可读字符占比过低就没意义
    if text.strip():
        sample = text[:2000]
        readable = sum(1 for ch in sample if ch.isprintable() and (ch.isascii() or "\u4e00" <= ch <= "\u9fff"))
        ratio = readable / max(len(sample), 1)
        if ratio < 0.5:
            warnings.append("PDF 文字抽取结果可读性很低，可能是 CID 子集字体或扫描件；建议改用 Word 或直接粘贴文字。")
            return "", warnings
        # 压缩后的内容流之间原本靠换行分隔，这里补回段落感
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text, warnings

    warnings.append(
        "这份 PDF 抽不出文字（多半是扫描件/纯图片 PDF）。"
        "请改用 Word（.docx）、或把 PDF 另存为文本、或直接把内容粘贴进文本框。"
    )
    return "", warnings


# ---------------------------------------------------------------------------
# 图片 —— 交给视觉模型
# ---------------------------------------------------------------------------
def image_to_data_url(data: bytes, ext: str) -> str:
    import base64
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
    }.get(ext.lower(), "image/png")
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


OCR_PROMPT = (
    "请把这张图片里的**全部文字**逐字转录出来，保持原有的换行与小节结构。"
    "如果是简历/测评/招聘要求，请完整保留标题、分点、数字。"
    "只输出转录的文字本身，不要加任何解释、总结或 Markdown 代码块。"
)

VISION_MODELS = ["glm-5v-turbo", "glm-4.6v"]


def parse_image(data: bytes, ext: str, model=None, timeout=180):
    """用视觉模型把图片 OCR 成文字。

    走 WorkBuddy 反代（复用本机积分），因为这是当前唯一可用的视觉通道。
    返回 (text, warnings)。
    """
    warnings = []
    try:
        import workbuddy_client as wb
    except Exception as e:
        return "", ["图片 OCR 需要 WorkBuddy 通道，但加载失败：%s" % e]

    data_url = image_to_data_url(data, ext)
    candidates = [model] if model else list(VISION_MODELS)

    last_err = None
    for mid in candidates:
        try:
            text = wb.chat(
                [{"role": "user", "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]}],
                model=mid, temperature=0.0, timeout=timeout,
            )
            if text and text.strip():
                return text.strip(), warnings
            last_err = "%s 返回了空内容" % mid
        except Exception as e:
            last_err = "%s：%s" % (mid, e)
    warnings.append("图片 OCR 失败（%s）。可以改成把文字直接粘贴进来。" % last_err)
    return "", warnings


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def parse_bytes(filename: str, data: bytes, use_vision=True):
    """按扩展名分派解析。

    返回 dict：{"filename", "ext", "text", "chars", "kind", "warnings"}
    kind: text / docx / pdf / image
    无法解析时抛 ParseError（附可操作的建议）。
    """
    ext = os.path.splitext(filename or "")[1].lower()
    warnings = []

    if ext in TEXT_EXTS:
        return {"filename": filename, "ext": ext, "kind": "text",
                "text": _clip(_read_text(data)), "warnings": warnings}

    if ext in DOCX_EXTS:
        return {"filename": filename, "ext": ext, "kind": "docx",
                "text": _clip(parse_docx(data)), "warnings": warnings}

    if ext in PDF_EXTS:
        text, w = parse_pdf(data)
        warnings.extend(w)
        return {"filename": filename, "ext": ext, "kind": "pdf",
                "text": _clip(text), "warnings": warnings}

    if ext in IMAGE_EXTS:
        if not use_vision:
            raise ParseError("图片需要开启 OCR 才能解析。")
        text, w = parse_image(data, ext)
        warnings.extend(w)
        return {"filename": filename, "ext": ext, "kind": "image",
                "text": _clip(text), "warnings": warnings}

    if ext == ".doc":
        raise ParseError("暂不支持旧版 .doc，请在 Word 里「另存为 .docx」后重试。")
    raise ParseError("不支持的文件类型：%s（支持 %s）"
                     % (ext or "无后缀", "、".join(sorted(SUPPORTED_EXTS))))


def parse_path(path, use_vision=True):
    with open(path, "rb") as f:
        return parse_bytes(os.path.basename(path), f.read(), use_vision=use_vision)


def is_supported(filename):
    return os.path.splitext(filename or "")[1].lower() in (SUPPORTED_EXTS | AUDIO_EXTS)
