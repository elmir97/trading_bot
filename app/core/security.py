"""Шифрование секретов пользователя перед записью в БД.

Биржевые ключи никогда не лежат в базе открытым текстом: при компрометации
дампа PostgreSQL они останутся нечитаемыми без ENCRYPTION_KEY, который
живёт только в окружении процесса.

Fernet = AES-128-CBC + HMAC-SHA256, с меткой времени и защитой целостности.
Для нашей задачи (симметричное шифрование коротких строк) этого достаточно.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretCipherError(RuntimeError):
    """Не удалось расшифровать: неверный ключ или повреждённые данные."""


class SecretCipher:
    """Обёртка над Fernet. Инстанцируется один раз и прокидывается через DI."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            raise ValueError("Нечего шифровать: пустая строка")
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecretCipherError(
                "Не удалось расшифровать секрет. Вероятно, ENCRYPTION_KEY "
                "изменился после сохранения данных."
            ) from exc

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()


def mask_secret(value: str, visible: int = 4) -> str:
    """Безопасное отображение ключа пользователю: 'AbCd...WxYz'.

    Показывать хвост нужно, чтобы пользователь мог отличить свои ключи
    друг от друга, не раскрывая их целиком.
    """
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"
