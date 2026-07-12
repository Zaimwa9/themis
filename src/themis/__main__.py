"""`python -m themis` entry point."""

import logging
import os
import sys

import uvicorn

from themis.app import create_app
from themis.agent import create_agent_app


def main(role: str | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    role = role or os.getenv("THEMIS_ROLE", "controller")
    if role not in ("controller", "agent"):
        raise SystemExit("usage: python -m themis [controller|agent]")
    app = create_agent_app() if role == "agent" else create_app()
    default_port = "8001" if role == "agent" else "8000"
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", default_port)))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
