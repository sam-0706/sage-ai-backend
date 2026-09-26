"""Clerk adapter: session-token verification (JWKS) and Backend API user lookup."""
import base64
import time
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.core.config import get_settings
from app.core.errors import ProviderError, Unauthorized
from app.integrations.http import request_json

CLERK_API = "https://api.clerk.com/v1"


def _frontend_api_from_publishable_key(pk: str) -> str:
    # pk_test_<base64("<frontend-api-host>$")>
    encoded = pk.split("_", 2)[2]
    encoded += "=" * (-len(encoded) % 4)
    return base64.b64decode(encoded).decode().rstrip("$")


@lru_cache
def _issuer_and_jwks() -> tuple[str, str]:
    s = get_settings()
    if s.clerk_issuer and s.clerk_jwks_url:
        return s.clerk_issuer, s.clerk_jwks_url
    if not s.clerk_publishable_key:
        raise RuntimeError("Set CLERK_PUBLISHABLE_KEY or CLERK_ISSUER + CLERK_JWKS_URL")
    host = _frontend_api_from_publishable_key(s.clerk_publishable_key)
    issuer = s.clerk_issuer or f"https://{host}"
    return issuer, s.clerk_jwks_url or f"{issuer}/.well-known/jwks.json"


@lru_cache
def _jwks_client() -> PyJWKClient:
    _, url = _issuer_and_jwks()
    return PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=10)


def verify_session_token(token: str) -> dict:
    """Verify a Clerk session JWT. Returns claims; raises Unauthorized on any failure."""
    issuer, _ = _issuer_and_jwks()
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"require": ["exp", "iat", "sub"], "verify_aud": False},
            leeway=10,
        )
    except jwt.ExpiredSignatureError as e:
        raise Unauthorized("Session token expired", code="token_expired") from e
    except (jwt.PyJWTError, jwt.PyJWKClientError) as e:
        raise Unauthorized("Invalid session token", code="invalid_token") from e

    parties = get_settings().clerk_authorized_parties
    azp = claims.get("azp")
    if parties and azp and azp not in parties:
        raise Unauthorized("Token issued for an unauthorized party", code="invalid_token")
    if claims.get("nbf") and claims["nbf"] > time.time() + 10:
        raise Unauthorized("Token not yet valid", code="invalid_token")
    return claims


async def get_clerk_user(clerk_user_id: str) -> dict:
    s = get_settings()
    data = await request_json(
        "GET", f"{CLERK_API}/users/{clerk_user_id}",
        headers={"Authorization": f"Bearer {s.clerk_secret_key}"}, provider="clerk",
    )
    if not isinstance(data, dict) or "id" not in data:
        raise ProviderError("Could not load identity from the authentication provider")
    return data


def verified_emails(clerk_user: dict) -> list[str]:
    return [
        e["email_address"].lower()
        for e in clerk_user.get("email_addresses", [])
        if (e.get("verification") or {}).get("status") == "verified"
    ]
