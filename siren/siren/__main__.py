"""``python -m siren`` — boot on SIREN_PORT (8603 by contract)."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "siren.service:app",
        host=os.environ.get("SIREN_HOST", "127.0.0.1"),
        port=int(os.environ.get("SIREN_PORT", "8603")),
        log_level=os.environ.get("SIREN_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
