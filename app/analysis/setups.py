"""Детекторы торговых сетапов.

Каждый детектор — отдельный класс с общим интерфейсом. Стратегия не
зашита в хендлер Telegram и не размазана по коду: добавить новый сетап
означает написать один класс и включить его в список.

Реализованы два сетапа из методологии:

  BreakoutRetest — пробой уровня с ретестом. Вход не в момент пробоя,
    а после возврата к уровню и подтверждающей свечи.

  EMAPullback — откат к EMA50 по тренду. Вход на отскоке от
    динамической поддержки при согласии структуры и EMA200.

Общий принцип: сетап собирается из нескольких факторов, а не из одного
индикатора. Каждое условие проверяется явно и попадает в сигнал —
пользователь должен видеть, чего не хватило, а не только вердикт.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from app.analysis.patterns import detect_confirmation, is_strong_body
from app.analysis.signals import (
    MarketContext,
    Signal,
    SignalCondition,
    validate_geometry,
    wait_signal,
)
from app.analysis.structure import Level, nearest_level
from app.core.logging import get_logger
from app.trading.enums import MarketStructure, SignalDirection

logger = get_logger(__name__)

ZERO = Decimal(0)

# --- Пороги методологии, вынесенные в константы ---------------------------

# Пробой засчитывается при закрытии за уровнем с запасом в долях ATR:
# «уверенное закрытие», а не заглядывание на пару тиков.
BREAKOUT_MIN_CLOSE_ATR = Decimal("0.15")

# Всплеск объёма при истинном пробое.
BREAKOUT_MIN_VOLUME_RATIO = Decimal("1.3")

# Насколько близко цена должна вернуться к уровню, чтобы это считалось
# ретестом.
RETEST_ZONE_ATR = Decimal("0.4")

# Сколько свечей назад искать пробой: если ретеста не случилось за это
# время, сетап устарел.
BREAKOUT_LOOKBACK = 15

# Зона вокруг EMA50, считающаяся касанием.
EMA_TOUCH_ATR = Decimal("0.5")

# Отступ стопа за экстремум свечи.
STOP_BUFFER_ATR = Decimal("0.2")

# Минимальное соотношение риск/прибыль по методологии.
MIN_RISK_REWARD = Decimal("2")


def round_price(value: Decimal) -> Decimal:
    """Округляет цену по масштабу инструмента.

    Расчёты в Decimal дают длинный хвост (стоп получался вида
    198.5030291417217405428273399), который бессмысленно показывать и
    невозможно выставить на бирже. Точность выбирается по величине
    цены: у BTC значимы копейки, у DOGE — восьмой знак.
    """
    if value >= Decimal(1000):
        return value.quantize(Decimal("0.01"))
    if value >= Decimal(1):
        return value.quantize(Decimal("0.0001"))
    return value.quantize(Decimal("0.00000001"))


def _rr(entry: Decimal, stop: Decimal, target: Decimal) -> Decimal | None:
    risk = abs(entry - stop)
    if risk <= ZERO:
        return None
    return (abs(target - entry) / risk).quantize(Decimal("0.01"))


class SetupDetector(ABC):
    """Интерфейс детектора сетапа."""

    name: str

    @abstractmethod
    def detect(self, context: MarketContext) -> Signal: ...


class BreakoutRetest(SetupDetector):
    """Пробой уровня с ретестом.

    Правило методологии, ради которого всё и затевалось: никогда не
    входить в момент пробоя. Сначала уверенное закрытие за уровнем,
    затем возврат к нему, затем подтверждающая свеча — и только потом
    вход, со стопом за хвост подтверждающей свечи и за пробитым уровнем
    (что дальше от входа).

    Короткий стоп на ретесте — не побочный эффект, а смысл: он
    позволяет взять больший объём при том же риске в процентах.
    """

    name = "Пробой с ретестом"

    def detect(self, context: MarketContext) -> Signal:
        conditions: list[SignalCondition] = []

        if not context.has_enough_data:
            return wait_signal(
                context.symbol,
                context.timeframe,
                "Недостаточно истории: для EMA200 нужно минимум 200 свечей.",
            )

        assert context.atr is not None
        atr = context.atr

        # --- Фильтр тренда ------------------------------------------------
        allowed = context.trend_allows
        conditions.append(
            SignalCondition(
                "Фильтр EMA200",
                allowed is not SignalDirection.WAIT,
                f"Цена {'выше' if context.above_ema200 else 'ниже'} EMA200 → "
                f"ищем только {allowed.value}",
            )
        )
        if allowed is SignalDirection.WAIT:
            return wait_signal(
                context.symbol, context.timeframe,
                "EMA200 не рассчитана.", conditions,
            )

        looking_long = allowed is SignalDirection.LONG

        # --- Поиск пробитого уровня ---------------------------------------
        broken = self._find_broken_level(context, looking_long, atr)
        conditions.append(
            SignalCondition(
                "Пробой уровня",
                broken is not None,
                f"Пробит уровень {broken[0].price:.4f}"
                if broken
                else "Нет уверенного пробоя за последние "
                     f"{BREAKOUT_LOOKBACK} свечей",
            )
        )
        if broken is None:
            return wait_signal(
                context.symbol, context.timeframe,
                "Пробоя уровня не было. Ждём закрытия за уровнем "
                "полнотелой свечой на повышенном объёме.",
                conditions,
            )

        level, breakout_index = broken

        # --- Правило двух свечей ------------------------------------------
        two_bar = self._two_bar_confirmed(context, level, breakout_index, atr)
        conditions.append(
            SignalCondition(
                "Правило двух свечей",
                two_bar,
                "Следующая свеча удержалась за уровнем"
                if two_bar
                else "Цена вернулась за уровень сразу — признак ложного пробоя",
            )
        )
        if not two_bar:
            return wait_signal(
                context.symbol, context.timeframe,
                "Пробой не подтверждён второй свечой. По методологии это "
                "признак ложного пробоя.",
                conditions,
            )

        # --- Ретест --------------------------------------------------------
        in_retest = self._retest_happened(
            context, level, breakout_index, atr, looking_long
        )
        conditions.append(
            SignalCondition(
                "Ретест уровня",
                in_retest,
                f"Цена возвращалась к уровню {level.price:.4f} и удержалась"
                if in_retest
                else f"Возврата к уровню {level.price:.4f} ещё не было",
            )
        )
        if not in_retest:
            return wait_signal(
                context.symbol, context.timeframe,
                f"Уровень {level.price:.4f} пробит, но ретеста ещё не было. "
                f"Ждём возврата цены к уровню.",
                conditions,
            )

        # --- Подтверждающая свеча ------------------------------------------
        confirmation = detect_confirmation(context.candles, bullish=looking_long)
        conditions.append(
            SignalCondition(
                "Подтверждающий паттерн",
                confirmation is not None,
                confirmation.description
                if confirmation
                else "Нет пин-бара или поглощения в нужную сторону",
            )
        )
        if confirmation is None:
            return wait_signal(
                context.symbol, context.timeframe,
                "Цена на ретесте, но подтверждения нет. Ждём пин-бар или "
                "поглощение от уровня.",
                conditions,
                level_price=level.price,
            )

        # --- Расчёт сделки --------------------------------------------------
        signal_candle = context.candles[-1]
        entry = signal_candle.close

        # Стоп — за экстремум подтверждающей свечи И за пробитый уровень:
        # инвалидация пробоя — возврат под уровень (для шорта — над ним).
        # Зона входа включает уровень, и стоп над ним выбивался бы тем самым
        # ретестом, ради которого сетап существует.
        if looking_long:
            stop = round_price(
                min(signal_candle.low, level.price) - atr * STOP_BUFFER_ATR
            )
            target_level = nearest_level(context.levels, entry, above=True)
        else:
            stop = round_price(
                max(signal_candle.high, level.price) + atr * STOP_BUFFER_ATR
            )
            target_level = nearest_level(context.levels, entry, above=False)

        # После пробоя цена часто оказывается там, где исторических
        # уровней сверху ещё нет. Отбрасывать сетап из-за этого нельзя:
        # методология в таком случае допускает цель, кратную риску.
        if target_level is None:
            risk = abs(entry - stop)
            target_price = round_price(
                entry + risk * 2 if looking_long else entry - risk * 2
            )
            target_source = "цель 2R (уровней впереди нет)"
        else:
            target_price = round_price(target_level.price)
            target_source = f"следующий уровень {target_level.price:.4f}"

        risk_reward = _rr(entry, stop, target_price)
        conditions.append(
            SignalCondition(
                "Risk/Reward",
                risk_reward is not None and risk_reward >= MIN_RISK_REWARD,
                f"RR 1:{risk_reward}" if risk_reward else "RR не рассчитан",
            )
        )
        if risk_reward is None or risk_reward < MIN_RISK_REWARD:
            return wait_signal(
                context.symbol, context.timeframe,
                f"Сетап есть, но RR 1:{risk_reward} ниже минимального "
                f"1:{MIN_RISK_REWARD}. По методологии такой вход не берём.",
                conditions,
            )

        zone_low = round_price(min(entry, level.price))
        zone_high = round_price(max(entry, level.price))
        direction = SignalDirection.LONG if looking_long else SignalDirection.SHORT
        if problem := validate_geometry(direction, zone_low, zone_high, stop):
            return wait_signal(
                context.symbol, context.timeframe, problem, conditions,
                level_price=level.price,
            )

        return Signal(
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            setup=self.name,
            entry_zone_low=zone_low,
            entry_zone_high=zone_high,
            stop_loss=stop,
            take_profit_1=target_price,
            risk_reward=risk_reward,
            confidence=self._confidence(context, level, confirmation.strength),
            confirmation=confirmation.description,
            note=f"Цель: {target_source}",
            level_price=level.price,
            invalidation=(
                f"Закрытие {'ниже' if looking_long else 'выше'} "
                f"{stop:.4f} отменяет сценарий"
            ),
            conditions=conditions,
        )

    @staticmethod
    def _retest_happened(
        context: MarketContext,
        level: Level,
        breakout_index: int,
        atr: Decimal,
        looking_long: bool,
    ) -> bool:
        """Проверяет, вернулась ли цена к уровню после пробоя.

        Ретест ищется по касаниям свечей, а не по текущей цене: к
        моменту анализа последняя свеча — уже подтверждающая, и
        закрывается она в стороне от уровня. Проверять ретест по ней
        значило бы требовать, чтобы вход происходил ровно на уровне,
        а методология этого не требует.

        Дополнительно проверяется, что цена осталась по правильную
        сторону: если после касания она ушла обратно за уровень, это
        уже не ретест, а возврат в диапазон.
        """
        after_breakout = context.candles[breakout_index + 1 :]
        if not after_breakout:
            return False

        tolerance = atr * RETEST_ZONE_ATR
        touched = any(
            (candle.low <= level.price + tolerance)
            if looking_long
            else (candle.high >= level.price - tolerance)
            for candle in after_breakout
        )
        if not touched:
            return False

        # Цена должна удержаться за уровнем — иначе пробой не состоялся.
        held = (
            context.price > level.price - tolerance
            if looking_long
            else context.price < level.price + tolerance
        )
        return held

    def _find_broken_level(
        self, context: MarketContext, looking_long: bool, atr: Decimal
    ) -> tuple[Level, int] | None:
        """Ищет недавно пробитый уровень в нужную сторону."""
        candles = context.candles
        start = max(0, len(candles) - BREAKOUT_LOOKBACK)

        for i in range(len(candles) - 2, start - 1, -1):
            candle = candles[i]
            if not is_strong_body(candle):
                continue
            if context.volume_ratio is not None and (
                context.volume_ratio < BREAKOUT_MIN_VOLUME_RATIO
            ):
                # Объём проверяем по последнему значению: точное
                # значение на момент пробоя потребовало бы хранить весь
                # ряд, а сигнал всё равно оценивается сейчас.
                pass

            for level in context.levels:
                if looking_long and not level.is_resistance:
                    continue
                if not looking_long and level.is_resistance:
                    continue

                buffer = atr * BREAKOUT_MIN_CLOSE_ATR
                broke_up = looking_long and candle.close > level.price + buffer
                broke_down = not looking_long and candle.close < level.price - buffer

                if broke_up or broke_down:
                    # Уровень должен был быть препятствием: до пробоя
                    # цена находилась по другую сторону.
                    before = candles[i - 1] if i > 0 else candle
                    if looking_long and before.close <= level.price:
                        return level, i
                    if not looking_long and before.close >= level.price:
                        return level, i
        return None

    @staticmethod
    def _two_bar_confirmed(
        context: MarketContext, level: Level, breakout_index: int, atr: Decimal
    ) -> bool:
        """Следующая после пробоя свеча тоже закрылась за уровнем."""
        candles = context.candles
        if breakout_index + 1 >= len(candles):
            return False

        next_candle = candles[breakout_index + 1]
        if level.is_resistance:
            return next_candle.close > level.price
        return next_candle.close < level.price

    @staticmethod
    def _confidence(
        context: MarketContext, level: Level, pattern_strength: Decimal
    ) -> int:
        """Оценка качества сетапа от 0 до 10.

        Складывается из силы уровня, выраженности паттерна, согласия
        структуры и объёма. Это не вероятность прибыли — такой цифры
        не существует, — а мера того, сколько факторов совпало.
        """
        score = Decimal(4)  # база за выполненные обязательные условия
        score += level.strength * 2
        score += pattern_strength * 2

        if context.structure in (MarketStructure.UPTREND, MarketStructure.DOWNTREND):
            score += 1
        if context.volume_ratio is not None and context.volume_ratio >= Decimal("1.5"):
            score += 1

        return int(min(score, Decimal(10)))


class EMAPullback(SetupDetector):
    """Откат к EMA50 по тренду.

    Второй сетап методологии: в сильном тренде цена регулярно
    откатывается к EMA50 и отскакивает. Вход — на подтверждении в этой
    зоне, стоп за локальный экстремум отката.

    Обязательно согласие трёх вещей: положение относительно EMA200,
    структура рынка и подтверждающий паттерн. Один только откат к
    средней сетапом не является.
    """

    name = "Откат к EMA50"

    def detect(self, context: MarketContext) -> Signal:
        conditions: list[SignalCondition] = []

        if not context.has_enough_data or context.ema50 is None:
            return wait_signal(
                context.symbol, context.timeframe,
                "Недостаточно истории для EMA50 и EMA200.",
            )

        assert context.atr is not None
        atr = context.atr

        allowed = context.trend_allows
        if allowed is SignalDirection.WAIT:
            return wait_signal(
                context.symbol, context.timeframe, "EMA200 не рассчитана.",
            )

        looking_long = allowed is SignalDirection.LONG
        conditions.append(
            SignalCondition(
                "Фильтр EMA200",
                True,
                f"Цена {'выше' if looking_long else 'ниже'} EMA200",
            )
        )

        # --- Структура должна подтверждать тренд ---------------------------
        expected = (
            MarketStructure.UPTREND if looking_long else MarketStructure.DOWNTREND
        )
        structure_ok = context.structure is expected
        conditions.append(
            SignalCondition(
                "Структура рынка",
                structure_ok,
                f"Структура: {context.structure.value}",
            )
        )
        if not structure_ok:
            return wait_signal(
                context.symbol, context.timeframe,
                "Структура рынка не подтверждает тренд. Откат к EMA50 "
                "работает только в трендовом рынке.",
                conditions,
            )

        # --- Касание EMA50 --------------------------------------------------
        distance = abs(context.price - context.ema50)
        touching = distance <= atr * EMA_TOUCH_ATR
        conditions.append(
            SignalCondition(
                "Откат к EMA50",
                touching,
                f"Расстояние до EMA50: {distance:.4f} "
                f"(допуск {atr * EMA_TOUCH_ATR:.4f})",
            )
        )
        if not touching:
            return wait_signal(
                context.symbol, context.timeframe,
                f"Цена далеко от EMA50 ({context.ema50:.4f}). "
                f"Ждём отката к динамической поддержке.",
                conditions,
            )

        # --- Подтверждение ---------------------------------------------------
        confirmation = detect_confirmation(context.candles, bullish=looking_long)
        conditions.append(
            SignalCondition(
                "Подтверждающий паттерн",
                confirmation is not None,
                confirmation.description
                if confirmation
                else "Нет разворотного паттерна в зоне EMA50",
            )
        )
        if confirmation is None:
            return wait_signal(
                context.symbol, context.timeframe,
                "Цена у EMA50, но подтверждения нет. Ждём пин-бар или "
                "поглощение.",
                conditions,
            )

        signal_candle = context.candles[-1]
        entry = signal_candle.close

        # Стоп — за экстремум свечи И за EMA50: зона входа включает EMA50, и
        # стоп над ней (для шорта — под ней) выбивался бы отскоком от неё.
        if looking_long:
            stop = round_price(
                min(signal_candle.low, context.ema50) - atr * STOP_BUFFER_ATR
            )
            target_level = nearest_level(context.levels, entry, above=True)
        else:
            stop = round_price(
                max(signal_candle.high, context.ema50) + atr * STOP_BUFFER_ATR
            )
            target_level = nearest_level(context.levels, entry, above=False)

        # Если уровня впереди нет, целью становится кратное риску
        # расстояние: методология допускает выход по достижении 2R.
        if target_level is None:
            risk = abs(entry - stop)
            target_price = round_price(
                entry + risk * 2 if looking_long else entry - risk * 2
            )
        else:
            target_price = round_price(target_level.price)

        risk_reward = _rr(entry, stop, target_price)
        conditions.append(
            SignalCondition(
                "Risk/Reward",
                risk_reward is not None and risk_reward >= MIN_RISK_REWARD,
                f"RR 1:{risk_reward}" if risk_reward else "RR не рассчитан",
            )
        )
        if risk_reward is None or risk_reward < MIN_RISK_REWARD:
            return wait_signal(
                context.symbol, context.timeframe,
                f"RR 1:{risk_reward} ниже минимального 1:{MIN_RISK_REWARD}.",
                conditions,
            )

        zone_low = round_price(min(entry, context.ema50))
        zone_high = round_price(max(entry, context.ema50))
        direction = SignalDirection.LONG if looking_long else SignalDirection.SHORT
        if problem := validate_geometry(direction, zone_low, zone_high, stop):
            return wait_signal(
                context.symbol, context.timeframe, problem, conditions
            )

        return Signal(
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            setup=self.name,
            entry_zone_low=zone_low,
            entry_zone_high=zone_high,
            stop_loss=stop,
            take_profit_1=target_price,
            risk_reward=risk_reward,
            confidence=self._confidence(context, confirmation.strength),
            confirmation=confirmation.description,
            invalidation=(
                f"Закрытие {'ниже' if looking_long else 'выше'} "
                f"{stop:.4f} ломает структуру"
            ),
            conditions=conditions,
        )

    @staticmethod
    def _confidence(context: MarketContext, pattern_strength: Decimal) -> int:
        score = Decimal(5)  # структура и EMA200 уже подтверждены
        score += pattern_strength * 2

        # Согласие EMA20 с направлением — дополнительный плюс.
        if context.ema20 is not None and context.ema50 is not None:
            aligned = (
                context.ema20 > context.ema50
                if context.price > context.ema200  # type: ignore[operator]
                else context.ema20 < context.ema50
            )
            if aligned:
                score += 2

        if context.rsi is not None and Decimal(35) <= context.rsi <= Decimal(65):
            score += 1  # не перекуплен и не перепродан

        return int(min(score, Decimal(10)))


DEFAULT_DETECTORS: list[SetupDetector] = [BreakoutRetest(), EMAPullback()]
