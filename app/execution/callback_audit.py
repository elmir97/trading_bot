"""Журнал нажатий кнопок исполнения (префлайт 15.7).

Каждое нажатие exn:open / exn:yes / exn:no — строка в execution_callbacks
и INFO в лог. Пишется первым действием хендлера, до любых проверок: попадают
и устаревшие карточки, и двойной тап при занятом локе, и битые данные кнопки.

Своя сессия с немедленным коммитом, а не сессия апдейта: middleware ведёт
одну транзакцию на апдейт и откатывает её при падении хендлера — запись
пропала бы ровно тогда, когда она нужнее всего. Пользователь обязан быть
закоммичен (FK на users): незакоммиченного своя сессия не видит — сразу
FK-ошибка. lock_timeout — от конфликта блокировок на строке users (удаление,
смена ключа в чужой транзакции): ошибка за секунды, по которой «Да» откажет,
а не хендлер, ждущий чужую транзакцию.

Сбой записи — исключение наружу; что с ним делать, решает хендлер: «Да»
блокирует вход, «Открыть» и «Нет» продолжают (они ничего не исполняют).

В лог — telegram_id только маской (mask_telegram_id). chat_id в лог не
пишется: в личном чате он равен telegram_id.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.logging import get_logger
from app.core.security import mask_telegram_id
from app.database.models.execution_callback import ExecutionCallback
from app.database.session import Database
from app.trading.enums import ExecutionCallbackAction

logger = get_logger(__name__)

# Сколько знаков сырых данных кнопки писать в лог, если id не разобран.
# callback_data в Telegram и так не длиннее 64 байт.
RAW_DATA_LOG_LIMIT = 64
LOCK_TIMEOUT = "3s"


async def record_callback(
    db: Database,
    *,
    user_id: int,
    telegram_id: int | None,
    action: ExecutionCallbackAction,
    notification_id: int | None,
    raw_data: str | None,
    chat_id: int | None,
    message_id: int | None,
    callback_query_id: str | None,
    position_action_id: int | None = None,
) -> None:
    async with db.session() as session:
        await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        session.add(
            ExecutionCallback(
                user_id=user_id,
                action=action.value,
                notification_id=notification_id,
                position_action_id=position_action_id,
                chat_id=chat_id,
                message_id=message_id,
                callback_query_id=callback_query_id,
            )
        )

    extra: dict[str, object] = {
        "action": action.value,
        "notification_id": notification_id,
        "position_action_id": position_action_id,
        "user_id": user_id,
        "tg": mask_telegram_id(telegram_id),
        "message_id": message_id,
        "callback_id": callback_query_id,
    }
    if notification_id is None and position_action_id is None:
        extra["raw_data"] = (raw_data or "")[:RAW_DATA_LOG_LIMIT]
    logger.info("Нажатие кнопки исполнения", extra=extra)
