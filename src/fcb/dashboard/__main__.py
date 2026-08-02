"""Entrypoint for the FCB dashboard process.

Run:  uv run python -m fcb.dashboard
"""

import uvicorn

from fcb import config


def main() -> None:
    uvicorn.run(
        "fcb.dashboard.app:app",
        host="127.0.0.1",
        port=config.FCB_WEB_PORT,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_config=None,
    )


if __name__ == "__main__":
    main()
