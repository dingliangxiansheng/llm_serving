"""``python -m gateway``：直接起 uvicorn（容器里的默认命令之一）。"""

from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "gateway.app:app",
        host=settings.server.host,
        port=settings.server.port,
        log_level=settings.server.log_level,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
