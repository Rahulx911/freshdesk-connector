"""Typed errors. Every error carries an agent-readable `code` and a `hint`
so the MCP layer can return something the model can act on (retry later,
ask the user for a valid id, re-authenticate) instead of a stack trace."""

from __future__ import annotations

from typing import Any


class FreshdeskError(Exception):
    code = "freshdesk_error"
    hint = "Unexpected Freshdesk error. Do not retry automatically."

    def __init__(self, message: str, *, status: int | None = None, details: object = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"error": self.code, "message": self.message, "hint": self.hint}
        if self.status is not None:
            out["http_status"] = self.status
        if self.details:
            out["details"] = self.details
        return out


class AuthError(FreshdeskError):
    code = "auth_failed"
    hint = ("The API key is missing, invalid or revoked. Ask the merchant to re-run "
            "`freshdesk-connector auth login`. Do not retry.")


class PermissionDenied(FreshdeskError):
    code = "permission_denied"
    hint = ("The agent whose API key is configured cannot see this resource "
            "(group/role restriction). Tell the user; do not retry.")


class NotFound(FreshdeskError):
    code = "not_found"
    hint = "No record with that id. Check the id or use a search tool first."


class InvalidRequest(FreshdeskError):
    code = "invalid_request"
    hint = "The arguments were invalid. Fix them as described in message/details and call again."


class RateLimited(FreshdeskError):
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after: float, status: int | None = 429):
        super().__init__(message, status=status)
        self.retry_after = retry_after

    @property
    def hint(self) -> str:  # type: ignore[override]
        return (f"Freshdesk API quota exhausted. Wait about {int(self.retry_after)}s before "
                "calling any Freshdesk tool again, or tell the user the data is temporarily "
                "unavailable.")

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["retry_after_seconds"] = round(self.retry_after, 1)
        return d


class UpstreamError(FreshdeskError):
    code = "upstream_unavailable"
    hint = "Freshdesk is failing or unreachable. Retry once later; otherwise tell the user."


class ConfigError(FreshdeskError):
    code = "not_configured"
    hint = "Connector is not configured. Run `freshdesk-connector auth login` first."
