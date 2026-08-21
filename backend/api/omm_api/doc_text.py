"""附件正文抽取：把上传的产物变成 Agent 能直接读的纯文本。

浏览器侧已经做过一轮即时解析，那是给用户看的反馈；这里是权威结果——格式覆盖
更全（旧版二进制 Office、图片 OCR），也不受浏览器内存和主线程的限制。建模与
论文环节引用附件内容时以本模块的输出为准。

依赖策略：OOXML、OpenDocument、压缩包与纯文本全部用标准库的 ``zipfile`` +
``ElementTree`` 完成，只有 PDF 依赖 ``pypdf``。旧版 ``.doc``/``.xls``、RTF 与
OCR 走可选依赖，缺库时如实返回 ``unsupported`` 并说明原因——控制面不该因为少装
一个包就抛 500，用户需要的是"换个格式重传"这样的可执行提示。
"""

from __future__ import annotations

import codecs
import io
import logging
import os
import queue
import re
import shutil
import struct
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from typing import Callable, Iterable, Optional

logger = logging.getLogger("omm.doc_text")

# 单个附件保留的正文上限：够覆盖整篇赛题与论文，又不至于把一行数据库记录撑爆。
MAX_TEXT_CHARS = 400_000
MAX_PDF_PAGES = 500
MAX_SHEET_ROWS = 5_000
MAX_ROW_CELLS = 128
MAX_ARCHIVE_ENTRIES = 500
MAX_ARCHIVE_TEXT_ENTRIES = 12
MAX_ARCHIVE_ENTRY_BYTES = 2 * 1024 * 1024

#: ready=完整抽出；partial=触顶截断；empty=文件正常但没有文字；
#: unsupported=缺少可选依赖或格式不支持；failed=文件损坏或抽取出错。
Status = str


@dataclass(frozen=True)
class Extraction:
    status: Status
    engine: str
    text: str = ""
    segments: Optional[int] = None
    detail: Optional[str] = None
    #: 文档内嵌图片数（去重后的近似值）；None = 该格式不统计。纯文本模型看不到
    #: 图片，这个数字是前端「单模态提醒」与后续视觉解析（ADR-0010）的数据底座。
    images: Optional[int] = None

    @property
    def characters(self) -> int:
        return len(self.text)

    def truncated(self) -> "Extraction":
        if len(self.text) <= MAX_TEXT_CHARS:
            return self
        return Extraction(
            status="partial",
            engine=self.engine,
            text=self.text[:MAX_TEXT_CHARS],
            segments=self.segments,
            detail=f"正文超过 {MAX_TEXT_CHARS} 字，已截断保存",
            images=self.images,
        )


def _decode(data: bytes) -> str:
    """字节转文本。国内赛题附件里 GB18030 编码的 CSV/TXT 很常见，UTF-8 解不动才回落。"""

    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            return data.decode(encoding, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gb18030", errors="replace")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _tidy(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n").replace("\r", "\n"))).strip()


def _paragraphs(
    xml: bytes,
    paragraph: str,
    leaf: str,
    breaks: frozenset[str] = frozenset(),
    skip: frozenset[str] = frozenset(),
) -> list[str]:
    """按文档顺序把 XML 折成行。

    ``skip`` 是属性容器（``w:pPr``、``w:rPr`` 等）。它们内部也会出现 ``w:tab``
    这类元素——那是"这一段的制表位设置"，不是正文里的制表符，照抽会在每段行首
    插入莫名其妙的空白。
    """

    lines: list[str] = []
    buffer: list[str] = []
    skipped = 0
    for event, element in ET.iterparse(io.BytesIO(xml), events=("start", "end")):
        tag = _local(element.tag)
        if event == "start":
            if tag in skip:
                skipped += 1
            elif skipped == 0 and tag in breaks:
                buffer.append("\t" if tag == "tab" else "\n")
            continue
        if tag in skip:
            skipped = max(0, skipped - 1)
        elif skipped > 0:
            continue
        elif tag == leaf:
            buffer.append(element.text or "")
        elif tag == paragraph:
            line = "".join(buffer)
            if line.strip():
                lines.append(line)
            buffer.clear()
            element.clear()
    return lines


