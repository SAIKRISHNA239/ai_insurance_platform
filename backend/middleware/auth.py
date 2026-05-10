"""
backend/middleware/auth.py
──────────────────────────
JWT Authentication + Role-Based Access Control middleware.

Architecture:
• JWTAuthMiddleware (Starlette BaseHTTPMiddleware):
    – Runs on every inbound request BEFORE routers are invoked.
    – Extracts the Bearer token from the Authorization header.
    – Decodes & validates it using python-jose.
    – Injects request.state.user_id (UUID) and request.state.role (UserRole).
    – Public routes (OPEN_PATHS) bypass authentication entirely.
    – Returns HTTP 401 on missing/invalid/expired token.

• RBACMiddleware:
    – Checks request.state.role against a ROUTE_ROLE_MAP.
    – Returns HTTP 403 Forbidden if the role is not permitted.
    – Must be added AFTER JWTAuthMiddleware in the stack.

Note: For finer-grained RBAC on individual endpoints, use the
require_role() FastAPI dependency in backend/api/deps.py.
This middleware provides a coarse-grained first-pass guard.
"""

from __future__ import annotations

import re
from typing import Callable

import structlog
from fastapi import status
from fastapi.responses import JSONResponse
from jose import ExpiredSignatureError, JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from backend.config import get_settings
from backend.database.models import UserRole

logger = structlog.get_logger(__name__)

# ─── Routes that skip JWT validation ──────────────────────────────────────────
OPEN_PATHS: set[str] = {
    "/health",
    "/api/v1/auth/token",
    "/api/v1/auth/register",
    # OpenAPI docs
    "/docs",
    "/redoc",
    "/openapi.json",
}

# ─── Prefix patterns that skip JWT validation (regex) ─────────────────────────
OPEN_PATH_PATTERNS: list[re.Pattern] = [
    re.compile(r"^/static/.*$"),
]


def _is_open_path(path: str) -> bool:
    """Return True if the path should bypass authentication."""
    if path in OPEN_PATHS:
        return True
    return any(pattern.match(path) for pattern in OPEN_PATH_PATTERNS)


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": detail},
    )


# ─────────────────────────────────────────────────────────────────────────────
# JWT Authentication Middleware
# ─────────────────────────────────────────────────────────────────────────────

class JWTAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates the JWT on every non-public request.
    Injects request.state.user_id and request.state.role on success.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if _is_open_path(request.url.path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _unauthorized("Authorization header missing or malformed.")

        token = auth_header.removeprefix("Bearer ").strip()
        settings = get_settings()

        try:
            payload = jwt.decode(
                token,
                settings.secret_key,
                algorithms=[settings.algorithm],
            )
        except ExpiredSignatureError:
            logger.warning("jwt_expired", path=request.url.path)
            return _unauthorized("Token has expired.")
        except JWTError as exc:
            logger.warning("jwt_invalid", path=request.url.path, error=str(exc))
            return _unauthorized("Invalid authentication token.")

        user_id: str | None = payload.get("sub")
        role: str | None = payload.get("role")

        if not user_id or not role:
            return _unauthorized("Token payload is missing required claims.")

        # Validate role is a known enum value
        try:
            validated_role = UserRole(role)
        except ValueError:
            return _unauthorized(f"Unknown role in token: {role!r}")

        # Inject into request state for downstream handlers / dependencies
        request.state.user_id = user_id
        request.state.role = validated_role

        logger.debug(
            "jwt_authenticated",
            user_id=user_id,
            role=role,
            path=request.url.path,
        )
        return await call_next(request)


# ─────────────────────────────────────────────────────────────────────────────
# Route → Allowed Roles map (coarse-grained RBAC)
# ─────────────────────────────────────────────────────────────────────────────

# Map: (HTTP method, path prefix) → set of allowed UserRole values.
# More specific rules should come first; first match wins.
ROUTE_ROLE_MAP: list[tuple[str, str, set[UserRole]]] = [
    # Admin-only management routes
    ("ANY", "/api/v1/admin", {UserRole.ADMIN}),

    # Underwriting — underwriters + admins
    ("ANY", "/api/v1/applications", {UserRole.ADMIN, UserRole.UNDERWRITER, UserRole.INSURED}),

    # Claims — adjusters, admins, insured can submit their own
    ("ANY", "/api/v1/claims", {UserRole.ADMIN, UserRole.CLAIMS_ADJUSTER, UserRole.INSURED}),

    # Policies — all authenticated users can read; write restricted by endpoint deps
    ("ANY", "/api/v1/policies", {UserRole.ADMIN, UserRole.UNDERWRITER, UserRole.INSURED}),
]


# ─────────────────────────────────────────────────────────────────────────────
# RBAC Middleware
# ─────────────────────────────────────────────────────────────────────────────

class RBACMiddleware(BaseHTTPMiddleware):
    """
    Coarse-grained Role-Based Access Control.
    Must be added to the middleware stack AFTER JWTAuthMiddleware.

    For fine-grained per-endpoint control, use the require_role() dependency.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if _is_open_path(request.url.path):
            return await call_next(request)

        # At this point JWTAuthMiddleware has already run and injected state
        role: UserRole | None = getattr(request.state, "role", None)
        if role is None:
            # JWT middleware did not authenticate — let it handle the 401
            return await call_next(request)

        path = request.url.path
        method = request.method.upper()

        for rule_method, prefix, allowed_roles in ROUTE_ROLE_MAP:
            if not path.startswith(prefix):
                continue
            if rule_method != "ANY" and rule_method != method:
                continue
            if role not in allowed_roles:
                logger.warning(
                    "rbac_denied",
                    user_id=getattr(request.state, "user_id", None),
                    role=role,
                    path=path,
                    allowed=allowed_roles,
                )
                return _forbidden(
                    f"Role '{role}' is not permitted to access this resource."
                )
            break  # First matching rule wins

        return await call_next(request)
