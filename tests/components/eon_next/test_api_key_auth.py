"""Tests for API-key authentication in the Eon Next API client."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.eon_next.eonnext import (
    EonNext,
    EonNextApiError,
    EonNextAuthError,
    METER_TYPE_ELECTRIC,
    redact_secrets,
)


def _token_response(token: str = "jwt", refresh: str = "refresh") -> dict:
    return {
        "data": {
            "obtainKrakenToken": {
                "token": token,
                "refreshToken": refresh,
                "refreshExpiresIn": 9999999999,
                "payload": {"iat": 1000, "exp": 9999999999},
            }
        }
    }


def _expire_tokens(api: EonNext) -> None:
    api.auth = {
        "issued": 0,
        "token": {"token": None, "expires": None},
        "refresh": {"token": None, "expires": None},
    }


@pytest.mark.asyncio
async def test_login_with_api_key_sends_key_and_stores_tokens() -> None:
    api = EonNext()
    api._graphql_post = AsyncMock(  # type: ignore[method-assign]
        return_value=_token_response("jwt-1", "refresh-1")
    )

    assert await api.login_with_api_key("sk_live_abc", initialise=False) is True

    operation, _query, variables, authenticated = api._graphql_post.await_args.args
    assert operation == "apiKeyAuthentication"
    assert variables == {"input": {"APIKey": "sk_live_abc"}}
    assert authenticated is False
    assert api.api_key == "sk_live_abc"
    assert api.auth["token"]["token"] == "jwt-1"
    assert api.auth["refresh"]["token"] == "refresh-1"


@pytest.mark.asyncio
async def test_login_with_api_key_failure_resets_auth() -> None:
    api = EonNext()
    api._graphql_post = AsyncMock(  # type: ignore[method-assign]
        return_value={"errors": [{"message": "Invalid API key"}]}
    )

    assert await api.login_with_api_key("sk_live_bad", initialise=False) is False
    assert api.auth["token"]["token"] is None
    assert api.auth["refresh"]["token"] is None


@pytest.mark.asyncio
async def test_login_with_api_key_auth_error_returns_false() -> None:
    api = EonNext()
    api._graphql_post = AsyncMock(  # type: ignore[method-assign]
        side_effect=EonNextAuthError("rejected")
    )
    assert await api.login_with_api_key("sk_live_bad", initialise=False) is False


@pytest.mark.asyncio
async def test_login_with_api_key_propagates_api_error_and_preserves_auth() -> None:
    api = EonNext()
    api.auth = {
        "issued": 1000000000,
        "token": {"token": "old-jwt", "expires": 0},
        "refresh": {"token": "saved-refresh", "expires": 9999999999},
    }
    api._graphql_post = AsyncMock(  # type: ignore[method-assign]
        side_effect=EonNextApiError("server unavailable")
    )

    with pytest.raises(EonNextApiError, match="server unavailable"):
        await api.login_with_api_key("sk_live_abc", initialise=False)

    assert api.auth["refresh"]["token"] == "saved-refresh"


@pytest.mark.asyncio
async def test_expired_tokens_relogin_with_api_key_not_password() -> None:
    """With an API key set, token renewal must not fall back to the password."""
    api = EonNext()
    api.username = "a@b.com"
    api.password = "pw"
    api.api_key = "sk_live_abc"
    _expire_tokens(api)

    calls: list[tuple[str, Any]] = []

    async def _fake_post(operation: str, _query: str, variables: Any, *_a: Any) -> dict:
        calls.append((operation, variables))
        return _token_response("fresh-jwt")

    api._graphql_post = AsyncMock(side_effect=_fake_post)  # type: ignore[method-assign]

    token = await api._EonNext__auth_token()  # type: ignore[attr-defined]

    assert token == "fresh-jwt"
    assert [c[0] for c in calls] == ["apiKeyAuthentication"]
    assert calls[0][1] == {"input": {"APIKey": "sk_live_abc"}}


@pytest.mark.asyncio
async def test_force_token_refresh_uses_api_key_when_refresh_token_gone() -> None:
    api = EonNext()
    api.api_key = "sk_live_abc"
    _expire_tokens(api)
    api._graphql_post = AsyncMock(return_value=_token_response("fresh-jwt"))  # type: ignore[method-assign]

    assert await api._force_token_refresh() is True
    assert api._graphql_post.await_args.args[0] == "apiKeyAuthentication"


@pytest.mark.asyncio
async def test_no_api_key_does_not_attempt_any_login(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without a key nothing is sent to E.ON (passwords are no longer accepted)."""
    api = EonNext()
    api.username = "a@b.com"
    api.password = "pw"
    _expire_tokens(api)
    api._graphql_post = AsyncMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="custom_components.eon_next.eonnext"):
        with pytest.raises(EonNextAuthError):
            await api._EonNext__auth_token()  # type: ignore[attr-defined]

    api._graphql_post.assert_not_awaited()
    assert "API key" in caplog.text
    assert "pw" not in caplog.text.replace("password", "")


