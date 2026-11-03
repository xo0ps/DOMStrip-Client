"""Adapter settings, read from the real environment only (never a ``.env``).

This module is deliberately standalone: the published adapter must not depend on
the private service package, so it carries its own minimal settings model rather
than importing the server's full configuration.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AdapterSettings(BaseSettings):
    """Environment-only settings for the local MCP adapter."""

    model_config = SettingsConfigDict(
        env_prefix="DOMSTRIP_MCP_",
        env_file=None,  # never read a .env from an untrusted working directory
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: Bare HTTPS origin of the DOMStrip API, e.g. ``https://api.domstrip.dev``.
    service_origin: str = ""
    #: Bearer key. Wrapped so it never appears in a repr or a log line.
    api_key: SecretStr = SecretStr("")
    #: Total seconds allowed per tool call, including retries.
    client_budget: float = 25.0
    #: Permit plain HTTP to a loopback host only (local development).
    allow_insecure_http: bool = False
