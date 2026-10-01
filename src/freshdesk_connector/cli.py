"""CLI: `freshdesk-connector auth login|status|logout`, `serve`, `export-spec`."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys

from .auth import Credentials, CredentialStore, normalize_base_url, resolve_credentials
from .client import FreshdeskClient
from .errors import FreshdeskError
from .observability import configure_logging
from .tenancy import RegistryWatcher, hash_token, mint_token


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
    from . import mcp_server as ms

    if args.transport == "stdio":
        if args.tenants:
            print("--tenants needs an HTTP transport (stdio is single-merchant)", file=sys.stderr)
            return 2
        ms.mcp.run(transport="stdio")
        return 0

    registry = None
    tenants = args.tenants or os.environ.get("FRESHDESK_TENANTS_FILE")
    if tenants:
        try:
            registry = RegistryWatcher(tenants)       # hot-reloads on file change
        except FreshdeskError as e:
            print(f"Cannot start: {e.message}", file=sys.stderr)
            return 2
    elif args.host not in ("127.0.0.1", "localhost") and not args.allow_unauthenticated:
        print("Refusing to serve HTTP on a non-local interface without --tenants (bearer-token "
              "auth). Pass --allow-unauthenticated only behind a trusted gateway.", file=sys.stderr)
        return 2
    ms.configure(ms.ServiceProvider(ms.Settings.from_env(), registry))
    allowed = [h for h in (args.allowed_hosts or os.environ.get("MCP_ALLOWED_HOSTS", "")).split(",") if h]
    server = ms.build_server(registry=registry, public_url=args.public_url or os.environ.get("MCP_PUBLIC_URL"),
                             stateless=args.transport == "streamable-http", host=args.host,
                             port=args.port, allowed_hosts=allowed or None)
    server.run(transport=args.transport)
    return 0


def cmd_token_create(args: argparse.Namespace) -> int:
    token = mint_token()
    entry = {"name": args.name, "tenant": args.tenant, "sha256": hash_token(token)}
    print("Bearer token (shown once; store it in Agent Studio's secret store):", file=sys.stderr)
    print(token)
    print("\nAdd this entry to the \"tokens\" list in your tenant registry:", file=sys.stderr)
    print(json.dumps(entry), file=sys.stderr)
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
    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"), os.environ.get("LOG_FORMAT", "text"))
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
    serve.add_argument("--tenants", help="tenant registry JSON (enables bearer-token auth, multi-merchant)")
    serve.add_argument("--public-url", help="externally visible base URL (for OAuth resource metadata)")
    serve.add_argument("--allowed-hosts", help="comma-separated Host headers to accept (DNS-rebinding protection)")
    serve.add_argument("--allow-unauthenticated", action="store_true",
                       help="serve HTTP on a non-local interface without auth (behind a trusted gateway only)")
    serve.set_defaults(func=cmd_serve)

    tok = sub.add_parser("token", help="manage bearer tokens for hosted mode")
    tsub = tok.add_subparsers(dest="token_cmd", required=True)
    tc = tsub.add_parser("create", help="mint a token bound to one tenant")
    tc.add_argument("--tenant", required=True)
    tc.add_argument("--name", required=True, help="label, e.g. agent-studio-acme-prod")
    tc.set_defaults(func=cmd_token_create)

    spec = sub.add_parser("export-spec", help="print MCP tool specification JSON")
    spec.add_argument("-o", "--output")
    spec.set_defaults(func=cmd_export_spec)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
