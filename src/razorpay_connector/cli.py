"""Command line entry point for the Razorpay connector."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .mcp_server import build_server


def cmd_serve(args: argparse.Namespace) -> int:
    build_server().run()
    return 0


def cmd_export_spec(args: argparse.Namespace) -> int:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    spec = {
        "server": {"name": "razorpay-connector", "mode": "read-only"},
        "tools": [
            {"name": t.name, "description": t.description, "input_schema": t.inputSchema,
             "annotations": {
                 "readOnlyHint": bool(getattr(t.annotations, "readOnlyHint", False)),
                 "destructiveHint": bool(getattr(t.annotations, "destructiveHint", False)),
                 "idempotentHint": bool(getattr(t.annotations, "idempotentHint", False)),
             }}
            for t in tools
        ],
    }
    text = json.dumps(spec, indent=2, default=str) + "\n"
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(spec['tools'])} tools)")
    else:
        sys.stdout.write(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="razorpay-connector",
                                description="Read-only Razorpay connector for Agent Studio")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the MCP server on stdio").set_defaults(func=cmd_serve)
    spec = sub.add_parser("export-spec", help="write the MCP tool spec as JSON")
    spec.add_argument("-o", "--out")
    spec.set_defaults(func=cmd_export_spec)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
