"""CLI: `freshdesk-connector auth login|status|logout`, `serve`, `export-spec`."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys

from .auth import CredentialStore, Credentials, normalize_base_url, resolve_credentials
from .client import FreshdeskClient
from .errors import FreshdeskError


async def _verify(creds: Credentials) -> Credentials:
    async with FreshdeskClient(creds, max_wait_s=10) as c:
        me = await c.whoami()
    contact = me.get("contact", {}) if isinstance(me, dict) else {}
    return Credentials(base_url=creds.base_url, api_key=creds.api_key,
                       agent_name=contact.get("name"), agent_email=contact.get("email"))


def cmd_login(args: argparse.Namespace) -> int:
    domain = args.domain or input("Freshdesk domain (e.g. acme or acme.freshdesk.com): ")
    if args.api_key_stdin:
        key = sys.stdin.readline().strip()
    else:
        key = os.environ.get("FRESHDESK_API_KEY") or getpass.getpass(
            "API key (Freshdesk > Profile settings > View API key; input hidden): ")
    try:
        creds = Credentials(base_url=normalize_base_url(domain), api_key=key.strip())
        verified = asyncio.run(_verify(creds))
    except FreshdeskError as e:
        print(f"Login failed: {e.message}. {e.hint}", file=sys.stderr)
        return 1
    store = CredentialStore()
    store.save(verified)
    print(f"Authenticated as {verified.agent_name or 'unknown agent'} on {verified.base_url}")
    print(f"Credentials stored at {store.path} (mode 0600)")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    try:
        creds = resolve_credentials()
        verified = asyncio.run(_verify(creds))
    except FreshdeskError as e:
        print(json.dumps(e.to_dict(), indent=2))
        return 1
    print(json.dumps({"status": "ok", **verified.redacted()}, indent=2, ensure_ascii=False))
    return 0


def cmd_logout(_: argparse.Namespace) -> int:
    removed = CredentialStore().delete()
    print("Stored credentials removed." if removed else "No stored credentials.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .mcp_server import mcp, run
    if args.transport != "stdio":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    run(args.transport)
    return 0


def cmd_export_spec(args: argparse.Namespace) -> int:
    from .mcp_server import INSTRUCTIONS, mcp

    async def _dump():
        tools = await mcp.list_tools()
        return {
            "server": {"name": "freshdesk", "instructions": INSTRUCTIONS},
            "tools": [t.model_dump(exclude_none=True, by_alias=True) for t in tools],
        }

    spec = asyncio.run(_dump())
    out = json.dumps(spec, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(out + "\n")
        print(f"Wrote {len(spec['tools'])} tool definitions to {args.output}")
    else:
        print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"), stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="freshdesk-connector")
    sub = p.add_subparsers(dest="cmd", required=True)

    auth = sub.add_parser("auth", help="manage credentials")
    asub = auth.add_subparsers(dest="auth_cmd", required=True)
    login = asub.add_parser("login", help="validate and store an API key")
    login.add_argument("--domain")
    login.add_argument("--api-key-stdin", action="store_true",
                       help="read the key from stdin (for scripts; avoids shell history)")
    login.set_defaults(func=cmd_login)
    asub.add_parser("status", help="verify current credentials").set_defaults(func=cmd_status)
    asub.add_parser("logout", help="delete stored credentials").set_defaults(func=cmd_logout)

    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    spec = sub.add_parser("export-spec", help="print MCP tool specification JSON")
    spec.add_argument("-o", "--output")
    spec.set_defaults(func=cmd_export_spec)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