# --- secret redaction -------------------------------------------------------


def test_redact_secrets_masks_echoed_credentials() -> None:
    leaked = (
        "Variable '$input' got invalid value {'identifier': 'a.person@example.com', "
        "'password': 'Hunter2-xyz'}; Field 'password' is not defined"
    )
    out = redact_secrets(leaked)
    assert "Hunter2-xyz" not in out
    assert "a.person@example.com" not in out
    assert "is not defined" in out  # the useful part survives


def test_redact_secrets_masks_keys_tokens_and_emails() -> None:
    assert "sk_live_AbC123" not in redact_secrets("bad key sk_live_AbC123")
    assert "eyJhbGciOi" not in redact_secrets("token eyJhbGciOi.eyJzdWIi.sig-x")
    assert "x@y.com" not in redact_secrets("contact x@y.com")
    assert redact_secrets({"APIKey": "sk_live_zzz"}).count("sk_live_zzz") == 0


def test_redact_secrets_leaves_plain_messages_alone() -> None:
    msg = "REST consumption endpoint returned status 400: Invalid group_by"
    assert redact_secrets(msg) == msg


@pytest.mark.asyncio
async def test_failed_api_key_login_does_not_log_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = EonNext()
    echoed = "Variable '$input' got invalid value {'APIKey': 'sk_live_TOPSECRET'}"
    api._graphql_post = AsyncMock(  # type: ignore[method-assign]
        return_value={"errors": [{"message": echoed}]}
    )

    with caplog.at_level(logging.DEBUG, logger="custom_components.eon_next.eonnext"):
        assert await api.login_with_api_key("sk_live_TOPSECRET", initialise=False) is False

    assert "sk_live_TOPSECRET" not in caplog.text
    assert "TOPSECRET" not in caplog.text


# --- REST consumption requests ---------------------------------------------


class _BodyResponse:
    def __init__(self, status: int, body: str = "", json_data: Any = None) -> None:
        self.status = status
        self._body = body
        self._json = json_data if json_data is not None else {}

    async def __aenter__(self) -> "_BodyResponse":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def json(self, *_a: Any, **_k: Any) -> Any:
        return self._json

    async def text(self) -> str:
        return self._body


class _ParamSession:
    def __init__(self, response: _BodyResponse) -> None:
        self._response = response
        self.params_seen: list[dict[str, str]] = []

    def get(self, _url: str, params=None, headers=None) -> _BodyResponse:
        self.params_seen.append(dict(params or {}))
        return self._response


def _api_with_valid_auth() -> EonNext:
    api = EonNext()
    now = api._EonNext__current_timestamp()  # type: ignore[attr-defined]
    api.auth = {
        "issued": now,
        "token": {"token": "jwt", "expires": now + 3600},
        "refresh": {"token": "refresh", "expires": now + 7200},
    }
    return api


@pytest.mark.asyncio
async def test_rest_error_includes_redacted_response_body() -> None:
    api = _api_with_valid_auth()
    session = _ParamSession(
        _BodyResponse(
            400,
            body='{"group_by": ["Invalid choice"], "token": "eyJabc.eyJdef.sig"}',
        )
    )
    api._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]

    with pytest.raises(EonNextApiError) as exc:
        await api.async_get_consumption(
            METER_TYPE_ELECTRIC, "sp", "m1", group_by="half_hour", page_size=100
        )

    msg = str(exc.value)
    assert "status 400" in msg
    assert "Invalid choice" in msg
    assert "eyJabc" not in msg


@pytest.mark.asyncio
async def test_group_by_none_is_omitted_from_request() -> None:
    api = _api_with_valid_auth()
    session = _ParamSession(_BodyResponse(200, json_data={"results": []}))
    api._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]

    await api.async_get_consumption(METER_TYPE_ELECTRIC, "sp", "m1", group_by=None, page_size=100)
    await api.async_get_consumption(METER_TYPE_ELECTRIC, "sp", "m1", group_by="day", page_size=7)

    assert session.params_seen[0] == {"page_size": "100"}
    assert session.params_seen[1] == {"group_by": "day", "page_size": "7"}
