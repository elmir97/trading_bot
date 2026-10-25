// Вкладка «Позиции» (П.1): только просмотр. Позиции счёта Mini App со стопом/тейком
// из openOrders, связь с журналом, «Только в журнале» (все счета с пометкой). Действия
// с позицией — экран 5 (actions.js): «Действия →», у позиции без стопа ещё «Поставить
// стоп →» (сразу редактор стопа). Действия в Mini App выключены (/api/me actions.enabled
// false, MINIAPP_ACTIONS_ENABLED) — те же кнопки открывают экран действий позиции в боте
// ссылкой t.me/<бот>?start=pos-… (параметр строит сервер).
//
// Опрос: GET /api/positions раз в POLL_MS цепочкой setTimeout (следующий — после ответа,
// один в полёте). Сервер держит кэш 5 с на пользователя (app/api/positions.py): к BingX
// не чаще positions + openOrders раз в 5 с, сколько бы экранов ни смотрели. Ошибка —
// пауза 10 → 20 → 30 с, прежние цифры остаются с пометкой; вкладка скрыта или ушли с
// экрана — опрос стоит, запрос в полёте отменяется. 401 — «Сессия истекла» (onFatal).

import { ApiError } from "./api.js";
import { groupDigits } from "./render.js";

export const POLL_MS = 5000;
export const MAX_BACKOFF_MS = 30000;
const CHAT_URL = "https://t.me/";

function $(id) {
  return document.getElementById(id);
}

function text(value) {
  return value === null || value === undefined ? "—" : String(value);
}

function short(symbol) {
  return String(symbol).replace(/-USDT$/, "");
}

export function accountLabel(mode) {
  if (mode === "LIVE") return "реальный счёт";
  if (mode === "DEMO") return "DEMO";
  return "без счёта";
}

// Ссылка в чат бота; имени бота нет (getMe не прочитан, стенд) — null, кнопки нет.
export function chatLink(username, param) {
  return username && param ? `${CHAT_URL}${username}?start=${param}` : null;
}

// Действия в Mini App включены сервером (/api/me → actions.enabled); нет поля (старый
// сервер) — выключены: кнопки ведут в чат.
export function actionsEnabled(meData) {
  return Boolean(meData && meData.actions && meData.actions.enabled);
}

// Как _level_text в чате: «нет», «1.3402 (на всю позицию)», «2 ордера: …; …».
export function levelText(orders) {
  if (!orders || !orders.length) return "нет";
  const one = (o) => `${o.price} (${o.close_position ? "на всю позицию" : text(o.quantity)})`;
  if (orders.length === 1) return one(orders[0]);
  return `${orders.length} ордера: ${orders.map(one).join("; ")}`;
}

// Цвет PnL — по знаку, который отдал сервер (pnl_sign: 1 / -1 / 0; Д.6): плюс — синий,
// минус — красный, ноль — muted. Текст со знаком — тоже сервера (pnl_text).
export function pnlClass(sign) {
  if (sign === 1) return "up";
  if (sign === -1) return "down";
  return "flat";
}

// Полоска стоп — вход — тейк (Д.6): доли 0..1 и подписи — сервера (bar); фронт только
// ставит --at (CSS: left: calc(var(--at) * 100%)). Нет bar — полоски нет.
export function positionBar(bar) {
  if (!bar) return null;
  const box = document.createElement("div");
  box.className = "position-bar";
  const track = document.createElement("div");
  track.className = "position-bar-track";
  track.dataset.entryAt = String(bar.entry_at);
  track.dataset.markAt = String(bar.mark_at);
  const risk = document.createElement("span");
  risk.className = "position-bar-risk";
  risk.style.setProperty("--at", String(bar.entry_at));
  const entry = document.createElement("span");
  entry.className = "position-bar-entry";
  entry.style.setProperty("--at", String(bar.entry_at));
  const mark = document.createElement("span");
  mark.className = `position-bar-mark${bar.mark_edge ? ` edge-${bar.mark_edge}` : ""}`;
  mark.style.setProperty("--at", String(bar.mark_at));
  mark.title = bar.mark;
  track.append(risk, entry, mark);
  const labels = document.createElement("div");
  labels.className = "position-bar-labels";
  for (const value of [bar.stop, bar.entry, bar.take]) {
    const label = document.createElement("span");
    label.textContent = value;
    labels.append(label);
  }
  box.append(track, labels);
  return box;
}

