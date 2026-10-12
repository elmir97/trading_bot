"""Авторизация Mini App по initData (docs/miniapp-plan.md §4).

Каждый запрос — заголовок «Authorization: tma <initData>». Подпись — HMAC по
документации Telegram «Validating data received via the Mini App»; срок —
miniapp_init_data_ttl_seconds от auth_date; пользователь — только из
подписанного поля user. Никаких user_id из тела, пути или заголовков.

initData и hash в лог не пишутся никогда; telegram_id — маской.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl

FUTURE_SKEW_SECONDS = 60
EXPIRED_TEXT = "Сессия устарела — закрой и открой приложение заново."
BAD_TEXT = "Нет доступа — открой приложение из бота."


class InitDataError(Exception):
    """401: подпись, срок или форма initData. Текст — для экрана, без деталей."""

    def __init__(self, reason: str, message: str = BAD_TEXT) -> None:
        super().__init__(reason)
        self.reason = reason      # для лога: что именно не так (без данных)
        self.message = message


@dataclass(frozen=True, slots=True)
class InitData:
    telegram_id: int
    auth_date: datetime


def secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def sign(pairs: list[tuple[str, str]], bot_token: str) -> str:
    """hash для набора полей (без hash): data_check_string — пары key=value,
    отсортированные по ключу, через \\n. signature — обычное поле."""
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs))
    return hmac.new(secret_key(bot_token), check.encode(), hashlib.sha256).hexdigest()


def validate_init_data(
    raw: str, bot_token: str, *, ttl_seconds: int, now: datetime | None = None
) -> InitData:
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("unparsable") from exc
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise InitDataError("duplicate_key")
    fields = dict(pairs)
    received = fields.pop("hash", None)
    if not received:
        raise InitDataError("no_hash")
    expected = sign(list(fields.items()), bot_token)
    if not hmac.compare_digest(expected, received):
        raise InitDataError("bad_hash")

    try:
        auth_ts = int(fields["auth_date"])
    except (KeyError, ValueError) as exc:
        raise InitDataError("no_auth_date") from exc
    current = (now or datetime.now(UTC)).timestamp()
    if auth_ts > current + FUTURE_SKEW_SECONDS:
        raise InitDataError("auth_date_future", EXPIRED_TEXT)
    if current - auth_ts > ttl_seconds:
        raise InitDataError("expired", EXPIRED_TEXT)

    try:
        user = json.loads(fields["user"])
        telegram_id = user["id"]
    except (KeyError, ValueError, TypeError) as exc:
        raise InitDataError("no_user") from exc
    if not isinstance(telegram_id, int) or isinstance(telegram_id, bool) or telegram_id <= 0:
        raise InitDataError("bad_user_id")
    return InitData(telegram_id, datetime.fromtimestamp(auth_ts, tz=UTC))


__all__ = ["InitData", "InitDataError", "secret_key", "sign", "validate_init_data"]
