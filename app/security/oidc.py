"""OIDC / government SSO — the integration seam (master prompt §7.6, Phase 4).

Disabled by default (`OIDC_ENABLED=false`) and **deliberately not implemented**: the government
identity provider, its discovery URL, its claim names and its group-to-role mapping are all
client-side dependencies that do not exist yet (architecture §11, "client-side dependencies
tracked separately").

What this module *is*: the documented interface the real implementation will fill, positioned so
that adding SSO changes this file and `app/deps.py`'s dependency wiring — and nothing else. In
particular, `map_claims_to_role` is where a ministry's group names become DANAH roles, and it is
the only place that mapping should ever live.

The alternative — half-implementing an OIDC flow against an imagined IdP — would produce code that
looks finished, passes no real test, and has to be thrown away the moment the real issuer's
metadata arrives.

To implement:
  1. Fetch `{OIDC_ISSUER_URL}/.well-known/openid-configuration` at startup; cache the JWKS.
  2. `POST /api/auth/oidc/login`  -> redirect to the IdP with PKCE + state + nonce.
  3. `GET  /api/auth/oidc/callback` -> exchange the code, verify the id_token against the JWKS
     (iss, aud, exp, nonce), then map claims -> DANAH user + role via `map_claims_to_role`.
  4. Issue DANAH's own access/refresh pair (the rest of the system stays unchanged — this is why
     the gateway is worth having).
  5. Keep password login available for break-glass admin access, or explicitly disable it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from app.config import Settings
from app.enums import Role
from app.exceptions import AuthError

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class OIDCClaims:
    """The subset of the id_token DANAH cares about."""

    subject: str
    email: str
    full_name: str
    groups: list[str]
    raw: dict[str, Any]


class OIDCNotConfiguredError(AuthError):
    code = "oidc_not_configured"
    message = (
        "Single sign-on is not enabled on this deployment. Set OIDC_ENABLED=true and configure "
        "OIDC_ISSUER_URL / OIDC_CLIENT_ID / OIDC_CLIENT_SECRET."
    )


def is_enabled(settings: Settings) -> bool:
    return settings.oidc_enabled and bool(settings.oidc_issuer_url)


def map_claims_to_role(claims: OIDCClaims, settings: Settings) -> Role:
    """Map the IdP's groups to a DANAH role.

    The single place this mapping is allowed to live. When the ministry supplies its group names,
    they are configured here (or, better, moved into `Settings` as a JSON map) — not scattered
    through the API layer.

    Fails CLOSED: an unrecognised group grants `viewer`, the least privilege, never `admin`.
    """
    groups = {g.lower() for g in claims.groups}

    if {"danah-admin", "ministry-it-admin"} & groups:
        return Role.ADMIN
    if {"danah-executive", "ministry-executive", "under-secretary"} & groups:
        return Role.EXECUTIVE
    if {"danah-analyst", "ministry-analyst"} & groups:
        return Role.ANALYST

    log.info("oidc_unmapped_groups_defaulted_to_viewer", group_count=len(groups))
    return Role.VIEWER


async def discover(settings: Settings) -> dict[str, Any]:
    """Fetch the IdP's OpenID configuration document."""
    if not is_enabled(settings):
        raise OIDCNotConfiguredError()
    raise NotImplementedError(
        "OIDC discovery is not implemented: the government IdP's issuer URL and metadata are a "
        "client-side dependency that has not yet been supplied. See the module docstring for the "
        "five steps and the exact seam to fill."
    )


async def exchange_code(settings: Settings, *, code: str, verifier: str) -> OIDCClaims:
    """Exchange an authorisation code for a verified id_token, and extract the claims."""
    if not is_enabled(settings):
        raise OIDCNotConfiguredError()
    raise NotImplementedError("OIDC code exchange is not implemented — see the module docstring.")
