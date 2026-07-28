from __future__ import annotations

import pytest
from argon2 import PasswordHasher, Type

from app.operator_identity import accounts
from app.operator_identity.passwords import (
    ARGON2_HASH_LENGTH,
    ARGON2_MEMORY_COST_KIB,
    ARGON2_PARALLELISM,
    ARGON2_SALT_LENGTH,
    ARGON2_TIME_COST,
    PasswordPolicyReason,
    PasswordPolicyViolation,
    USER_PASSWORD_MAX_LENGTH,
    USER_PASSWORD_MIN_LENGTH,
    hash_password,
    needs_rehash,
    validate_user_chosen_password,
    verify_password,
)

USERNAME = "operator.one"
DISPLAY_NAME = "Operator One"


def _validate(password: str, *, current_password_hash: str | None = None) -> None:
    validate_user_chosen_password(
        password,
        username=USERNAME,
        display_name=DISPLAY_NAME,
        current_password_hash=current_password_hash,
    )


def _assert_rejected(password: str, reason: PasswordPolicyReason) -> None:
    with pytest.raises(PasswordPolicyViolation) as exc_info:
        _validate(password)
    assert exc_info.value.reason is reason
    assert str(exc_info.value) == reason.value


def test_user_password_length_boundaries_are_unicode_character_counts() -> None:
    _assert_rejected("a" * 13, PasswordPolicyReason.LENGTH)
    _validate("a" * USER_PASSWORD_MIN_LENGTH)
    _validate("界" * USER_PASSWORD_MAX_LENGTH)
    _assert_rejected("界" * (USER_PASSWORD_MAX_LENGTH + 1), PasswordPolicyReason.LENGTH)


def test_single_character_class_passphrase_is_accepted() -> None:
    _validate("only lowercase words make this long")


def test_control_characters_are_rejected() -> None:
    _assert_rejected(
        "valid length but\u0000controlled",
        PasswordPolicyReason.CONTROL_CHARACTER,
    )


def test_normalized_username_is_rejected() -> None:
    _assert_rejected(
        "safe-prefix-OPERATOR.ONE-suffix",
        PasswordPolicyReason.USERNAME,
    )


@pytest.mark.parametrize(
    "password",
    (
        "safe Operator_One suffix",
        "safe operatoronesuffix",
    ),
)
def test_obvious_normalized_display_name_variants_are_rejected(
    password: str,
) -> None:
    _assert_rejected(password, PasswordPolicyReason.DISPLAY_NAME)


def test_display_name_diacritic_variant_is_rejected() -> None:
    with pytest.raises(PasswordPolicyViolation) as exc_info:
        validate_user_chosen_password(
            "safe jose-mbuyi phrase",
            username="another.user",
            display_name="José Mbuyi",
        )
    assert exc_info.value.reason is PasswordPolicyReason.DISPLAY_NAME


def test_current_password_reuse_is_rejected() -> None:
    current_password = "only lowercase current passphrase"
    with pytest.raises(PasswordPolicyViolation) as exc_info:
        _validate(
            current_password,
            current_password_hash=hash_password(current_password),
        )
    assert exc_info.value.reason is PasswordPolicyReason.CURRENT_PASSWORD


def test_offline_common_password_is_rejected_after_normalization() -> None:
    _assert_rejected(
        "Correct Horse Battery Staple",
        PasswordPolicyReason.DENYLISTED,
    )


def test_generated_temporary_credential_behavior_is_policy_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(accounts.secrets, "token_bytes", lambda count: b"\x00" * count)
    temporary_password = accounts._temporary_password()
    assert temporary_password == "AAAAAAAAAAAAAAAAAAAAAA"
    assert verify_password(hash_password(temporary_password), temporary_password)


def test_argon2id_hash_verify_and_parameters() -> None:
    plaintext = "temporary-private-value"
    encoded = hash_password(plaintext)
    assert encoded.startswith("$argon2id$")
    assert plaintext not in encoded
    assert verify_password(encoded, plaintext) is True
    assert verify_password(encoded, "wrong-password") is False
    assert needs_rehash(encoded) is False
    assert (
        f"m={ARGON2_MEMORY_COST_KIB},t={ARGON2_TIME_COST},p={ARGON2_PARALLELISM}"
        in encoded
    )
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
