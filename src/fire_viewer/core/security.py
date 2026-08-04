from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
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
    email_verified: bool = False
    issued_at: datetime | None = None
    auth_time: datetime | None = None
    session_id: str | None = None
    token: str | None = field(default=None, repr=False, compare=False)

    def has_any_role(self, required: set[str] | frozenset[str]) -> bool:
        return bool(self.roles.intersection(required))

    def has_all_roles(self, required: set[str] | frozenset[str]) -> bool:
        return required.issubset(self.roles)


class JwtVerifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        jwks_url: str | None = str(settings.oidc_jwks_url) if settings.oidc_jwks_url else None
        if settings.auth_mode == "supabase" and settings.supabase_url is not None:
            jwks_url = HttpUrlAdapter.join(
                str(settings.supabase_url), "auth/v1/.well-known/jwks.json"
            )
        self._jwk_client = PyJWKClient(str(jwks_url), cache_keys=True) if jwks_url else None

    def verify(self, token: str) -> Actor:
        if self._jwk_client is None:
            raise UnauthorizedError("JWT verification is not configured.")
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            issuer = self.settings.oidc_issuer
            audience = self.settings.oidc_audience
            if self.settings.auth_mode == "supabase":
                issuer = HttpUrlAdapter.join(str(self.settings.supabase_url), "auth/v1")
                audience = self.settings.supabase_jwt_audience
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=self.settings.oidc_algorithms,
                audience=audience,
                issuer=issuer,
                leeway=self.settings.oidc_leeway_seconds,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("The bearer token is invalid or expired.") from exc

        actor_id = str(claims.get("sub", "")).strip()
        if not actor_id:
            raise UnauthorizedError("The bearer token has no subject.")

        if self.settings.auth_mode == "supabase":
            app_metadata = claims.get("app_metadata")
            raw_roles = app_metadata.get("roles", []) if isinstance(app_metadata, dict) else []
        else:
            raw_roles = claims.get(self.settings.oidc_roles_claim, [])
        if isinstance(raw_roles, str):
            roles = frozenset(part for part in raw_roles.replace(",", " ").split() if part)
        elif isinstance(raw_roles, list):
            roles = frozenset(str(role) for role in raw_roles)
        else:
            roles = frozenset()
        if self.settings.auth_mode == "supabase":
            allowed = {"analyst", "editor", "security_operator", "administrator"}
            roles = frozenset({"contributor", *(role for role in roles if role in allowed)})
            # `email_verified` is not part of the standard Supabase access-token
            # contract. A signed custom claim can short-circuit the live lookup,
            # but user_metadata is deliberately never consulted for this gate.
            email_verified = (
                claims.get("email_verified") is True
                and claims.get("is_anonymous") is not True
            )
        else:
            email_verified = bool(claims.get("email_verified", True))
        issued_at = datetime.fromtimestamp(float(claims["iat"]), tz=utcnow().tzinfo)
        raw_auth_time = claims.get("auth_time", claims["iat"])
        auth_time = datetime.fromtimestamp(float(raw_auth_time), tz=utcnow().tzinfo)
        return Actor(
            actor_id=actor_id,
            roles=roles,
            email_verified=email_verified,
            issued_at=issued_at,
            auth_time=auth_time,
            session_id=str(claims.get("session_id", "")).strip() or None,
            token=token,
        )


class HttpUrlAdapter:
    @staticmethod
    def join(base: str, suffix: str) -> str:
        return f"{base.rstrip('/')}/{suffix.lstrip('/')}"


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
            roles=frozenset(
                {
                    "administrator",
                    "analyst",
                    "editor",
                    "validator",
                    "security_operator",
                    "contributor",
                }
            ),
            email_verified=True,
            issued_at=utcnow(),
            auth_time=utcnow(),
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
            roles=frozenset(
                {
                    "administrator",
                    "analyst",
                    "editor",
                    "validator",
                    "security_operator",
                    "contributor",
                }
            ),
            csrf_token=row.csrf_token,
            email_verified=True,
            issued_at=now,
            auth_time=now,
        )
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise UnauthorizedError()
    verifier: JwtVerifier = request.app.state.jwt_verifier
    return verifier.verify(credentials.credentials)


