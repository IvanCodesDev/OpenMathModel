from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """API 配置。环境变量前缀 OMM_，如 OMM_DATABASE_URL。

    本地开发可在 services/api/.env 写入配置（已被 .gitignore 排除，绝不入库），
    如 SMTP 授权码等敏感项；环境变量优先于 .env 文件。
    """

    model_config = SettingsConfigDict(
        env_prefix="OMM_",
        env_file=str(SERVICE_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 默认 SQLite 零依赖起步（services/api/data/dev.db）。
    # 切 PostgreSQL：先起本地底座（tools/dev-up.ps1，定义见 infra/docker/compose.dev.yaml），再设
    #   OMM_DATABASE_URL=postgresql+psycopg://openmathmodel:openmathmodel-dev@127.0.0.1:5432/openmathmodel
    database_url: str = f"sqlite:///{(SERVICE_ROOT / 'data' / 'dev.db').as_posix()}"

    # 内嵌模拟工作流推进器（T5 将替换为 agents/core 驱动的 worker）。
    runner_enabled: bool = True
    runner_tick_seconds: float = 1.2

    # SSE 轮询与心跳间隔（秒）。事件表是事实来源，Redis 通知后续批次引入。
    sse_poll_seconds: float = 0.5
    sse_heartbeat_seconds: float = 15.0

    # Artifact 二进制内容存储根（本地内容寻址；MinIO/S3 待底座就绪后经同一协议接入）
    artifacts_dir: Path = SERVICE_ROOT / "data" / "artifacts"
    # 上传大小上限（字节）；对象存储直传落地前的临时护栏
    artifact_max_bytes: int = 50 * 1024 * 1024

    # 开发环境 CORS（Vite 默认端口）；生产由网关处理。
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

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
