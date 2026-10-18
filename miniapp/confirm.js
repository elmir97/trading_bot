// Экран «Подтверждение» (§5а, экран 3; план 2.5 — «да» владельца 17.10).
//
// «Открыть» с формы → POST /api/open (свежие данные биржи, строка CARD) → карточка
// сервера (тот же текст, что в чате) с обратным отсчётом до expires_at и «было → стало»
// против превью. «Подтвердить» / «Открыть всё равно» (есть WARN) → showConfirm
// (сторона, объём, риск $; LIVE — «⚠️ Реальный счёт» первой строкой) → POST
// /api/open/{id}/confirm. BLOCK на свежих данных — кнопки нет.
//
// Ответ: итог — экран итога (текст сервера, сделка #N, стоп/тейк); ещё идёт (pending) —
// «Проверяю…» и опрос GET /api/open/{id}; лок занят (busy) — кнопка снова через 2 с.
// Обрыв сети на /confirm — POST не повторяется сам (§5): опрос GET; открытие ещё CARD
// (POST не дошёл) — «Не дошло, нажми ещё раз», повтор — по тому же id (сервер отсекает
// второе открытие переходом CARD → CONFIRMED). Пока /confirm в полёте — «Назад» нет.
// Лимит выставлен — «⏳ … истекает HH:MM» и «Отменить лимит» (тоже через showConfirm).
// Карточка истекла — «Пересчитать»: новый POST /api/open с тем же телом.
// Чисел клиент не считает: всё — строки сервера; время — только для отсчёта.

import { ApiError } from "./api.js";
import {
  cancelQuestion, cardDiff, confirmQuestion, LIVE_TEXT, resultLines,
} from "./render.js";
import { paintButton } from "./theme.js";

export const POLL_MS = 2000;
export const POLL_LIMIT_MS = 180000;
export const BUSY_RETRY_MS = 2000;
export const NOT_DELIVERED_TEXT = "Не дошло, нажми ещё раз.";
const SIDE_TEXT = { LONG: "Лонг", SHORT: "Шорт" };
const clock = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });

function $(id) {
  return document.getElementById(id);
}

