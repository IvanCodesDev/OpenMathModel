from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """API 配置。环境变量前缀 OMM_，如 OMM_DATABASE_URL。

    本地开发可在 backend/api/.env 写入配置（已被 .gitignore 排除，绝不入库），
    如 SMTP 授权码等敏感项；环境变量优先于 .env 文件。
    """

    model_config = SettingsConfigDict(
        env_prefix="OMM_",
        env_file=str(SERVICE_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 数据库限定 PostgreSQL（2026-08-28 起）：默认连本地免安装实例
    # （tools/pg-dev.ps1，port 5433；Docker 底座为 5432，见 infra/docker/compose.dev.yaml）。
    # SQLite 不再是任何默认路径：仅测试夹具显式传入临时库，或应急排查时显式覆盖本变量。
    database_url: str = "postgresql+psycopg://openmathmodel:openmathmodel@127.0.0.1:5433/openmathmodel"
    # 启动探库失败且目标就是 tools/pg-dev.ps1 管的本地实例时自动 `start` 一次（仅 Windows、
    # 仅 127.0.0.1/localhost:5433），让单独起 uvicorn 与 `npm run dev` 一样不用先手动拉库。
    local_pg_autostart: bool = True

    # 内嵌模拟工作流推进器（T5 将替换为 agents/core 驱动的 worker）。
    # tick 即模拟阶段的停留时长：每 tick 完成一个阶段。1.2s 会让审批后的
    # 实验→验证→论文在数秒内全部完成，页面直接跳到最终成果、中间阶段不可感知；
    # 放慢到 5s 让六阶段推进可以被逐页跟随（真实节点接入后此参数退役）。
    runner_enabled: bool = True
    runner_tick_seconds: float = 5.0

    # SSE 轮询与心跳间隔（秒）。事件表是事实来源，Redis 通知后续批次引入。
    sse_poll_seconds: float = 0.5
    sse_heartbeat_seconds: float = 15.0

    # Artifact 二进制内容存储根（本地内容寻址；MinIO/S3 待底座就绪后经同一协议接入）
    artifacts_dir: Path = SERVICE_ROOT / "data" / "artifacts"
    # 实验代码的隔离工作区根（每个 run 一个子目录）与单次执行时限。
    # 工作区是执行暂存；产物文件通过 ArtifactStore 端口进内容寻址存储。
    workspaces_dir: Path = SERVICE_ROOT / "data" / "workspaces"
    experiment_timeout_seconds: float = 120.0
    # 上传大小上限（字节）；对象存储直传落地前的临时护栏
    artifact_max_bytes: int = 50 * 1024 * 1024
    # 正文抽取上限（字节）：比上传上限更严，超过的附件只保留原文件不抽正文，
    # 免得单个大附件把控制面的请求线程占住。
    attachment_text_max_bytes: int = 32 * 1024 * 1024
    # 图片 OCR 的识别语言（Tesseract 语言包名）；未安装 Tesseract 时该项不生效。
    ocr_languages: str = "chi_sim+eng"
    # ── 远程 OCR（讯飞星辰 MaaS 上的 PaddleOCR，OpenAI 兼容协议）────────
    # 本地 paddle 栈已移除（2026-08-30）：扫描件 PDF 与图片的文档解析改走远程
    # chat/completions（图片以 base64 data URL 传入，输出 Markdown，公式为
    # LaTeX）。key 是敏感项，放 backend/api/.env（OMM_OCR_API_KEY=...）或环境
    # 变量，绝不写进任何入库文件；留空 = 功能关闭，图片回落 Tesseract、扫描件
    # 如实 empty。base_url 与 model 非敏感，默认即当前使用的服务。
    ocr_api_base_url: str = "https://maas-api.cn-huabei-1.xf-yun.com/v2"
    ocr_api_model: str = "xoppaddleocrv16"
    ocr_api_key: str = ""
    # 单次远程识别（每页一次调用）的超时；扫描件逐页串行，总时长 ≈ 页数 × 单次。
    ocr_api_timeout_seconds: float = 60.0

    # 高级设置「最大并发任务」的默认值与可调上限；用户改动存 users 表，按用户生效。
    default_max_concurrent_runs: int = 3
    max_concurrent_runs_ceiling: int = 8

    # ── 论文导出（ADR-0012 阶段 A：服务端 Tectonic 编译 PDF）──────
    # Tectonic 可执行文件路径；空 = 在 PATH 探测。未安装时导出任务落
    # UNSUPPORTED 并说明启用途径（诚实降级，不阻断其他功能）。
    tectonic_path: str = ""
    paper_export_timeout_seconds: float = 120.0
    # source_tex 的 UTF-8 字节上限
    paper_export_max_bytes: int = 2 * 1024 * 1024
    # 编译消费线程：开发链沿 RunnerThread 模式在 API 进程内直跑，目标态随
    # 执行面迁往 backend/worker；测试关闭线程改用手动 process 驱动。
    paper_export_worker_enabled: bool = True
    paper_export_poll_seconds: float = 1.0
    # 全局在途上限（QUEUED+RUNNING），防止编译队列被灌爆；每用户同时 1 个在提交时另行校验。
    paper_export_queue_limit: int = 20

    # 「数据与隐私」保留清扫的周期（秒）。首轮延后一个周期执行；
    # 测试环境与 runner 一样可整体关闭，避免后台线程干扰断言。
    retention_sweep_enabled: bool = True
    retention_sweep_seconds: float = 900.0

    # 用户头像内容存储根：与运行产物同一存储协议但目录独立，
    # 二者生命周期和归属边界不同（产物按项目回收，头像随账户长期存在）。
    avatars_dir: Path = SERVICE_ROOT / "data" / "avatars"
    # 前端会先裁剪压缩到 256×256 再上传，这里是服务端兜底上限
    avatar_max_bytes: int = 2 * 1024 * 1024

    # 开发环境 CORS（Vite 端口：5183 为本项目固定端口，5173 为历史默认保留）；生产由网关处理。
    cors_origins: list[str] = [
        "http://localhost:5183",
        "http://127.0.0.1:5183",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # ── 认证与会话（账户与安全批次） ─────────────────────────────
    # 开发默认值仅用于本地；生产部署必须以 OMM_SECRET_KEY 覆盖。
    secret_key: str = "dev-secret-change-me"
    session_cookie_name: str = "omm_session"
    session_ttl_days: int = 30
    challenge_ttl_seconds: int = 300
    cookie_secure: bool = False
    login_max_attempts: int = 10
    login_window_seconds: int = 300
    password_min_length: int = 8
    # bcrypt 算法输入上限为 72 字节，超长必须显式拒绝而非静默截断
    password_max_bytes: int = 72
    recovery_code_count: int = 10

    # ── 注册邮箱验证码 ───────────────────────────────────────────
    email_code_ttl_seconds: int = 600
    email_code_max_sends: int = 3       # 每邮箱/每 IP 在窗口内的发送上限
    email_code_window_seconds: int = 300
    # 开发模式：无 SMTP 时验证码直接随响应返回并写日志，便于本地联调；
    # 配置 OMM_SMTP_HOST 后自动走真实邮件，dev_code 不再返回。
    email_dev_mode: bool = True
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "OpenMathModel <no-reply@openmathmodel.dev>"
    smtp_starttls: bool = False  # False=SSL(465)，True=STARTTLS(587)


@lru_cache
def get_settings() -> Settings:
    return Settings()


# 认证模块以模块级常量方式引用这些配置（进程级，启动时从环境读取一次）。
_auth_settings = get_settings()
SECRET_KEY = _auth_settings.secret_key
SESSION_COOKIE_NAME = _auth_settings.session_cookie_name
SESSION_TTL_DAYS = _auth_settings.session_ttl_days
CHALLENGE_TTL_SECONDS = _auth_settings.challenge_ttl_seconds
COOKIE_SECURE = _auth_settings.cookie_secure
LOGIN_MAX_ATTEMPTS = _auth_settings.login_max_attempts
LOGIN_WINDOW_SECONDS = _auth_settings.login_window_seconds
PASSWORD_MIN_LENGTH = _auth_settings.password_min_length
PASSWORD_MAX_BYTES = _auth_settings.password_max_bytes
RECOVERY_CODE_COUNT = _auth_settings.recovery_code_count
DEFAULT_MAX_CONCURRENT_RUNS = _auth_settings.default_max_concurrent_runs
MAX_CONCURRENT_RUNS_CEILING = _auth_settings.max_concurrent_runs_ceiling
