"""从 backend/api 真实应用导出 OpenAPI 基线（确定性 JSON），支持漂移门禁。

用法（任何装有 backend/api 及其依赖的 Python 环境）:
    python packages/contracts/scripts/export_openapi.py            # 导出/刷新基线
    python packages/contracts/scripts/export_openapi.py --check    # 比对不落盘，漂移退出码 1（CI）

约定：
- 基线文件 openapi/v1/openapi.api.json 是"从代码导出"的事实快照，禁止手改；
  接口有意变更时重跑本脚本刷新基线并随代码一并提交评审。
- 输出经 sort_keys + 固定缩进序列化，同版本依赖下逐字节稳定；
  fastapi/pydantic 大版本升级可能改变渲染细节，届时有意识地重新冻结。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CONTRACTS_ROOT = Path(__file__).resolve().parent.parent
BASELINE = CONTRACTS_ROOT / "openapi" / "v1" / "openapi.api.json"


def _render_spec() -> str:
    from omm_api.config import Settings
    from omm_api.main import create_app

    # 仅构建应用拿 schema：不启动 lifespan、不建表、不落任何运行时文件
    app = create_app(Settings(database_url="sqlite://", runner_enabled=False))
    spec = app.openapi()
    return json.dumps(spec, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export/check the OpenAPI baseline of backend/api.")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    rendered = _render_spec()

    if args.check:
        if not BASELINE.exists():
            print(f'CONTRACTS_OPENAPI_NO_BASELINE {{"hint":"run without --check first","path":"{BASELINE.as_posix()}"}}')
            return 3
        current = BASELINE.read_text(encoding="utf-8")
        if current != rendered:
            print("CONTRACTS_OPENAPI_STALE OpenAPI 与基线不一致：接口变更须重跑 export_openapi.py 刷新基线并随代码提交评审")
            return 1
        print('CONTRACTS_OPENAPI_OK {"baseline":"openapi/v1/openapi.api.json"}')
        return 0

    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(rendered, encoding="utf-8")
    print(f'CONTRACTS_OPENAPI_FROZEN {{"path":"{BASELINE.as_posix()}","bytes":{len(rendered.encode("utf-8"))}}}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
