"""为附件验收拉起一个干净的 API 实例。

不能直接用 uvicorn 命令行：``backend/api/.env`` 里配了真实 SMTP，注册验证码会
真的发信；这里显式构造 Settings，把 SMTP 关掉并换独立的临时库。

    .venv\\Scripts\\python tools/serve-verify-api.py [port]
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import uvicorn

from omm_api.config import Settings
from omm_api.main import create_app

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8077
workspace = Path(tempfile.gettempdir()) / "omm-verify-attachments"
workspace.mkdir(parents=True, exist_ok=True)

app = create_app(Settings(
    database_url=f"sqlite:///{(workspace / 'verify.db').as_posix()}",
    artifacts_dir=workspace / "artifacts",
    avatars_dir=workspace / "avatars",
    runner_enabled=False,
    smtp_host="",
    email_dev_mode=True,
))

print(f"verify API workspace: {workspace}")
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