def _zip_of(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def _read(archive: zipfile.ZipFile, name: str) -> Optional[bytes]:
    try:
        return archive.read(name)
    except KeyError:
        return None


def _media_count(archive: zipfile.ZipFile, prefix: str) -> int:
    """OOXML 的插图统一存放在 media/ 部件下，数条目即得内嵌图片数。"""

    return sum(1 for name in archive.namelist() if name.startswith(prefix) and not name.endswith("/"))


DOCX_BREAKS = frozenset({"tab", "br", "cr"})
DOCX_SKIP = frozenset({"pPr", "rPr", "sectPr", "tblPr", "trPr", "tcPr", "tabs"})


def _extract_docx(data: bytes) -> Extraction:
    with _zip_of(data) as archive:
        main = _read(archive, "word/document.xml")
        if main is None:
            return Extraction("failed", "docx", detail="docx 缺少正文部件 word/document.xml")
        parts = [main]
        for extra in ("word/footnotes.xml", "word/endnotes.xml"):
            payload = _read(archive, extra)
            if payload:
                parts.append(payload)
        images = _media_count(archive, "word/media/")
    lines = [line for part in parts for line in _paragraphs(part, "p", "t", DOCX_BREAKS, DOCX_SKIP)]
    return Extraction("ready", "docx", _tidy("\n".join(lines)), segments=len(lines), images=images)


PPTX_SKIP = frozenset({"pPr", "rPr", "endParaRPr", "defRPr", "lstStyle"})


def _slide_ordinal(name: str) -> int:
    found = re.search(r"(\d+)\D*$", name)
    return int(found.group(1)) if found else 0


def _extract_pptx(data: bytes) -> Extraction:
    with _zip_of(data) as archive:
        names = archive.namelist()
        slides = sorted(
            (name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=_slide_ordinal,
        )
        if not slides:
            return Extraction("failed", "pptx", detail="pptx 里没有找到任何幻灯片")
        pages: list[str] = []
        for index, name in enumerate(slides, start=1):
            body = _paragraphs(archive.read(name), "p", "t", skip=PPTX_SKIP)
            block = [f"# 第 {index} 页", *body]
            notes = _read(archive, f"ppt/notesSlides/notesSlide{_slide_ordinal(name)}.xml")
            if notes:
                spoken = _paragraphs(notes, "p", "t", skip=PPTX_SKIP)
                if spoken:
                    block.append("备注：" + " ".join(spoken))
            pages.append("\n".join(block))
        images = _media_count(archive, "ppt/media/")
    return Extraction("ready", "pptx", _tidy("\n\n".join(pages)), segments=len(slides), images=images)


def _column_index(reference: str) -> int:
    letters = re.match(r"^[A-Z]+", reference)
    if not letters:
        return 0
    total = 0
    for letter in letters.group(0):
        total = total * 26 + (ord(letter) - 64)
    return total


def _shared_strings(xml: Optional[bytes]) -> list[str]:
    if not xml:
        return []
    strings: list[str] = []
    for _event, element in ET.iterparse(io.BytesIO(xml), events=("end",)):
        if _local(element.tag) != "si":
            continue
        strings.append("".join(node.text or "" for node in element.iter() if _local(node.tag) == "t"))
        element.clear()
    return strings


def _sheet_lines(xml: bytes, shared: list[str]) -> tuple[list[str], int]:
    lines: list[str] = []
    cells: list[str] = []
    rows = 0
    for _event, element in ET.iterparse(io.BytesIO(xml), events=("end",)):
        tag = _local(element.tag)
        if tag == "c":
            kind = element.get("t") or ""
            value = ""
            for child in element:
                child_tag = _local(child.tag)
                if child_tag == "v":
                    value = child.text or ""
                elif child_tag == "is":
                    value = "".join(node.text or "" for node in child.iter() if _local(node.tag) == "t")
            if kind == "s":
                index = int(value) if value.isdigit() else -1
                value = shared[index] if 0 <= index < len(shared) else ""
            # 空单元格在 XML 里直接缺席，按列号补齐才能保住列的对应关系。
            column = _column_index(element.get("r") or "")
            while 0 < column and len(cells) < column - 1 and len(cells) < MAX_ROW_CELLS:
                cells.append("")
            if len(cells) < MAX_ROW_CELLS:
                cells.append(value)
            element.clear()
        elif tag == "row":
            rows += 1
            if rows <= MAX_SHEET_ROWS:
                line = "\t".join(cells)
                if line.strip():
                    lines.append(line)
            cells = []
            element.clear()
    return lines, rows


def _extract_xlsx(data: bytes) -> Extraction:
    with _zip_of(data) as archive:
        workbook = _read(archive, "xl/workbook.xml")
        if workbook is None:
            return Extraction("failed", "xlsx", detail="xlsx 缺少工作簿部件 xl/workbook.xml")
        shared = _shared_strings(_read(archive, "xl/sharedStrings.xml"))

        relationships: dict[str, str] = {}
        rels = _read(archive, "xl/_rels/workbook.xml.rels")
        if rels:
            for element in ET.fromstring(rels):
                identifier, target = element.get("Id"), element.get("Target")
                if identifier and target:
                    relationships[identifier] = "xl/" + re.sub(r"^/?(xl/)?", "", target)

        blocks: list[str] = []
        truncated = False
        # 工作表顺序按 workbook 里的关系走，不能靠 sheet1.xml 这样的文件名猜——
        # 删过工作表的簿子里两者对不上。
        for position, element in enumerate(ET.fromstring(workbook).iter(), start=0):
            if _local(element.tag) != "sheet":
                continue
            name = element.get("name") or f"Sheet{len(blocks) + 1}"
            relation = next((v for k, v in element.attrib.items() if _local(k) == "id"), "")
            path = relationships.get(relation) or f"xl/worksheets/sheet{len(blocks) + 1}.xml"
            payload = _read(archive, path)
            if payload is None:
                continue
            lines, rows = _sheet_lines(payload, shared)
            truncated = truncated or rows > MAX_SHEET_ROWS
            blocks.append("\n".join([f"# 工作表：{name}（{rows} 行）", *lines]))

    if not blocks:
        return Extraction("failed", "xlsx", detail="xlsx 里没有找到任何工作表数据")
    suffix = f"\n\n（每个工作表最多抽取前 {MAX_SHEET_ROWS} 行）" if truncated else ""
    return Extraction("ready", "xlsx", _tidy("\n\n".join(blocks) + suffix), segments=len(blocks))


def _odf_line(element: ET.Element) -> str:
    """把一个 ODF 段落摊平成一行。

    ``itertext()`` 不够用：空白在 ODF 里是显式元素（``text:tab``、``text:s``、
    ``text:line-break``），直接取文本会把制表和换行全丢掉。
    """

    parts: list[str] = []
    if element.text:
        parts.append(element.text)

    def walk(node: ET.Element) -> None:
        for child in node:
            tag = _local(child.tag)
            if tag == "tab":
                parts.append("\t")
            elif tag == "line-break":
                parts.append("\n")
            elif tag == "s":
                count = next((v for k, v in child.attrib.items() if _local(k) == "c"), "1")
                parts.append(" " * (int(count) if count.isdigit() else 1))
            else:
                if child.text:
                    parts.append(child.text)
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(element)
    return "".join(parts)


def _extract_opendocument(data: bytes) -> Extraction:
    with _zip_of(data) as archive:
        content = _read(archive, "content.xml")
    if content is None:
        return Extraction("failed", "odf", detail="OpenDocument 缺少 content.xml")

    lines: list[str] = []
    row: Optional[list[str]] = None
    # 表格行的开始必须在它内部的段落之前被看到，所以不能只监听 end 事件。
    for event, element in ET.iterparse(io.BytesIO(content), events=("start", "end")):
        tag = _local(element.tag)
        if event == "start":
            if tag == "table-row":
                row = []
            continue
        if tag in ("p", "h"):
            line = _odf_line(element)
            if line.strip():
                (row if row is not None else lines).append(line)
        elif tag == "table-row" and row is not None:
            if any(cell.strip() for cell in row):
                lines.append("\t".join(row))
            row = None
    return Extraction("ready", "odf", _tidy("\n".join(lines)), segments=len(lines))


def _pdf_image_count(reader: object, page_limit: int) -> Optional[int]:
    """按页扫 /Resources//XObject 统计 /Image，按间接引用去重（页眉 logo 会跨页复用）。

    只统计顶层 XObject，Form 内嵌套的图不递归——这是给单模态提醒（ADR-0010）用的
    近似值，不是渲染。任何结构异常都放弃计数，绝不拖垮正文抽取。
    """

    try:
        pages = reader.pages  # type: ignore[attr-defined]
        seen: set[tuple[int, int]] = set()
        anonymous = 0
        for index in range(page_limit):
            resources = pages[index].get("/Resources")
            if resources is None:
                continue
            xobjects = resources.get_object().get("/XObject")
            if xobjects is None:
                continue
            for value in xobjects.get_object().values():
                target = value.get_object()
                if target.get("/Subtype") != "/Image":
                    continue
                reference = getattr(target, "indirect_reference", None) or (
                    value if hasattr(value, "idnum") else None
                )
                if reference is not None:
                    seen.add((reference.idnum, reference.generation))
                else:
                    anonymous += 1
        return len(seen) + anonymous
    except Exception:
        return None


def _extract_pdf(data: bytes) -> Extraction:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:  # pragma: no cover - 部署缺依赖时的兜底
        return Extraction("unsupported", "pdf", detail="服务端未安装 pypdf，无法解析 PDF")

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return Extraction("failed", "pdf", detail="PDF 已加密，需要去除口令后重新上传")

    total = len(reader.pages)
    read = min(total, MAX_PDF_PAGES)
    pages = [reader.pages[index].extract_text() or "" for index in range(read)]
    text = _tidy("\n\n".join(pages))
    images = _pdf_image_count(reader, read)
    if not text:
        # 扫描件：文字层为空时优先走 PaddleOCR-VL（公式→LaTeX、表格→表格标记）。
        parsed = _vl_parse(data, ".pdf")
        if parsed:
            return Extraction(
                "ready",
                "paddleocr-vl",
                parsed,
                segments=total,
                detail="扫描件经 PaddleOCR-VL 解析为 Markdown（公式为 LaTeX）",
                images=images,
            )
        detail = (
            "PaddleOCR-VL 没有从扫描件中识别出内容"
            if parsed == ""
            else "PDF 没有文字层（多半是扫描件），需要在服务端启用 OCR（Tesseract 或 vl 附加项）后重试"
        )
        return Extraction("empty", "pdf", segments=total, detail=detail, images=images)
    if read < total:
        return Extraction("partial", "pdf", text, segments=total, detail=f"仅抽取了前 {read} 页", images=images)
    return Extraction("ready", "pdf", text, segments=total, images=images)


def _extract_plain(data: bytes) -> Extraction:
    text = _tidy(_decode(data))
    if not text:
        return Extraction("empty", "text", detail="文件里没有可读文字")
    return Extraction("ready", "text", text, segments=text.count("\n") + 1)


# ── PaddleOCR-VL 可选视觉解析后端（ADR-0010 批次二）─────────────────
#
# 0.9B 文档解析 VLM：扫描件与图片解析成 Markdown（公式→LaTeX、表格→表格标记），
# 对纯文本模型几乎无损。惰性单例：首次调用加载模型（可能下载权重），之后复用；
# 开发链在 API 进程内直跑，生产隔离随独立 Worker 接线迁移到执行面。

_VL_PIPELINE: Optional[object] = None
_VL_UNAVAILABLE: Optional[str] = None
#: paddle 动态图模式是线程局部状态：模型在哪个线程创建就只能在哪个线程推理，
#: 跨线程 predict 会以「int(Tensor) is not supported in static graph mode」的
#: RuntimeError 失败（2026-08-20 实测）。因此初始化与全部推理都固定在同一条
#: 专用工作线程上；单工作线程同时天然串行化了并发解析（旧 _VL_LOCK 的职责）。
#:
#: 刻意不用 concurrent.futures.ThreadPoolExecutor：它的工作线程非守护且注册了
#: atexit join——若线程恰好阻塞（如向已死的 stderr 管道写日志），整个进程退出
#: 会被永久挂住，uvicorn --reload 的重启因此堵死（2026-08-20 py-spy 实证）。
_VL_QUEUE: "queue.SimpleQueue[tuple[Callable[[], object], dict, threading.Event]]" = (
    queue.SimpleQueue()
)
_VL_WORKER: Optional[threading.Thread] = None
_VL_WORKER_LOCK = threading.Lock()


def _vl_worker_loop() -> None:
    while True:
        func, box, done = _VL_QUEUE.get()
        try:
            box["value"] = func()
        except BaseException as error:  # noqa: BLE001 异常原样带回调用线程
            box["error"] = error
        finally:
            done.set()


def _vl_call(func: Callable[[], object]) -> object:
    """把调用投递到 VL 专用守护线程执行并等待结果；工作线程按需惰性拉起。"""

    global _VL_WORKER
    with _VL_WORKER_LOCK:
        if _VL_WORKER is None or not _VL_WORKER.is_alive():
            _VL_WORKER = threading.Thread(target=_vl_worker_loop, name="omm-vl", daemon=True)
            _VL_WORKER.start()
    box: dict = {}
    done = threading.Event()
    _VL_QUEUE.put((func, box, done))
    done.wait()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


def _vl_pipeline() -> Optional[object]:
    """构建/返回惰性单例。只允许在 VL 专用工作线程内调用。"""

    global _VL_PIPELINE, _VL_UNAVAILABLE
    if _VL_PIPELINE is not None or _VL_UNAVAILABLE is not None:
        return _VL_PIPELINE
    try:
        from paddleocr import PaddleOCRVL
    except ModuleNotFoundError:
        _VL_UNAVAILABLE = "未安装 paddleocr[doc-parser]"
        return None
    except Exception as error:  # paddle 栈在个别环境上导入即崩，如实降级
        _VL_UNAVAILABLE = f"paddleocr 导入失败：{type(error).__name__}"
        logger.warning("%s", _VL_UNAVAILABLE, exc_info=True)
        return None
    try:
        _VL_PIPELINE = PaddleOCRVL()
    except Exception as error:
        _VL_UNAVAILABLE = f"PaddleOCR-VL 初始化失败：{type(error).__name__}"
        logger.warning("%s", _VL_UNAVAILABLE, exc_info=True)
        return None
    return _VL_PIPELINE


def _vl_markdown(results: Iterable[object]) -> str:
    """把 predict 返回的逐页结果收敛成 Markdown 文本（在 VL 工作线程内取尽）。"""

    pages: list[str] = []
    for result in results:
        markdown = getattr(result, "markdown", None)
        text = markdown.get("markdown_texts", "") if isinstance(markdown, Mapping) else ""
        if isinstance(text, str) and text.strip():
            pages.append(text)
    return _tidy("\n\n".join(pages))


def _vl_text(path: str) -> Optional[str]:
    """专用工作线程内的完整解析：建管线 → predict → 抽取逐页 Markdown。

    推理结果也在本线程内取尽（paddleocr 的 predict 返回已物化的列表），
    绝不把惰性对象带出线程。
    """

    pipeline = _vl_pipeline()
    if pipeline is None:
        return None
    text = _vl_markdown(pipeline.predict(path))  # type: ignore[attr-defined]
    if text:
        return text
    # 版面检测（PP-DocLayoutV3）面向整页文档训练，对稀疏截图——大片留白里
    # 两行公式的小图——常检出 0 个区域，放大也无济于事（2026-08-21 实测），
    # 正文因此恒为空。版面一无所获时把整图直接交给识别端重试一次；
    # 真实文档页首轮已有结果，不会走到这里。
    return _vl_markdown(pipeline.predict(path, use_layout_detection=False))  # type: ignore[attr-defined]


def warmup_vl() -> None:
    """预热 VL 惰性单例：把约 90 秒的模型冷加载从首个用户请求挪到进程启动。

    设计给启动期的守护线程调用；加载发生在 VL 专用工作线程上（与后续推理
    同一线程），请求撞上预热未完成时在队列里排队等待而不是重复加载。
    未安装 vl 附加项时等同一次空操作。
    """

    started = time.monotonic()
    if _vl_call(_vl_pipeline) is None:
        logger.info("PaddleOCR-VL 预热跳过：%s", _VL_UNAVAILABLE)
    else:
        logger.info("PaddleOCR-VL 预热完成，耗时 %.1f 秒", time.monotonic() - started)


def _vl_parse(data: bytes, suffix: str) -> Optional[str]:
    """把图片/PDF 字节交给 PaddleOCR-VL，返回逐页 Markdown 合并文本。

    返回 None = 后端不可用或本次解析失败（调用方走既有降级路径）；
    返回 ""   = 后端跑了但没有识别出内容。predict 只收路径，先落临时文件。
    """

    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        handle.write(data)
        handle.close()
        result = _vl_call(partial(_vl_text, handle.name))
        return result if result is None else str(result)
    except Exception:
        # 静默降级曾让「跨线程推理失败」隐身数日；失败原因必须落日志。
        logger.warning("PaddleOCR-VL 解析失败", exc_info=True)
        return None
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def _extract_rtf(data: bytes) -> Extraction:
    try:
        from striprtf.striprtf import rtf_to_text
    except ModuleNotFoundError:
        return Extraction("unsupported", "rtf", detail="服务端未安装 striprtf，无法解析 RTF")
    text = _tidy(rtf_to_text(_decode(data), errors="ignore"))
    if not text:
        return Extraction("empty", "rtf", detail="RTF 里没有可读文字")
    return Extraction("ready", "rtf", text, segments=text.count("\n") + 1)


# Word 二进制格式里的控制字符：制表/换行照映射，域代码与图片锚点直接丢弃。
_DOC_REPLACEMENTS = {
    0x07: "\t",
    0x0B: "\n",
    0x0C: "\n",
    0x0D: "\n",
    0x1E: "-",
    0x1F: "",
    0xA0: " ",
}
_DOC_DROPPED = {0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x08}


def _clean_doc_text(raw: str) -> str:
    out: list[str] = []
    in_field_code = False
    for character in raw:
        code = ord(character)
        if code == 0x13:  # 域开始，后面是 HYPERLINK 之类的代码而不是正文
            in_field_code = True
            continue
        if code in (0x14, 0x15):  # 域分隔/结束，之后是域的显示结果
            in_field_code = False
            continue
        if in_field_code or code in _DOC_DROPPED:
            continue
        out.append(_DOC_REPLACEMENTS.get(code, character))
    return "".join(out)


def _word_piece_table(clx: bytes) -> Optional[bytes]:
    index = 0
    while index + 1 < len(clx):
        kind = clx[index]
        if kind == 0x01:  # Prc：属性修饰，跳过
            size = struct.unpack_from("<h", clx, index + 1)[0]
            index += 3 + size
        elif kind == 0x02:  # Pcdt：分片表本体
            size = struct.unpack_from("<I", clx, index + 1)[0]
            return clx[index + 5:index + 5 + size]
        else:
            return None
    return None


def _extract_doc(data: bytes) -> Extraction:
    """Word 97-2003：按 FIB 找到分片表，再逐片从正文流里取字。

    ``.doc`` 的正文不是连续存放的，而是被分片表（piece table）切成若干段，每段
    自带偏移量和"单字节 CP1252 还是双字节 UTF-16"的标志。不走分片表直接扫字符串
    会把修订记录、域代码和已删除的文字一起抽出来。
    """

    try:
        import olefile
    except ModuleNotFoundError:
        return Extraction("unsupported", "doc", detail="服务端未安装 olefile，无法解析旧版 .doc")

    if not olefile.isOleFile(io.BytesIO(data)):
        return Extraction("failed", "doc", detail="不是合法的 Word 97-2003 文件")
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        if not ole.exists("WordDocument"):
            return Extraction("failed", "doc", detail="缺少 WordDocument 流")
        document = ole.openstream("WordDocument").read()
        if len(document) < 0x01A6 + 4:
            return Extraction("failed", "doc", detail="WordDocument 流不完整")
        flags = struct.unpack_from("<H", document, 0x0A)[0]
        table_name = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(table_name):
            return Extraction("failed", "doc", detail=f"缺少 {table_name} 流")
        table = ole.openstream(table_name).read()

    fc_clx, lcb_clx = struct.unpack_from("<II", document, 0x01A2)
    plc = _word_piece_table(table[fc_clx:fc_clx + lcb_clx])
    if not plc or len(plc) < 16:
        return Extraction("failed", "doc", detail="分片表缺失或损坏")

    count = (len(plc) - 4) // 12
    positions = struct.unpack_from(f"<{count + 1}I", plc, 0)
    chunks: list[str] = []
    for piece in range(count):
        offset = 4 * (count + 1) + 8 * piece
        descriptor = struct.unpack_from("<I", plc, offset + 2)[0]
        length = positions[piece + 1] - positions[piece]
        if descriptor & 0x40000000:
            start = (descriptor & 0x3FFFFFFF) // 2
            chunks.append(document[start:start + length].decode("cp1252", errors="replace"))
        else:
            start = descriptor & 0x3FFFFFFF
            chunks.append(document[start:start + length * 2].decode("utf-16-le", errors="replace"))

    text = _tidy(_clean_doc_text("".join(chunks)))
    if not text:
        return Extraction("empty", "doc", detail="文档里没有可读正文")
    return Extraction("ready", "doc", text, segments=text.count("\n") + 1)


def _extract_xls(data: bytes) -> Extraction:
    try:
        import xlrd
    except ModuleNotFoundError:
        return Extraction("unsupported", "xls", detail="服务端未安装 xlrd，无法解析旧版 .xls")
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as error:  # xlrd 对损坏文件抛的异常类型不统一
        return Extraction("failed", "xls", detail=f"无法打开 .xls：{error}")

    blocks: list[str] = []
    truncated = False
    for sheet in book.sheets():
        lines = [f"# 工作表：{sheet.name}（{sheet.nrows} 行）"]
        truncated = truncated or sheet.nrows > MAX_SHEET_ROWS
        for index in range(min(sheet.nrows, MAX_SHEET_ROWS)):
            cells = ["" if cell.value is None else str(cell.value) for cell in sheet.row(index)[:MAX_ROW_CELLS]]
            line = "\t".join(cells)
            if line.strip():
                lines.append(line)
        blocks.append("\n".join(lines))
    suffix = f"\n\n（每个工作表最多抽取前 {MAX_SHEET_ROWS} 行）" if truncated else ""
    return Extraction("ready", "xls", _tidy("\n\n".join(blocks) + suffix), segments=len(blocks))


def _extract_image(data: bytes, languages: str) -> Extraction:
    # 优先 PaddleOCR-VL：对公式、表格、图表远强于逐字 OCR；不可用再回落 Tesseract。
    parsed = _vl_parse(data, ".png")
    if parsed:
        return Extraction("ready", "paddleocr-vl", parsed, segments=parsed.count("\n") + 1, images=1)
    if parsed == "":
        return Extraction("empty", "paddleocr-vl", detail="PaddleOCR-VL 没有识别出内容", images=1)

    if shutil.which("tesseract") is None:
        return Extraction(
            "unsupported",
            "ocr",
            detail="服务端未安装 Tesseract OCR，图片附件暂时只登记不转文字（可安装 ocr 或 vl 附加项启用识别）",
            images=1,
        )
    try:
        import pytesseract
        from PIL import Image
    except ModuleNotFoundError:
        return Extraction(
            "unsupported",
            "ocr",
            detail="服务端未安装 pytesseract/Pillow，图片附件暂时只登记不转文字",
            images=1,
        )

    with Image.open(io.BytesIO(data)) as image:
        text = _tidy(pytesseract.image_to_string(image, lang=languages))
    if not text:
        return Extraction("empty", "ocr", detail="OCR 没有识别出文字", images=1)
    return Extraction("ready", "ocr", text, segments=text.count("\n") + 1, images=1)


def _extract_archive(data: bytes, languages: str) -> Extraction:
    with _zip_of(data) as archive:
        entries = [item for item in archive.infolist() if not item.is_dir()][:MAX_ARCHIVE_ENTRIES]
        manifest = [f"# 压缩包内容（{len(entries)} 个文件）"]
        manifest += [f"- {item.filename}（{item.file_size} 字节）" for item in entries]

        bodies: list[str] = []
        expanded = 0
        for item in entries:
            if expanded >= MAX_ARCHIVE_TEXT_ENTRIES or item.file_size > MAX_ARCHIVE_ENTRY_BYTES:
                continue
            extractor = _extractor_for(item.filename, "")
            # 压缩包里再套压缩包不展开：一层就够用，再深下去容易被构造成解压炸弹。
            if extractor is None or extractor is _extract_archive:
                continue
            nested = _run(extractor, archive.read(item), languages)
            if nested.status in ("ready", "partial") and nested.text:
                bodies.append(f"# {item.filename}\n{nested.text}")
                expanded += 1

    return Extraction(
        "ready",
        "zip",
        _tidy("\n\n".join([*manifest, *bodies])),
        segments=len(entries),
        detail=None if expanded == len(entries) else f"已展开 {expanded} 个文件的正文，其余仅登记文件名",
    )


Extractor = Callable[..., Extraction]

_BY_EXTENSION: dict[str, Extractor] = {
    "pdf": _extract_pdf,
    "docx": _extract_docx,
    "docm": _extract_docx,
    "doc": _extract_doc,
    "rtf": _extract_rtf,
    "odt": _extract_opendocument,
    "ods": _extract_opendocument,
    "odp": _extract_opendocument,
    "xlsx": _extract_xlsx,
    "xlsm": _extract_xlsx,
    "xls": _extract_xls,
    "pptx": _extract_pptx,
    "zip": _extract_archive,
    "png": _extract_image,
    "jpg": _extract_image,
    "jpeg": _extract_image,
    "webp": _extract_image,
    "gif": _extract_image,
    "bmp": _extract_image,
    "tif": _extract_image,
    "tiff": _extract_image,
}

_PLAIN_EXTENSIONS = frozenset({
    "txt", "log", "md", "markdown", "mdx", "rst", "tex", "bib", "csv", "tsv",
    "json", "jsonl", "ndjson", "geojson", "ipynb", "py", "m", "r", "jl", "js",
    "ts", "java", "c", "h", "cpp", "cs", "go", "sql", "sh", "yaml", "yml",
    "toml", "ini", "xml", "html", "css",
})

_BY_MEDIA_TYPE: tuple[tuple[str, Extractor], ...] = (
    ("application/pdf", _extract_pdf),
    ("wordprocessingml.document", _extract_docx),
    ("spreadsheetml.sheet", _extract_xlsx),
    ("presentationml.presentation", _extract_pptx),
    ("application/msword", _extract_doc),
    ("application/vnd.ms-excel", _extract_xls),
    ("application/rtf", _extract_rtf),
    ("application/zip", _extract_archive),
    ("image/", _extract_image),
    ("text/", _extract_plain),
    ("application/json", _extract_plain),
)


def _extension_of(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base[1:] else ""


def _extractor_for(name: str, media_type: str) -> Optional[Extractor]:
    extension = _extension_of(name)
    if extension in _BY_EXTENSION:
        return _BY_EXTENSION[extension]
    if extension in _PLAIN_EXTENSIONS:
        return _extract_plain
    lowered = (media_type or "").lower()
    for marker, extractor in _BY_MEDIA_TYPE:
        if marker in lowered:
            return extractor
    return None


def _run(extractor: Extractor, data: bytes, languages: str) -> Extraction:
    if extractor in (_extract_image, _extract_archive):
        return extractor(data, languages)
    return extractor(data)


def extract_text(data: bytes, name: str, media_type: str, *, ocr_languages: str = "chi_sim+eng") -> Extraction:
    """按文件名与媒体类型选择抽取器；任何抽取失败都收敛成 failed 而不是异常。"""

    extractor = _extractor_for(name, media_type)
    if extractor is None:
        return Extraction("unsupported", "none", detail=f"暂不支持解析该格式：{name}")
    try:
        return _run(extractor, data, ocr_languages).truncated()
    except zipfile.BadZipFile:
        return Extraction("failed", "zip", detail="文件不是合法的压缩包，可能在传输中损坏")
    except ET.ParseError as error:
        return Extraction("failed", "xml", detail=f"文档内部 XML 损坏：{error}")
    except Exception as error:  # 抽取器种类多，最后一道网防止把控制面拖成 500
        return Extraction("failed", "unknown", detail=f"解析失败：{type(error).__name__}: {error}")


def supported_extensions() -> Iterable[str]:
    return sorted({*_BY_EXTENSION, *_PLAIN_EXTENSIONS})
