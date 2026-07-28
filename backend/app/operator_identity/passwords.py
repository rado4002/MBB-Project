"""Argon2id password hashing for operator accounts."""

from __future__ import annotations

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

ARGON2_MEMORY_COST_KIB = 65_536
ARGON2_TIME_COST = 3
ARGON2_PARALLELISM = 1
ARGON2_SALT_LENGTH = 16
ARGON2_HASH_LENGTH = 32

_PASSWORD_HASHER = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST_KIB,
    parallelism=ARGON2_PARALLELISM,
    salt_len=ARGON2_SALT_LENGTH,
    hash_len=ARGON2_HASH_LENGTH,
    type=Type.ID,
)


def hash_password(password: str) -> str:
    """Return a self-describing Argon2id hash for a non-empty password."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must not be empty")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Safely verify a password; malformed or mismatched hashes return False."""
    if not isinstance(password_hash, str) or not isinstance(password, str):
        return False
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Return whether a valid hash differs from current centralized parameters."""
    if not isinstance(password_hash, str):
        return True
    try:
        return _PASSWORD_HASHER.check_needs_rehash(password_hash)
    except (InvalidHashError, VerificationError):
        return True
