"""Тесты слоя шифрования.

Проверяем в первую очередь то, что при поломке приведёт к утечке ключей
пользователя или к потере доступа к бирже.
"""

from __future__ import annotations

import pytest

from app.core.security import SecretCipher, SecretCipherError, mask_secret


@pytest.fixture
def cipher() -> SecretCipher:
    return SecretCipher(SecretCipher.generate_key())


def test_roundtrip(cipher: SecretCipher) -> None:
    secret = "bingx_api_secret_value_12345"
    assert cipher.decrypt(cipher.encrypt(secret)) == secret


def test_ciphertext_does_not_contain_plaintext(cipher: SecretCipher) -> None:
    secret = "SUPER_SECRET_KEY"
    assert secret not in cipher.encrypt(secret)


def test_same_plaintext_gives_different_ciphertext(cipher: SecretCipher) -> None:
    """Fernet добавляет IV, поэтому одинаковый вход даёт разный шифротекст.
    Без этого по базе было бы видно, у кого совпадают ключи."""
    secret = "identical"
    assert cipher.encrypt(secret) != cipher.encrypt(secret)


def test_wrong_key_raises_clear_error() -> None:
    token = SecretCipher(SecretCipher.generate_key()).encrypt("value")
    other = SecretCipher(SecretCipher.generate_key())
    with pytest.raises(SecretCipherError):
        other.decrypt(token)


def test_empty_plaintext_rejected(cipher: SecretCipher) -> None:
    with pytest.raises(ValueError):
        cipher.encrypt("")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abcdefghijklmnop", "abcd...mnop"),
        ("short", "*****"),
        ("12345678", "********"),
    ],
)
def test_mask_secret(value: str, expected: str) -> None:
    assert mask_secret(value) == expected
