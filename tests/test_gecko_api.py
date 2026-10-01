"""Requirements R-API: Gecko REST API (TC-API-01 through TC-API-12).

``SidecarGeckoApi`` extends the library's ``GeckoApiClient``. The tests replace
the base-class methods so they can run without a network or real library
logic. The library itself is not tested.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest
from gecko_iot_client import GeckoApiClient

from app.gecko_api import SidecarGeckoApi
from app.oauth_flow import OAuthAuthenticationError

from .fakes import FakeOAuthFlow, FakeResponse, FakeWebSession


class _RequestInfo:
    """Minimal replacement for aiohttp.RequestInfo."""

    url = "https://api.test/v2/user/user-1"


def client_error(status: int, message: str = "error") -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(
        _RequestInfo(), (), status=status, message=message
    )


@pytest.fixture
def api_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patches the base class and records calls."""
    recorded: dict[str, Any] = {"requests": [], "responses": {}, "livestream": {}, "livestream_calls": []}

    def fake_init(self, websession, api_url=None, auth0_url=None, **kwargs) -> None:
        self.websession = websession
        self.api_url = api_url
        self.auth0_url = auth0_url

    async def fake_async_request(self, method, path, **kwargs) -> Any:
        recorded["requests"].append((method, path))
        if path not in recorded["responses"]:
            raise AssertionError(f"Unexpected API call: {method} {path}")
        value = recorded["responses"][path]
        if isinstance(value, BaseException):
            raise value
        return value

    async def fake_livestream(self, monitor_id) -> Any:
        recorded["livestream_calls"].append(monitor_id)
        value = recorded["livestream"]
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(GeckoApiClient, "__init__", fake_init)
    monkeypatch.setattr(GeckoApiClient, "async_request", fake_async_request)
    monkeypatch.setattr(GeckoApiClient, "async_get_monitor_livestream", fake_livestream)
    return recorded


@pytest.fixture
def session() -> FakeWebSession:
    return FakeWebSession()


@pytest.fixture
def api(api_env: dict[str, Any], session: FakeWebSession) -> SidecarGeckoApi:
    return SidecarGeckoApi(session, FakeOAuthFlow())


# --------------------------------------------------------------------------
# R-API-01 Benutzerkennung
# --------------------------------------------------------------------------


async def test_tc_api_01_user_id_is_read_from_userinfo(api: SidecarGeckoApi, session: FakeWebSession) -> None:
    """R-API-01: The user ID comes from Auth0 userinfo."""
    session.queue_get(FakeResponse(200, {"sub": "user-1", "email": "a@b.c"}))

    assert await api.async_get_user_id() == "user-1"
    assert session.gets[0]["headers"] == {"Authorization": "Bearer token"}


@pytest.mark.parametrize("status", [401, 403])
async def test_tc_api_01_rejected_userinfo_raises_auth_error(
    api: SidecarGeckoApi, session: FakeWebSession, status: int
) -> None:
    """R-API-01: 401 and 403 from userinfo produce OAuthAuthenticationError."""
    session.queue_get(FakeResponse(status, {}))

    with pytest.raises(OAuthAuthenticationError) as error:
        await api.async_get_user_id()

    assert error.value.status == status


async def test_tc_api_01_missing_sub_raises_value_error(api: SidecarGeckoApi, session: FakeWebSession) -> None:
    """R-API-01: The response is unusable without sub."""
    session.queue_get(FakeResponse(200, {"email": "a@b.c"}))

    with pytest.raises(ValueError, match="User ID"):
        await api.async_get_user_id()


# --------------------------------------------------------------------------
# R-API-02 Fehleruebersetzung und Account-Discovery
# --------------------------------------------------------------------------


async def test_tc_api_02_account_discovery_returns_id_and_name(
    api: SidecarGeckoApi, api_env: dict[str, Any], session: FakeWebSession
) -> None:
    """R-API-02: Account discovery returns the ID and name."""
    session.queue_get(FakeResponse(200, {"sub": "user-1"}))
    api_env["responses"]["/v2/user/user-1"] = {"account": {"accountId": "acc-1", "name": "Mein Pool"}}

    account_id, account = await api.async_discover_account()

    assert account_id == "acc-1"
    assert account["name"] == "Mein Pool"


async def test_tc_api_02_missing_account_id_raises(
    api: SidecarGeckoApi, api_env: dict[str, Any], session: FakeWebSession
) -> None:
    """R-API-02: Account discovery is unusable without accountId."""
    session.queue_get(FakeResponse(200, {"sub": "user-1"}))
    api_env["responses"]["/v2/user/user-1"] = {"account": {}}

    with pytest.raises(ValueError, match="No account ID"):
        await api.async_discover_account()


@pytest.mark.parametrize("status", [401, 403])
async def test_tc_api_02_credential_errors_become_auth_errors(
    api: SidecarGeckoApi, api_env: dict[str, Any], session: FakeWebSession, status: int
) -> None:
    """R-API-02: 401 and 403 from the Gecko API are translated."""
    session.queue_get(FakeResponse(200, {"sub": "user-1"}))
    api_env["responses"]["/v2/user/user-1"] = client_error(status)

    with pytest.raises(OAuthAuthenticationError) as error:
        await api.async_discover_account()

    assert error.value.status == status


