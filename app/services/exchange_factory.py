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
from app.trading.enums import ExchangeKeyMode

logger = get_logger(__name__)

_MODE_LABEL = {ExchangeKeyMode.LIVE: "Реальные", ExchangeKeyMode.DEMO: "Демо"}


class ExchangeFactory:
    def __init__(self, settings: Settings, cipher: SecretCipher) -> None:
        self._settings = settings
        self._cipher = cipher

    def public_client(self) -> ExchangeClient:
        """Клиент без ключей — только рыночные данные.

        Рыночные данные публичны и одинаковы на обоих хостах, поэтому
        режим тут ни при чём — всегда боевой хост (см. bingx_live_base_url)."""
        return BingXClient(
            base_url=self._settings.bingx_base_url,
            recv_window=self._settings.bingx_recv_window,
            timeout=self._settings.http_timeout_seconds,
            max_retries=self._settings.http_max_retries,
        )

    def _base_url(self, mode: ExchangeKeyMode) -> str:
        return (
            self._settings.bingx_live_base_url
            if mode is ExchangeKeyMode.LIVE
            else self._settings.bingx_demo_base_url
        )

    async def for_user(
        self,
        session: AsyncSession,
        user_id: int,
        exchange: str = "bingx",
        mode: ExchangeKeyMode = ExchangeKeyMode.LIVE,
    ) -> ExchangeClient:
        """Клиент с ключами пользователя для конкретного режима (LIVE/DEMO).

        Бросает ExchangeAuthError, если ключи этого режима не подключены
        или не расшифровываются — вызывающий код обязан показать понятное
        сообщение, а не молча работать без аутентификации. Хост клиента
        выбирается по тому же режиму (этап 15.4в, раздел "Выбор хоста").
        """
        credentials = await self.get_credentials(session, user_id, exchange, mode=mode)
        if credentials is None:
            raise ExchangeAuthError(
                f"{_MODE_LABEL[mode]} ключи BingX не подключены. "
                "Добавь их в разделе «Настройки»."
            )

        try:
            api_key = self._cipher.decrypt(credentials.api_key_encrypted)
            api_secret = self._cipher.decrypt(credentials.api_secret_encrypted)
        except SecretCipherError as exc:
            logger.error(  # noqa: TRY400 — traceback тут не нужен
                "Ключи не расшифровываются", extra={"user_id": user_id, "mode": mode.value}
            )
            raise ExchangeAuthError(
                "Сохранённые ключи не читаются. Вероятно, изменился "
                "ENCRYPTION_KEY. Подключи ключи заново в настройках."
            ) from exc

        return BingXClient(
            api_key=api_key,
            api_secret=api_secret,
            base_url=self._base_url(mode),
            recv_window=self._settings.bingx_recv_window,
            timeout=self._settings.http_timeout_seconds,
            max_retries=self._settings.http_max_retries,
        )

    async def has_credentials(
        self,
        session: AsyncSession,
        user_id: int,
        exchange: str = "bingx",
        mode: ExchangeKeyMode = ExchangeKeyMode.LIVE,
    ) -> bool:
        return (
            await session.scalar(
                select(ExchangeCredentials.id).where(
                    ExchangeCredentials.user_id == user_id,
                    ExchangeCredentials.exchange == exchange,
                    ExchangeCredentials.mode == mode,
                    ExchangeCredentials.is_active.is_(True),
                )
            )
        ) is not None

    async def get_credentials(
        self,
        session: AsyncSession,
        user_id: int,
        exchange: str = "bingx",
        mode: ExchangeKeyMode = ExchangeKeyMode.LIVE,
    ) -> ExchangeCredentials | None:
        """Активная строка ключей конкретного режима без расшифровки —
        этап 15 использует её, чтобы узнать is_read_only (guard
        NO_TRADING_KEY), не трогая секреты."""
        return await session.scalar(
            select(ExchangeCredentials).where(
                ExchangeCredentials.user_id == user_id,
                ExchangeCredentials.exchange == exchange,
                ExchangeCredentials.mode == mode,
                ExchangeCredentials.is_active.is_(True),
            )
        )

    async def list_credentials(
        self, session: AsyncSession, user_id: int, exchange: str = "bingx"
    ) -> dict[ExchangeKeyMode, ExchangeCredentials]:
        """Обе пары ключей пользователя (LIVE и DEMO, какие есть) — экран
        настроек показывает их разом, не по одной за раз."""
        rows = await session.scalars(
            select(ExchangeCredentials).where(
                ExchangeCredentials.user_id == user_id,
                ExchangeCredentials.exchange == exchange,
                ExchangeCredentials.is_active.is_(True),
            )
        )
        return {row.mode: row for row in rows}
