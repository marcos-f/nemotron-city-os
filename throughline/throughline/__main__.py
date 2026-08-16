"""``python -m throughline`` — serve on THROUGHLINE_PORT (default 8600)."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .service import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
