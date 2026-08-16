"""Boot the service: python -m docket (or the `docket` console script)."""
from __future__ import annotations

import uvicorn

from . import config


def main() -> None:
    uvicorn.run(
        "docket.app:app",
        host=__import__("os").environ.get("DOCKET_HOST", "127.0.0.1"),
        port=config.PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
