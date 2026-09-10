"""Генерация ENCRYPTION_KEY.

Запуск:  python -m scripts.generate_key

Ключ сохранить в .env один раз. При его смене ранее зашифрованные
биржевые ключи станут нечитаемыми и их придётся ввести заново.
"""

from cryptography.fernet import Fernet


def main() -> None:
    print(Fernet.generate_key().decode())


if __name__ == "__main__":
    main()
