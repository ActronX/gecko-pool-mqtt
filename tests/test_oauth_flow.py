"""Requirements R-OA: OAuth2 PKCE flow (TC-OA-01 through TC-OA-16)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import settings
from app.oauth_flow import (
    OAuthAuthenticationError,
    OAuthFlow,
    _b64url_encode,
    generate_pkce,
)

from .fakes import FakeResponse, FakeWebSession


@pytest.fixture
def session() -> FakeWebSession:
    return FakeWebSession()


@pytest.fixture
def flow(session: FakeWebSession) -> OAuthFlow:
    return OAuthFlow(session)


def prepare(flow: OAuthFlow) -> None:
    """Creates the verifier and state as build_authorize_url does."""
    flow.build_authorize_url()


# --------------------------------------------------------------------------
# R-OA-01 PKCE
# --------------------------------------------------------------------------


def test_tc_oa_01_b64url_has_no_padding() -> None:
    """R-OA-01: base64url has no padding."""
    encoded = _b64url_encode(b"\x00\xff\xfe")

    assert "=" not in encoded
    assert base64.urlsafe_b64decode(encoded + "==") == b"\x00\xff\xfe"


def test_tc_oa_01_challenge_is_s256_of_verifier() -> None:
    """R-OA-01: The challenge is the verifier's S256 hash."""
    verifier, challenge = generate_pkce()
    expected = _b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())

    assert challenge == expected
    assert verifier != challenge


def test_tc_oa_01_verifier_is_random() -> None:
    """R-OA-01: Every verifier is new and long enough."""
    first, _ = generate_pkce()
    second, _ = generate_pkce()

    assert first != second
    assert len(first) >= 43


# --------------------------------------------------------------------------
# R-OA-02 Authorize-URL
# --------------------------------------------------------------------------


def test_tc_oa_02_authorize_url_contains_all_parameters(flow: OAuthFlow) -> None:
    """R-OA-02: The URL contains all PKCE and OAuth parameters."""
    url, state = flow.build_authorize_url()
    query = parse_qs(urlsplit(url).query)

    assert url.startswith(f"{settings.auth0_url}/authorize?")
    assert query["client_id"] == [settings.oauth_client_id]
    assert query["redirect_uri"] == [settings.oauth_redirect_uri]
    assert query["response_type"] == ["code"]
    assert query["scope"] == [settings.oauth_scope]
    assert query["audience"] == [settings.oauth_audience]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [state]
    assert query["code_challenge"][0]


def test_tc_oa_02_verifier_is_stored(flow: OAuthFlow) -> None:
    """R-OA-02: The verifier and state are stored for the exchange."""
    _url, state = flow.build_authorize_url()

    assert flow.oauth_state.state == state
    assert flow.oauth_state.code_verifier


# --------------------------------------------------------------------------
# R-OA-03, R-OA-04 Vorbedingungen
# --------------------------------------------------------------------------


async def test_tc_oa_03_exchange_without_verifier_fails(flow: OAuthFlow) -> None:
    """R-OA-03: No exchange is possible without a prior build_authorize_url call."""
    with pytest.raises(RuntimeError, match="No PKCE verifier found"):
        await flow.exchange_code("code-1")


async def test_tc_oa_04_state_mismatch_is_rejected(flow: OAuthFlow) -> None:
    """R-OA-04: A different state is rejected."""
    prepare(flow)

    with pytest.raises(RuntimeError, match="State mismatch"):
        await flow.exchange_code("code-1", state="wrong")


async def test_tc_oa_04_matching_state_is_accepted(flow: OAuthFlow, session: FakeWebSession) -> None:
    """R-OA-04: The matching state is accepted."""
    _url, state = flow.build_authorize_url()
    session.queue_post(FakeResponse(200, {"access_token": "at", "refresh_token": "rt"}))

    await flow.exchange_code("code-1", state=state)

    assert flow._access_token == "at"


# --------------------------------------------------------------------------
# R-OA-05, R-OA-06 Fehlerantworten
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_tc_oa_05_unauthorized_exchange_raises(flow: OAuthFlow, session: FakeWebSession, status: int) -> None:
    """R-OA-05: 401 and 403 during the exchange produce OAuthAuthenticationError."""
    prepare(flow)
    session.queue_post(FakeResponse(status, None, text='{"error":"access_denied"}'))

    with pytest.raises(OAuthAuthenticationError) as error:
        await flow.exchange_code("code-1")

    assert error.value.status == status


