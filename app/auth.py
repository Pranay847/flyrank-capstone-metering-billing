"""Tenant and admin authentication. API keys are stored only as SHA-256 hashes;
the plain key is shown once, at creation, and never logged."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .errors import ApiError
from .models import Tenant


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "mb_" + secrets.token_urlsafe(24)


def current_tenant(
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Tenant:
    if not x_api_key:
        raise ApiError(401, "missing_api_key", "Send your tenant API key in the X-API-Key header.")
    tenant = db.execute(
        select(Tenant).where(Tenant.api_key_hash == hash_key(x_api_key))
    ).scalar_one_or_none()
    if tenant is None:
        raise ApiError(401, "invalid_api_key", "That API key does not match any tenant.")
    return tenant


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_token
    if not expected:
        raise ApiError(503, "admin_disabled", "ADMIN_TOKEN is not configured on this server.")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise ApiError(401, "invalid_admin_token", "Admin endpoints require a valid X-Admin-Token.")