async def test_tc_api_02_other_errors_pass_through(
    api: SidecarGeckoApi, api_env: dict[str, Any], session: FakeWebSession
) -> None:
    """R-API-02: Other HTTP errors pass through unchanged."""
    session.queue_get(FakeResponse(200, {"sub": "user-1"}))
    api_env["responses"]["/v2/user/user-1"] = client_error(500)

    with pytest.raises(aiohttp.ClientResponseError) as error:
        await api.async_discover_account()

    assert not isinstance(error.value, OAuthAuthenticationError)
    assert error.value.status == 500


# --------------------------------------------------------------------------
# R-API-03 Vessel-Discovery
# --------------------------------------------------------------------------


async def test_tc_api_03_vessels_from_list(api: SidecarGeckoApi, api_env: dict[str, Any]) -> None:
    """R-API-03: A simple list is passed through directly."""
    api_env["responses"]["/v4/accounts/acc-1/vessels"] = [{"vesselId": "1"}]

    assert await api.async_get_vessels("acc-1") == [{"vesselId": "1"}]


@pytest.mark.parametrize("key", ["vessels", "data", "results"])
async def test_tc_api_03_vessels_from_dict(api: SidecarGeckoApi, api_env: dict[str, Any], key: str) -> None:
    """R-API-03: A dict with vessels, data, or results is unpacked."""
    api_env["responses"]["/v4/accounts/acc-1/vessels"] = {key: [{"vesselId": "1"}], "total": 1}

    assert await api.async_get_vessels("acc-1") == [{"vesselId": "1"}]


@pytest.mark.parametrize("payload", [{"unexpected": []}, "text", 42])
async def test_tc_api_03_unexpected_shape_yields_empty_list(
    api: SidecarGeckoApi, api_env: dict[str, Any], payload: Any
) -> None:
    """R-API-03: Unexpected shapes produce an empty list."""
    api_env["responses"]["/v4/accounts/acc-1/vessels"] = payload

    assert await api.async_get_vessels("acc-1") == []


# --------------------------------------------------------------------------
# R-API-04 Broker-URL
# --------------------------------------------------------------------------


async def test_tc_api_04_broker_url_is_extracted(
    api: SidecarGeckoApi, api_env: dict[str, Any]
) -> None:
    """R-API-04: The broker URL comes from the livestream response."""
    api_env["livestream"] = {"brokerUrl": "wss://broker.test/mqtt", "other": 1}

    assert await api.async_get_broker_url("mon-1") == "wss://broker.test/mqtt"
    assert api_env["livestream_calls"] == ["mon-1"]


async def test_tc_api_04_missing_broker_url_lists_keys(
    api: SidecarGeckoApi, api_env: dict[str, Any]
) -> None:
    """R-API-04: If brokerUrl is missing, the error names the available keys."""
    api_env["livestream"] = {"someKey": 1, "otherKey": 2}

    with pytest.raises(RuntimeError, match="someKey"):
        await api.async_get_broker_url("mon-1")


async def test_tc_api_04_non_dict_livestream_raises(
    api: SidecarGeckoApi, api_env: dict[str, Any]
) -> None:
    """R-API-04: An unexpected response shape is detected."""
    api_env["livestream"] = ["nope"]

    with pytest.raises(RuntimeError, match="No brokerUrl"):
        await api.async_get_broker_url("mon-1")


# --------------------------------------------------------------------------
# R-API-05 Refresh callback from a library thread
# --------------------------------------------------------------------------


async def test_tc_api_05_refresh_callback_returns_new_url(api: SidecarGeckoApi, api_env: dict[str, Any]) -> None:
    """R-API-05: The callback returns the new broker URL from the library thread."""
    api_env["livestream"] = {"brokerUrl": "wss://neu.test/mqtt"}
    callback = api.make_sync_refresh_callback(asyncio.get_running_loop())

    result = await asyncio.to_thread(callback, "mon-1")

    assert result == "wss://neu.test/mqtt"


async def test_tc_api_05_refresh_callback_reports_auth_failure(
    api: SidecarGeckoApi, api_env: dict[str, Any]
) -> None:
    """R-API-05: On authentication errors, the callback reports the error and returns None."""
    api_env["livestream"] = OAuthAuthenticationError("Token abgelehnt", 401)
    failures: list[str] = []
    notified = asyncio.Event()

    async def on_auth_failure(error: Exception) -> None:
        failures.append(str(error))
        notified.set()

    callback = api.make_sync_refresh_callback(asyncio.get_running_loop(), on_auth_failure)

    result = await asyncio.to_thread(callback, "mon-1")
    await asyncio.wait_for(notified.wait(), timeout=2.0)

    assert result is None
    assert failures == ["Token abgelehnt"]


async def test_tc_api_05_refresh_callback_swallows_other_errors(
    api: SidecarGeckoApi, api_env: dict[str, Any]
) -> None:
    """R-API-05: Other errors return None without triggering reauthentication."""
    api_env["livestream"] = RuntimeError("netzwerk weg")
    callback = api.make_sync_refresh_callback(asyncio.get_running_loop())

    assert await asyncio.to_thread(callback, "mon-1") is None
