// Экран «Действия с позицией» (экран 5, §9а; план — «да» владельца 25.10). Своей логики
// нет: расчёт, проверки, лок, перечитывание биржи на «Да» — сервер (PositionActionService,
// тот же, что у кнопок чата). Здесь — ввод, показ строк сервера и вызовы API.
//
// Экран: GET /api/positions/{symbol}/{side}/actions — позиция, PnL в R, безубыток и
// подсказки кнопок (подсказка; решает карточка). Опрос раз в INFO_POLL_MS цепочкой
// setTimeout, скрытая вкладка или уход — стоп. График — свечи 5м один раз и линии позиции;
// стоп/тейк после действия — пунктир «новый» из new_level сервера.
//
// Стоп и тейк: ввод цены или чип стопа (0.5 · 1 · 1.5 % от mark, БУ) → превью POST
// /api/actions/preview (debounce, «последний побеждает»), без таймера → MainButton
// «Перенести стоп на X» → POST /api/actions (свежие данные биржи, строка CARD) → карточка с
// таймером → MainButton → /confirm. Чип уходит на сервер строкой level_percent — перевод в
// цену и округление на сервере. Прямые действия (БУ, 25/50 %, всё): кнопка → карточка →
// MainButton → /confirm — два нажатия, как в чате.
//
// Подтверждение (правка владельца 25.10): showConfirm — только LIVE («⚠️ Реальный счёт») и
// рост риска; на демо без роста риска — сразу. Клиент ниже 6.2 (без showConfirm) — второе
// нажатие не раньше SECOND_PRESS_MS. Риск растёт — MainButton «⚠️ Да, увеличить риск»,
// risk_confirmed = card.risk_increase.
//
// Таймер — ttl_seconds ответа от момента ответа, по монотонным часам (performance.now):
// часы телефона не участвуют. Истёк — «Пересчитать» (новый POST /api/actions с тем же
// телом), подтверждать нельзя.
//
// Итог: pending — опрос GET /api/actions/{id} раз в POLL_MS, предел POLL_LIMIT_MS, скрытая
// вкладка — пауза; busy (лок позиции занят) — кнопка снова через BUSY_RETRY_MS. Обрыв сети
// на /confirm — POST не повторяется сам: опрос GET; действие ещё CARD — «Не дошло, нажми ещё
// раз». Пока /confirm в полёте или идёт проверка — «Назад» нет.
// Чисел клиент не считает: всё — строки сервера; время — только для отсчёта и опроса.

import { ApiError } from "./api.js";
import { createPriceChart } from "./chart.js";
import { BUSY_RETRY_MS, NOT_DELIVERED_TEXT, SECOND_PRESS_MS } from "./confirm.js";
import { PREVIEW_DEBOUNCE_MS, PREVIEW_RETRY_MS } from "./form.js";
import { pnlClass } from "./positions.js";
import { actionDiff, baseAsset, clockLeft, groupDigits, LIVE_TEXT, parseAmount } from "./render.js";
import { paintButton } from "./theme.js";

export const INFO_POLL_MS = 5000;
export const MAX_BACKOFF_MS = 30000;
export const POLL_MS = 2000;
export const POLL_LIMIT_MS = 300000;      // 5 мин: лок 187 с + цикл восстановления (§9а)
export const RISK_TEXT = "⚠️ Да, увеличить риск";
// К6 (27.10, владелец): «Перенести стоп на X» и «Да» — одна MainButton в одном месте, второе
// нажатие легко сделать по инерции. Новая карточка (и после «Пересчитать») — кнопка неактивна
// CARD_ARM_MS, и «Да» другого цвета, чем подпись превью.
export const CARD_ARM_MS = 1000;
export const STOP_CHIPS = ["0.5", "1", "1.5"];
const SIDE_TEXT = { LONG: "Лонг", SHORT: "Шорт" };
const TITLES = {
  be: "СТОП В БЕЗУБЫТОК", sl: "ПЕРЕНОС СТОПА", tp: "ТЕЙК",
  c25: "ЗАКРЫТЬ 25%", c50: "ЗАКРЫТЬ 50%", cf: "ЗАКРЫТЬ ВСЁ",
};
const NAMES = {
  be: "Стоп в безубыток", sl: "Перенести стоп", tp: "Изменить тейк",
  c25: "Закрыть 25%", c50: "Закрыть 50%", cf: "Закрыть всё",
};
const EXPIRED_TEXT = "⌛ Карточка устарела — цены могли уйти. Пересчитай.";
export const CLOSED_TEXT = "✅ Позиция закрыта";
const CANDLES_INTERVAL = "5m";

