"""论文导出执行面：两种 PDF 引擎，按源产物的媒体类型分派。

- ADR-0012 阶段 A：Tectonic 编译 .tex → PDF。子进程 ``--untrusted``（禁 shell-escape）、
  独立临时工作目录、超时强杀。
- ADR-0025：无头 Chromium 系浏览器（Chrome / Edge / Chromium）把编辑器导出的自足 HTML
  按 A4 打印成 PDF。走 DevTools 协议：HTML 经 ``Page.setDocumentContent`` 写进 about:blank
  （读不到 file://），文档最前面强插 CSP（禁脚本、禁一切外部资源，只认 data: 图片与字体），
  浏览器进程再挂一个不存在的代理，网络请求全部落空；独立临时用户目录、超时强杀。

开发链沿 RunnerThread 模式在 API 进程内消费队列，目标态随执行面迁往 backend/worker，
接口契约不变。引擎缺失时任务落 UNSUPPORTED 并说明启用途径，不假装成功。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Optional

from sqlalchemy import select

from omm_contracts import ArtifactKind, ArtifactStatus, PaperExportStatus

from .blobstore import ArtifactBlobStore
from .config import Settings
from .db import Database
from .events import append_event, lock_run
from .ids import new_id
from .orm import ArtifactRow, PaperExportRow
from .serialize import utcnow

logger = logging.getLogger("omm.paper_export")

UNSUPPORTED_HINT = (
    "服务端未安装 Tectonic 编译器，无法编译 PDF。安装 tectonic 并加入 PATH，"
    "或设置 OMM_TECTONIC_PATH 指向可执行文件；离线部署需先联网预热宏包缓存。"
    "在此之前可改用「导出 LaTeX (.zip)」本机编译。"
)

CHROMIUM_UNSUPPORTED_HINT = (
    "服务端没有可用的 Chromium 系浏览器（Chrome / Edge / Chromium），无法把论文打印成 PDF。"
    "安装其一，或设置 OMM_CHROMIUM_PATH 指向可执行文件；Linux 服务器另需中文字体（如 fonts-noto-cjk）。"
)

#: HTML 源的媒体类型：处理器据此把任务分给浏览器引擎（其余按 .tex 交给 Tectonic）。
HTML_MEDIA_TYPE = "text/html"


def find_tectonic(settings: Settings) -> Optional[str]:
    """定位 Tectonic 可执行文件：配置项优先，否则探测 PATH。"""
    if settings.tectonic_path:
        path = Path(settings.tectonic_path)
        return str(path) if path.is_file() else None
    return shutil.which("tectonic")


_CHROMIUM_COMMANDS = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "microsoft-edge", "microsoft-edge-stable", "chrome", "msedge",
)


def _chromium_install_paths() -> list[Path]:
    if sys.platform == "win32":
        roots = [os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        relative = (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe", r"Chromium\Application\chrome.exe")
        return [Path(root) / suffix for root in roots if root for suffix in relative]
    if sys.platform == "darwin":
        return [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    return []


def find_chromium(settings: Settings) -> Optional[str]:
    """定位无头打印用的浏览器：配置项优先，其次 PATH，最后各平台的默认安装位置。"""
    if settings.chromium_path:
        path = Path(settings.chromium_path)
        return str(path) if path.is_file() else None
    for command in _CHROMIUM_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    for candidate in _chromium_install_paths():
        if candidate.is_file():
            return str(candidate)
    return None


@dataclass
class CompileResult:
    ok: bool
    pdf: bytes = field(default=b"", repr=False)
    log_tail: str = ""


def run_tectonic(tectonic: str, source_tex: bytes, timeout_seconds: float) -> CompileResult:
    """在独立临时目录内编译一份 .tex；超时由 subprocess 强杀子进程。"""
    with TemporaryDirectory(prefix="omm-paper-") as workdir:
        (Path(workdir) / "main.tex").write_bytes(source_tex)
        try:
            proc = subprocess.run(
                [tectonic, "--untrusted", "--chatter", "minimal", "main.tex"],
                cwd=workdir,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return CompileResult(ok=False, log_tail=f"编译超时（{timeout_seconds:.0f} 秒），已终止")
        except OSError as exc:
            return CompileResult(ok=False, log_tail=f"编译器无法启动：{exc}")
        log = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace").strip()
        pdf_path = Path(workdir) / "main.pdf"
        if proc.returncode != 0 or not pdf_path.exists():
            return CompileResult(ok=False, log_tail=log or "编译失败且没有日志输出")
        return CompileResult(ok=True, pdf=pdf_path.read_bytes(), log_tail=log)


# ── HTML → PDF：无头浏览器打印（ADR-0025）────────────────────────────────────

#: 强插在文档最前面的 CSP：禁一切脚本、外部请求、子框架与插件，图片 / 字体只认 data:，
#: 样式只认内联。DevTools 的 Runtime.evaluate 不受页面 CSP 约束，就绪检测照常可用。
PRINT_CSP = (
    "default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; "
    "script-src 'none'; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)

#: 字体全部装载、图片全部解码后才打印：KaTeX 字体是 font-display:block，没装好时字形是隐形的。
_READY_SCRIPT = """
(async () => {
  void document.body && document.body.offsetHeight;
  await Promise.all([...document.fonts].map(face => face.load().catch(() => null)));
  await Promise.all([...document.images].map(img => (img.complete ? null : img.decode().catch(() => null))));
  await document.fonts.ready;
  return document.readyState;
})()
"""

_DOCTYPE = re.compile(r"^\s*(?:\ufeff)?\s*<!doctype[^>]*>", re.IGNORECASE)


def guard_print_html(html: str) -> str:
    """把 CSP 放到解析器见到的第一个元素：任何脚本、外链都排在它后面，被它管住。"""
    body = _DOCTYPE.sub("", html, count=1)
    return f'<!DOCTYPE html><meta http-equiv="Content-Security-Policy" content="{PRINT_CSP}">{body}'


def _chromium_args(chromium: str, profile: str) -> list[str]:
    args = [
        chromium,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-sync",
        "--mute-audio",
        "--hide-scrollbars",
        # 网络全部指向一个不存在的代理（连本机回环也不例外）：页面里漏网的任何请求都落空
        "--proxy-server=http://127.0.0.1:9",
        "--proxy-bypass-list=<-loopback>",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # 容器里以 root 运行时 Chromium 拒绝启用沙箱
        args.insert(1, "--no-sandbox")
    return args


def _devtools_port(profile: Path, proc: subprocess.Popen[bytes], deadline: float) -> int:
    """浏览器把实际监听端口写进用户目录的 DevToolsActivePort（--remote-debugging-port=0 时由系统分配）。"""
    marker = profile / "DevToolsActivePort"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"浏览器启动后立即退出（退出码 {proc.returncode}）")
        try:
            first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
            if first.isdigit():
                return int(first)
        except (OSError, IndexError):
            pass
        time.sleep(0.05)
    raise TimeoutError("等待浏览器调试端口超时")


def _page_websocket(port: int, deadline: float) -> str:
    # 本机回环不走任何代理（环境变量里的 HTTP_PROXY 也不认）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                targets = json.loads(response.read().decode("utf-8"))
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return str(target["webSocketDebuggerUrl"])
        except OSError:
            pass
        time.sleep(0.05)
    raise TimeoutError("等待浏览器页面就绪超时")


class _DevTools:
    """最小 CDP 客户端：按 id 等回包，期间到达的事件丢弃（打印流程不依赖事件）。"""

    def __init__(self, socket: Any, deadline: float) -> None:
        self._socket = socket
        self._deadline = deadline
        self._next_id = 0

    def call(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        self._next_id += 1
        ident = self._next_id
        self._socket.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method} 超时")
            message = json.loads(self._socket.recv(timeout=remaining))
            if message.get("id") != ident:
                continue
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error'].get('message', message['error'])}")
            return message.get("result") or {}


def print_html_with_devtools(devtools: Callable[[str, Optional[dict[str, Any]]], dict[str, Any]], html: str) -> bytes:
    """在已连上的页面里完成「写入 HTML → 等字体与图片 → 打印」，返回 PDF 字节（与浏览器进程解耦，便于测试）。"""
    devtools("Page.enable", None)
    frame_id = devtools("Page.getFrameTree", None)["frameTree"]["frame"]["id"]
    devtools("Page.setDocumentContent", {"frameId": frame_id, "html": guard_print_html(html)})
    devtools("Runtime.evaluate", {"expression": _READY_SCRIPT, "awaitPromise": True, "returnByValue": True})
    options: dict[str, Any] = {
        "printBackground": True,
        # 纸张与页边距听 HTML 里的 @page（A4、页码在页脚的 @bottom-center）
        "preferCSSPageSize": True,
        "displayHeaderFooter": False,
        "generateDocumentOutline": True,
    }
    try:
        result = devtools("Page.printToPDF", options)
    except RuntimeError:
        # 旧版浏览器不认书签参数：去掉再打一次
        options.pop("generateDocumentOutline")
        result = devtools("Page.printToPDF", options)
    return base64.b64decode(result["data"])


def run_chromium(chromium: str, html: str, timeout_seconds: float) -> CompileResult:
    """起一个一次性的无头浏览器把 HTML 打印成 PDF；任何环节失败都落 CompileResult(ok=False)。"""
    try:
        from websockets.sync.client import connect
    except ImportError:  # uvicorn[standard] 带着它；精简部署缺了就如实报错
        return CompileResult(ok=False, log_tail="缺少 websockets 包，无法驱动无头浏览器")
    deadline = time.monotonic() + timeout_seconds
    with TemporaryDirectory(prefix="omm-pdf-", ignore_cleanup_errors=True) as profile:
        try:
            proc = subprocess.Popen(
                _chromium_args(chromium, profile),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return CompileResult(ok=False, log_tail=f"浏览器无法启动：{exc}")
        try:
            port = _devtools_port(Path(profile), proc, deadline)
            url = _page_websocket(port, deadline)
            with connect(url, max_size=None, proxy=None, open_timeout=max(1.0, deadline - time.monotonic())) as socket:
                client = _DevTools(socket, deadline)
                pdf = print_html_with_devtools(client.call, html)
        except TimeoutError as exc:
            return CompileResult(ok=False, log_tail=f"打印超时（{timeout_seconds:.0f} 秒），已终止：{exc}")
        except Exception as exc:  # 浏览器崩溃、协议报错：如实落失败，不拖垮消费线程
            return CompileResult(ok=False, log_tail=f"浏览器打印失败：{exc}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    if not pdf.startswith(b"%PDF"):
        return CompileResult(ok=False, log_tail="浏览器返回的不是 PDF")
    return CompileResult(ok=True, pdf=pdf)


class PaperExportProcessor:
    """对单个导出任务执行一次完整处理；后台线程与测试共用（镜像 WorkflowAdvancer）。"""

    def __init__(self, db: Database, settings: Settings, blobs: ArtifactBlobStore) -> None:
        self._db = db
        self._settings = settings
        self._blobs = blobs

    def pending_ids(self) -> list[str]:
        session = self._db.session_factory()
        try:
            rows = session.execute(
                select(PaperExportRow.id)
                .where(PaperExportRow.status == PaperExportStatus.QUEUED.value)
                .order_by(PaperExportRow.created_at.asc())
            ).scalars()
            return list(rows)
        finally:
            session.close()

    def process(self, export_id: str) -> Optional[str]:
        """处理一个导出并返回终态；非 QUEUED 的任务原样返回当前状态（幂等）。"""
        session = self._db.session_factory()
        try:
            row = session.get(PaperExportRow, export_id)
            if row is None:
                return None
            if row.status != PaperExportStatus.QUEUED.value:
                return row.status
            row.status = PaperExportStatus.RUNNING.value
            row.started_at = utcnow()
            # 先提交 RUNNING：编译最长两分钟，期间 GET 轮询要能看到真实状态
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        status, artifact_id, detail = self._compile(export_id)

        session = self._db.session_factory()
        try:
            row = session.get(PaperExportRow, export_id)
            if row is None:
                return None
            row.status = status
            row.artifact_id = artifact_id
            row.detail = detail[:500] if detail else None
            row.ended_at = utcnow()
            if row.run_id:
                # 沿 run 事件流原位通知工作台；事件序列在行锁保护下分配
                lock_run(session, row.run_id)
                append_event(
                    session,
                    row.run_id,
                    "paper.export.finished",
                    {"export_id": row.id, "status": status, "artifact_id": artifact_id},
                )
            session.commit()
            return status
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _compile(self, export_id: str) -> tuple[str, Optional[str], Optional[str]]:
        """编译并落 PDF 产物，返回 (status, artifact_id, detail)。

        源内容按 export 行登记的 sha256 从 blobstore 取回；编译在会话之外执行，
        不占数据库连接。源产物是 HTML 的交给无头浏览器打印，其余按 .tex 交给 Tectonic。
        """
        session = self._db.session_factory()
        try:
            row = session.get(PaperExportRow, export_id)
            if row is None:
                return PaperExportStatus.FAILED.value, None, "导出记录不存在"
            source_sha256 = row.source_sha256
            source_artifact_id = row.source_artifact_id
            project_id = row.project_id
            run_id = row.run_id
            source_name = None
            source_media_type = None
            if source_artifact_id:
                source = session.get(ArtifactRow, source_artifact_id)
                if source is not None:
                    source_name = source.name
                    source_media_type = source.media_type
        finally:
            session.close()

        is_html = source_media_type == HTML_MEDIA_TYPE
        engine = find_chromium(self._settings) if is_html else find_tectonic(self._settings)
        if engine is None:
            return PaperExportStatus.UNSUPPORTED.value, None, CHROMIUM_UNSUPPORTED_HINT if is_html else UNSUPPORTED_HINT

        handle = self._blobs.open(source_sha256) if source_sha256 else None
        if handle is None:
            return PaperExportStatus.FAILED.value, None, f"{'html' if is_html else 'tex'} 源内容对象缺失，无法生成 PDF"
        with handle:
            source_bytes = handle.read()

        timeout = self._settings.paper_export_timeout_seconds
        if is_html:
            result = run_chromium(engine, source_bytes.decode("utf-8", errors="replace"), timeout)
        else:
            result = run_tectonic(engine, source_bytes, timeout)
        if not result.ok:
            # detail 只留日志尾部（契约上限 500 字），完整日志进服务端日志
            logger.warning("paper export %s failed: %s", export_id, result.log_tail[-2000:])
            return PaperExportStatus.FAILED.value, None, result.log_tail[-500:]

        sha256, size = self._blobs.put(result.pdf)
        pdf_name = f"{Path(source_name).stem}.pdf" if source_name else "论文导出.pdf"
        session = self._db.session_factory()
        try:
            artifact = ArtifactRow(
                id=new_id("art"),
                project_id=project_id,
                run_id=run_id,
                kind=ArtifactKind.paper.value,
                name=pdf_name,
                uri=f"local://{sha256}",
                sha256=sha256,
                size_bytes=size,
                media_type="application/pdf",
                producer_step=None,
                inputs=[source_artifact_id] if source_artifact_id else [],
                status=ArtifactStatus.READY.value,
                created_at=utcnow(),
            )
            session.add(artifact)
            session.commit()
            return PaperExportStatus.READY.value, artifact.id, None
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class PaperExportThread:
    """后台编译消费线程：周期性处理排队中的导出（镜像 RunnerThread）。"""

    def __init__(self, processor: PaperExportProcessor, settings: Settings) -> None:
        self._processor = processor
        self._interval = settings.paper_export_poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="omm-paper-export", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for export_id in self._processor.pending_ids():
                    if self._stop.is_set():
                        break
                    self._processor.process(export_id)
            except Exception:  # 单个任务失败不允许杀死线程
                logger.exception("paper export tick failed")
            self._stop.wait(self._interval)
