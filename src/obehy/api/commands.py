"""``obehy api`` subcommands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from obehy.runtime_config import ConfigurationError, load_database_url


def add_parsers(commands: Any) -> None:
    api = commands.add_parser("api", help="the project API")
    api_commands = api.add_subparsers(dest="api_command", required=True)
    serve = api_commands.add_parser("serve", help="serve the API over HTTP (uvicorn)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--config", type=Path)
    serve.add_argument("--database-url", help="default: OBEHY_DATABASE_URL or [database] url")
    openapi = api_commands.add_parser(
        "openapi", help="write the OpenAPI document (the frontend's types come from it)"
    )
    openapi.add_argument("--out", type=Path, help="default: standard output")


def openapi_document() -> str:
    """The OpenAPI document, without a database: building the schema never opens the pool."""

    from obehy.api.app import create_app

    app = create_app("postgresql://unused")
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def run(args: argparse.Namespace) -> int:
    if args.api_command == "openapi":
        document = openapi_document()
        out = cast(Path | None, args.out)
        if out is None:
            sys.stdout.write(document)
        else:
            out.write_text(document, encoding="utf-8", newline="\n")
        return 0

    import uvicorn

    from obehy.api.app import create_app

    try:
        url = cast(str | None, args.database_url) or load_database_url(
            cast(Path | None, args.config)
        )
    except ConfigurationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    uvicorn.run(
        create_app(url),
        host=cast(str, args.host),
        port=cast(int, args.port),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0
