from __future__ import annotations

import platform
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.engine import make_url

from . import engine_glue, model_catalog
from .blobstore import LocalContentStore
from .chat_turns import ChatTurnHub
from .config import Settings, get_settings
from .db import Database
from .db_ready import ensure_database_ready
from .errors import register_error_handlers
from .middleware import OriginCheckMiddleware, RequestIdMiddleware
from .paper_export import PaperExportProcessor, PaperExportThread
from .privacy import RetentionThread
from .routers import (
    account,
    artifacts,
    auth,
    chat,
    events,
    intake,
    paper_exports,
    projects,
    stage_outputs,
    task_runs,
    usage,
    workspace,
)
from .runner import RunnerThread, WorkflowAdvancer


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    resolved = settings or get_settings()
    db = Database(resolved.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 先探库再建表：连不上时给一行能照着做的提示（本地 pg-dev 实例还会自动拉起），
        # 而不是让 create_all 抛出百行 psycopg 超时 traceback。
        ensure_database_ready(db, resolved)
        # 开发环境用 create_all 保证可用；PostgreSQL 部署走 Alembic 迁移。
        db.create_all()
        # 托管对话轮（ADR-0016）：上一进程里还在生成的轮已经没人接手，如实标中断。
        app.state.chat_turns.recover_interrupted()
        runner: Optional[RunnerThread] = None
        if resolved.runner_enabled:
            runner = RunnerThread(db, resolved)
            runner.start()
        app.state.runner = runner
        # 「数据与隐私」的任务保留与文件缓存清扫（首轮延后一个周期）。
        sweeper: Optional[RetentionThread] = None
        if resolved.retention_sweep_enabled:
            sweeper = RetentionThread(db, resolved, app.state.blobs)
            sweeper.start()
        app.state.retention = sweeper
        # 论文导出编译线程（ADR-0012）：开发链在 API 进程内消费队列，
        # 目标态随执行面迁往 backend/worker；测试关闭后改用手动 process 驱动。
        exporter: Optional[PaperExportThread] = None
        if resolved.paper_export_worker_enabled:
            exporter = PaperExportThread(app.state.paper_exports, resolved)
            exporter.start()
        app.state.paper_export_thread = exporter
        # 模型目录（ADR-0017）：后台按 TTL 拉公共目录；启动不等它，先用缓存/内置快照。
        if resolved.model_catalog_enabled:
            app.state.model_catalog.start()
        try:
            yield
        finally:
            if runner is not None:
                runner.stop()
            if sweeper is not None:
                sweeper.stop()
            if exporter is not None:
                exporter.stop()
            app.state.model_catalog.stop()
            app.state.chat_turns.shutdown()
            db.dispose()

    app = FastAPI(
        title="OpenMathModel API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.db = db
    # 服务端托管对话轮的进程内中枢（ADR-0016）：生成线程、事件缓冲与回写。
    app.state.chat_turns = ChatTurnHub(db)
    # 模型目录（ADR-0017）：厂商在售型号与单价的同步视图；用量估价经进程级访问点取用。
    catalog = model_catalog.ModelCatalog(resolved)
    app.state.model_catalog = catalog
    model_catalog.set_current(catalog)
    # Artifact 二进制内容存储（本地内容寻址）；引擎产物经同一存储端口写入
    blobs = LocalContentStore(resolved.artifacts_dir)
    app.state.blobs = blobs
    engine_glue.set_blobstore(blobs)
    # 沙盒工作区根等运行时配置也用这一份 Settings（测试夹具指向 tmp_path 才真隔离）
    engine_glue.set_settings(resolved)
    # 用户头像共用同一存储实现，但目录独立于运行产物（归属与回收边界不同）
    app.state.avatars = LocalContentStore(resolved.avatars_dir)
    # 测试与内部工具可直接驱动推进（runner_enabled=False 时手动 tick）
    app.state.advancer = WorkflowAdvancer(db)
    # 论文导出处理器：编译线程与测试共用（paper_export_worker_enabled=False 时手动 process）
    app.state.paper_exports = PaperExportProcessor(db, resolved, blobs)
    # 登录限速：数据库实现，多实例一致（后续可等接口替换为 Redis）
    from .rate_limit import DbLoginRateLimiter

    app.state.login_limiter = DbLoginRateLimiter(
        db.session_factory,
        resolved.login_max_attempts,
        resolved.login_window_seconds,
    )
    # 注册验证码发送限速（独立窗口：每邮箱/每 IP）
    app.state.email_code_limiter = DbLoginRateLimiter(
        db.session_factory,
        resolved.email_code_max_sends,
        resolved.email_code_window_seconds,
    )

    # 中间件顺序（外→内）：CORS → RequestId → OriginCheck → 路由
    # add_middleware 后添加者在外层，故按内层先加的顺序书写。
    app.add_middleware(OriginCheckMiddleware, allowed_origins=resolved.cors_origins)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)

    # 统一挂载在 /api 下：前端经 Vite 代理 /api → 8000 同源转发。
    app.include_router(projects.router, prefix="/api")
    app.include_router(intake.router, prefix="/api")
    app.include_router(task_runs.router, prefix="/api")
    app.include_router(workspace.router, prefix="/api")
    app.include_router(stage_outputs.router, prefix="/api")
    app.include_router(events.router, prefix="/api")
    app.include_router(artifacts.router, prefix="/api")
    app.include_router(paper_exports.router, prefix="/api")
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(account.router, prefix="/api/account")
    app.include_router(chat.chat_router, prefix="/api/chat")
    app.include_router(chat.llm_router, prefix="/api/llm")
    app.include_router(usage.router, prefix="/api/usage")

    @app.get("/api/health", tags=["ops"])
    def api_health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # 供设置中心「诊断」使用的运行时信息。只暴露后端方言名等非敏感事实，
    # 不含连接串、路径或凭据；与 /api/health 一样无需登录。
    @app.get("/api/system", tags=["ops"])
    def api_system() -> dict[str, object]:
        return {
            "name": app.title,
            "version": app.version,
            "python": platform.python_version(),
            "database": make_url(resolved.database_url).get_backend_name(),
            "runner_enabled": resolved.runner_enabled,
            "time": datetime.now(timezone.utc).isoformat(),
        }

    return app
