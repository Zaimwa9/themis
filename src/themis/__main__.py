"""`python -m themis` entry point."""

import argparse
import logging
import os
import sys

import httpx
import uvicorn

from themis.app import create_app
from themis.agent import create_agent_app
from themis.bootstrap import BootstrapError, add_init_parser, options_from_args, run_bootstrap


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


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m themis")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("controller", help="run the GitHub-facing controller")
    subparsers.add_parser("agent", help="run the isolated model agent")
    add_init_parser(subparsers)
    args = parser.parse_args(argv)
    if args.command == "init":
        try:
            run_bootstrap(options_from_args(args))
        except (BootstrapError, httpx.HTTPError, OSError) as error:
            parser.error(str(error))
        return
    main(args.command)


if __name__ == "__main__":
    cli(sys.argv[1:])