function $(id) {
  return document.getElementById(id);
}

// Подпись MainButton превью стопа/тейка: цена — new_level сервера; нет — кнопки нет.
export function editLabel(code, card) {
  if (!card || card.refusal || !card.new_level) return null;
  const level = groupDigits(card.new_level);
  return code === "tp" ? `Изменить тейк на ${level}` : `Перенести стоп на ${level}`;
}

// Подпись MainButton карточки: рост риска — «⚠️ Да, увеличить риск»; закрытие — «Закрыть
// 25% · N XRP» (что и сколько — строка «Закрыть …» карточки сервера, только подписи).
export function cardLabel(code, card, symbol) {
  if (card.risk_increase) return RISK_TEXT;
  if (code === "c25" || code === "c50" || code === "cf") {
    const row = card.rows.find((r) => r.label.startsWith("Закрыть ")
      && r.value.endsWith(" маркетом"));
    if (!row) return "Да, закрыть";
    const what = row.label.slice("Закрыть ".length);
    const qty = row.value.slice(0, -" маркетом".length);
    return `Закрыть ${what} · ${groupDigits(qty)} ${baseAsset(symbol)}`;
  }
  if (!card.new_level) return "Да, подтвердить";
  const level = groupDigits(card.new_level);
  return code === "tp" ? `Да, тейк на ${level}` : `Да, стоп на ${level}`;
}

// Вопрос showConfirm: что за действие (заголовок карточки сервера) и строка риска;
// реальный счёт — первой строкой.
export function actionQuestion(card, live) {
  const risk = card.rows.find((r) => r.label === "Риск увеличится");
  const lines = [`${card.title}?`];
  if (risk) lines.push(`Риск увеличится: ${risk.value}`);
  if (live) lines.unshift(LIVE_TEXT);
  return lines.join("\n");
}

// Линии графика: позиция (вход, стоп, тейк — строки сервера) и пунктир «новый» из
// new_level карточки или превью.
export function actionLines(position, code, card) {
  if (!position) return [];
  const lines = [{ kind: "entry", price: position.entry_price, title: "вход" }];
  if (position.stops.length) lines.push({ kind: "stop", price: position.stops[0].price, title: "стоп" });
  if (position.takes.length) lines.push({ kind: "take", price: position.takes[0].price, title: "тейк" });
  if (card && !card.refusal && card.new_level) {
    lines.push({ kind: code === "tp" ? "new-take" : "new-stop", price: card.new_level,
      title: "новый" });
  }
  return lines;
}

