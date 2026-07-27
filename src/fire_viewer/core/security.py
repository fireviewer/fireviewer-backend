from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import jwt
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import AdminLocalSession
from fire_viewer.domain.enums import ActorType
from fire_viewer.domain.errors import ForbiddenError, UnauthorizedError

bearer_scheme = HTTPBearer(auto_error=False)
SCRYPT_MAXMEM_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    roles: frozenset[str]
    actor_type: ActorType = ActorType.OPERATOR
    csrf_token: str | None = None

    def has_any_role(self, required: set[str] | frozenset[str]) -> bool:
        return bool(self.roles.intersection(required))

    def has_all_roles(self, required: set[str] | frozenset[str]) -> bool:
        return required.issubset(self.roles)


class JwtVerifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwk_client = (
            PyJWKClient(str(settings.oidc_jwks_url), cache_keys=True)
            if settings.oidc_jwks_url
            else None
        )

    def verify(self, token: str) -> Actor:
        if self._jwk_client is None:
            raise UnauthorizedError("JWT verification is not configured.")
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=self.settings.oidc_algorithms,
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                leeway=self.settings.oidc_leeway_seconds,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("The bearer token is invalid or expired.") from exc

        actor_id = str(claims.get("sub", "")).strip()
        if not actor_id:
            raise UnauthorizedError("The bearer token has no subject.")

        raw_roles = claims.get(self.settings.oidc_roles_claim, [])
        if isinstance(raw_roles, str):
            roles = frozenset(part for part in raw_roles.replace(",", " ").split() if part)
        elif isinstance(raw_roles, list):
            roles = frozenset(str(role) for role in raw_roles)
        else:
            roles = frozenset()
        return Actor(actor_id=actor_id, roles=roles)


def hash_local_password(password: str, *, salt: bytes | None = None) -> str:
    actual_salt = salt or os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=actual_salt,
        n=2**15,
        r=8,
        p=1,
        maxmem=SCRYPT_MAXMEM_BYTES,
        dklen=32,
    )
    return (
        "scrypt$"
        + base64.urlsafe_b64encode(actual_salt).decode()
        + "$"
        + base64.urlsafe_b64encode(digest).decode()
    )


def verify_local_password(password: str, encoded: str) -> bool:
    try:
        scheme, raw_salt, raw_digest = encoded.split("$", 2)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(raw_salt.encode())
        expected = base64.urlsafe_b64decode(raw_digest.encode())
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=2**15,
            r=8,
            p=1,
            maxmem=SCRYPT_MAXMEM_BYTES,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def new_local_session(session: Session, settings: Settings) -> tuple[str, str]:
    token = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    csrf = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    now = utcnow()
    session.add(
        AdminLocalSession(
            session_hash=hashlib.sha256(token.encode()).hexdigest(),
            csrf_token=csrf,
            expires_at=now + timedelta(hours=settings.local_admin_session_hours),
            idle_expires_at=now + timedelta(minutes=settings.local_admin_idle_minutes),
            last_seen_at=now,
        )
    )
    session.commit()
    return token, csrf


def actor_from_request(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    session: Session | None = None,
) -> Actor:
    settings: Settings = request.app.state.settings
    if settings.auth_mode == "disabled":
        if settings.environment not in {"development", "test"}:
            raise UnauthorizedError("Disabled authentication is not allowed in this environment.")
        return Actor(
            actor_id="local-development-operator",
            roles=frozenset({"administrator", "analyst", "validator", "security_operator"}),
        )

    if settings.auth_mode == "local_admin":
        if session is None:
            raise UnauthorizedError()
        token = request.cookies.get("fireviewer_admin")
        if not token:
            raise UnauthorizedError()
        row = session.execute(
            select(AdminLocalSession).where(
                AdminLocalSession.session_hash == hashlib.sha256(token.encode()).hexdigest()
            )
        ).scalar_one_or_none()
        now = utcnow()
        if (
            row is None
            or row.revoked_at is not None
            or as_utc(row.expires_at) <= now
            or as_utc(row.idle_expires_at) <= now
        ):
            raise UnauthorizedError()
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(
            request.headers.get("X-CSRF-Token", ""), row.csrf_token
        ):
            raise ForbiddenError("Invalid CSRF token.")
        row.last_seen_at = now
        row.idle_expires_at = now + timedelta(minutes=settings.local_admin_idle_minutes)
        session.commit()
        return Actor(
            actor_id="local-admin",
            roles=frozenset({"administrator", "analyst", "validator", "security_operator"}),
            csrf_token=row.csrf_token,
        )
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise UnauthorizedError()
    verifier: JwtVerifier = request.app.state.jwt_verifier
    return verifier.verify(credentials.credentials)


def require_role(actor: Actor, *roles: str) -> None:
    if not actor.has_any_role(set(roles)):
        raise ForbiddenError(f"One of the following roles is required: {', '.join(roles)}")
