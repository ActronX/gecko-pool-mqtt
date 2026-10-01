from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .config import settings

logger = logging.getLogger(__name__)


class OAuthAuthenticationError(RuntimeError):
    """An OAuth provider rejected credentials or returned an unusable token."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    code_verifier = _b64url_encode(secrets.token_bytes(32))
    code_challenge = _b64url_encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
    return code_verifier, code_challenge


class OAuthState:
    def __init__(self):
        self.code_verifier: str | None = None
        self.state: str | None = None

    def clear(self) -> None:
        self.code_verifier = None
        self.state = None


class OAuthFlow:
    """PKCE-based OAuth2 flow matching ha-gecko-integration.

    Flow:
    1. build_authorize_url() → returns authorize URL (PKCE verifier + state stored)
    2. User opens URL in browser, logs in, gets redirected with ?code=...
    3. exchange_code(code) → exchanges code for tokens, persists refresh_token
    4. get_valid_access_token() → returns valid token (refreshes if expired)
    """

    def __init__(self, websession: aiohttp.ClientSession):
        self.websession = websession
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._lock: Any | None = None
        self.oauth_state = OAuthState()
        self._loaded = False

    async def _ensure_lock(self) -> None:
        if self._lock is None:
            import asyncio
            self._lock = asyncio.Lock()

    def build_authorize_url(self) -> tuple[str, str]:
        code_verifier, code_challenge = generate_pkce()
        state = _b64url_encode(secrets.token_bytes(16))

        self.oauth_state.code_verifier = code_verifier
        self.oauth_state.state = state

        params = {
            "client_id": settings.oauth_client_id,
            "redirect_uri": settings.oauth_redirect_uri,
            "response_type": "code",
            "scope": settings.oauth_scope,
            "audience": settings.oauth_audience,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        url = f"{settings.auth0_url}/authorize?{urlencode(params)}"
        return url, state

    async def exchange_code(self, code: str, state: str | None = None) -> dict[str, Any]:
        if not self.oauth_state.code_verifier:
            raise RuntimeError("No PKCE verifier found — call /auth/login first")

        if state and self.oauth_state.state and state != self.oauth_state.state:
            raise RuntimeError(f"State mismatch: expected {self.oauth_state.state}, got {state}")

        payload = {
            "grant_type": "authorization_code",
            "client_id": settings.oauth_client_id,
            "code": code,
            "redirect_uri": settings.oauth_redirect_uri,
            "code_verifier": self.oauth_state.code_verifier,
        }

        url = f"{settings.auth0_url}/oauth/token"
        async with self.websession.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                message = f"Token exchange failed ({resp.status}): {self._safe_error_text(body)}"
                if resp.status in (401, 403):
                    raise OAuthAuthenticationError(message, resp.status)
                raise RuntimeError(message)
            data = await resp.json()

        if "access_token" not in data:
            raise OAuthAuthenticationError("Token response missing access_token")

        self._set_tokens(data)
        self._save_tokens(data)
        self.oauth_state.clear()
        logger.info("OAuth code exchanged successfully — tokens persisted")
        return data

    async def get_valid_access_token(self) -> str:
        await self._ensure_lock()
        async with self._lock:
            if not self._loaded:
                self._load_tokens()
                self._loaded = True

            if self._access_token and time.time() < self._expires_at - 60:
                return self._access_token

            if not self._refresh_token:
                raise RuntimeError("No tokens available — complete OAuth login first")

            logger.info("Access token expired, refreshing via refresh_token grant ...")
            data = await self._refresh_token_grant()
            self._set_tokens(data)
            self._save_tokens(data)
            return self._access_token

    async def _refresh_token_grant(self) -> dict[str, Any]:
        payload = {
            "grant_type": "refresh_token",
            "client_id": settings.oauth_client_id,
            "refresh_token": self._refresh_token,
        }
        url = f"{settings.auth0_url}/oauth/token"
        async with self.websession.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                if resp.status in (401, 403) or self._is_invalid_grant(body):
                    raise OAuthAuthenticationError(
                        f"Token refresh rejected ({resp.status}): {self._safe_error_text(body)}",
                        resp.status,
                    )
                raise RuntimeError(
                    f"Token refresh failed ({resp.status}): {self._safe_error_text(body)}"
                )
            data = await resp.json()
            if "access_token" not in data:
                raise OAuthAuthenticationError("Refresh response missing access_token")
            return data

    def _set_tokens(self, data: dict[str, Any]) -> None:
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        expires_in = data.get("expires_in", 3600)
        self._expires_at = time.time() + expires_in
        logger.debug("Tokens set — expires in %ss", expires_in)

    def _save_tokens(self, data: dict[str, Any]) -> None:
        token_data = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", self._refresh_token),
            "expires_at": self._expires_at,
        }
        path = settings.oauth_token_file
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            temp_path = f"{path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(token_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
            logger.info("Tokens saved to %s", path)
        except OSError as e:
            logger.error("Failed to save tokens to %s: %s", path, e)

    @staticmethod
    def _safe_error_text(body: str) -> str:
        """Keep provider diagnostics useful without logging token-like values."""
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                return str(payload.get("error_description") or payload.get("error") or "provider rejected request")[:240]
        except (TypeError, json.JSONDecodeError):
            pass
        return "provider rejected request"

    @staticmethod
    def _is_invalid_grant(body: str) -> bool:
        try:
            payload = json.loads(body)
            return isinstance(payload, dict) and payload.get("error") in {
                "invalid_grant", "invalid_refresh_token", "invalid_token"
            }
        except (TypeError, json.JSONDecodeError):
            return False

    def _load_tokens(self) -> bool:
        path = settings.oauth_token_file
        if not os.path.isfile(path):
            logger.debug("No token file at %s", path)
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            self._expires_at = data.get("expires_at", 0)
            if self._refresh_token:
                logger.info("Tokens loaded from %s", path)
                return True
            logger.warning("Token file exists but no refresh_token found")
            return False
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to load tokens from %s: %s", path, e)
            return False

    def has_tokens(self) -> bool:
        if not self._loaded:
            self._load_tokens()
            self._loaded = True
        return self._refresh_token is not None

    def clear_tokens(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0
        path = settings.oauth_token_file
        try:
            if os.path.isfile(path):
                os.remove(path)
                logger.info("Token file removed: %s", path)
        except OSError as e:
            logger.error("Failed to remove token file: %s", e)
