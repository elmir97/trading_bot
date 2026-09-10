"""Полная проверка бота перед выкаткой.

Проходит все разделы меню и все команды, отмечая проблемы. Задача —
поймать то, что не видят юнит-тесты: пустые ответы, необработанные
кнопки, застрявшие состояния, ошибки в связках между разделами.

Запуск: python -m scripts.smoke_check
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, ".")

from scripts.simulate_chat import build  # noqa: E402

problems: list[str] = []
checks = {"total": 0, "passed": 0}


def check(name: str, condition: bool, detail: str = "") -> None:
    checks["total"] += 1
    if condition:
        checks["passed"] += 1
        print(f"  ✓ {name}")
    else:
        problems.append(f"{name}: {detail}")
        print(f"  ✗ {name} — {detail}")


def has(text: str, *fragments: str) -> bool:
    return all(f.lower() in text.lower() for f in fragments)


async def add_trade(sim, symbol, side, entry, sl, tp, balance="10000"):  # type: ignore[no-untyped-def]
    await sim.send("/start")
    await sim.tap("Добавить сделку")
    await sim.tap(symbol)
    await sim.tap(side)
    await sim.send(entry)
    await sim.send(sl)
    await sim.send(tp)
    await sim.tap("Рассчитать от риска")
    await sim.send(balance)
    await sim.send("10")
    await sim.tap("ретест")
    await sim.tap("4H")
    return await sim.send("тестовый сетап")


async def close_trade(sim, symbol, exit_price, fee="10", mistakes=()):  # type: ignore[no-untyped-def]
    await sim.send("/start")
    await sim.tap("Открытые")
    await sim.tap(f"{symbol}-USDT")
    await sim.send(exit_price)
    await sim.send(fee)
    text = await sim.send("тест выхода")
    for mistake in mistakes:
        await sim.tap(mistake)
    if "ошибки" in text.lower():
        text = await sim.tap("Готово")
    return text


async def main() -> None:
    sim, tg, db = await build()

    print("\n[1] Базовые команды")
    text = await sim.send("/start")
    check("/start открывает меню", has(text, "торговый журнал", "выбери раздел"), text[:60])
    check("меню содержит все разделы", len(sim.available_buttons()) == 12,
          f"кнопок {len(sim.available_buttons())}")

    text = await sim.send("/ping")
    check("/ping отвечает", "мс" in text, text[:60])

    text = await sim.send("/help")
    check("/help перечисляет команды", has(text, "/stats", "/trade"), text[:60])

    text = await sim.send("/plan")
    check("/plan показывает план", has(text, "риск на сделку: 2%"), text[:80])
    check("/plan без хвоста нулей", "2.0000" not in text, "формат Decimal")

    print("\n[2] Пустое состояние")
    for label, fragment in [
        ("Статистика", "сделок"), ("Открытые", "нет"),
        ("Сделки", "нет"), ("Просадка", "нет"),
    ]:
        await sim.send("/start")
        text = await sim.tap(label)
        check(f"«{label}» на пустой базе", len(text) > 10, "пустой ответ")

    # «Ошибки» убрали из меню (дублировала «Анализ ошибок»), но команда
    # /mistakes с числовой статистикой по-прежнему работает.
    text = await sim.send("/mistakes")
    check("«/mistakes» на пустой базе", len(text) > 10, "пустой ответ")

    print("\n[3] Добавление сделки")
    text = await add_trade(sim, "BTC", "LONG", "100500", "99500", "102500")
    check("сделка сохраняется", has(text, "сделка записана"), text[:80])
    check("объём рассчитан", has(text, "объём: 0.2"), text[:200])
    check("риск верный", has(text, "риск: 2%"), text[:200])
    check("плановый RR", has(text, "1:2"), text[:200])

    print("\n[4] Закрытие и разметка ошибок")
    text = await close_trade(sim, "BTC", "102500", mistakes=("Ранний выход",))
    check("сделка закрывается", has(text, "сделка закрыта"), text[:80])
    check("PnL верный", "390" in text, text[:200])
    check("результат в R", "2R" in text, text[:200])
    check("ошибка записана", has(text, "ранний выход"), text[:250])

    print("\n[5] Статистика с данными")
    await sim.send("/start")
    text = await sim.tap("Статистика")
    check("win rate", has(text, "win rate: 100"), text[:150])
    check("PnL", "390" in text, text[:150])
    check("profit factor без убытков", "убыточных сделок нет" in text.lower(),
          text[:250])

    for label in ("По инструментам", "По стратегиям", "LONG / SHORT", "По времени"):
        text = await sim.tap(label)
        check(f"срез «{label}»", len(text) > 30, "пустой срез")

    print("\n[6] Убыточная сделка и метрики")
    await add_trade(sim, "ETH", "LONG", "3000", "2940", "3120")
    await close_trade(sim, "ETH", "2940", fee="5", mistakes=("FOMO",))

    await sim.send("/start")
    text = await sim.tap("Статистика")
    check("win rate пересчитан", has(text, "win rate: 50"), text[:150])
    check("profit factor появился", "profit factor:" in text.lower()
          and "нет" not in text.split("Profit Factor:")[1][:20].lower(), text[:250])
    check("просадка учтена", "просадка" in text.lower(), text[:250])

    text = await sim.send("/mistakes")
    check("анализ ошибок с деньгами", has(text, "fomo", "usdt"), text[:200])
    check("сравнение со средней", "средн" in text.lower(), text[:250])

    print("\n[7] Калькулятор риска")
    await sim.send("/start")
    await sim.tap("🧮 Риск")
    await sim.send("10000")
    await sim.tap("По плану")
    await sim.send("100.50")
    text = await sim.send("98.50")
    check("объём по методичке", has(text, "объём: 100"), text[:200])
    check("маржа по плечам", has(text, "10x"), text[:250])
    check("сумма риска без плюса", "+200" not in text, "ложный знак прибыли")

    text = await sim.send("104.50")
    check("RR рассчитан", has(text, "1:2"), text[:200])
    check("вердикт по методологии", "методолог" in text.lower(), text[:250])

    print("\n[8] Настройки")
    await sim.send("/start")
    text = await sim.tap("Настройки")
    check("настройки открываются", has(text, "риск на сделку"), text[:100])
    check("ключи не подключены", "не подключены" in text.lower(), text[:200])

    await sim.tap("Риск на сделку")
    text = await sim.send("3")
    check("риск меняется", has(text, "3%"), text[:100])
    check("предупреждение о риске", "потолка" in text.lower(), text[:200])

    await sim.send("/start")
    await sim.tap("Настройки")
    await sim.tap("Риск на сделку")
    await sim.send("2")

    print("\n[9] Биржа без ключей")
    await sim.send("/start")
    text = await sim.tap("Биржа")
    check("раздел биржи", has(text, "bingx"), text[:80])
    check("объяснение про ключи", "только на чтение" in text.lower(), text[:250])

    text = await sim.send("/balance")
    check("баланс без ключей объясняет", "не подключены" in text.lower(), text[:120])

    text = await sim.send("/import")
    check("импорт без ключей не спрашивает период",
          "не подключены" in text.lower() or "требует ключей" in text.lower(),
          text[:150])

    print("\n[10] Защита формы командой")
    await sim.send("/start")
    await sim.tap("Добавить сделку")
    await sim.tap("BTC")
    text = await sim.send("/stats")
    check("команда не попадает в форму", "направление" not in text.lower(), text[:120])
    check("форма отменяется с пояснением", "отменено" in text.lower(), text[:120])
    text = await sim.send("/stats")
    check("повторная команда работает", "win rate" in text.lower(), text[:120])

    print("\n[11] Некорректный ввод")
    await sim.send("/start")
    await sim.tap("Добавить сделку")
    await sim.tap("BTC")
    await sim.tap("LONG")
    text = await sim.send("не число")
    check("нечисловая цена отклонена", "не понял" in text.lower(), text[:100])
    await sim.send("100000")
    text = await sim.send("101000")
    check("стоп с неверной стороны отклонён", "ниже" in text.lower(), text[:120])
    await sim.send("/start")

    print("\n[12] Отчёты")
    await sim.send("/start")
    text = await sim.tap("Отчёт")
    check("сводный отчёт", has(text, "сегодня", "неделя"), text[:150])

    for command in ("/today", "/week", "/month"):
        text = await sim.send(command)
        check(f"{command}", len(text) > 20, "пустой отчёт")

    print("\n[13] Анализ рынка")
    # Раньше был заглушкой («этап 9»), теперь настоящий раздел — сеть не
    # дёргаем (данные с BingX живые), проверяем только сам экран выбора
    # инструмента, как и остальной смок-тест избегает внешних запросов.
    await sim.send("/start")
    text = await sim.tap("Анализ рынка")
    check("раздел не заглушка", "этап" not in text.lower(), text[:100])
    check("просит выбрать инструмент", has(text, "выбери инструмент"), text[:150])

    await db.dispose()

    print("\n" + "=" * 60)
    print(f"Проверок: {checks['total']}, успешно: {checks['passed']}")
    if problems:
        print(f"\nПРОБЛЕМЫ ({len(problems)}):")
        for p in problems:
            print(f"  • {p}")
    else:
        print("Проблем не найдено.")


if __name__ == "__main__":
    asyncio.run(main())
