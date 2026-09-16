"""Synthetic signed-token tests; no provider, live data, or authentication bypass."""

import base64
import json
import time
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, Request
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from axe.config import Settings
from axe.db.models import FundEntity, PMUser
from axe.exceptions import AuthError
from axe.main import create_app
from axe.security.authz import require_role
from axe.security.context import RequestContext, get_request_context, require_identity
from axe.security.jwt import JWTVerifier


@pytest.fixture(scope="module")
def signing_key() -> tuple[str, dict[str, Any]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()

    def encode(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).decode().rstrip("=")

    public = {"kty": "RSA", "kid": "synthetic", "alg": "RS256", "use": "sig",
              "n": encode(numbers.n), "e": encode(numbers.e)}
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return pem, public


@pytest.fixture
def auth_settings(signing_key: tuple[str, dict[str, Any]]) -> Settings:
    return Settings(app_env="test", clerk_issuer="https://issuer.example.invalid",
                    clerk_authorized_parties=["https://signin.example.invalid"],
                    clerk_jwks_json=json.dumps({"keys": [signing_key[1]]}))


def token(signing_key: tuple[str, dict[str, Any]], **overrides: Any) -> str:
    now = int(time.time())
    claims = {"iss": "https://issuer.example.invalid", "sub": "user_synthetic",
              "sid": "sess_synthetic", "iat": now - 1, "nbf": now - 1, "exp": now + 60,
              "azp": "https://signin.example.invalid", "email": "member@example.invalid",
              "email_verified": True}
    claims.update(overrides)
    return jwt.encode(claims, signing_key[0], algorithm="RS256", headers={"kid": "synthetic"})


async def test_signed_email(auth_settings: Settings, signing_key: tuple[str, dict[str, Any]]) -> None:
    identity = await JWTVerifier(auth_settings).verify(token(signing_key))
    assert identity.email == "member@example.invalid"
    assert identity.subject == "user_synthetic"
    assert identity.session_id == "sess_synthetic"


@pytest.mark.parametrize("overrides", [
    {"email_verified": False}, {"email_verified": "true"}, {"email_verified": 1},
    {"email_verified": None}, {"email": " member@example.invalid"}, {"email": "a@@b"},
    {"email": "@b"}, {"email": None}, {"azp": None}, {"azp": "https://attacker.invalid"},
    {"iss": "https://attacker.invalid"}, {"sub": "user_"}, {"sub": None},
    {"sid": "sess_"}, {"sid": None}, {"exp": 0}, {"exp": True},
    {"iat": 9999999999}, {"iat": "1"}, {"nbf": 9999999999}, {"nbf": None},
])
async def test_invalid_claims(auth_settings: Settings, signing_key: tuple[str, dict[str, Any]],
                              overrides: dict[str, Any]) -> None:
    with pytest.raises(AuthError):
        await JWTVerifier(auth_settings).verify(token(signing_key, **overrides))


@pytest.mark.parametrize("environment", ["test", "development", "production"])
async def test_missing_origin_blocks_every_environment(
    auth_settings: Settings, signing_key: tuple[str, dict[str, Any]], environment: str,
) -> None:
    settings = auth_settings.model_copy(update={"app_env": environment, "clerk_authorized_parties": []})
    with pytest.raises(AuthError):
        await JWTVerifier(settings).verify(token(signing_key))


async def test_audience_optional_but_enforced_when_configured(
    auth_settings: Settings, signing_key: tuple[str, dict[str, Any]],
) -> None:
    await JWTVerifier(auth_settings).verify(token(signing_key))
    auth_settings.clerk_audience = "axe"
    for overrides in ({}, {"aud": "foreign"}, {"aud": 1}):
        with pytest.raises(AuthError):
            await JWTVerifier(auth_settings).verify(token(signing_key, **overrides))
    await JWTVerifier(auth_settings).verify(token(signing_key, aud="axe"))


async def test_internal_binding_cannot_authenticate() -> None:
    request = Request({"type": "http", "headers": []})
    with RequestContext.bind(pm_id="forged", fund_id="forged", role="admin"):
        with pytest.raises(AuthError):
            await get_request_context(request)
        with pytest.raises(AuthError):
            require_identity(request)


async def test_http_membership_is_authoritative_and_fresh(
    auth_settings: Settings, signing_key: tuple[str, dict[str, Any]],
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fund = FundEntity(legal_name="Synthetic fund")
    db_session.add(fund)
    await db_session.flush()
    member = PMUser(fund_entity_id=fund.id, email="member@example.invalid", role="pm")
    db_session.add(member)
    await db_session.flush()
    app = create_app(auth_settings)
    app.state.identity_session_factory = db_session_factory
    calls = []

    @app.get("/__identity")
    async def identity(ctx: RequestContext = Depends(require_role("pm")),
                       same: RequestContext = Depends(get_request_context)) -> dict[str, Any]:
        assert ctx is same is RequestContext.current()
        calls.append(ctx.pm_id)
        return {"pm_id": ctx.pm_id, "fund_id": ctx.fund_id, "role": ctx.role}

    headers = {"Authorization": f"Bearer {token(signing_key, role='admin', pm_id='forged')}",
               "X-PM-ID": "forged", "X-Fund-ID": "forged", "X-Role": "admin"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/__identity", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"pm_id": member.id, "fund_id": fund.id, "role": "pm"}
        member.active = False
        await db_session.flush()
        response = await client.get("/__identity", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert len(calls) == 1
    assert RequestContext.current_or_none() is None


@pytest.mark.parametrize("path", [
    "/onboarding/start", "/api/v1/persona", "/api/v1/transcripts", "/api/v1/mnpi",
    "/api/v1/audit/log", "/api/v1/compliance", "/api/v1/deals", "/api/v1/lp",
    "/api/v1/artifacts", "/api/v1/connectors", "/docs", "/openapi.json", "/metrics",
    "/future-business-route",
])
async def test_every_business_path_denies_raw_headers(auth_settings: Settings, path: str) -> None:
    app = create_app(auth_settings)
    with RequestContext.bind(pm_id="forged", fund_id="forged", role="admin"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(path, headers={"X-PM-ID": "forged", "X-Role": "admin"})
            assert response.status_code == 401
            assert response.json()["code"] == "auth.failed"
        assert RequestContext.current().pm_id == "forged"
