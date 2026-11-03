"""Tests for the standalone MCP adapter's config and client behaviour."""

from __future__ import annotations

import httpx
import pytest
import respx

from domstrip_mcp.config import AdapterSettings
from domstrip_mcp.server import AdapterConfig, DomstripClient, DomstripServiceError


def _settings(**kw) -> AdapterSettings:
    return AdapterSettings(service_origin="https://api.example.com", api_key="k", **kw)


def _mcp_config(**kw) -> AdapterConfig:
    return AdapterConfig(origin="https://api.example.com", api_key="k", budget_seconds=5, **kw)


# -- configuration -----------------------------------------------------------


def test_requires_https_origin() -> None:
    with pytest.raises(RuntimeError, match="https"):
        AdapterConfig.from_settings(
            AdapterSettings(service_origin="http://x.example", api_key="k")
        )
    with pytest.raises(RuntimeError, match="required"):
        AdapterConfig.from_settings(AdapterSettings())


@pytest.mark.parametrize(
    "origin",
    ["https://:pw@api.example.com", "https://user@api.example.com", "https://api.example.com#x"],
)
def test_rejects_non_bare_origins(origin: str) -> None:
    with pytest.raises(RuntimeError, match="bare origin"):
        AdapterConfig.from_settings(AdapterSettings(service_origin=origin, api_key="k"))


def test_insecure_http_only_for_loopback() -> None:
    with pytest.raises(RuntimeError, match="https"):
        AdapterConfig.from_settings(
            AdapterSettings(
                service_origin="http://api.example.com", api_key="k", allow_insecure_http=True
            )
        )
    config = AdapterConfig.from_settings(
        AdapterSettings(
            service_origin="http://127.0.0.1:8080", api_key="k", allow_insecure_http=True
        )
    )
    assert config.origin == "http://127.0.0.1:8080"


def test_repr_hides_api_key() -> None:
    config = AdapterConfig(origin="https://api.example.com", api_key="domstrip_secret_value")
    assert "domstrip_secret_value" not in repr(config)


# -- client behaviour --------------------------------------------------------


@respx.mock
async def test_retries_with_same_idempotency_key() -> None:
    success = {
        "status": "success",
        "request_id": "req_1",
        "markdown": "# Hi\n",
        "extraction_method": "full",
        "warnings": [],
        "usage": {"units": 0, "state": "unbilled"},
    }
    route = respx.post("https://api.example.com/v1/clean-url").mock(
        side_effect=[
            httpx.Response(
                503,
                json={"error": {"code": "overloaded", "message": "busy", "retryable": True}},
                headers={"retry-after": "0"},
            ),
            httpx.Response(200, json=success),
        ]
    )
    client = DomstripClient(_mcp_config())
    try:
        result = await client.convert("/v1/clean-url", {"url": "https://example.com"})
    finally:
        await client.aclose()
    assert result.markdown == "# Hi\n"
    keys = {call.request.headers["idempotency-key"] for call in route.calls}
    assert len(route.calls) == 2 and len(keys) == 1


@respx.mock
async def test_surfaces_non_retryable_error_and_refuses_redirects() -> None:
    respx.post("https://api.example.com/v1/clean-url").mock(
        return_value=httpx.Response(
            403,
            json={
                "error": {
                    "code": "target_blocked",
                    "message": "no",
                    "retryable": False,
                    "request_id": "req_x",
                }
            },
        )
    )
    respx.post("https://api.example.com/v1/clean-raw").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example/"})
    )
    client = DomstripClient(_mcp_config())
    try:
        with pytest.raises(DomstripServiceError) as exc:
            await client.convert("/v1/clean-url", {"url": "http://10.0.0.1"})
        assert exc.value.code == "target_blocked" and not exc.value.retryable
        assert "request_id=req_x" in str(exc.value)
        with pytest.raises(DomstripServiceError) as exc:
            await client.convert("/v1/clean-raw", {"raw_html": "<p>x</p>"})
        assert exc.value.code == "unexpected_redirect"
    finally:
        await client.aclose()
