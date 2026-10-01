"""Typed errors. Every error carries an agent-readable `code` and a `hint` so
the MCP layer can return something the model can act on (retry later, ask the
user for a valid id, re-authenticate) instead of a stack trace."""

from __future__ import annotations

from typing import Any


class WooError(Exception):
    code = "woocommerce_error"
    hint = "Unexpected WooCommerce error. Do not retry automatically."

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


class AuthError(WooError):
    code = "auth_failed"
    hint = ("The consumer key/secret is missing, invalid or revoked. Ask the merchant to re-run "
            "`woocommerce-connector auth login`. Do not retry.")


class PermissionDenied(WooError):
    code = "permission_denied"
    hint = ("The API key does not have permission to read this resource. WooCommerce keys are "
            "scoped (Read / Write / Read-Write); this connector needs a Read key. Tell the user; "
            "do not retry.")


class NotFound(WooError):
    code = "not_found"
    hint = "No record with that id. Check the id or use a search tool first."


class InvalidRequest(WooError):
    code = "invalid_request"
    hint = "The arguments were invalid. Fix them as described in message/details and call again."


class RateLimited(WooError):
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after: float, status: int | None = 429):
        super().__init__(message, status=status)
        self.retry_after = retry_after

    @property
    def hint(self) -> str:  # type: ignore[override]
        return (f"The store is throttling requests. Wait about {int(self.retry_after)}s before "
                "calling any WooCommerce tool again, or tell the user the data is temporarily "
                "unavailable.")

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["retry_after_seconds"] = round(self.retry_after, 1)
        return d


class UpstreamError(WooError):
    code = "upstream_unavailable"
    hint = "The store is failing or unreachable. Retry once later; otherwise tell the user."


class ConfigError(WooError):
    code = "not_configured"
    hint = "Connector is not configured. Run `woocommerce-connector auth login` first."
