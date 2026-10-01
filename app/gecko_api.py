from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from gecko_iot_client import GeckoApiClient

from .config import settings
from .oauth_flow import OAuthAuthenticationError, OAuthFlow

logger = logging.getLogger(__name__)


class SidecarGeckoApi(GeckoApiClient):
    """Gecko API client backed by OAuth2 PKCE tokens."""

    def __init__(self, websession: aiohttp.ClientSession, oauth_flow: OAuthFlow):
        super().__init__(websession, api_url=settings.api_url, auth0_url=settings.auth0_url)
        self.oauth_flow = oauth_flow

    async def async_get_access_token(self) -> str:
        return await self.oauth_flow.get_valid_access_token()

    async def async_get_user_id(self) -> str:
        token = await self.async_get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.auth0_url}/userinfo"
        async with self.websession.get(url, headers=headers) as response:
            if response.status in (401, 403):
                raise OAuthAuthenticationError(
                    f"Auth0 userinfo rejected credentials ({response.status})", response.status
                )
            response.raise_for_status()
            payload = await response.json()
        if "sub" not in payload:
            raise ValueError(f"User ID ('sub') not found in Auth0 response: {payload}")
        return payload["sub"]

    async def async_discover_account(self) -> tuple[str, dict[str, Any]]:
        user_id = await self.async_get_user_id()
        user_data = await self.async_request("GET", f"/v2/user/{user_id}")
        account_data = user_data.get("account", {})
        account_id = str(account_data.get("accountId", ""))
        if not account_id:
            raise ValueError(f"No account ID found in user data: {user_data}")
        logger.info("Discovered account_id=%s (%s)", account_id, account_data.get("name", ""))
        return account_id, account_data

    async def async_get_vessels(self, account_id: str) -> list[dict[str, Any]]:
        data = await self.async_request("GET", f"/v4/accounts/{account_id}/vessels")
        if isinstance(data, dict):
            for key in ("vessels", "data", "results"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return []
        return data if isinstance(data, list) else []

    async def async_request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return await super().async_request(method, path, **kwargs)
        except aiohttp.ClientResponseError as exc:
            if exc.status in (401, 403):
                raise OAuthAuthenticationError(
                    f"Gecko API rejected credentials ({exc.status})", exc.status
                ) from exc
            raise

    async def async_get_broker_url(self, monitor_id: str) -> str:
        livestream_data = await self.async_get_monitor_livestream(monitor_id)
        broker_url = livestream_data.get("brokerUrl") if isinstance(livestream_data, dict) else None
        if not broker_url:
            raise RuntimeError(
                f"No brokerUrl in livestream response. Keys: "
                f"{list(livestream_data.keys()) if isinstance(livestream_data, dict) else type(livestream_data)}"
            )
        logger.info("Broker URL obtained for monitor %s", monitor_id)
        return broker_url

    def make_sync_refresh_callback(self, loop: asyncio.AbstractEventLoop, on_auth_failure=None):
        def refresh_callback(monitor_id: str) -> str | None:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.async_get_broker_url(monitor_id), loop
                )
                new_url = future.result(timeout=30)
                logger.info("Token refreshed for monitor %s", monitor_id)
                return new_url
            except OAuthAuthenticationError as e:
                logger.error("Token refresh rejected for monitor %s: %s", monitor_id, e)
                if on_auth_failure is not None:
                    asyncio.run_coroutine_threadsafe(on_auth_failure(e), loop)
                return None
            except Exception as e:
                logger.error("Token refresh failed for monitor %s: %s", monitor_id, e)
                return None

        return refresh_callback