async def test_tc_oa_05_other_status_raises_runtime_error(flow: OAuthFlow, session: FakeWebSession) -> None:
    """R-OA-05: Other error statuses produce a plain RuntimeError."""
    prepare(flow)
    session.queue_post(FakeResponse(500, None, text="kaputt"))

    with pytest.raises(RuntimeError) as error:
        await flow.exchange_code("code-1")

    assert not isinstance(error.value, OAuthAuthenticationError)
    assert "Token exchange failed (500)" in str(error.value)


async def test_tc_oa_06_missing_access_token_raises(flow: OAuthFlow, session: FakeWebSession) -> None:
    """R-OA-06: A response without access_token is unusable."""
    prepare(flow)
    session.queue_post(FakeResponse(200, {"token_type": "Bearer"}))

    with pytest.raises(OAuthAuthenticationError, match="missing access_token"):
        await flow.exchange_code("code-1")


# --------------------------------------------------------------------------
# R-OA-07, R-OA-12 Persistenz
# --------------------------------------------------------------------------


async def test_tc_oa_07_exchange_persists_tokens_atomically(flow: OAuthFlow, session: FakeWebSession) -> None:
    """R-OA-07: Tokens are written atomically through a temporary file."""
    prepare(flow)
    session.queue_post(FakeResponse(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 900}))

    await flow.exchange_code("code-1")

    path = settings.oauth_token_file
    assert os.path.isfile(path)
    assert not os.path.exists(f"{path}.tmp")
    stored = json.loads(open(path, encoding="utf-8").read())
    assert stored["access_token"] == "at"
    assert stored["refresh_token"] == "rt"
    assert stored["expires_at"] > 0


async def test_tc_oa_07_exchange_clears_oauth_state(flow: OAuthFlow, session: FakeWebSession) -> None:
    """R-OA-07: The PKCE state is cleared after the exchange."""
    prepare(flow)
    session.queue_post(FakeResponse(200, {"access_token": "at", "refresh_token": "rt"}))

    await flow.exchange_code("code-1")

    assert flow.oauth_state.code_verifier is None
    assert flow.oauth_state.state is None


async def test_tc_oa_12_tokens_survive_restart(session: FakeWebSession) -> None:
    """R-OA-12: A new flow loads the persisted tokens."""
    first = OAuthFlow(session)
    prepare(first)
    session.queue_post(FakeResponse(200, {"access_token": "at", "refresh_token": "rt"}))
    await first.exchange_code("code-1")

    second = OAuthFlow(FakeWebSession())

    assert second.has_tokens() is True
    assert await second.get_valid_access_token() == "at"


def test_tc_oa_12_has_tokens_is_false_without_file() -> None:
    """R-OA-12: No tokens exist without a token file."""
    assert OAuthFlow(FakeWebSession()).has_tokens() is False


async def test_tc_oa_12_clear_tokens_removes_file(flow: OAuthFlow, session: FakeWebSession) -> None:
    """R-OA-12: clear_tokens removes the file and in-memory state."""
    prepare(flow)
    session.queue_post(FakeResponse(200, {"access_token": "at", "refresh_token": "rt"}))
    await flow.exchange_code("code-1")

    flow.clear_tokens()

    assert not os.path.exists(settings.oauth_token_file)
    assert flow.has_tokens() is False
    assert flow._access_token is None


def test_tc_oa_12_token_file_without_refresh_token_is_ignored() -> None:
    """R-OA-12: A file without refresh_token is considered unusable."""
    with open(settings.oauth_token_file, "w", encoding="utf-8") as handle:
        json.dump({"access_token": "at"}, handle)

    assert OAuthFlow(FakeWebSession()).has_tokens() is False


# --------------------------------------------------------------------------
# R-OA-08, R-OA-09, R-OA-10 Tokenverwendung
# --------------------------------------------------------------------------


async def test_tc_oa_08_valid_token_is_returned_without_refresh(flow: OAuthFlow, session: FakeWebSession) -> None:
    """R-OA-08: A still valid token triggers no refresh."""
    flow._access_token = "at"
    flow._refresh_token = "rt"
    flow._expires_at = 10_000_000_000.0
    flow._loaded = True

    assert await flow.get_valid_access_token() == "at"
    assert session.posts == []


async def test_tc_oa_08_token_near_expiry_is_refreshed(flow: OAuthFlow, session: FakeWebSession) -> None:
    """R-OA-08: The 60-second safety margin triggers a refresh."""
    flow._access_token = "at"
    flow._refresh_token = "rt"
    # get_valid_access_token() compares against time.time(), not the loop time.
    flow._expires_at = time.time() + 30
    flow._loaded = True
    session.queue_post(FakeResponse(200, {"access_token": "at2", "refresh_token": "rt2"}))

    assert await flow.get_valid_access_token() == "at2"
    assert session.posts[0]["json"]["grant_type"] == "refresh_token"


