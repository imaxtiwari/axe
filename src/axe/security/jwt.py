"""Verify Clerk session tokens using explicitly configured public signing keys."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from jose import jwt
from jose.exceptions import JOSEError

from axe.config import Settings
from axe.exceptions import AuthError


@dataclass(frozen=True)
class VerifiedEmail:
    email: str
    subject: str
    session_id: str


class JWTVerifier:
    """RS256 only; no token-directed key discovery or stale-key fallback."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._keys: dict[str, dict[str, Any]] = {}
        self._expires = 0.0

    def _check_configuration(self) -> None:
        settings = self.settings
        issuer = urlsplit(settings.clerk_issuer or "")
        if issuer.scheme != "https" or not issuer.hostname or issuer.username or issuer.password:
            raise AuthError("Signing issuer is not configured")
        if issuer.query or issuer.fragment:
            raise AuthError("Invalid signing issuer")
        if not settings.clerk_authorized_parties:
            raise AuthError("No authorized origins configured")
        for origin in settings.clerk_authorized_parties:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                raise AuthError("Invalid authorized origin")
        if settings.clerk_jwks_json is not None:
            if not settings.is_testing:
                raise AuthError("Synthetic keys are test-only")
        else:
            source = urlsplit(settings.clerk_jwks_url or "")
            if (
                source.scheme != "https"
                or not source.hostname
                or source.username
                or source.password
                or source.fragment
            ):
                raise AuthError("Public signing key source is not configured")

    @staticmethod
    def _index_keys(document: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise AuthError("Invalid signing keys")
        keys = {}
        for key in document["keys"]:
            if not isinstance(key, dict):
                raise AuthError("Invalid signing key")
            kid = key.get("kid")
            if (
                not isinstance(kid, str)
                or not kid
                or kid in keys
                or key.get("kty") != "RSA"
                or key.get("alg", "RS256") != "RS256"
                or key.get("use", "sig") != "sig"
                or key.get("key_ops", ["verify"]) != ["verify"]
                or any(name in key for name in ("d", "p", "q", "dp", "dq", "qi", "k"))
                or not isinstance(key.get("n"), str)
                or not isinstance(key.get("e"), str)
            ):
                raise AuthError("Invalid public signing key")
            keys[kid] = key
        if not keys:
            raise AuthError("No public signing keys")
        return keys

    async def _key(self, kid: str) -> dict[str, Any]:
        if self.settings.clerk_jwks_json is not None:
            keys = self._index_keys(json.loads(self.settings.clerk_jwks_json))
        else:
            if time.monotonic() >= self._expires:
                # No environment proxies, redirects, or token-supplied URLs.
                async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                    response = await client.get(self.settings.clerk_jwks_url or "")
                    response.raise_for_status()
                    keys = self._index_keys(response.json())
                self._keys = keys
                self._expires = time.monotonic() + self.settings.clerk_jwks_ttl_seconds
            keys = self._keys
        if kid not in keys:
            # Unknown IDs fail until the bounded TTL refresh; no attacker-driven fetch loop.
            raise AuthError("Unknown signing key")
        return keys[kid]

    async def verify(self, token: str) -> VerifiedEmail:
        """Verify signature before consuming identity; reject malformed claims strictly."""
        try:
            self._check_configuration()
            if not token or len(token) > 16384:
                raise AuthError("Invalid token")
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (
                header.get("alg") != "RS256"
                or not isinstance(kid, str)
                or not kid
                or any(name in header for name in ("crit", "jku", "jwk", "x5u", "x5c"))
            ):
                raise AuthError("Invalid signing header")
            key = await self._key(kid)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.settings.clerk_issuer,
                audience=self.settings.clerk_audience,
                options={
                    "require_exp": True,
                    "require_iat": True,
                    "require_nbf": True,
                    "require_sub": True,
                    "require_iss": True,
                    "require_aud": self.settings.clerk_audience is not None,
                    "verify_aud": self.settings.clerk_audience is not None,
                    "verify_at_hash": False,
                },
            )
            now = time.time()
            if any(type(claims.get(name)) is not int for name in ("exp", "iat", "nbf")):
                raise AuthError("Invalid time claims")
            if not (0 <= claims["iat"] <= now < claims["exp"] and 0 <= claims["nbf"] <= now):
                raise AuthError("Invalid token time window")
            if claims["nbf"] >= claims["exp"]:
                raise AuthError("Invalid token time window")
            for name, prefix in (("sub", "user_"), ("sid", "sess_")):
                value = claims.get(name)
                if not isinstance(value, str) or not value.startswith(prefix) or len(value) <= len(prefix):
                    raise AuthError("Invalid session identity")
            azp = claims.get("azp")
            if not isinstance(azp, str) or azp not in self.settings.clerk_authorized_parties:
                raise AuthError("Unauthorized origin")
            email = claims.get("email")
            if (
                not isinstance(email, str)
                or not 3 <= len(email) <= 255
                or email.count("@") != 1
                or any(character.isspace() for character in email)
                or not all(email.split("@"))
                or claims.get("email_verified") is not True
            ):
                raise AuthError("Verified email is required")
            return VerifiedEmail(email=email, subject=claims["sub"], session_id=claims["sid"])
        except AuthError:
            raise
        except (JOSEError, ValueError, TypeError, KeyError, httpx.HTTPError) as exc:
            raise AuthError("Token verification unavailable or invalid") from exc
