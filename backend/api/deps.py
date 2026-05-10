"""
backend/api/deps.py
────────────────────
Shared FastAPI dependency functions used across all routers.

Separation of concerns:
• get_db          — yields an async DB session (delegates to database.base)
• get_current_user — decodes JWT from header and returns the authenticated User ORM object
• require_role    — factory that returns a dependency enforcing role membership

These are the fine-grained building blocks for per-endpoint security on top of
the coarse-grained middleware in backend/middleware/auth.py.
"""

from __future__ import annotations

import uuid
from typing import Callable

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database.base import get_db
from backend.database.models import User, UserRole

logger = structlog.get_logger(__name__)

# OAuth2 scheme — instructs FastAPI /docs to show "Authorize" button
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Decode the JWT and return the authenticated User from the DB.

    Raises HTTP 401 if:
    - Token is missing, malformed, or expired
    - The user_id (sub claim) is not a valid UUID
    - The user is not found or is inactive
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm],
        )
        user_id_str: str | None = payload.get("sub")
        if user_id_str is None:
            raise credentials_exception
        user_id = uuid.UUID(user_id_str)
    except (JWTError, ValueError):
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        logger.warning("user_not_found_from_token", user_id=str(user_id))
        raise credentials_exception

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated.",
        )

    return user


def require_role(*allowed_roles: UserRole) -> Callable:
    """
    Dependency factory for per-endpoint role enforcement.

    Usage:
        @router.delete("/users/{id}", dependencies=[Depends(require_role(UserRole.ADMIN))])
        async def delete_user(...): ...

    Or as a typed dependency:
        async def endpoint(current_user: User = Depends(require_role(UserRole.ADMIN, UserRole.UNDERWRITER))):
    """
    async def _check_role(
        current_user: User = Depends(get_current_user),
    ) -> User:
        if current_user.role not in allowed_roles:
            logger.warning(
                "endpoint_rbac_denied",
                user_id=str(current_user.id),
                role=current_user.role,
                allowed=allowed_roles,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required: {[r.value for r in allowed_roles]}",
            )
        return current_user

    return _check_role
