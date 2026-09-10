"""Создание биржевого клиента из сохранённых ключей.

Расшифровка секретов происходит здесь и только здесь, непосредственно
перед созданием клиента. Расшифрованные значения живут в памяти
процесса, никуда не записываются и не логируются.

Публичный клиент (без ключей) создаётся всегда: рыночные данные не
требуют аутентификации, и анализ рынка должен работать у пользователя,
который ключи не подключал.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher, SecretCipherError
from app.database.models.credentials import ExchangeCredentials
from app.exchanges.base import ExchangeAuthError, ExchangeClient
from app.exchanges.bingx import BingXClient

logger = get_logger(__name__)


class ExchangeFactory:
    def __init__(self, settings: Settings, cipher: SecretCipher) -> None:
        self._settings = settings
        self._cipher = cipher

    def public_client(self) -> ExchangeClient:
        """Клиент без ключей — только рыночные данные."""
        return BingXClient(
            base_url=self._settings.bingx_base_url,
            recv_window=self._settings.bingx_recv_window,
            timeout=self._settings.http_timeout_seconds,
            max_retries=self._settings.http_max_retries,
        )

    async def for_user(
        self, session: AsyncSession, user_id: int, exchange: str = "bingx"
    ) -> ExchangeClient:
        """Клиент с ключами пользователя.

        Бросает ExchangeAuthError, если ключи не подключены или не
        расшифровываются — вызывающий код обязан показать понятное
        сообщение, а не молча работать без аутентификации.
        """
        credentials = await session.scalar(
            select(ExchangeCredentials).where(
                ExchangeCredentials.user_id == user_id,
                ExchangeCredentials.exchange == exchange,
                ExchangeCredentials.is_active.is_(True),
            )
        )
        if credentials is None:
            raise ExchangeAuthError(
                "Ключи BingX не подключены. Добавь их в разделе «Настройки»."
            )

        try:
            api_key = self._cipher.decrypt(credentials.api_key_encrypted)
            api_secret = self._cipher.decrypt(credentials.api_secret_encrypted)
        except SecretCipherError as exc:
            logger.error(  # noqa: TRY400 — traceback тут не нужен
                "Ключи не расшифровываются", extra={"user_id": user_id}
            )
            raise ExchangeAuthError(
                "Сохранённые ключи не читаются. Вероятно, изменился "
                "ENCRYPTION_KEY. Подключи ключи заново в настройках."
            ) from exc

        return BingXClient(
            api_key=api_key,
            api_secret=api_secret,
            base_url=self._settings.bingx_base_url,
            recv_window=self._settings.bingx_recv_window,
            timeout=self._settings.http_timeout_seconds,
            max_retries=self._settings.http_max_retries,
        )

    async def has_credentials(
        self, session: AsyncSession, user_id: int, exchange: str = "bingx"
    ) -> bool:
        return (
            await session.scalar(
                select(ExchangeCredentials.id).where(
                    ExchangeCredentials.user_id == user_id,
                    ExchangeCredentials.exchange == exchange,
                    ExchangeCredentials.is_active.is_(True),
                )
            )
        ) is not None
