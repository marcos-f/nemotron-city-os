"""``python -m helm`` — boot the console on :8610."""

from __future__ import annotations

import logging
import sys

import uvicorn

from helm.app import create_app
from helm.config import BootRefused, load_settings


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        settings = load_settings()
    except BootRefused as exc:
        print(str(exc), file=sys.stderr)
        return 78  # EX_CONFIG
    application = create_app(settings)
    uvicorn.run(application, host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