export function createPositionsScreen({ api, tg, me, onSymbol, onActions, onMarket, onFatal }) {
  const el = {
    state: $("positions-state"),
    stateText: $("positions-state-text"),
    retry: $("positions-retry"),
    body: $("positions-body"),
    note: $("positions-note"),
    stale: $("positions-stale"),
    list: $("positions-list"),
    empty: $("positions-empty"),
    emptyText: $("positions-empty-text"),
    toMarket: $("positions-to-market"),
    journal: $("positions-journal"),
    journalList: $("positions-journal-list"),
  };

  let active = false;
  let loaded = false;
  let gen = 0;
  let timer = null;
  let controller = null;
  let failures = 0;

  function fatal(err) {
    if (err instanceof ApiError && err.status === 401) {
      onFatal(err);
      return true;
    }
    return false;
  }

  function showState(message, { retry = false } = {}) {
    el.stateText.textContent = message;
    el.retry.hidden = !retry;
    el.state.hidden = false;
    el.body.hidden = true;
  }

  // --- отрисовка -------------------------------------------------------------------

  function row(label, value, className) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    if (className) dd.className = className;
    return [dt, dd];
  }

  // Кнопка действий: в Mini App — data-act (код редактора или ""), выключено — data-chat.
  function actButton(label, link, editor, inApp) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `button button-small button-plain position-chat${editor ? " position-stop" : ""}`;
    if (inApp) button.dataset.act = editor || "";
    else button.dataset.chat = link;
    button.textContent = label;
    return button;
  }

  function card(p, username, inApp) {
    const item = document.createElement("li");
    // Без стопа — красная рамка и тень (макет «Позиции»).
    item.className = `position-card${p.stops.length ? "" : " danger"}`;
    item.dataset.position = `${p.symbol}:${p.side}`;

    const head = document.createElement("button");
    head.type = "button";
    head.className = "position-head";
    head.dataset.symbol = p.symbol;
    const name = document.createElement("span");
    name.className = "position-symbol";
    name.textContent = `${short(p.symbol)} · ${p.side === "LONG" ? "Лонг" : "Шорт"}`;
    const pnl = document.createElement("span");
    pnl.className = `position-pnl ${pnlClass(p.pnl_sign)}`;
    pnl.textContent = p.pnl_text ?? "—";
    head.append(name, pnl);

    const details = document.createElement("dl");
    details.className = "details position-details";
    details.append(
      ...row("Объём", `${groupDigits(p.quantity)} ${short(p.symbol)} · плечо ${p.leverage}×`),
      ...row("Вход · mark", `${text(p.entry_price)} · ${text(p.mark_price)}`),
      ...row("Стоп", levelText(p.stops), p.stops.length ? "" : "warn"),
      ...row("Тейк", levelText(p.takes)),
    );
    if (p.liquidation_price !== null) details.append(...row("Ликвидация", text(p.liquidation_price)));
    details.append(...row("Журнал", p.trade_id !== null ? `сделка #${p.trade_id}`
      : "⚠️ не в журнале"));

    item.append(head, details);
    const bar = positionBar(p.bar);
    if (bar) item.append(bar);
    const link = chatLink(username, p.start_param);
    item.dataset.chatLink = link || "";
    if (inApp || link) {
      // Выключено — экран действий позиции в чате бота (те же кнопки).
      if (!p.stops.length) item.append(actButton("Поставить стоп →", link, "sl", inApp));
      item.append(actButton("Действия →", link, null, inApp));
    } else {
      const hint = document.createElement("p");
      hint.className = "muted small";
      hint.textContent = "Действия с позицией — в чате бота: «💼 Позиции».";
      item.append(hint);
    }
    return item;
  }

  function journalItem(t) {
    const item = document.createElement("li");
    item.dataset.trade = String(t.id);
    item.textContent = `#${t.id} ${t.symbol} ${t.side} · вход ${text(t.entry_price)}`
      + ` · ${accountLabel(t.account_mode)}`;
    return item;
  }

  function render(data) {
    const notes = [];
    if (data.exchange_error) notes.push(data.exchange_error.message);
    if (data.bot_account_mode !== data.account_mode) {
      notes.push(`В боте выбран ${accountLabel(data.bot_account_mode)} — «Позиции» в чате `
        + `покажут его, здесь — ${accountLabel(data.account_mode)}.`);
    }
    el.note.textContent = notes.join(" ");
    el.note.hidden = notes.length === 0;

    const username = (me() || {}).bot_username || null;
    const inApp = actionsEnabled(me());
    el.list.replaceChildren(...data.positions.map((p) => card(p, username, inApp)));
    el.list.hidden = data.positions.length === 0;
    el.empty.hidden = data.positions.length > 0 || Boolean(data.exchange_error);
    el.emptyText.textContent = `На ${accountLabel(data.account_mode)} открытых позиций нет.`;

    el.journalList.replaceChildren(...data.journal_only.map(journalItem));
    el.journal.hidden = data.journal_only.length === 0;

    el.state.hidden = true;
    el.body.hidden = false;
  }

  // --- загрузка и опрос ------------------------------------------------------------

  function stopPolling() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (controller) controller.abort();
    controller = null;
  }

  function schedule() {
    if (!active || document.hidden || timer !== null) return;
    const delay = failures ? Math.min(POLL_MS * 2 ** failures, MAX_BACKOFF_MS) : POLL_MS;
    timer = setTimeout(tick, delay);
  }

  async function fetchOnce(myGen) {
    controller = new AbortController();
    const data = await api.get("/api/positions", { signal: controller.signal });
    if (myGen !== gen) return false;
    render(data);
    loaded = true;
    failures = 0;
    el.stale.hidden = true;
    return true;
  }

  async function load() {
    stopPolling();
    const myGen = ++gen;
    failures = 0;
    loaded = false;
    showState("Загрузка…");
    try {
      await fetchOnce(myGen);
    } catch (err) {
      if (myGen !== gen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      if (!(err instanceof ApiError)) console.error(err);
      showState(err instanceof ApiError ? err.message : "Ошибка на странице — открой заново.",
        { retry: true });
      return;
    }
    if (myGen === gen) {
      controller = null;
      schedule();
    }
  }

  async function tick() {
    timer = null;
    if (!active || document.hidden) return;
    const myGen = gen;
    try {
      await fetchOnce(myGen);
    } catch (err) {
      if (myGen !== gen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      failures += 1;
      const wait = Math.min(POLL_MS * 2 ** failures, MAX_BACKOFF_MS) / 1000;
      el.stale.textContent = `${err instanceof ApiError ? err.message : "Нет связи."} `
        + `Данные могут быть устаревшими — повторю через ${wait} с.`;
      el.stale.hidden = false;
    }
    if (myGen === gen) {
      controller = null;
      schedule();
    }
  }

  function onVisibility() {
    if (document.hidden) {
      stopPolling();
    } else if (active && loaded && timer === null && controller === null) {
      tick();
    }
  }

  function openChat(link) {
    if (typeof tg.openTelegramLink === "function" && tg.isVersionAtLeast("6.1")) {
      tg.openTelegramLink(link);
    } else {
      tg.openLink(link);
    }
  }

  el.list.addEventListener("click", (event) => {
    if (!active) return;
    const chat = event.target.closest("button[data-chat]");
    if (chat) {
      openChat(chat.dataset.chat);
      return;
    }
    const act = event.target.closest("button[data-act]");
    if (act) {
      const item = act.closest("li[data-position]");
      const [symbol, side] = item.dataset.position.split(":");
      onActions({ symbol, side, editor: act.dataset.act || null,
        chat: item.dataset.chatLink || null });
      return;
    }
    const head = event.target.closest("button[data-symbol]");
    if (head) onSymbol(head.dataset.symbol);
  });
  el.retry.addEventListener("click", () => active && load());
  el.toMarket.addEventListener("click", () => active && onMarket());

  return {
    enter() {
      if (active) return;
      active = true;
      document.addEventListener("visibilitychange", onVisibility);
      load();
    },
    leave() {
      if (!active) return;
      active = false;
      gen += 1;
      stopPolling();
      document.removeEventListener("visibilitychange", onVisibility);
      loaded = false;
    },
    refresh() {
      if (active) load();
    },
  };
}