def require_role(actor: Actor, *roles: str) -> None:
    if not actor.has_any_role(set(roles)):
        raise ForbiddenError(f"One of the following roles is required: {', '.join(roles)}")


def _current_supabase_roles(user: dict[str, Any]) -> frozenset[str]:
    app_metadata = user.get("app_metadata")
    raw_roles = app_metadata.get("roles", []) if isinstance(app_metadata, dict) else []
    if isinstance(raw_roles, str):
        parsed = [part for part in raw_roles.replace(",", " ").split() if part]
    elif isinstance(raw_roles, list):
        parsed = [str(role) for role in raw_roles]
    else:
        parsed = []
    allowed = {"analyst", "editor", "security_operator", "administrator"}
    return frozenset(role for role in parsed if role in allowed)


def require_current_role(actor: Actor, settings: Settings, *roles: str) -> None:
    """Recheck elevated Supabase roles so a stale JWT cannot retain access."""

    require_role(actor, *roles)
    if settings.auth_mode != "supabase":
        return
    current_roles = _current_supabase_roles(_load_active_supabase_user(actor, settings))
    if not current_roles.intersection(roles):
        raise ForbiddenError(f"One of the following roles is required: {', '.join(roles)}")


def _load_active_supabase_user(actor: Actor, settings: Settings) -> dict[str, Any]:
    if (
        not settings.supabase_session_validation_enabled
        or actor.token is None
        or settings.supabase_url is None
        or settings.supabase_publishable_key is None
    ):
        raise ForbiddenError("Active Supabase session validation is unavailable.")
    try:
        response = httpx.get(
            HttpUrlAdapter.join(str(settings.supabase_url), "auth/v1/user"),
            headers={
                "Authorization": f"Bearer {actor.token}",
                "apikey": settings.supabase_publishable_key.get_secret_value(),
            },
            timeout=5.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ForbiddenError("The Supabase session is no longer active.") from exc
    if not isinstance(payload, dict) or str(payload.get("id", "")) != actor.actor_id:
        raise ForbiddenError("The Supabase session subject is inconsistent.")
    return payload


def require_verified_contributor(actor: Actor, settings: Settings) -> None:
    if "contributor" not in actor.roles:
        raise ForbiddenError("A verified contributor account is required.")
    if actor.email_verified:
        return
    if settings.auth_mode != "supabase":
        raise ForbiddenError("A verified contributor account is required.")
    user = _load_active_supabase_user(actor, settings)
    confirmed_at = user.get("email_confirmed_at") or user.get("confirmed_at")
    if (
        user.get("is_anonymous") is True
        or not str(user.get("email", "")).strip()
        or not str(confirmed_at or "").strip()
    ):
        raise ForbiddenError("A verified contributor account is required.")


def require_recent_active_session(
    actor: Actor,
    settings: Settings,
    *,
    required_roles: tuple[str, ...] = (),
) -> None:
    """Require a recent token and optionally validate the live Supabase session."""

    if actor.auth_time is None:
        raise ForbiddenError("A recent authenticated session is required.")
    age = (utcnow() - as_utc(actor.auth_time)).total_seconds()
    if age < 0 or age > settings.event_publication_session_max_age_seconds:
        raise ForbiddenError("The authenticated session is too old for publication.")
    if settings.auth_mode != "supabase":
        return
    user = _load_active_supabase_user(actor, settings)
    if required_roles and not _current_supabase_roles(user).intersection(required_roles):
        raise ForbiddenError(
            f"One of the following roles is required: {', '.join(required_roles)}"
        )
