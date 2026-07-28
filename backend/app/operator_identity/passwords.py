"""Central password policy and Argon2id hashing for operator accounts."""

from __future__ import annotations

import unicodedata
from enum import Enum
from pathlib import Path

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

ARGON2_MEMORY_COST_KIB = 65_536
ARGON2_TIME_COST = 3
ARGON2_PARALLELISM = 1
ARGON2_SALT_LENGTH = 16
ARGON2_HASH_LENGTH = 32
USER_PASSWORD_MIN_LENGTH = 14
USER_PASSWORD_MAX_LENGTH = 128

_PASSWORD_HASHER = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST_KIB,
    parallelism=ARGON2_PARALLELISM,
    salt_len=ARGON2_SALT_LENGTH,
    hash_len=ARGON2_HASH_LENGTH,
    type=Type.ID,
)


class PasswordPolicyReason(str, Enum):
    LENGTH = "length"
    CONTROL_CHARACTER = "control_character"
    USERNAME = "username"
    DISPLAY_NAME = "display_name"
    CURRENT_PASSWORD = "current_password"
    DENYLISTED = "denylisted"


class PasswordPolicyViolation(ValueError):
    """A deterministic internal policy result; never expose its detail publicly."""

    def __init__(self, reason: PasswordPolicyReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def _normalized_comparison(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _compact_identity(value: str) -> str:
    return "".join(
        character for character in _normalized_comparison(value) if character.isalnum()
    )


def _without_diacritics(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if unicodedata.category(character) != "Mn"
    )


def _display_name_variants(display_name: str) -> frozenset[str]:
    normalized = _normalized_comparison(display_name)
    words = tuple(
        "".join(
            character if character.isalnum() else " " for character in normalized
        ).split()
    )
    candidates = {
        _compact_identity(normalized),
        _compact_identity(_without_diacritics(normalized)),
    }
    for word in words:
        if len(word) >= 4:
            candidates.add(_compact_identity(word))
            candidates.add(_compact_identity(_without_diacritics(word)))
    return frozenset(candidate for candidate in candidates if len(candidate) >= 4)


def _load_password_denylist() -> frozenset[str]:
    resource = Path(__file__).with_name("common_compromised_passwords.txt")
    entries = (
        line.strip()
        for line in resource.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    return frozenset(_normalized_comparison(entry) for entry in entries)


_COMMON_PASSWORDS = _load_password_denylist()


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


def validate_user_chosen_password(
    password: str,
    *,
    username: str,
    display_name: str,
    current_password_hash: str | None = None,
) -> None:
    """Validate one user-chosen password without changing or returning it."""
    if not USER_PASSWORD_MIN_LENGTH <= len(password) <= USER_PASSWORD_MAX_LENGTH:
        raise PasswordPolicyViolation(PasswordPolicyReason.LENGTH)
    if any(unicodedata.category(character) == "Cc" for character in password):
        raise PasswordPolicyViolation(PasswordPolicyReason.CONTROL_CHARACTER)

    normalized_password = _normalized_comparison(password)
    normalized_username = _normalized_comparison(username)
    if normalized_username and normalized_username in normalized_password:
        raise PasswordPolicyViolation(PasswordPolicyReason.USERNAME)

    compact_password = _compact_identity(normalized_password)
    if any(
        variant in compact_password for variant in _display_name_variants(display_name)
    ):
        raise PasswordPolicyViolation(PasswordPolicyReason.DISPLAY_NAME)

    if current_password_hash is not None and verify_password(
        current_password_hash, password
    ):
        raise PasswordPolicyViolation(PasswordPolicyReason.CURRENT_PASSWORD)
    if normalized_password in _COMMON_PASSWORDS:
        raise PasswordPolicyViolation(PasswordPolicyReason.DENYLISTED)
