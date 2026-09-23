"""Сканер сетапов (этап 12).

Классификация READY/FORMING — в app/analysis/classify.py (общая с экраном
«Анализ рынка»); здесь она только применяется.

Дедуп — по "слоту" (user, symbol, timeframe, level) в таблице signals:
не то же самое, что случалось в прошлом скане, а то, что сейчас активно.
См. SignalRepository и docstring SignalRecord.

Шаг 15.5.2а: каждое реально отправленное уведомление пишет неизменяемый
снимок в signal_notifications, и кнопка «Открыть сделку» адресует его
notification_id, а не изменчивую строку слота. Порядок: вставка снимка в
SAVEPOINT -> flush (id) -> отправка -> при недоставке откат SAVEPOINT;
коммит — один на весь run() (Database.session). Если коммит не случится
уже после отправки, кнопка укажет на id, которого в БД нет, — хендлер
ответит «Уведомление устарело»; sequence PostgreSQL не откатывается,
поэтому этот id никогда не достанется другому уведомлению.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.charting import render_setup_chart
from app.analysis.classify import (
    CONFIRMATION_CONDITION_NAME,
    SCAN_TIMEFRAMES,
    classify_signal,
)
from app.analysis.engine import AnalysisEngine
from app.analysis.signals import MarketContext, Signal, wait_signal
from app.bot.keyboards.execution import open_trade_button
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.numfmt import fmt_price
from app.database.models.signal import SignalRecord
from app.database.models.signal_notification import SignalNotification
from app.database.models.user import User
from app.database.repositories.signal import SignalRepository
from app.database.repositories.signal_notification import SignalNotificationRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.trading.enums import SignalLevel, SignalRecordStatus
from app.workers.base import fmt_decimal
from app.workers.notifier import (
    notification_enabled,
    send_notification,
    send_notification_photo,
)

logger = get_logger(__name__)


class _NotDeliveredError(Exception):
    """Внутренний сигнал «сообщение не ушло» — откатывает SAVEPOINT со
    снимком уведомления (см. SetupScanner._notify)."""


__all__ = [
    "CONFIRMATION_CONDITION_NAME",
    "SCAN_TIMEFRAMES",
    "ScanCycleStats",
    "SetupScanner",
    "build_fingerprint",
    "classify_signal",
    "render_detail",
]


@dataclass(frozen=True, slots=True)
class ScanCycleStats:
    """Замер одного полного прогона run() — раздел "троттлинг сканера".

    Нужен, чтобы расширение списка символов было измеримым, а не на глаз
    (см. docstring SetupScanner.run()). Пишется в лог и попадает строкой в
    ежедневную сводку исполнения (app/workers/execution_digest.py)."""

    symbols_scanned: int
    requests_made: int
    duration_seconds: float


def build_fingerprint(signal: Signal, level: SignalLevel) -> str:
    """Хэш условий сетапа — дедуп сравнивает его, а не поля по отдельности.

    READY зависит от конкретных цен: заметно изменившийся вход/стоп/цель —
    это по сути другой сетап, даже если имя и направление те же.

    FORMING не может использовать signal.setup: wait_signal() в
    app/analysis/signals.py всегда пишет туда "Нет сетапа" независимо от
    того, какой детектор сработал — само имя детектора для WAIT-сигнала
    нигде не сохраняется. Вместо этого берём signal.note — у каждого
    детектора текст "не хватает подтверждения" свой и не меняется от
    скана к скану, так что он и определяет "какой именно сетап формируется"
    не хуже имени, и заодно не плодит уведомления от мелких колебаний.
    """
    if level is SignalLevel.READY:
        parts = [
            signal.setup,
            signal.direction.value,
            fmt_decimal(signal.entry_zone_low),
            fmt_decimal(signal.entry_zone_high),
            fmt_decimal(signal.stop_loss),
            fmt_decimal(signal.take_profit_1),
        ]
    else:
        parts = [signal.note, "forming"]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def render_detail(
    signal: Signal, level: SignalLevel, price_precision: int | None = None
) -> str:
    """price_precision — SymbolInfo.price_precision; None — по порядку
    величины цены (см. fmt_price). Цены на карточке округляются, а не режутся
    по нулям: цена сигнала — round_price с 4 знаками, а у символа точность
    может быть меньше."""
    if level is SignalLevel.READY:
        return (
            f"🎯 <b>Сетап готов: {signal.symbol} · {signal.timeframe.upper()}</b>\n\n"
            f"{signal.setup} — {signal.direction.value}\n"
            f"Вход: {fmt_price(signal.entry_zone_low, price_precision)} – "
            f"{fmt_price(signal.entry_zone_high, price_precision)}\n"
            f"Стоп: {fmt_price(signal.stop_loss, price_precision)}\n"
            f"Цель: {fmt_price(signal.take_profit_1, price_precision)}\n"
            f"RR: 1:{fmt_decimal(signal.risk_reward)} · Качество: {signal.confidence}/10\n\n"
            f"<i>Проверь актуальность перед входом — рынок мог уйти с момента скана.</i>"
        )
    # signal.setup здесь бесполезен (см. docstring build_fingerprint) —
    # signal.note уже содержит конкретику детектора ("цена на ретесте,
    # но подтверждения нет" и т.п.), этого достаточно без имени сетапа.
    return (
        f"🌱 <b>Формируется сетап: {signal.symbol} · {signal.timeframe.upper()}</b>\n\n"
        f"{signal.note}"
    )


class SetupScanner:
    def __init__(self, bot: Bot, db: Database, settings: Settings) -> None:
        self._bot = bot
        self._db = db
        self._settings = settings
        # Сохраняем ссылку (раньше терялась в замыкании MarketDataService) —
        # SetupScanner создаётся один раз в BackgroundJobs и живёт весь
        # процесс (см. app/workers/scheduler.py), поэтому этот клиент —
        # единственное место, где троттлинг по остатку лимита (раздел
        # X-RateLimit в app/exchanges/bingx.py) реально накапливает
        # состояние между тиками, а не создаётся заново на каждый вызов,
        # как в интерактивных хендлерах бота.
        self._client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
        # Свой кэш, не общий с интерактивными хендлерами — воркеры не
        # зависят от app.bot.handlers, см. app/workers/__init__.py.
        self._market = MarketDataService(self._client, TTLCache())
        self._engine = AnalysisEngine(self._market)
        # Последний замер run() — читает DailyJobs для строки в сводке
        # исполнения (app/workers/execution_digest.py). None, пока сканер
        # не отработал ни разу после старта процесса.
        self.last_cycle: ScanCycleStats | None = None

    async def run(self) -> None:
        """Один полный проход по всем пользователям.

        Замеряет символы/запросы/время цикла (раздел "троттлинг сканера") —
        request_count берётся diff'ом с self._client до/после: клиент
        общий и растущий, а не сбрасывается на каждый run() (см. коммент
        у self._client в __init__)."""
        started = time.monotonic()
        requests_before = getattr(self._client, "request_count", 0)
        symbols_scanned = 0

        async with self._db.session() as session:
            users = await UserRepository(session).list_active_with_plan()
            for user in users:
                symbols_scanned += await self._scan_user(session, user)
            await session.flush()

        requests_made = getattr(self._client, "request_count", 0) - requests_before
        duration_seconds = time.monotonic() - started
        self.last_cycle = ScanCycleStats(
            symbols_scanned=symbols_scanned,
            requests_made=requests_made,
            duration_seconds=duration_seconds,
        )
        logger.info(
            "Цикл сканера завершён",
            extra={
                "symbols_scanned": symbols_scanned,
                "requests_made": requests_made,
                "duration_seconds": round(duration_seconds, 2),
            },
        )

    async def _scan_user(self, session: AsyncSession, user: User) -> int:
        """Возвращает число реально просканированных символов у этого
        пользователя (0 — нет плана, список пуст, или оба уровня
        уведомлений выключены) — run() суммирует это в symbols_scanned."""
        plan = user.trading_plan
        if plan is None or not plan.allowed_symbols:
            return 0

        want_ready = notification_enabled(user.settings, "setup_ready")
        want_forming = notification_enabled(user.settings, "setup_forming")
        if not want_ready and not want_forming:
            return 0

        repo = SignalRepository(session)
        symbols_scanned = 0
        for symbol in plan.allowed_symbols:
            symbols_scanned += 1
            for timeframe in SCAN_TIMEFRAMES:
                try:
                    # build_context/evaluate вместо analyze(): графику
                    # нужен тот же MarketContext, что видел детектор, а
                    # analyze() его не отдаёт наружу.
                    context = await self._engine.build_context(symbol, timeframe)
                    signal = (
                        self._engine.evaluate(context)
                        if context is not None
                        else wait_signal(
                            symbol, timeframe,
                            "Недостаточно рыночных данных для анализа.",
                        )
                    )
                except Exception:
                    logger.exception(
                        "Скан инструмента упал",
                        extra={"user_id": user.id, "symbol": symbol, "timeframe": timeframe},
                    )
                    continue
                await self._handle_signal(
                    repo, user, signal, symbol, timeframe, want_ready, want_forming, context
                )
        return symbols_scanned

    async def _price_precision(self, symbol: str) -> int | None:
        """SymbolInfo.price_precision для подписей цен на графике.

        Запрашивается только когда график реально рисуется. Список
        инструментов кэшируется на час (TTL_SYMBOLS) в кэше сканера — один
        запрос в час на процесс. Недоступность биржи или отсутствие символа
        в списке не должны лишать уведомление графика: None откатывает
        подписи на fmt_price по порядку величины цены. Не молча — в лог."""
        try:
            info = await self._engine.get_symbol_info(symbol)
        except Exception:
            logger.warning(
                "Не удалось получить точность цены символа, подписи графика по "
                "порядку величины",
                extra={"symbol": symbol},
                exc_info=True,
            )
            return None
        return info.price_precision if info is not None else None

    async def _handle_signal(
        self,
        repo: SignalRepository,
        user: User,
        signal: Signal,
        symbol: str,
        timeframe: str,
        want_ready: bool,
        want_forming: bool,
        context: MarketContext | None = None,
    ) -> None:
        level = classify_signal(signal)
        wanted = {SignalLevel.READY: want_ready, SignalLevel.FORMING: want_forming}

        if level is None or not wanted[level]:
            # Сетапа нет (или он есть, но пользователь отключил именно этот
            # уровень) — гасим оба возможных активных слота немедленно, не
            # дожидаясь TTL.
            for lvl, enabled in wanted.items():
                if enabled:
                    await repo.expire_active_slot(user.id, symbol, timeframe, lvl)
            # Сессия открыта с autoflush=False (см. Database.session) —
            # без явного flush следующий скан того же слота в этом же
            # цикле не увидит только что погашенную строку.
            await repo.flush()
            return

        # Сетап дозрел до READY или откатился до FORMING — слот другого
        # уровня для этой же пары больше не актуален.
        other = SignalLevel.FORMING if level is SignalLevel.READY else SignalLevel.READY
        if wanted[other]:
            await repo.expire_active_slot(user.id, symbol, timeframe, other)

        fingerprint = build_fingerprint(signal, level)
        # Любой статус, не только ACTIVE: uq_signal_slot не включает status,
        # так что EXPIRED-строка слота всё ещё занимает уникальный ключ —
        # её нужно переиспользовать (UPDATE), иначе INSERT ниже словит
        # IntegrityError, когда сетап появляется снова после того как погас.
        existing = await repo.get_slot(user.id, symbol, timeframe, level)
        now = datetime.now(UTC)
        ttl = timedelta(hours=self._settings.setup_scanner_ttl_hours)

        should_notify = False
        if existing is None:
            record = SignalRecord(
                user_id=user.id, symbol=symbol, timeframe=timeframe, level=level
            )
            repo.add(record)
            should_notify = True
        elif existing.status is not SignalRecordStatus.ACTIVE:
            # Слот был погашен и сетап появился заново (требование 3).
            record = existing
            should_notify = True
        elif level is SignalLevel.READY:
            # Шаг 15.5.2а, Р1: READY сверяется с последним ОТПРАВЛЕННЫМ
            # уведомлением, а не со слотом. expires_at слота продлевается
            # каждым пересканом, и сетап, видимый непрерывно, не уведомлялся
            # бы повторно никогда, — а кнопка из снимка живёт ровно TTL.
            # Заодно недоставленное уведомление (снимка нет) повторится на
            # следующем скане, а не потеряется.
            record = existing
            latest = await SignalNotificationRepository(repo.session).latest_for_slot(
                existing.id
            )
            should_notify = (
                latest is None
                or latest.fingerprint != fingerprint
                or now >= latest.expires_at
            )
        else:
            # FORMING кнопки не несёт — прежнее правило по слоту: условия
            # изменились либо слот провисел дольше TTL без пересканов.
            record = existing
            should_notify = (
                existing.fingerprint != fingerprint or now >= existing.expires_at
            )

        record.status = SignalRecordStatus.ACTIVE
        # signal.setup у WAIT/FORMING всегда "Нет сетапа" (см. docstring
        # build_fingerprint) — берём note, но обрезаем: колонка String(64),
        # а note — законченное предложение, которое туда не влезает целиком.
        record.setup = (signal.setup if level is SignalLevel.READY else signal.note)[:64]
        record.direction = signal.direction if level is SignalLevel.READY else None
        record.fingerprint = fingerprint
        record.entry_low = signal.entry_zone_low
        record.entry_high = signal.entry_zone_high
        record.stop_loss = signal.stop_loss
        record.take_profit = signal.take_profit_1
        record.confidence = signal.confidence if level is SignalLevel.READY else None
        # Точность нужна и карточке, и графику — запрашивается один раз (кэш
        # символов живёт час). FORMING цен в тексте не показывает.
        precision = (
            await self._price_precision(symbol) if level is SignalLevel.READY else None
        )
        record.detail = render_detail(signal, level, precision)
        record.expires_at = now + ttl
        if should_notify:
            record.notified_at = now

        # Как и в ветке выше: autoflush=False, следующий слот в этом же
        # цикле (или expire_active_slot другого уровня) должен видеть эту
        # запись, а не только committed-состояние из прошлого тика.
        await repo.flush()

        if should_notify:
            await self._notify(
                repo, user, record, signal, level, context, precision,
                notified_at=now, expires_at=now + ttl,
            )

    async def _notify(
        self,
        repo: SignalRepository,
        user: User,
        record: SignalRecord,
        signal: Signal,
        level: SignalLevel,
        context: MarketContext | None,
        precision: int | None,
        *,
        notified_at: datetime,
        expires_at: datetime,
    ) -> None:
        """Снимок уведомления + отправка (шаг 15.5.2а).

        SAVEPOINT вокруг вставки и отправки: в signal_notifications остаются
        только реально ушедшие сообщения. Недоставка (send_* вернул False)
        откатывает снимок; слот при этом уже обновлён — как и до 15.5.2а.
        record.id назначен предыдущим repo.flush() в _handle_signal."""
        notifications = SignalNotificationRepository(repo.session)
        try:
            async with repo.session.begin_nested():
                notification = notifications.add(
                    SignalNotification.snapshot_of(
                        record, notified_at=notified_at, expires_at=expires_at
                    )
                )
                await notifications.flush()

                # Раздел 5 ТЗ: кнопка входа только под READY, под FORMING —
                # никогда. Адресует снимок, а не слот.
                keyboard = (
                    open_trade_button(notification.id) if level is SignalLevel.READY else None
                )

                photo = None
                if context is not None and notification_enabled(user.settings, "setup_charts"):
                    if precision is None:
                        precision = await self._price_precision(record.symbol)
                    # В отдельном потоке: matplotlib/mplfinance синхронны и
                    # заметно тяжелее текста — рендер не должен задерживать
                    # остальных пользователей в этом цикле сканера.
                    photo = await asyncio.to_thread(
                        render_setup_chart, context, signal, level, precision
                    )
                if photo is not None:
                    delivered = await send_notification_photo(
                        self._bot, user.telegram_id, photo, record.detail, reply_markup=keyboard
                    )
                else:
                    delivered = await send_notification(
                        self._bot, user.telegram_id, record.detail, reply_markup=keyboard
                    )
                if not delivered:
                    raise _NotDeliveredError
        except _NotDeliveredError:
            logger.warning(
                "Уведомление о сетапе не доставлено — снимок не сохранён",
                extra={"user_id": user.id, "signal_id": record.id, "level": level.value},
            )
