"""Authentication, roles and the access-control policy.

The single most important rule in the system lives here: `allowed_classifications`.
Every read path (document list, document detail, retrieval, structured facts,
summaries) filters with it *in the database query*, before any text can reach
the language model. The model is never asked to enforce permissions itself.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import jwt

from advisor.db import Database

ROLES = ("admin", "user")
_PBKDF2_ITERATIONS = 240_000


class AuthError(Exception):
    """Raised for any authentication failure (message is safe to show the user)."""


# ---------------------------------------------------------------- access policy
def allowed_classifications(role: str) -> tuple[str, ...]:
    if role == "admin":
        return ("public", "confidential")
    return ("public",)


def can_upload(role: str, classification: str) -> bool:
    return classification in allowed_classifications(role)


def can_delete(role: str) -> bool:
    return role == "admin"


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex),
                                     int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- users
def create_user(db: Database, username: str, password: str, role: str) -> int:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    return db.execute(
        "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        (username, hash_password(password), role, time.time()),
    )


def ensure_user(db: Database, username: str, password: str, role: str) -> None:
    if not db.one("SELECT id FROM users WHERE username = ?", (username,)):
        create_user(db, username, password, role)


def authenticate(db: Database, username: str, password: str, max_attempts: int,
                 lockout_minutes: int) -> dict:
    """Return the user row or raise AuthError. Locks the account after repeated failures."""
    user = db.one("SELECT * FROM users WHERE username = ?", (username,))
    now = time.time()
    if user is None:
        verify_password(password, hash_password("timing-equaliser"))  # constant-ish time
        raise AuthError("invalid username or password")
    if user["locked_until"] > now:
        raise AuthError("account temporarily locked after repeated failed logins")
    if not verify_password(password, user["password_hash"]):
        attempts = user["failed_attempts"] + 1
        locked_until = now + lockout_minutes * 60 if attempts >= max_attempts else 0
        db.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                   (0 if locked_until else attempts, locked_until, user["id"]))
        raise AuthError("invalid username or password")
    db.execute("UPDATE users SET failed_attempts = 0, locked_until = 0 WHERE id = ?", (user["id"],))
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


# ---------------------------------------------------------------- tokens
def issue_token(user: dict, secret: str, ttl_minutes: int) -> str:
    now = int(time.time())
    payload = {"sub": user["username"], "uid": user["id"], "role": user["role"],
               "iat": now, "exp": now + ttl_minutes * 60}
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str, db: Database) -> dict:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as e:
        raise AuthError("session expired, please log in again") from e
    except jwt.InvalidTokenError as e:
        raise AuthError("invalid token") from e
    # Re-read the role from the DB so a demoted/deleted user loses access immediately
    user = db.one("SELECT id, username, role FROM users WHERE id = ?", (payload.get("uid"),))
    if user is None or user["username"] != payload.get("sub"):
        raise AuthError("user no longer exists")
    return user
