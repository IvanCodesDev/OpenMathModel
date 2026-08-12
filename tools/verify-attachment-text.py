"""对真实 uvicorn 实例跑一遍附件闭环：注册 → 建项目 → 上传 → 读正文。

TestClient 走的是 ASGI 内存管道，绕过了真实的 multipart 边界解析、Cookie 往返和
内容寻址落盘。这个脚本用真 HTTP 打一遍，验收附件功能时手动执行。

    .venv\\Scripts\\python tools/verify-attachment-text.py [http://127.0.0.1:8000]
"""

from __future__ import annotations

import io
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
import json

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def _docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in paragraphs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="http://w"><w:body>{body}</w:body></w:document>',
        )
    return buffer.getvalue()


def _multipart(filename: str, content: bytes, fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----omm{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Session:
    def __init__(self) -> None:
        self.cookie = ""

    def call(self, method: str, path: str, body: bytes | None = None, content_type: str = "") -> dict:
        request = urllib.request.Request(f"{BASE}{path}", data=body, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Origin", BASE)
        if content_type:
            request.add_header("Content-Type", content_type)
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(request) as response:
                for header in response.headers.get_all("Set-Cookie") or []:
                    self.cookie = header.split(";", 1)[0]
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raise SystemExit(f"{method} {path} -> {error.code} {error.read().decode('utf-8', 'replace')}")

    def json_call(self, method: str, path: str, payload: dict) -> dict:
        return self.call(method, path, json.dumps(payload).encode(), "application/json")


def main() -> int:
    session = Session()
    email = f"verify-{uuid.uuid4().hex[:10]}@test.dev"
    sent = session.json_call("POST", "/api/auth/register/send-code", {"email": email})
    session.json_call("POST", "/api/auth/register", {
        "email": email,
        "code": sent["dev_code"],
        "password": "Passw0rd123",
        "name": "附件验收",
    })
    project = session.json_call("POST", "/api/v1/projects", {"name": "附件解析验收"})

    content = _docx(["共享单车调度赛题", "问题一：需求预测", "问题二：区域划分"])
    body, content_type = _multipart("题目.docx", content, {"kind": "paper"})
    artifact = session.call("POST", f"/api/v1/projects/{project['id']}/artifacts", body, content_type)

    started = time.perf_counter()
    text = session.call("GET", f"/api/v1/artifacts/{artifact['id']}/text")
    cold = time.perf_counter() - started
    started = time.perf_counter()
    session.call("GET", f"/api/v1/artifacts/{artifact['id']}/text")
    warm = time.perf_counter() - started

    print(f"artifact  : {artifact['id']} sha256={artifact['sha256'][:12]}… {artifact['size_bytes']} bytes")
    print(f"status    : {text['status']} engine={text['engine']} characters={text['characters']}")
    print(f"segments  : {text['segments']}  detail={text['detail']}")
    print(f"timing    : cold={cold * 1000:.0f}ms  cached={warm * 1000:.0f}ms")
    print("text      :")
    for line in text["text"].split("\n"):
        print(f"  | {line}")

    expected = "共享单车调度赛题\n问题一：需求预测\n问题二：区域划分"
    if text["status"] != "ready" or text["text"] != expected:
        print("MISMATCH: 抽取结果与期望不一致")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
