"""Приветствие /start описывает то, что бот умеет сейчас (18.10).

До 18.10 в нём оставалось «ищу сетапы по твоей методологии» — сканер и сигналы
удалены 02.10, текст молча устарел. Тест держит две вещи: слов про сканер нет,
и у каждого пункта приветствия есть раздел меню (или кнопка Mini App), куда
он ведёт.
"""

from app.bot.handlers.common import GREETING
from app.bot.keyboards.main import main_menu
from app.main import MINIAPP_BUTTON_TEXT


def _menu_texts() -> list[str]:
    return [b.text for row in main_menu().inline_keyboard for b in row]


def test_greeting_has_no_scanner_words() -> None:
    low = GREETING.lower()
    for word in ("сетап", "сигнал", "методолог", "скан"):
        assert word not in low, word


def test_greeting_points_lead_to_existing_sections() -> None:
    menu = _menu_texts()
    # пункт приветствия → раздел меню, куда он ведёт
    points = {
        "открываю сделки": "📝 Добавить сделку",
        "показываю открытые позиции": "💼 Позиции",
        "сделки с биржи в журнал": "💼 Позиции",   # «📥 В журнал» — на экране позиций
        "статистику": "📊 Статистика",
        "просадку": "📉 Просадка",
        "дневной лимит риска": "🧮 Риск",
        "разбираю ошибки": "🧠 Анализ ошибок",
    }
    for phrase, button in points.items():
        assert phrase in GREETING, phrase
        assert button in menu, button


def test_greeting_names_miniapp_button() -> None:
    assert f"Mini App «{MINIAPP_BUTTON_TEXT}»" in GREETING


def test_greeting_bullets_count() -> None:
    # новый пункт — новая строка в points выше
    assert GREETING.count("• ") == 5