async def test_tc_oa_10_missing_refresh_token_raises(flow: OAuthFlow) -> None:
    """R-OA-10: Without a refresh token no refresh is possible."""
    flow._loaded = True

    with pytest.raises(RuntimeError, match="No tokens available"):
        await flow.get_valid_access_token()


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ('{"error":"invalid_grant"}', 400),
        ('{"error":"access_denied"}', 403),
        ("garbage", 401),
    ],
)
async def test_tc_oa_09_rejected_refresh_raises(flow: OAuthFlow, session: FakeWebSession, body: str, status: int) -> None:
    """R-OA-09: Abgelehnte Refresh-Tokens ergeben OAuthAuthenticationError."""
    flow._refresh_token = "rt"
    flow._loaded = True
    session.queue_post(FakeResponse(status, None, text=body))

    with pytest.raises(OAuthAuthenticationError) as error:
        await flow.get_valid_access_token()

    assert error.value.status == status


async def test_tc_oa_09_server_error_refresh_raises_runtime_error(flow: OAuthFlow, session: FakeWebSession) -> None:
    """R-OA-09: A server error during refresh stays a plain RuntimeError."""
    flow._refresh_token = "rt"
    flow._loaded = True
    session.queue_post(FakeResponse(503, None, text="busy"))

    with pytest.raises(RuntimeError) as error:
        await flow.get_valid_access_token()

    assert not isinstance(error.value, OAuthAuthenticationError)


async def test_tc_oa_09_refresh_keeps_existing_refresh_token(flow: OAuthFlow, session: FakeWebSession) -> None:
    """R-OA-09: If refresh_token is missing from the answer, the old one is kept."""
    flow._refresh_token = "rt-alt"
    flow._loaded = True
    session.queue_post(FakeResponse(200, {"access_token": "at2"}))

    await flow.get_valid_access_token()

    assert flow._refresh_token == "rt-alt"


# --------------------------------------------------------------------------
# R-OA-11, R-OA-13 Hilfsfunktionen und Nebenlaeufigkeit
# --------------------------------------------------------------------------


def test_tc_oa_11_safe_error_text_prefers_description() -> None:
    """R-OA-11: error_description takes precedence over error."""
    assert OAuthFlow._safe_error_text('{"error_description":"zu lang","error":"invalid_grant"}') == "zu lang"


def test_tc_oa_11_safe_error_text_falls_back_to_error() -> None:
    """R-OA-11: Without a description error is used."""
    assert OAuthFlow._safe_error_text('{"error":"invalid_grant"}') == "invalid_grant"


def test_tc_oa_11_safe_error_text_is_truncated() -> None:
    """R-OA-11: The output is truncated to 240 characters."""
    text = OAuthFlow._safe_error_text(json.dumps({"error_description": "x" * 500}))

    assert len(text) == 240


@pytest.mark.parametrize("body", ["", "no json", "[]", "{}", '{"other":"x"}'])
def test_tc_oa_11_safe_error_text_hides_unknown_content(body: str) -> None:
    """R-OA-11: Unknown content is not emitted unfiltered."""
    assert OAuthFlow._safe_error_text(body) == "provider rejected request"


@pytest.mark.parametrize("code", ["invalid_grant", "invalid_refresh_token", "invalid_token"])
def test_tc_oa_11_invalid_grant_is_detected(code: str) -> None:
    """R-OA-11: Known error codes are recognised."""
    assert OAuthFlow._is_invalid_grant(json.dumps({"error": code})) is True


@pytest.mark.parametrize("body", ['{"error":"other"}', "not json", "[]"])
def test_tc_oa_11_other_codes_are_not_invalid_grant(body: str) -> None:
    """R-OA-11: Other codes do not count as an invalid refresh."""
    assert OAuthFlow._is_invalid_grant(body) is False


async def test_tc_oa_13_concurrent_access_is_serialised(flow: OAuthFlow, session: FakeWebSession) -> None:
    """R-OA-13: Concurrent token requests trigger exactly one refresh."""
    flow._refresh_token = "rt"
    flow._loaded = True
    # Exactly one answer: any further refresh would find an empty queue.
    session.queue_post(FakeResponse(200, {"access_token": "at", "refresh_token": "rt2", "expires_in": 3600}))

    results = await asyncio.gather(*(flow.get_valid_access_token() for _ in range(5)))

    assert results == ["at"] * 5
    assert len(session.posts) == 1
