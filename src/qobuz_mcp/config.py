from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_csv(name: str) -> list[str] | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    transport: str = field(
        default_factory=lambda: os.getenv("QOBUZ_MCP_TRANSPORT", "stdio")
    )
    http_host: str = field(
        default_factory=lambda: os.getenv("QOBUZ_MCP_HTTP_HOST", "127.0.0.1")
    )
    http_port: int = field(
        default_factory=lambda: int(os.getenv("QOBUZ_MCP_HTTP_PORT", "8000"))
    )
    http_bearer_token: str | None = field(
        default_factory=lambda: (os.getenv("QOBUZ_MCP_HTTP_BEARER_TOKEN") or "").strip()
        or None
    )
    # OIDC / remote OAuth — for hosting behind a reverse proxy (Claude apps). When
    # config_url/client_id/client_secret/base_url are all set, HTTP transport exposes a
    # DCR-compliant OAuth interface (FastMCP OIDCProxy) brokered to the upstream IdP.
    oidc_config_url: str | None = field(
        default_factory=lambda: os.getenv("OIDC_CONFIG_URL")
    )
    oidc_client_id: str | None = field(
        default_factory=lambda: os.getenv("OIDC_CLIENT_ID")
    )
    oidc_client_secret: str | None = field(
        default_factory=lambda: os.getenv("OIDC_CLIENT_SECRET")
    )
    oidc_base_url: str | None = field(
        default_factory=lambda: os.getenv("OIDC_BASE_URL")
    )
    oidc_redirect_path: str = field(
        default_factory=lambda: os.getenv("OIDC_REDIRECT_PATH", "/auth/callback")
    )
    oidc_required_scopes: list[str] | None = field(
        default_factory=lambda: _env_csv("OIDC_REQUIRED_SCOPES")
    )
    oidc_allowed_redirect_uris: list[str] | None = field(
        default_factory=lambda: _env_csv("OIDC_ALLOWED_REDIRECT_URIS")
    )
    oidc_verify_id_token: bool = field(
        default_factory=lambda: _env_bool("OIDC_VERIFY_ID_TOKEN")
    )
    # RFC 8707 resource indicator: PocketID (and many OIDC IdPs) reject it upstream, so
    # do not forward by default. Enable only for IdPs that support resource indicators.
    oidc_forward_resource: bool = field(
        default_factory=lambda: _env_bool("OIDC_FORWARD_RESOURCE")
    )
    # FastMCP (v3) Host/Origin request guard: allows only localhost by default and 421s a
    # proxied Host header, so it must be relaxed behind a reverse proxy. Default off for HTTP.
    host_origin_protection: bool | None = field(
        default_factory=lambda: (
            _env_bool("MCP_HOST_ORIGIN_PROTECTION")
            if os.getenv("MCP_HOST_ORIGIN_PROTECTION") is not None
            else None
        )
    )
    allowed_hosts: list[str] | None = field(
        default_factory=lambda: _env_csv("MCP_ALLOWED_HOSTS")
    )
    allowed_origins: list[str] | None = field(
        default_factory=lambda: _env_csv("MCP_ALLOWED_ORIGINS")
    )

    @property
    def oidc_enabled(self) -> bool:
        return bool(
            self.oidc_config_url
            and self.oidc_client_id
            and self.oidc_client_secret
            and self.oidc_base_url
        )


config = Config()
