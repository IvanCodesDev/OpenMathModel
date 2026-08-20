"""附件正文抽取：格式覆盖、缓存复用、归属隔离与优雅降级。

用例里的 docx/xlsx/pptx/odt 都是现场拼出来的最小 OOXML/ODF 包——这样测的是抽取
器对真实容器结构的处理（分片、共享字符串、关系表），而不是某个二进制样本。
"""

from __future__ import annotations

import io
import threading
import zipfile

import pytest

from omm_api.doc_text import extract_text

from conftest import API, create_project

pytest.importorskip("pypdf")


@pytest.fixture(autouse=True)
def _vl_disabled_by_default(monkeypatch):
    """开发机可能真装了 paddle 栈；测试默认强制 VL 不可用保证确定性。

    需要 VL 路径的用例经 _install_fake_paddleocr 显式复位并注入假模块。
    """

    import omm_api.doc_text as doc_text

    monkeypatch.setattr(doc_text, "_VL_PIPELINE", None)
    monkeypatch.setattr(doc_text, "_VL_UNAVAILABLE", "测试默认禁用")


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


# ── PaddleOCR-VL 可选后端（假模块注入，不依赖真实 paddle 栈）──────────


def _install_fake_paddleocr(monkeypatch, pages: list[str]) -> None:
    import sys
    import types

    import omm_api.doc_text as doc_text

    def _result(text: str):
        return types.SimpleNamespace(markdown={"markdown_texts": text})

    module = types.ModuleType("paddleocr")
    module.PaddleOCRVL = lambda: types.SimpleNamespace(
        predict=lambda _path: [_result(text) for text in pages],
    )
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    # 惰性单例复位，避免其他用例触发的可用性判定串场；teardown 时自动还原。
    monkeypatch.setattr(doc_text, "_VL_PIPELINE", None)
    monkeypatch.setattr(doc_text, "_VL_UNAVAILABLE", None)


def test_scanned_pdf_upgrades_to_paddleocr_vl_markdown(monkeypatch):
    from pypdf import PdfWriter

    _install_fake_paddleocr(monkeypatch, ["# 题目\n$E=mc^2$", "|站点|需求|\n|---|---|"])
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    result = extract_text(buffer.getvalue(), "扫描件.pdf", "application/pdf")
    assert result.status == "ready"
    assert result.engine == "paddleocr-vl"
    assert "$E=mc^2$" in result.text
    assert result.images == 0


def test_image_prefers_paddleocr_vl_over_tesseract(monkeypatch):
    _install_fake_paddleocr(monkeypatch, ["流程图：输入 → 模型 → 输出"])
    result = extract_text(b"\x89PNG fake", "figure.png", "image/png")
    assert result.status == "ready"
    assert result.engine == "paddleocr-vl"
    assert result.images == 1


def test_warmup_vl_preloads_the_lazy_singleton(monkeypatch):
    import omm_api.doc_text as doc_text

    _install_fake_paddleocr(monkeypatch, ["预热"])
    doc_text.warmup_vl()
    # 预热即加载：随后的图片解析直接复用单例，不再付冷启动成本。
    assert doc_text._VL_PIPELINE is not None
    result = extract_text(b"\x89PNG fake", "figure.png", "image/png")
    assert result.engine == "paddleocr-vl"


def test_warmup_vl_is_a_noop_when_the_backend_is_unavailable():
    import omm_api.doc_text as doc_text

    # autouse fixture 已把 VL 置为不可用；预热不得抛异常、不得改变不可用状态。
    doc_text.warmup_vl()
    assert doc_text._VL_PIPELINE is None


def test_vl_init_and_predict_run_on_the_same_dedicated_thread(monkeypatch):
    """paddle 动态图模式是线程局部状态：模型在哪个线程创建就只能在哪个线程推理，
    跨线程 predict 以「static graph mode」RuntimeError 失败（2026-08-20 实测）。
    契约：预热初始化与请求推理必须发生在同一条专用线程上，且不在调用方线程。"""

    import sys
    import types

    import omm_api.doc_text as doc_text

    seen: dict[str, int] = {}

    def _result(text: str):
        return types.SimpleNamespace(markdown={"markdown_texts": text})

    class _Pipeline:
        def __init__(self) -> None:
            seen["init"] = threading.get_ident()

        def predict(self, _path: str):
            seen["predict"] = threading.get_ident()
            return [_result("同线程推理")]

    module = types.ModuleType("paddleocr")
    module.PaddleOCRVL = _Pipeline
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    monkeypatch.setattr(doc_text, "_VL_PIPELINE", None)
    monkeypatch.setattr(doc_text, "_VL_UNAVAILABLE", None)

    doc_text.warmup_vl()
    result = extract_text(b"\x89PNG fake", "figure.png", "image/png")
    assert result.engine == "paddleocr-vl"
    assert result.text == "同线程推理"
    assert seen["init"] == seen["predict"]
    assert seen["init"] != threading.get_ident()


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