export function createConfirmScreen({ api, tg, me, onBack, onMarket, onFatal }) {
  const native = tg && tg.isVersionAtLeast("7.10");
  const popup = tg && tg.isVersionAtLeast("6.2");       // showConfirm — с Bot API 6.2
  const el = {
    live: $("confirm-live"),
    title: $("confirm-title"),
    timer: $("confirm-timer"),
    card: $("confirm-card"),
    diff: $("confirm-diff"),
    status: $("confirm-status"),
    action: $("confirm-action"),
    back: $("confirm-back"),
  };

  let active = false;
  let body = null;           // тело превью — уходит в POST /api/open как есть
  let preview = null;        // ответ превью — для «было → стало»
  let card = null;           // карточка сервера
  let openingId = null;
  let opening = null;        // последний статус открытия
  let mode = "idle";         // card | sending | checking | working | result | expired | refused
  let action = null;         // что делает главная кнопка
  let armed = false;         // старый клиент без showConfirm: второе нажатие подтверждает
  let gen = 0;
  let timer = null;
  let pollTimer = null;
  let pollUntil = 0;
  let controller = null;

  const live = () => me().account_mode === "LIVE";
  const confirmLabel = () => (card.has_warnings ? "Открыть всё равно" : "Подтвердить");

  // --- кнопки --------------------------------------------------------------------------

  function mainButton(textValue, { progress = false } = {}) {
    if (native) {
      if (!textValue) {
        tg.MainButton.setParams({ is_visible: false });
      } else {
        paintButton(tg.MainButton, live() ? "live" : "main",
          { text: textValue, is_active: !progress, is_visible: true });
      }
      tg.SecondaryButton.setParams({ is_visible: false });
    } else {
      el.action.textContent = textValue || "";
      el.action.disabled = progress;
      el.action.hidden = !textValue;
    }
  }

  function secondary(textValue, handler) {
    el.back.textContent = textValue || "";
    el.back.hidden = !textValue;
    el.back.onclick = textValue ? () => active && handler() : null;
  }

  function backAllowed(allowed) {
    if (tg && tg.isVersionAtLeast("6.1")) {
      if (allowed) tg.BackButton.show();
      else tg.BackButton.hide();
    }
  }

  function setAction(textValue, handler, options) {
    action = handler;
    armed = false;
    mainButton(textValue, options);
  }

  function status(message) {
    el.status.textContent = message || "";
    el.status.hidden = !message;
  }

  // --- таймеры -------------------------------------------------------------------------

  function stopTimers() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = null;
    if (controller) controller.abort();
    controller = null;
  }

  // Отсчёт до expires_at сервера; дошёл до нуля — «Пересчитать».
  function countdown(expiresAt) {
    const left = Math.ceil((Date.parse(expiresAt) - Date.now()) / 1000);
    if (!Number.isFinite(left)) {
      el.timer.hidden = true;
      return;
    }
    if (left <= 0) {
      expired("⌛ Карточка устарела — цены могли уйти. Пересчитай.");
      return;
    }
    el.timer.textContent = `Карточка действует ещё ${left} с`;
    el.timer.hidden = false;
    timer = setTimeout(() => countdown(expiresAt), 1000);
  }

  // --- состояния -----------------------------------------------------------------------

  function showCard(next) {
    card = next.card;
    openingId = next.opening_id;
    el.card.textContent = card.text;
    const diff = cardDiff(preview, card);
    el.diff.replaceChildren(...diff.map((line) => {
      const item = document.createElement("li");
      item.textContent = `Карточка разошлась с превью — ${line}`;
      return item;
    }));
    el.diff.hidden = diff.length === 0;
    if (!openingId || next.status !== "CARD" || !card.can_open) {
      // Отказ или BLOCK на свежих данных — кнопки нет (решение 05.10).
      mode = "refused";
      el.timer.hidden = true;
      setAction(null);
      secondary("Назад к форме", onBack);
      return;
    }
    mode = "card";
    status("");
    setAction(confirmLabel(), confirmPressed);
    secondary("Назад к форме", onBack);
    countdown(next.expires_at);
  }

  function expired(message) {
    stopTimers();
    mode = "expired";
    el.timer.hidden = true;
    status(message);
    setAction("Пересчитать", recalc);
    secondary("Назад к форме", onBack);
    backAllowed(true);
  }

  function result(view, serverText) {
    stopTimers();
    mode = "result";
    opening = view;
    el.timer.hidden = true;
    el.diff.hidden = true;
    el.card.textContent = resultLines(view, serverText).join("\n");
    status("");
    if (tg && tg.HapticFeedback && tg.isVersionAtLeast("6.1")) {
      tg.HapticFeedback.notificationOccurred(view.status === "DONE" ? "success" : "error");
    }
    setAction("Закрыть", () => (tg ? tg.close() : onMarket()));
    secondary("К рынку", onMarket);
    backAllowed(true);
  }

  function working(view, serverText) {
    stopTimers();
    mode = "working";
    opening = view;
    el.timer.hidden = true;
    const until = view.expires_at ? new Date(view.expires_at) : null;
    el.card.textContent = [serverText || view.text,
      `⏳ Лимит выставлен${until ? `, истекает ${clock.format(until)}` : ""}`].join("\n");
    status("");
    setAction("Отменить лимит", cancelPressed);
    secondary("К рынку", onMarket);
    backAllowed(true);
  }

  // Статус открытия → экран. CARD — POST не дошёл или не принят (предупреждения, лок).
  function handle(view, serverText, { busy = false } = {}) {
    opening = view;
    if (view.status === "EXPIRED_CARD") {
      expired(serverText || view.text);
    } else if (view.status === "WORKING") {
      working(view, serverText);
    } else if (view.final) {
      result(view, serverText);
    } else if (view.status === "CARD") {
      mode = "card";
      backAllowed(true);
      status(serverText || "");
      secondary("Назад к форме", onBack);
      // Лок позиции занят — кнопка снова через BUSY_RETRY_MS.
      setAction(confirmLabel(), confirmPressed, { progress: busy });
      if (busy) {
        const myGen = gen;
        setTimeout(() => {
          if (active && myGen === gen && mode === "card") setAction(confirmLabel(), confirmPressed);
        }, BUSY_RETRY_MS);
      }
    } else {
      checking();
    }
  }

  // --- опрос GET /api/open/{id} -----------------------------------------------------------

  function checking({ afterFailure = false } = {}) {
    mode = "checking";
    status("Проверяю…");
    setAction("Проверяю…", null, { progress: true });
    secondary(null);
    backAllowed(false);
    if (!pollUntil) pollUntil = Date.now() + POLL_LIMIT_MS;
    poll(afterFailure);
  }

  async function poll(afterFailure) {
    const myGen = gen;
    controller = new AbortController();
    let view;
    try {
      view = await api.get(`/api/open/${openingId}`, { signal: controller.signal });
    } catch (err) {
      if (myGen !== gen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      view = null;
    }
    if (myGen !== gen) return;
    controller = null;
    if (view && view.status === "CARD" && afterFailure) {
      // /confirm не дошёл до сервера: открытие не начато — повтор по тому же id.
      pollUntil = 0;
      mode = "card";
      backAllowed(true);
      status(NOT_DELIVERED_TEXT);
      setAction(confirmLabel(), confirmPressed);
      secondary("Назад к форме", onBack);
      return;
    }
    if (view && (view.final || view.status === "WORKING")) {
      pollUntil = 0;
      handle(view, null);
      return;
    }
    if (Date.now() >= pollUntil) {
      pollUntil = 0;
      status("Итог ещё не пришёл — смотри его в чате и в «Позициях».");
      setAction("К рынку", onMarket);
      backAllowed(true);
      return;
    }
    pollTimer = setTimeout(() => {
      pollTimer = null;
      if (active) poll(afterFailure && !view);
    }, POLL_MS);
  }

  // --- действия --------------------------------------------------------------------------

  function fatal(err) {
    if (err instanceof ApiError && err.status === 401) {
      stopTimers();
      onFatal(err);
      return true;
    }
    return false;
  }

  function ask(question) {
    if (popup) {
      return new Promise((resolve) => tg.showConfirm(question, (ok) => resolve(Boolean(ok))));
    }
    // Старый клиент: без window.confirm — второе нажатие той же кнопки.
    if (armed) {
      armed = false;
      return Promise.resolve(true);
    }
    armed = true;
    status(`${question}\nНажми ещё раз, чтобы подтвердить.`);
    return Promise.resolve(false);
  }

  async function confirmPressed() {
    if (mode !== "card") return;
    if (!(await ask(confirmQuestion(card, body.side, body.symbol, live())))) return;
    if (!active || mode !== "card") return;
    stopTimers();
    mode = "sending";
    status("Отправляю…");
    setAction("Отправляю…", null, { progress: true });
    secondary(null);
    backAllowed(false);
    const myGen = gen;
    let res;
    try {
      res = await api.post(`/api/open/${openingId}/confirm`,
        { accept_warnings: Boolean(card.has_warnings) });
    } catch (err) {
      if (myGen !== gen) return;
      if (fatal(err)) return;
      if (err instanceof ApiError && err.status === 0) {
        // Обрыв сети: дошёл ли POST — неизвестно; повторять сам не будем — опрос.
        checking({ afterFailure: true });
        return;
      }
      mode = "card";
      backAllowed(true);
      status(err instanceof ApiError ? err.message : "Не получилось — повтори.");
      setAction(confirmLabel(), confirmPressed);
      secondary("Назад к форме", onBack);
      return;
    }
    if (myGen !== gen) return;
    if (res.pending) {
      checking();
      return;
    }
    handle(res.opening, res.text, { busy: res.busy });
  }

  async function cancelPressed() {
    if (mode !== "working" || !opening) return;
    if (!(await ask(cancelQuestion(opening, live())))) return;
    if (!active || mode !== "working") return;
    mode = "sending";
    setAction("Отменяю…", null, { progress: true });
    const myGen = gen;
    try {
      const res = await api.post(`/api/open/${openingId}/cancel`);
      if (myGen !== gen) return;
      handle(res.opening, res.text, { busy: res.busy });
    } catch (err) {
      if (myGen !== gen) return;
      if (fatal(err)) return;
      status(err instanceof ApiError ? err.message : "Не получилось — повтори.");
      checking();
    }
  }

  async function create() {
    stopTimers();
    gen += 1;
    const myGen = gen;
    mode = "loading";
    pollUntil = 0;
    card = null;
    openingId = null;
    el.live.hidden = !live();
    el.live.textContent = LIVE_TEXT;
    el.title.textContent = `${body.symbol} · ${SIDE_TEXT[body.side]}`;
    el.card.textContent = "Готовлю карточку на свежих данных биржи…";
    el.diff.hidden = true;
    el.timer.hidden = true;
    status("");
    setAction(null);
    secondary("Назад к форме", onBack);
    backAllowed(true);
    try {
      const res = await api.post("/api/open", body);
      if (myGen !== gen || !active) return;
      showCard(res);
    } catch (err) {
      if (myGen !== gen) return;
      if (fatal(err)) return;
      el.card.textContent = err instanceof ApiError ? err.message : "Карточка не получилась.";
      setAction("Повторить", create);
    }
  }

  function recalc() {
    if (active) create();
  }

  function pressed() {
    if (active && action) action();
  }

  el.action.addEventListener("click", pressed);

  return {
    // Перед show("confirm"): тело и ответ последнего превью формы.
    start(next) {
      body = next.body;
      preview = next.card;
      mode = "idle";
    },
    enter() {
      if (active || !body) return;
      active = true;
      if (native) tg.MainButton.onClick(pressed);
      create();
    },
    leave() {
      if (!active) return;
      active = false;
      gen += 1;
      stopTimers();
      armed = false;
      if (native) {
        tg.MainButton.offClick(pressed);
        tg.MainButton.setParams({ is_visible: false });
      }
      el.action.hidden = true;
      backAllowed(true);
    },
    // Пока /confirm в полёте или идёт проверка — «Назад» не уводит с экрана.
    busy: () => active && (mode === "sending" || mode === "checking"),
  };
}
