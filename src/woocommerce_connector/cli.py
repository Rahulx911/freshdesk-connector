"""Command line entry point: auth, serve, export-spec."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from . import auth, tenancy
from .client import WooClient
from .errors import WooError
from .mcp_server import build_server


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


async def _verify(creds: auth.Credentials) -> dict:
    async with WooClient(creds) as client:
        return await client.ping()


def cmd_login(args: argparse.Namespace) -> int:
    store = args.store or input("Store URL (e.g. https://shop.example.com): ").strip()
    try:
        store_url = auth.normalize_store_url(store)
    except WooError as e:
        print(f"error: {e.message}", file=sys.stderr)
        return 2

    key = args.key or input(
        "Consumer key (WooCommerce > Settings > Advanced > REST API > Add key, "
        "Permissions = Read): "
    ).strip()
    secret = args.secret or getpass.getpass("Consumer secret (input hidden): ").strip()
    if not key or not secret:
        print("error: both a consumer key and secret are required", file=sys.stderr)
        return 2

    creds = auth.Credentials(store_url, key, secret)
    try:
        info = asyncio.run(_verify(creds))
    except WooError as e:
        print(f"error: {e.message}\nhint: {e.hint}", file=sys.stderr)
        return 1

    path = auth.save(creds)
    _print({"saved": str(path), "store": creds.redacted(), "orders_visible": info.get("orders_visible")})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        creds = auth.load()
    except WooError as e:
        _print({"configured": False, "error": e.code, "message": e.message, "hint": e.hint})
        return 1
    out: dict[str, Any] = {"configured": True, "store": creds.redacted()}
    try:
        out.update(asyncio.run(_verify(creds)))
    except WooError as e:
        out.update({"reachable": False, "error": e.code, "message": e.message})
        _print(out)
        return 1
    _print(out)
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    _print({"removed": auth.clear()})
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    build_server().run()
    return 0


def cmd_export_spec(args: argparse.Namespace) -> int:
    """Write the MCP tool specification as JSON, for review and for docs."""
    server = build_server()
    tools = asyncio.run(server.list_tools())
    prompts = asyncio.run(server.list_prompts())
    spec = {
        "server": {"name": "woocommerce-connector", "mode": "read-only"},
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.inputSchema,
                "annotations": {
                    "readOnlyHint": bool(getattr(t.annotations, "readOnlyHint", False)),
                    "destructiveHint": bool(getattr(t.annotations, "destructiveHint", False)),
                    "idempotentHint": bool(getattr(t.annotations, "idempotentHint", False)),
                },
            }
            for t in tools
        ],
        "prompts": [{"name": p.name, "description": p.description} for p in prompts],
    }
    text = json.dumps(spec, indent=2, default=str) + "\n"
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(spec['tools'])} tools, {len(spec['prompts'])} prompts)")
    else:
        sys.stdout.write(text)
    return 0


def cmd_token_create(args: argparse.Namespace) -> int:
    """Mint a bearer token and print the registry line to paste.

    The token itself is shown once and never stored. Only its SHA-256 goes in
    the registry, so a leaked registry cannot be replayed against the API.
    """
    token = tenancy.mint_token()
    digest = tenancy.hash_token(token)
    print("Token (shown once, store it in Agent Studio now):\n")
    print(f"    {token}\n")
    print("Registry line to add under \"tokens\":\n")
    _print({"name": args.name, "tenant": args.tenant, "sha256": digest})
    return 0


def cmd_tenants(args: argparse.Namespace) -> int:
    """Show what the registry contains, without revealing any of it."""
    try:
        registry = tenancy.Registry.load(args.file)
    except WooError as e:
        _print({"ok": False, "error": e.code, "message": e.message, "hint": e.hint})
        return 1
    _print({"ok": True, **registry.describe()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="woocommerce-connector",
                                description="Read-only WooCommerce connector for Agent Studio")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("auth", help="manage store credentials")
    asub = a.add_subparsers(dest="auth_command", required=True)
    login = asub.add_parser("login", help="store and verify a consumer key/secret")
    login.add_argument("--store")
    login.add_argument("--key")
    login.add_argument("--secret")
    login.set_defaults(func=cmd_login)
    asub.add_parser("status", help="show the configured store").set_defaults(func=cmd_status)
    asub.add_parser("logout", help="delete stored credentials").set_defaults(func=cmd_logout)

    sub.add_parser("serve", help="run the MCP server on stdio").set_defaults(func=cmd_serve)

    token = sub.add_parser("token", help="manage hosted-mode bearer tokens")
    tsub = token.add_subparsers(dest="token_command", required=True)
    create = tsub.add_parser("create", help="mint a token and print its registry line")
    create.add_argument("--name", required=True, help="a label for this agent")
    create.add_argument("--tenant", required=True, help="the merchant this token is bound to")
    create.set_defaults(func=cmd_token_create)

    tenants = sub.add_parser("tenants", help="summarise the tenant registry")
    tenants.add_argument("-f", "--file", help=f"registry path (default: ${tenancy.ENV_REGISTRY})")
    tenants.set_defaults(func=cmd_tenants)

    spec = sub.add_parser("export-spec", help="write the MCP tool spec as JSON")
    spec.add_argument("-o", "--out")
    spec.set_defaults(func=cmd_export_spec)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
