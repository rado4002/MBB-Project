from __future__ import annotations

from argon2 import PasswordHasher, Type

from app.operator_identity.passwords import (
    ARGON2_HASH_LENGTH,
    ARGON2_MEMORY_COST_KIB,
    ARGON2_PARALLELISM,
    ARGON2_SALT_LENGTH,
    ARGON2_TIME_COST,
    hash_password,
    needs_rehash,
    verify_password,
)


def test_argon2id_hash_verify_and_parameters() -> None:
    plaintext = "temporary-private-value"
    encoded = hash_password(plaintext)
    assert encoded.startswith("$argon2id$")
    assert plaintext not in encoded
    assert verify_password(encoded, plaintext) is True
    assert verify_password(encoded, "wrong-password") is False
    assert needs_rehash(encoded) is False
    assert f"m={ARGON2_MEMORY_COST_KIB},t={ARGON2_TIME_COST},p={ARGON2_PARALLELISM}" in encoded
    assert ARGON2_SALT_LENGTH >= 16
    assert ARGON2_HASH_LENGTH == 32


def test_malformed_hash_fails_safely() -> None:
    assert verify_password("not-an-argon2-hash", "password") is False
    assert needs_rehash("not-an-argon2-hash") is True


def test_needs_rehash_detects_older_parameters() -> None:
    weaker_hasher = PasswordHasher(
        time_cost=2,
        memory_cost=32_768,
        parallelism=1,
        salt_len=16,
        hash_len=32,
        type=Type.ID,
    )
    assert needs_rehash(weaker_hasher.hash("password")) is True


def test_password_plaintext_is_not_exposed_by_utility_results() -> None:
    plaintext = "do-not-log-this"
    encoded = hash_password(plaintext)
    assert plaintext not in repr(encoded)