export function createActionsScreen({ api, tg, me, onBack, onFatal }) {
  const native = tg && tg.isVersionAtLeast("7.10");
  const popup = tg && tg.isVersionAtLeast("6.2");       // showConfirm — с Bot API 6.2
  const el = {
    back: $("actions-back"),
    title: $("actions-title"),
    state: $("actions-state"),
    stateText: $("actions-state-text"),
    retry: $("actions-retry"),
    chat: $("actions-chat"),
    body: $("actions-body"),
    mark: $("actions-mark"),
    pnl: $("actions-pnl"),
    sub: $("actions-sub"),
    stale: $("actions-stale"),
    chart: $("actions-chart"),
    chartState: $("actions-chart-state"),
    chartText: $("actions-chart-text"),
    grid: $("actions-grid"),
    hints: $("actions-hints"),
    panel: $("actions-panel"),
    panelTitle: $("actions-panel-title"),
    timer: $("actions-timer"),
    editor: $("actions-editor"),
    levelLabel: $("actions-level-label"),
    level: $("actions-level"),
    chips: $("actions-chips"),
    rows: $("actions-rows"),
    notes: $("actions-notes"),
    diffBox: $("actions-diff-box"),
    diff: $("actions-diff"),
    risk: $("actions-risk"),
    dry: $("actions-dry"),
    status: $("actions-status"),
    main: $("actions-main"),
    cancel: $("actions-cancel"),
  };

  let active = false;
  let target = null;         // {symbol, side, editor, chat} — с вкладки «Позиции»
  let info = null;           // последний ответ GET …/actions
  let chart = null;
  let gen = 0;               // экран: загрузка и опрос
  let infoTimer = null;
  let infoController = null;
  let failures = 0;
  let closed = false;        // «Закрыть всё» исполнено: позиции нет — итог, не «на бирже нет»

  let panelGen = 0;          // панель: превью, карточка, «Да», опрос итога
  let mode = "idle";         // idle | edit | loading | card | sending | checking | result | expired | refused
  let code = null;           // be | sl | tp | c25 | c50 | cf
  let chip = null;           // выбранный чип стопа: "0.5" | "1" | "1.5" | "be"
  let preview = null;        // последний ответ превью
  let previewBody = null;    // тело, на которое он получен
  let previewTimer = null;
  let previewController = null;
  let previewSeq = 0;
  let body = null;           // тело POST /api/actions — и для «Пересчитать»
  let card = null;           // карточка сервера (ActionCardOut)
  let actionId = null;
  let deadline = null;       // срок карточки, performance.now() — монотонные часы
  let action = null;         // что делает главная кнопка
  let armed = false;         // клиент без showConfirm: второе нажатие подтверждает
  let armedAt = 0;
  let cardReadyAt = 0;       // «Да» новой карточки — не раньше (performance.now)
  let asking = false;        // открыт вопрос showConfirm
  let timer = null;
  let pollTimer = null;
  let pollUntil = 0;
  let pollFailure = false;   // опрос после обрыва /confirm
  let controller = null;

  const live = () => me().account_mode === "LIVE";

  function fatal(err) {
    if (err instanceof ApiError && err.status === 401) {
      stopAll();
      onFatal(err);
      return true;
    }
    return false;
  }

  // --- кнопки --------------------------------------------------------------------------

  // confirm — «Да» карточки: на LIVE красная (реальный счёт), на демо синяя; остальные
  // подписи (превью, «Пересчитать», «К позициям») — чернилами: «Да» всегда другого цвета.
  function mainButton(textValue, { progress = false, confirm = false } = {}) {
    if (native) {
      if (!textValue) {
        tg.MainButton.setParams({ is_visible: false });
      } else {
        const role = confirm ? (live() ? "live" : "confirm") : "main";
        paintButton(tg.MainButton, role,
          { text: textValue, is_active: !progress, is_visible: true });
      }
      tg.SecondaryButton.setParams({ is_visible: false });
    } else {
      el.main.textContent = textValue || "";
      el.main.disabled = progress;
      el.main.hidden = !textValue;
      el.main.classList.toggle("button-confirm", confirm);
    }
  }

  function setAction(textValue, handler, options) {
    action = handler;
    armed = false;
    mainButton(textValue, options);
  }

  function cancelButton(textValue) {
    el.cancel.textContent = textValue || "";
    el.cancel.hidden = !textValue;
  }

  function backAllowed(allowed) {
    if (tg && tg.isVersionAtLeast("6.1")) {
      if (allowed) tg.BackButton.show();
      else tg.BackButton.hide();
    }
    el.back.disabled = !allowed;
  }

  function status(message) {
    el.status.textContent = message || "";
    el.status.hidden = !message;
  }

  // --- отрисовка экрана ------------------------------------------------------------------

  function showState(message, { retry = false, chat = false } = {}) {
    el.stateText.textContent = message;
    el.retry.hidden = !retry;
    el.chat.hidden = !(chat && target && target.chat);
    el.state.hidden = false;
    el.body.hidden = true;
  }

  function drawLines() {
    if (chart) chart.setLines(actionLines(info && info.position, code, card || preview));
  }

  function renderInfo(data) {
    info = data;
    const p = data.position;
    if (!p) {
      showState(data.refusal ? data.refusal.message : "Позиции нет.", { retry: true });
      return;
    }
    const base = baseAsset(p.symbol);
    el.mark.textContent = groupDigits(p.mark_price);
    el.pnl.className = `position-pnl ${pnlClass(p.pnl_sign)}`;
    el.pnl.textContent = [p.pnl_text ?? "—", data.pnl_r_text].filter(Boolean).join(" · ");
    el.sub.textContent = `${groupDigits(p.quantity)} ${base} · вход ${p.entry_price} · ${p.leverage}×`;
    const hints = [];
    for (const hint of data.actions) {
      const button = el.grid.querySelector(`button[data-code="${hint.code}"]`);
      if (button) button.disabled = !hint.enabled;
      if (!hint.enabled && hint.hint) hints.push(`${NAMES[hint.code]}: ${hint.hint}`);
    }
    if (data.breakeven_price !== null && data.breakeven_price !== undefined) {
      hints.push(`безубыток — ${groupDigits(data.breakeven_price)}`);
    }
    el.hints.replaceChildren(...hints.map((line) => {
      const item = document.createElement("li");
      item.textContent = line;
      return item;
    }));
    el.hints.hidden = hints.length === 0;
    el.state.hidden = true;
    el.body.hidden = false;
    if (chart) chart.setPrecision(p.price_precision ?? 4);
    drawLines();
  }

  function chartState(message) {
    el.chartText.textContent = message || "";
    el.chartState.hidden = !message;
  }

  // --- экран: загрузка и опрос ------------------------------------------------------------

  function infoPath() {
    return `/api/positions/${encodeURIComponent(target.symbol)}/${target.side}/actions`;
  }

  function stopInfo() {
    if (infoTimer !== null) clearTimeout(infoTimer);
    infoTimer = null;
    if (infoController) infoController.abort();
    infoController = null;
  }

  function scheduleInfo() {
    if (!active || document.hidden || infoTimer !== null) return;
    const delay = failures ? Math.min(INFO_POLL_MS * 2 ** failures, MAX_BACKOFF_MS) : INFO_POLL_MS;
    infoTimer = setTimeout(infoTick, delay);
  }

  function disabled(err) {
    return err instanceof ApiError && err.status === 403;
  }

  async function loadInfo() {
    if (closed) return;
    stopInfo();
    const myGen = ++gen;
    failures = 0;
    if (!info) showState("Загрузка…");
    infoController = new AbortController();
    const { signal } = infoController;
    try {
      const data = await api.get(infoPath(), { signal });
      if (myGen !== gen) return;
      renderInfo(data);
    } catch (err) {
      if (myGen !== gen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      if (!(err instanceof ApiError)) console.error(err);
      // Выключатель MINIAPP_ACTIONS_ENABLED — 403: действия — в чате.
      showState(err instanceof ApiError ? err.message : "Ошибка на странице — открой заново.",
        { retry: !disabled(err), chat: disabled(err) });
      closePanel();
      return;
    }
    if (myGen === gen) {
      infoController = null;
      scheduleInfo();
    }
    if (chart && chart.lastTime() === null) loadCandles(myGen);
  }

  async function loadCandles(myGen) {
    chartState("Загрузка…");
    try {
      const data = await api.get(`/api/market/${encodeURIComponent(target.symbol)}/candles`
        + `?interval=${CANDLES_INTERVAL}`);
      if (myGen !== gen || !chart) return;
      chart.setCandles(data.candles);
      chartState(data.candles.length ? null : "Свечей нет.");
    } catch (err) {
      if (myGen !== gen) return;
      if (fatal(err)) return;
      chartState(err instanceof ApiError ? err.message : "График не загрузился.");
    }
  }

  async function infoTick() {
    infoTimer = null;
    if (!active || closed || document.hidden) return;
    const myGen = gen;
    infoController = new AbortController();
    try {
      const data = await api.get(infoPath(), { signal: infoController.signal });
      if (myGen !== gen) return;
      renderInfo(data);
      failures = 0;
      el.stale.hidden = true;
    } catch (err) {
      if (myGen !== gen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      if (disabled(err)) {
        showState(err.message, { chat: true });
        closePanel();
        return;
      }
      failures += 1;
      const wait = Math.min(INFO_POLL_MS * 2 ** failures, MAX_BACKOFF_MS) / 1000;
      el.stale.textContent = `${err instanceof ApiError ? err.message : "Нет связи."} `
        + `Данные могут быть устаревшими — повторю через ${wait} с.`;
      el.stale.hidden = false;
    }
    if (myGen === gen) {
      infoController = null;
      scheduleInfo();
    }
  }

  // --- панель: строки ---------------------------------------------------------------------

  function showRows(next) {
    if (!next || next.refusal || !next.rows.length) {
      el.rows.className = "card-text";
      el.rows.textContent = next ? next.text : "";
    } else {
      el.rows.className = "record-rows";
      el.rows.replaceChildren(...next.rows.map((r) => {
        const row = document.createElement("div");
        row.className = "receipt-row";
        const a = document.createElement("span");
        a.textContent = r.label;
        const b = document.createElement("span");
        b.textContent = r.value;
        row.append(a, b);
        return row;
      }));
    }
    const notes = next && !next.refusal ? next.notes : [];
    el.notes.replaceChildren(...notes.map((line) => {
      const item = document.createElement("li");
      item.textContent = line;
      return item;
    }));
    el.notes.hidden = notes.length === 0;
    el.risk.hidden = !(next && next.risk_increase);
    el.dry.hidden = !(next && !next.refusal && me().actions && me().actions.dry_run);
  }

  function showText(value) {
    el.rows.className = "card-text";
    el.rows.textContent = value;
    el.notes.hidden = true;
    el.risk.hidden = true;
    el.dry.hidden = true;
    el.diffBox.hidden = true;
  }

  function markChips() {
    for (const button of el.chips.querySelectorAll("button[data-chip]")) {
      button.classList.toggle("selected", button.dataset.chip === chip);
    }
  }

  // --- панель: таймеры --------------------------------------------------------------------

  function stopTimers() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = null;
    if (controller) controller.abort();
    controller = null;
  }

  function cancelPreview() {
    if (previewTimer !== null) clearTimeout(previewTimer);
    previewTimer = null;
    previewSeq += 1;
    if (previewController) previewController.abort();
    previewController = null;
  }

  // Срок карточки — ttl_seconds сервера от ответа, монотонными часами (не часы телефона).
  function startDeadline(ttlSeconds) {
    deadline = performance.now() + ttlSeconds * 1000;
  }

  function secondsLeft() {
    return Math.ceil((deadline - performance.now()) / 1000);
  }

  function countdown() {
    timer = null;
    const left = secondsLeft();
    if (!Number.isFinite(left)) {
      el.timer.hidden = true;
      return;
    }
    if (left <= 0) {
      expired(EXPIRED_TEXT);
      return;
    }
    el.timer.textContent = clockLeft(left);
    el.timer.hidden = false;
    timer = setTimeout(countdown, 1000);
  }

  // --- панель: состояния ------------------------------------------------------------------

  function openPanel(nextCode) {
    stopTimers();
    cancelPreview();
    panelGen += 1;
    code = nextCode;
    chip = null;
    preview = null;
    previewBody = null;
    body = null;
    card = null;
    actionId = null;
    deadline = null;
    pollUntil = 0;
    el.panel.hidden = false;
    el.panel.classList.toggle("danger", nextCode === "sl" || nextCode === "be");
    el.panelTitle.textContent = TITLES[nextCode];
    el.timer.hidden = true;
    el.diffBox.hidden = true;
    status("");
    showRows(null);
  }

  function closePanel() {
    stopTimers();
    cancelPreview();
    panelGen += 1;
    mode = "idle";
    code = null;
    card = null;
    preview = null;
    actionId = null;
    el.panel.hidden = true;
    el.editor.hidden = true;
    setAction(null);
    cancelButton(null);
    backAllowed(true);
    drawLines();
  }

  function startEdit(nextCode) {
    openPanel(nextCode);
    mode = "edit";
    el.editor.hidden = false;
    el.chips.hidden = nextCode !== "sl";
    el.levelLabel.textContent = nextCode === "tp" ? "Новый тейк" : "Новый стоп";
    el.level.value = "";
    el.level.classList.remove("invalid");
    markChips();
    setAction(null);
    cancelButton("Отмена");
    backAllowed(true);
    drawLines();
  }

  // Тело превью из поля или чипа; ошибка ввода — подсветка и null (запрос не уходит).
  function editBody() {
    const base = { symbol: target.symbol, side: target.side };
    if (chip === "be") return { ...base, kind: "be" };
    if (chip) return { ...base, kind: "sl", level_percent: chip };
    const raw = el.level.value.trim();
    const level = parseAmount(raw);
    el.level.classList.toggle("invalid", level === null && raw !== "");
    return level === null ? null : { ...base, kind: code, level };
  }

  function schedulePreview(delay = PREVIEW_DEBOUNCE_MS) {
    if (!active || mode !== "edit") return;
    cancelPreview();
    setAction(null);
    previewTimer = setTimeout(runPreview, delay);
  }

  async function runPreview() {
    previewTimer = null;
    const next = editBody();
    if (!next) {
      preview = null;
      previewBody = null;
      showRows(null);
      drawLines();
      setAction(null);
      return;
    }
    const seq = ++previewSeq;
    previewController = new AbortController();
    let res;
    try {
      res = await api.post("/api/actions/preview", next, { signal: previewController.signal });
    } catch (err) {
      if (seq !== previewSeq || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      if (err instanceof ApiError && err.status === 429) {
        schedulePreview(PREVIEW_RETRY_MS);
        return;
      }
      showText(err instanceof ApiError ? err.message : "Пересчёт не удался.");
      return;
    }
    if (seq !== previewSeq || mode !== "edit") return;   // ответ на устаревший ввод
    previewController = null;
    preview = res;
    previewBody = next;
    showRows(res);
    drawLines();
    const label = editLabel(next.kind, res);
    setAction(label, label ? editPressed : null);
  }

  function editPressed() {
    if (mode !== "edit" || !preview || !previewBody) return;
    cancelPreview();
    createCard(previewBody, preview);
  }

  // Прямое действие (БУ, 25/50 %, всё): кнопка → карточка на свежих данных.
  function direct(nextCode) {
    openPanel(nextCode);
    el.editor.hidden = true;
    createCard({ symbol: target.symbol, side: target.side, kind: nextCode }, null);
  }

  async function createCard(nextBody, previewCard) {
    stopTimers();
    const myGen = ++panelGen;
    mode = "loading";
    body = nextBody;
    preview = previewCard;
    card = null;
    actionId = null;
    deadline = null;
    pollUntil = 0;
    el.editor.hidden = true;
    el.timer.hidden = true;
    showText("Готовлю карточку на свежих данных биржи…");
    status("");
    setAction(null);
    cancelButton("Отмена");
    backAllowed(true);
    let res;
    try {
      res = await api.post("/api/actions", body);
    } catch (err) {
      if (myGen !== panelGen || !active) return;
      if (fatal(err)) return;
      mode = "refused";
      showText(err instanceof ApiError ? err.message : "Карточка не получилась.");
      setAction("Повторить", () => createCard(body, preview));
      return;
    }
    if (myGen !== panelGen || !active) return;
    showCard(res);
  }

  function showCard(res) {
    card = res.card;
    actionId = res.action_id;
    showRows(card);
    const diff = actionDiff(preview, card);
    el.diff.replaceChildren(...diff.map((line) => {
      const item = document.createElement("li");
      item.textContent = line;
      return item;
    }));
    el.diffBox.hidden = diff.length === 0;
    drawLines();
    if (!actionId || res.status !== "CARD" || card.refusal) {
      // Отказ на свежих данных — кнопки нет, как в чате.
      mode = "refused";
      el.timer.hidden = true;
      setAction(null);
      cancelButton("Закрыть");
      return;
    }
    mode = "card";
    startDeadline(res.ttl_seconds);
    // Каждая новая карточка (и после «Пересчитать») — «Да» не раньше CARD_ARM_MS: кнопка
    // неактивна (видно), нажатие раньше — ничего (проверка cardReadyAt при «Да»).
    cardReadyAt = performance.now() + CARD_ARM_MS;
    setAction(cardLabel(code, card, target.symbol), confirmPressed,
      { progress: true, confirm: true });
    const myGen = panelGen;
    setTimeout(() => {
      if (active && myGen === panelGen && mode === "card") {
        setAction(cardLabel(code, card, target.symbol), confirmPressed, { confirm: true });
      }
    }, CARD_ARM_MS);
    cancelButton("Отмена");
    countdown();
  }

  function expired(message) {
    stopTimers();
    mode = "expired";
    el.timer.hidden = true;
    status(message);
    setAction("Пересчитать", recalc);
    cancelButton("Отмена");
    backAllowed(true);
  }

  function recalc() {
    if (active && body) createCard(body, preview);
  }

  function result(view, serverText) {
    stopTimers();
    mode = "result";
    // Итог — пунктира «новый» нет: уровень встал (линия позиции) или не встал (линии нет).
    card = null;
    preview = null;
    drawLines();
    el.timer.hidden = true;
    showText(serverText || view.text);
    status("");
    if (tg && tg.HapticFeedback && tg.isVersionAtLeast("6.1")) {
      tg.HapticFeedback.notificationOccurred(view.status === "DONE" ? "success" : "error");
    }
    setAction("К позициям", () => onBack());
    backAllowed(true);
    if (view.status === "DONE" && code === "cf") {
      positionClosed();
      return;
    }
    cancelButton("Закрыть");
    // Позиция изменилась: экран и линии — заново.
    loadInfo();
  }

  // Позицию закрыло наше «Закрыть всё»: экран не грузим и не опрашиваем — сервер ответил бы
  // «Позиции … на бирже нет» с «Повторить», а это читается как ошибка.
  function positionClosed() {
    closed = true;
    gen += 1;
    stopInfo();
    cancelButton(null);
    showState(CLOSED_TEXT);
  }

  // Карточка снова ждёт «Да» (лок занят, ошибка, «Не дошло»): отсчёт — к тому же сроку.
  function cardAgain(message, { busy = false } = {}) {
    mode = "card";
    backAllowed(true);
    status(message || "");
    cancelButton("Отмена");
    setAction(cardLabel(code, card, target.symbol), confirmPressed,
      { progress: busy, confirm: true });
    if (timer === null) countdown();
    if (busy) {
      const myGen = panelGen;
      setTimeout(() => {
        if (active && myGen === panelGen && mode === "card") {
          setAction(cardLabel(code, card, target.symbol), confirmPressed, { confirm: true });
        }
      }, BUSY_RETRY_MS);
    }
  }

  // Ответ /confirm или опроса → панель. CARD — «Да» не прошло (лок занят).
  function handle(view, serverText, { busy = false } = {}) {
    if (view.status === "EXPIRED") {
      expired(serverText || view.text);
    } else if (view.final) {
      result(view, serverText);
    } else if (view.status === "CARD") {
      cardAgain(serverText, { busy });
    } else {
      checking();
    }
  }

  // --- опрос GET /api/actions/{id} --------------------------------------------------------

  function checking({ afterFailure = false } = {}) {
    stopTimers();
    mode = "checking";
    el.timer.hidden = true;
    status("Проверяю…");
    setAction("Проверяю…", null, { progress: true });
    cancelButton(null);
    backAllowed(false);
    pollFailure = afterFailure;
    if (!pollUntil) pollUntil = performance.now() + POLL_LIMIT_MS;
    poll();
  }

  async function poll() {
    pollTimer = null;
    if (!active || mode !== "checking") return;
    if (document.hidden) return;              // пауза; onVisibility продолжит
    const myGen = panelGen;
    controller = new AbortController();
    let view;
    try {
      view = await api.get(`/api/actions/${actionId}`, { signal: controller.signal });
    } catch (err) {
      if (myGen !== panelGen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      view = null;
    }
    if (myGen !== panelGen) return;
    controller = null;
    if (view && view.status === "CARD" && pollFailure) {
      // /confirm не дошёл до сервера: действие не начато — повтор по тому же id.
      pollUntil = 0;
      cardAgain(NOT_DELIVERED_TEXT);
      return;
    }
    if (view && view.final) {
      pollUntil = 0;
      handle(view, null);
      return;
    }
    if (performance.now() >= pollUntil) {
      pollUntil = 0;
      mode = "result";
      status("Итог придёт в чат — проверь «Позиции».");
      setAction("К позициям", () => onBack());
      cancelButton(null);
      backAllowed(true);
      return;
    }
    if (view) pollFailure = false;
    pollTimer = setTimeout(poll, POLL_MS);
  }

  // --- «Да» ---------------------------------------------------------------------------------

  function ask(question) {
    return new Promise((resolve) => tg.showConfirm(question, (ok) => resolve(Boolean(ok))));
  }

  // Клиент без showConfirm: второе нажатие той же кнопки, не раньше SECOND_PRESS_MS после
  // первого; раньше — не считается. Синхронно, без await до «sending».
  function secondPress(question) {
    const now = performance.now();
    if (armed) {
      if (now - armedAt < SECOND_PRESS_MS) return false;
      armed = false;
      return true;
    }
    armed = true;
    armedAt = now;
    status(`${question}\nНажми ещё раз, чтобы подтвердить.`);
    return false;
  }

  // Демо без роста риска — второе нажатие уже было (кнопка → MainButton): сразу /confirm.
  // LIVE или рост риска — showConfirm (старый клиент — второе нажатие). Срок карточки
  // истёк — не подтверждаем, «Пересчитать».
  async function confirmPressed() {
    if (mode !== "card" || asking) return;
    if (performance.now() < cardReadyAt) return;     // по инерции — сразу после карточки
    if (secondsLeft() <= 0) {
      expired(EXPIRED_TEXT);
      return;
    }
    const needAsk = live() || Boolean(card.risk_increase);
    if (needAsk) {
      const question = actionQuestion(card, live());
      if (!popup) {
        if (!secondPress(question)) return;
      } else {
        asking = true;
        let ok;
        try {
          ok = await ask(question);
        } finally {
          asking = false;
        }
        if (!ok || !active || mode !== "card") return;
        if (secondsLeft() <= 0) {
          expired(EXPIRED_TEXT);
          return;
        }
      }
    }
    stopTimers();
    mode = "sending";
    el.timer.hidden = true;
    status("Отправляю…");
    setAction("Отправляю…", null, { progress: true });
    cancelButton(null);
    backAllowed(false);
    const myGen = panelGen;
    let res;
    try {
      res = await api.post(`/api/actions/${actionId}/confirm`,
        { risk_confirmed: Boolean(card.risk_increase) });
    } catch (err) {
      if (myGen !== panelGen) return;
      if (fatal(err)) return;
      if (err instanceof ApiError && err.status === 0) {
        // Обрыв сети: дошёл ли POST — неизвестно; повторять сам не будем — опрос.
        checking({ afterFailure: true });
        return;
      }
      cardAgain(err instanceof ApiError ? err.message : "Не получилось — повтори.");
      return;
    }
    if (myGen !== panelGen) return;
    if (res.pending) {
      checking();
      return;
    }
    handle(res.action, res.text, { busy: res.busy });
  }

  // «Отмена» карточки — «Нет» (как в чате); до карточки и после итога — просто закрыть.
  async function cancelPressed() {
    if (mode === "sending" || mode === "checking") return;
    if ((mode === "card" || mode === "expired") && actionId) {
      const id = actionId;
      closePanel();
      try {
        await api.post(`/api/actions/${id}/decline`);
      } catch (err) {
        if (fatal(err)) return;
        // Не отклонилась — истечёт сама: на биржу без «Да» ничего не уйдёт.
      }
      return;
    }
    closePanel();
  }

  function pressed() {
    if (active && action) action();
  }

  // --- события ------------------------------------------------------------------------------

  function stopAll() {
    stopInfo();
    stopTimers();
    cancelPreview();
  }

  function onVisibility() {
    if (document.hidden) {
      stopInfo();
      if (pollTimer !== null) clearTimeout(pollTimer);
      pollTimer = null;
      return;
    }
    if (!active) return;
    if (info && infoTimer === null && infoController === null) infoTick();
    if (mode === "checking" && pollTimer === null && controller === null) poll();
    if (mode === "card") {
      if (timer !== null) clearTimeout(timer);
      countdown();
    }
  }

  el.grid.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-code]");
    if (!active || !button || button.disabled) return;
    // Карточка готовится, «Да» в полёте или идёт проверка — повторное нажатие (двойной тап)
    // не создаёт вторую карточку.
    if (mode === "loading" || mode === "sending" || mode === "checking") return;
    const next = button.dataset.code;
    if (next === "sl" || next === "tp") startEdit(next);
    else direct(next);
  });
  el.chips.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-chip]");
    if (!active || mode !== "edit" || !button) return;
    chip = button.dataset.chip;
    el.level.value = "";
    el.level.classList.remove("invalid");
    markChips();
    schedulePreview(0);
  });
  el.level.addEventListener("input", () => {
    if (!active || mode !== "edit") return;
    chip = null;
    markChips();
    schedulePreview();
  });
  el.main.addEventListener("click", pressed);
  el.cancel.addEventListener("click", () => active && cancelPressed());
  el.back.addEventListener("click", () => active && !el.back.disabled && onBack());
  el.retry.addEventListener("click", () => active && loadInfo());
  el.chat.addEventListener("click", () => {
    if (!active || !target || !target.chat) return;
    if (typeof tg.openTelegramLink === "function" && tg.isVersionAtLeast("6.1")) {
      tg.openTelegramLink(target.chat);
    } else {
      tg.openLink(target.chat);
    }
  });

  return {
    // Перед show("actions"): позиция с вкладки «Позиции»; editor "sl" — «Поставить стоп →»
    // (сразу редактор стопа); chat — ссылка на экран действий в чате (выключатель).
    open(next) {
      target = next;
      info = null;
      closed = false;
    },
    enter() {
      if (active || !target) return;
      active = true;
      el.title.textContent = `${baseAsset(target.symbol)} · ${SIDE_TEXT[target.side]}`;
      document.addEventListener("visibilitychange", onVisibility);
      if (native) tg.MainButton.onClick(pressed);
      if (!chart) chart = createPriceChart(el.chart, { interval: CANDLES_INTERVAL });
      chart.setType("candles");
      closePanel();
      loadInfo();
      if (target.editor === "sl") startEdit("sl");
    },
    leave() {
      if (!active) return;
      active = false;
      gen += 1;
      closePanel();
      stopAll();
      document.removeEventListener("visibilitychange", onVisibility);
      if (native) {
        tg.MainButton.offClick(pressed);
        tg.MainButton.setParams({ is_visible: false });
      }
      el.main.hidden = true;
      if (chart) {
        chart.destroy();
        chart = null;
      }
      info = null;
    },
    refresh() {
      if (active) loadInfo();
    },
    // Пока /confirm в полёте или идёт проверка — «Назад» не уводит с экрана.
    busy: () => active && (mode === "sending" || mode === "checking"),
  };
}
