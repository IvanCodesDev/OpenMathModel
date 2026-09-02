"""附件正文抽取：格式覆盖、缓存复用、归属隔离与优雅降级。

用例里的 docx/xlsx/pptx/odt 都是现场拼出来的最小 OOXML/ODF 包——这样测的是抽取
器对真实容器结构的处理（分片、共享字符串、关系表），而不是某个二进制样本。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from omm_api.doc_text import OcrApi, extract_text

from conftest import API, create_project

pytest.importorskip("pypdf")

# 单元用例不传 ocr_api 即远程 OCR 关闭；应用级用例经 conftest 的 ocr_api_key=""
# 关闭。需要远程路径的用例显式传本配置并 monkeypatch _ocr_api_chat，绝不外呼。
OCR_API = OcrApi(base_url="https://ocr.test/v2", model="xoppaddleocrv16", api_key="test-key")


def _zip(parts: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _docx(body: str) -> bytes:
    return _zip({
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": (
            '<?xml version="1.0"?><w:document xmlns:w="http://x"><w:body>'
            f"{body}</w:body></w:document>"
        ),
    })


def _upload(client, project_id: str, content: bytes, filename: str, media_type: str, kind: str = "other"):
    response = client.post(
        f"{API}/projects/{project_id}/artifacts",
        files={"file": (filename, content, media_type)},
        data={"kind": kind},
    )
    assert response.status_code == 201, response.text
    return response.json()


# ── 抽取器单元覆盖 ────────────────────────────────────────────────


def test_docx_keeps_paragraph_order_and_ignores_paragraph_properties():
    document = _docx(
        '<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="420"/></w:tabs></w:pPr>'
        "<w:r><w:t>共享单车调度</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>站点</w:t></w:r><w:r><w:tab/><w:t>需求</w:t></w:r></w:p>"
    )
    result = extract_text(document, "题目.docx", "")
    assert result.status == "ready"
    # w:pPr 里的 w:tab 是制表位设置而不是正文制表符，不能抽进来。
    assert result.text.split("\n") == ["共享单车调度", "站点\t需求"]


def test_xlsx_follows_workbook_relationships_and_pads_missing_columns():
    workbook = _zip({
        "xl/workbook.xml": (
            '<workbook xmlns:r="http://rel"><sheets>'
            '<sheet name="需求" sheetId="3" r:id="rId7"/>'
            '<sheet name="站点" sheetId="1" r:id="rId4"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rId7" Target="worksheets/sheet3.xml"/>'
            '<Relationship Id="rId4" Target="/xl/worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/sharedStrings.xml": "<sst><si><t>站点</t></si><si><r><t>需</t></r><r><t>求</t></r></si></sst>",
        "xl/worksheets/sheet3.xml": (
            '<worksheet><sheetData><row r="1">'
            '<c r="A1" t="s"><v>1</v></c><c r="C1"><v>42</v></c>'
            "</row></sheetData></worksheet>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>东站</t></is></c>'
            '<c r="B2"><v>3.5</v></c></row></sheetData></worksheet>'
        ),
    })
    result = extract_text(workbook, "附件.xlsx", "")
    assert result.status == "ready"
    assert result.segments == 2
    assert result.text.split("\n") == [
        "# 工作表：需求（1 行）",
        "需求\t\t42",
        "",
        "# 工作表：站点（2 行）",
        "站点",
        "东站\t3.5",
    ]


def test_xlsx_treats_a_self_closing_empty_cell_as_one_column():
    workbook = _zip({
        "xl/workbook.xml": '<workbook><sheets><sheet name="表" sheetId="1"/></sheets></workbook>',
        "xl/worksheets/sheet1.xml": (
            '<worksheet><sheetData><row r="1">'
            '<c r="A1"><v>1</v></c><c r="B1"/><c r="C1"><v>3</v></c>'
            "</row></sheetData></worksheet>"
        ),
    })
    result = extract_text(workbook, "空格.xlsx", "")
    assert result.text.split("\n") == ["# 工作表：表（1 行）", "1\t\t3"]


def test_pptx_orders_slides_numerically_with_notes():
    def slide(text: str) -> str:
        return (
            '<p:sld xmlns:p="http://p" xmlns:a="http://a">'
            f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>"
        )

    deck = _zip({
        "ppt/slides/slide10.xml": slide("第十页"),
        "ppt/slides/slide2.xml": slide("第二页"),
        "ppt/slides/slide1.xml": slide("标题页"),
        "ppt/notesSlides/notesSlide1.xml": slide("开场白"),
    })
    result = extract_text(deck, "答辩.pptx", "")
    assert result.segments == 3
    assert result.text.split("\n\n") == [
        "# 第 1 页\n标题页\n备注：开场白",
        "# 第 2 页\n第二页",
        "# 第 3 页\n第十页",
    ]


def test_opendocument_keeps_table_rows_on_one_line():
    content = (
        '<office:document-content xmlns:office="http://o" xmlns:text="http://t"'
        ' xmlns:table="http://tb"><office:body><office:text>'
        "<text:h>摘要</text:h><text:p>第一段<text:tab/>缩进</text:p>"
        "<table:table-row><table:table-cell><text:p>甲</text:p></table:table-cell>"
        "<table:table-cell><text:p>乙</text:p></table:table-cell></table:table-row>"
        "</office:text></office:body></office:document-content>"
    )
    result = extract_text(_zip({"content.xml": content}), "说明.odt", "")
    assert result.text.split("\n") == ["摘要", "第一段\t缩进", "甲\t乙"]


def test_plain_text_falls_back_to_gb18030():
    result = extract_text("站点,需求\n东站,12\n".encode("gb18030"), "站点.csv", "text/csv")
    assert result.status == "ready"
    assert "东站" in result.text


def test_docx_counts_embedded_media_images():
    document = _zip({
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": (
            '<?xml version="1.0"?><w:document xmlns:w="http://x"><w:body>'
            "<w:p><w:r><w:t>需求曲线见图1与图2</w:t></w:r></w:p></w:body></w:document>"
        ),
        "word/media/image1.png": "fake-png-bytes",
        "word/media/image2.jpeg": "fake-jpeg-bytes",
    })
    result = extract_text(document, "题目.docx", "")
    assert result.status == "ready"
    # 正文抽得出来，但纯文本模型看不到这两张图——计数是单模态提醒的数据底座。
    assert result.images == 2


def test_image_attachment_reports_single_image_when_ocr_is_unavailable(monkeypatch):
    import omm_api.doc_text as doc_text

    monkeypatch.setattr(doc_text.shutil, "which", lambda _name: None)
    result = extract_text(b"\x89PNG fake", "figure.png", "image/png")
    assert result.status == "unsupported"
    assert result.images == 1


def test_archive_lists_entries_and_expands_text_members():
    result = extract_text(_zip({"附件/站点.csv": "id,name\n1,东站\n"}), "附件.zip", "")
    assert result.status == "ready"
    assert "# 压缩包内容（1 个文件）" in result.text
    assert "东站" in result.text


def test_pdf_without_text_layer_reports_empty_instead_of_failing():
    # 一份结构合法但没有任何文字内容的最小 PDF，等价于扫描件的处境。
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    result = extract_text(buffer.getvalue(), "扫描件.pdf", "application/pdf")
    assert result.status == "empty"
    assert "OCR" in (result.detail or "")
    # 空白页没有 XObject：计数应得出确定的 0 而不是放弃（None）。
    assert result.images == 0


def test_unknown_and_corrupted_inputs_degrade_without_raising():
    assert extract_text(b"whatever", "mystery.qqq", "").status == "unsupported"
    broken = extract_text(b"not-a-zip-at-all", "题目.docx", "")
    assert broken.status == "failed"
    assert "压缩包" in (broken.detail or "")


# ── 远程 OCR 后端（monkeypatch HTTP 调用与光栅化模块，CI 绝不外呼）─────


def _fake_ocr_chat(monkeypatch, replies: list[str], calls: list[str] | None = None) -> None:
    """按调用顺序弹出 replies 作为识别结果；calls 记录每次上送的 data URL 头。"""

    import omm_api.doc_text as doc_text

    pending = list(replies)

    def _chat(_api, data_url: str) -> str:
        if calls is not None:
            calls.append(data_url.split(";", 1)[0])
        assert pending, "远程 OCR 被调用的次数超过预期"
        return pending.pop(0)

    monkeypatch.setattr(doc_text, "_ocr_api_chat", _chat)


def _install_fake_pdfium(monkeypatch, page_count: int) -> None:
    """假 pypdfium2：render→to_pil→save 产出带 JPEG 魔数的假页图字节。"""

    import sys
    import types

    class _Image:
        def convert(self, _mode: str) -> _Image:
            return self

        def save(self, buffer: io.BytesIO, **_kw) -> None:
            buffer.write(b"\xff\xd8\xff fake-jpeg")

    class _Page:
        def render(self, scale: float):
            return types.SimpleNamespace(to_pil=lambda: _Image())

    class _Document:
        def __init__(self, _data: bytes) -> None:
            self._pages = [_Page() for _ in range(page_count)]

        def __len__(self) -> int:
            return len(self._pages)

        def __getitem__(self, index: int) -> _Page:
            return self._pages[index]

        def close(self) -> None:
            pass

    module = types.ModuleType("pypdfium2")
    module.PdfDocument = _Document
    monkeypatch.setitem(sys.modules, "pypdfium2", module)


def _blank_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_scanned_pdf_upgrades_to_remote_ocr_markdown(monkeypatch):
    _install_fake_pdfium(monkeypatch, page_count=2)
    calls: list[str] = []
    _fake_ocr_chat(monkeypatch, ["# 题目\n$E=mc^2$", "|站点|需求|\n|---|---|"], calls)

    result = extract_text(_blank_pdf(), "扫描件.pdf", "application/pdf", ocr_api=OCR_API)
    assert result.status == "ready"
    assert result.engine == "paddleocr-api"
    assert "$E=mc^2$" in result.text
    assert "站点" in result.text
    assert result.images == 0
    # 每页渲染成 JPEG 后各上送一次。
    assert calls == ["data:image/jpeg", "data:image/jpeg"]


def test_image_prefers_remote_ocr_over_tesseract(monkeypatch):
    calls: list[str] = []
    _fake_ocr_chat(monkeypatch, ["流程图：输入 → 模型 → 输出"], calls)
    result = extract_text(b"\x89PNG fake", "figure.png", "image/png", ocr_api=OCR_API)
    assert result.status == "ready"
    assert result.engine == "paddleocr-api"
    assert result.images == 1
    assert calls == ["data:image/png"]


def test_image_reports_empty_when_remote_ocr_finds_nothing(monkeypatch):
    _fake_ocr_chat(monkeypatch, [""])
    result = extract_text(b"\x89PNG fake", "白图.png", "image/png", ocr_api=OCR_API)
    assert result.status == "empty"
    assert result.engine == "paddleocr-api"
    assert result.images == 1


def test_image_falls_back_honestly_when_remote_ocr_fails(monkeypatch):
    import omm_api.doc_text as doc_text

    def _boom(_api, _data_url: str) -> str:
        raise RuntimeError("上游 502")

    monkeypatch.setattr(doc_text, "_ocr_api_chat", _boom)
    monkeypatch.setattr(doc_text.shutil, "which", lambda _name: None)
    result = extract_text(b"\x89PNG fake", "figure.png", "image/png", ocr_api=OCR_API)
    # 远程失败不抛错：回落 Tesseract 路径，缺 Tesseract 时如实 unsupported。
    assert result.status == "unsupported"
    assert result.images == 1


def test_image_without_api_key_keeps_local_degradation(monkeypatch):
    import omm_api.doc_text as doc_text

    monkeypatch.setattr(doc_text.shutil, "which", lambda _name: None)
    unconfigured = OcrApi(base_url="https://ocr.test/v2", model="m", api_key="")
    result = extract_text(b"\x89PNG fake", "figure.png", "image/png", ocr_api=unconfigured)
    assert result.status == "unsupported"
    assert "OMM_OCR_API_KEY" in (result.detail or "")


def test_scanned_pdf_reports_missing_rasterizer(monkeypatch):
    import sys

    # sys.modules 置 None：import pypdfium2 即刻 ModuleNotFoundError，模拟未安装。
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    result = extract_text(_blank_pdf(), "扫描件.pdf", "application/pdf", ocr_api=OCR_API)
    assert result.status == "empty"
    assert "pdf-ocr" in (result.detail or "")


def test_legacy_binary_formats_report_a_reason_rather_than_crashing():
    result = extract_text(b"\xd0\xcf\x11\xe0not-really", "旧稿.doc", "application/msword")
    assert result.status in ("failed", "unsupported")
    assert result.detail


# ── 接口行为 ──────────────────────────────────────────────────────


def test_text_endpoint_extracts_uploads_and_caches_the_result(client):
    project = create_project(client)
    artifact = _upload(
        client,
        project["id"],
        _docx("<w:p><w:r><w:t>需求预测与调度优化</w:t></w:r></w:p>"),
        "题目.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        kind="paper",
    )

    response = client.get(f"{API}/artifacts/{artifact['id']}/text")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["engine"] == "docx"
    assert payload["text"] == "需求预测与调度优化"
    assert payload["characters"] == len("需求预测与调度优化")
    assert payload["name"] == "题目.docx"
    assert payload["images"] == 0

    # 第二次读命中缓存：即便底层内容对象被移走也仍能返回同一份正文。
    digest = artifact["sha256"]
    blob = client.app.state.settings.artifacts_dir / digest[:2] / digest[2:4] / digest
    blob.unlink()
    cached = client.get(f"{API}/artifacts/{artifact['id']}/text")
    assert cached.status_code == 200
    assert cached.json()["text"] == payload["text"]
    assert cached.json()["images"] == 0


def test_text_endpoint_refresh_reruns_extraction(client):
    project = create_project(client)
    artifact = _upload(client, project["id"], "行1\n行2\n".encode("utf-8"), "记录.txt", "text/plain")

    assert client.get(f"{API}/artifacts/{artifact['id']}/text").json()["characters"] == 5
    refreshed = client.get(f"{API}/artifacts/{artifact['id']}/text?refresh=true")
    assert refreshed.status_code == 200
    assert refreshed.json()["text"] == "行1\n行2"


def test_text_endpoint_retries_stale_negative_cache_without_refresh(client, app):
    """解析缺陷期落库的空结果不能永久挡路：超过 TTL 的负缓存在下一次读取时
    自动重跑（无需前端知道 refresh 参数），成功后转为永久缓存。"""

    from datetime import timedelta

    from omm_api.orm import ArtifactTextRow
    from omm_api.routers.artifacts import NEGATIVE_TEXT_CACHE_TTL
    from omm_api.serialize import utcnow

    project = create_project(client)
    artifact = _upload(client, project["id"], "正文其实在这".encode("utf-8"), "迟到.txt", "text/plain")
    assert client.get(f"{API}/artifacts/{artifact['id']}/text").json()["status"] == "ready"

    def _force_negative(created_at) -> None:
        with app.state.db.session_factory() as session:
            cached = session.get(ArtifactTextRow, artifact["id"])
            cached.status = "empty"
            cached.text = ""
            cached.characters = 0
            cached.detail = "远程 OCR 没有识别出内容"
            cached.created_at = created_at
            session.commit()

    # 过期负缓存 → 读取即自愈，正文回来了。
    _force_negative(utcnow() - NEGATIVE_TEXT_CACHE_TTL - timedelta(minutes=1))
    healed = client.get(f"{API}/artifacts/{artifact['id']}/text").json()
    assert healed["status"] == "ready"
    assert healed["text"] == "正文其实在这"

    # TTL 内的负缓存仍然复用：不为注定失败的解析反复买单。
    _force_negative(utcnow())
    fresh = client.get(f"{API}/artifacts/{artifact['id']}/text").json()
    assert fresh["status"] == "empty"
    assert fresh["text"] == ""


def test_text_endpoint_serializes_concurrent_extraction_of_the_same_artifact(client, monkeypatch):
    """同一附件被并发读取时只抽取一次：真实事故路径是扫描件逐页远程 OCR 超过前端
    180 秒预算 → 浏览器中止并重发 /text，而服务端第一次仍在抽取。修复前两次并行抽取
    烧两遍 OCR 配额，最后都 INSERT 同一主键、后提交者 500；修复后排队等待并复用结果。"""

    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import omm_api.routers.artifacts as artifacts_router

    project = create_project(client)
    artifact = _upload(client, project["id"], "慢得像扫描件".encode("utf-8"), "慢.txt", "text/plain")

    real_extract = artifacts_router.extract_text
    calls = 0
    calls_lock = threading.Lock()

    def _slow_extract(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.4)  # 让三个请求确实重叠在抽取窗口里
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(artifacts_router, "extract_text", _slow_extract)

    url = f"{API}/artifacts/{artifact['id']}/text"
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: client.get(url), range(3)))

    assert [response.status_code for response in responses] == [200, 200, 200], [
        response.text for response in responses
    ]
    assert {response.json()["text"] for response in responses} == {"慢得像扫描件"}
    assert calls == 1, f"同一附件被并发抽取了 {calls} 次"


def test_text_endpoint_is_owner_scoped(client, second_client):
    project = create_project(client)
    artifact = _upload(client, project["id"], b"secret notes", "secret.txt", "text/plain")

    from conftest import register_user

    register_user(second_client, "text-intruder@test.dev")
    stolen = second_client.get(f"{API}/artifacts/{artifact['id']}/text")
    assert stolen.status_code == 404
    assert stolen.json()["code"] == "NOT_FOUND"


def test_text_endpoint_skips_files_above_the_extraction_limit(client, app):
    app.state.settings.attachment_text_max_bytes = 8
    project = create_project(client)
    artifact = _upload(client, project["id"], b"a much longer body than eight bytes", "big.txt", "text/plain")

    payload = client.get(f"{API}/artifacts/{artifact['id']}/text").json()
    assert payload["status"] == "unsupported"
    assert payload["text"] == ""
    assert "上限" in payload["detail"]


# ── 对话附件即席解析（ADR-0010 批次三：不落库、不建产物）─────────────


def test_adhoc_parse_extracts_text_without_persisting(client):
    response = client.post(
        f"{API}/artifacts/parse",
        files={"file": ("追问.txt", "补充约束：预算不超过 100 万".encode("utf-8"), "text/plain")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ready"
    assert "预算" in payload["text"]
    assert payload["name"] == "追问.txt"


def test_adhoc_parse_degrades_honestly_for_images_without_ocr(client, monkeypatch):
    import omm_api.doc_text as doc_text

    monkeypatch.setattr(doc_text.shutil, "which", lambda _name: None)
    response = client.post(
        f"{API}/artifacts/parse",
        files={"file": ("流程图.png", b"\x89PNG fake", "image/png")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unsupported"
    assert payload["images"] == 1


def test_adhoc_parse_requires_login(second_client):
    response = second_client.post(
        f"{API}/artifacts/parse",
        files={"file": ("匿名.txt", b"anonymous", "text/plain")},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"
