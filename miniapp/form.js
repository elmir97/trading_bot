// Экран «Сделка» (§5а, экран 2): одна форма — сторона, вход, стоп, тейк, риск, плечо —
// и живой пересчёт: любое изменение → POST /api/open/preview через PREVIEW_DEBOUNCE_MS
// после последнего. «Последний побеждает»: номер запроса + AbortController, устаревший
// ответ не рисуется; пока считается — прежние цифры приглушены, кнопки нет. Для маркета
// пересчёт и при новой цене (опрос /api/market раз в 3 с, как на экране рынка).
// Чип «2R» ставит цену тейка из ответа превью (calc.take_2r — от входа и стопа).
//
// Своих расчётов нет (§5): % → цена, $ ↔ % риска, объём, маржа, RR, ликвидация,
// нарушения — ответ сервера (render.js только показывает). Риск по умолчанию — из плана,
// при каждом открытии формы заново; сам клиент риск не меняет (ни после убытка, ни
// после прибыли — мартингейла нет). Плечо — не выше min(максимум стороны, план): ползунок
// и тело запроса ограничены потолком (clampLeverage), сервер отклоняет выше тоже. Пока
// плечо не трогали — берётся предложенное сервером (suggested_leverage, от стопа).
// Нет ключа или максимум плеча не прочитан — формы нет, «Подключи ключи биржи».
//
// «Открыть» (MainButton) — по решению 05.10: BLOCK — кнопки нет, WARN — «Открыть всё
// равно». Отправка (POST /api/open, подтверждение) — подэтап 2.5: onOpen получает
// тело последнего превью и его ответ.

import { ApiError } from "./api.js";
import { createPriceChart, intervalSeconds } from "./chart.js";
import { candlesDue, INTERVALS, MAX_BACKOFF_MS, PRICE_POLL_MS } from "./market.js";
import { paintButton } from "./theme.js";
import {
  chartLines, clampLeverage, leverageCap, leverageHint, NO_KEY_TEXT, openAction, parseAmount,
  renderPreview, riskHint,
} from "./render.js";

export const PREVIEW_DEBOUNCE_MS = 300;
export const PREVIEW_RETRY_MS = 1000;      // 429: preview 5/с на пользователя (§3)
const EXPIRY_LABELS = { 15: "15 мин", 60: "1 ч", 240: "4 ч", 1440: "24 ч" };

function $(id) {
  return document.getElementById(id);
}

function select(group, attr, value) {
  for (const button of group.querySelectorAll(`button[data-${attr}]`)) {
    button.classList.toggle("selected", button.dataset[attr] === value);
  }
}

export function createTradeScreen({ api, tg, storage, me, onOpen, onFatal }) {
  const native = tg && tg.isVersionAtLeast("7.10");
  const el = {
    title: $("trade-title"),
    last: $("trade-last"),
    stale: $("trade-stale"),
    chart: $("trade-chart"),
    chartState: $("trade-chart-state"),
    chartText: $("trade-chart-text"),
    blocked: $("trade-blocked"),
    blockedTitle: $("trade-blocked-title"),
    blockedText: $("trade-blocked-text"),
    retry: $("trade-retry"),
    form: $("trade-form"),
    side: $("trade-side"),
    entry: $("trade-entry"),
    limitRow: $("trade-limit-row"),
    limit: $("trade-limit"),
    expiry: $("trade-expiry"),
    stop: $("trade-stop"),
    stopKind: $("trade-stop-kind"),
    stopChips: $("trade-stop-chips"),
    take: $("trade-take"),
    takeKind: $("trade-take-kind"),
    takeChips: $("trade-take-chips"),
    take2r: $("trade-take-2r"),
    risk: $("trade-risk"),
    riskKind: $("trade-risk-kind"),
    riskHint: $("trade-risk-hint"),
    leverage: $("trade-leverage"),
    leverageValue: $("trade-leverage-value"),
    leverageHint: $("trade-leverage-hint"),
    result: $("trade-result"),
    previewState: $("trade-preview-state"),
    out: {
      quantity: $("trade-quantity"),
      notional: $("trade-notional"),
      marginLabel: $("trade-margin-label"),
      margin: $("trade-margin"),
      risk: $("trade-risk-usd"),
      rr: $("trade-rr"),
      liq: $("trade-liq"),
      fee: $("trade-fee"),
      issues: $("trade-issues"),
      rounded: $("trade-rounded"),
    },
    fallback: $("fallback-buttons"),
    fallbackMain: $("fallback-main"),
    fallbackSecondary: $("fallback-secondary"),
  };

  let active = false;
  let symbol = null;
  let side = "LONG";
  let entry = "MARKET";
  let stopKind = "price";
  let takeKind = "price";
  let riskKind = "percent";
  let interval = "5m";
  let market = null;         // последний ответ /api/market
  let cap = null;            // потолок плеча формы; null — открытия нет
  let leverage = 1;
  let leverageTouched = false;
  let card = null;           // последний ответ preview
  let cardBody = null;       // тело, на которое он получен
  let pending = false;
  let chart = null;
  let gen = 0;
  let timer = null;
  let controller = null;
  let failures = 0;
  let candlesAt = 0;
  let asked = null;
  let askedAt = 0;
  let previewTimer = null;
  let previewController = null;
  let previewSeq = 0;

  // --- кнопка «Открыть» ------------------------------------------------------------

  function openButton() {
    const action = pending || !cap ? { visible: false } : openAction(card, side, symbol);
    if (native) {
      tg.SecondaryButton.setParams({ is_visible: false });
      if (action.visible) {
        paintButton(tg.MainButton, side === "SHORT" ? "short" : "main",
          { text: action.text, is_active: true, is_visible: true });
      } else {
        tg.MainButton.setParams({ is_visible: false });
      }
    } else {
      el.fallbackMain.textContent = action.visible ? action.text : "";
      el.fallbackSecondary.hidden = true;
      el.fallback.hidden = !action.visible;
    }
  }

  function onOpenClick() {
    if (!active || pending || !cap) return;
    if (!openAction(card, side, symbol).visible) return;
    onOpen({ body: cardBody, card });
  }

  el.fallbackMain.addEventListener("click", () => active && onOpenClick());

  // --- ошибки --------------------------------------------------------------------------

  function fatal(err) {
    if (err instanceof ApiError && err.status === 401) {
      onFatal(err);
      return true;
    }
    return false;
  }

  function blocked(title, detail) {
    el.blockedTitle.textContent = title;
    el.blockedText.textContent = detail || "";
    el.blockedText.hidden = !detail;
    el.blocked.hidden = false;
    el.form.hidden = true;
    el.result.hidden = true;
    cancelPreview();
    card = null;
    if (chart) chart.setLines([]);
    openButton();
  }

  // --- поля ----------------------------------------------------------------------------

  function renderControls() {
    el.title.textContent = `${symbol} · ${side === "LONG" ? "Лонг" : "Шорт"}`;
    select(el.side, "side", side);
    select(el.entry, "entry", entry);
    select(el.stopKind, "kind", stopKind);
    select(el.takeKind, "kind", takeKind);
    select(el.riskKind, "kind", riskKind);
    el.limitRow.hidden = entry !== "LIMIT";
    if (cap) {
      el.leverage.max = String(cap);
      el.leverage.value = String(leverage);
      el.leverageValue.textContent = `${leverage}×`;
    }
    el.leverageHint.textContent = cap ? leverageHint(card, cap) : "";
    el.take2r.disabled = !take2r();
    const plan = me().plan;
    el.riskHint.textContent = riskHint(card, riskKind, plan.risk_percent);
    markChips();
  }

  // Выбранный чип — рамкой (макет «Форма»): тот, чьё значение сейчас в поле. Только показ:
  // сравнение строк, без расчётов. «нет» у тейка — когда поле пустое.
  function markChips() {
    const stop = stopKind === "percent" ? el.stop.value.trim() : null;
    for (const chip of el.stopChips.querySelectorAll("button[data-percent]")) {
      chip.classList.toggle("selected", chip.dataset.percent === stop);
    }
    const take = el.take.value.trim();
    for (const chip of el.takeChips.querySelectorAll("button[data-percent]")) {
      const none = chip.dataset.percent === "";
      chip.classList.toggle("selected", none ? take === ""
        : takeKind === "percent" && chip.dataset.percent === take);
    }
  }

  function resetFields() {
    const settings = me();
    entry = "MARKET";
    stopKind = "price";
    takeKind = "price";
    riskKind = "percent";
    el.limit.value = "";
    el.stop.value = "";
    el.take.value = "";
    // Риск по умолчанию — из плана; клиент его не увеличивает и не запоминает.
    el.risk.value = settings.plan.risk_percent === null ? "" : String(settings.plan.risk_percent);
    el.expiry.replaceChildren(...settings.limit_expiry_minutes.map((minutes) => {
      const option = document.createElement("option");
      option.value = String(minutes);
      option.textContent = EXPIRY_LABELS[minutes] || `${minutes} мин`;
      option.selected = minutes === settings.limit_expiry_default;
      return option;
    }));
    for (const input of [el.limit, el.stop, el.take, el.risk]) input.classList.remove("invalid");
    leverageTouched = false;
    card = null;
    cardBody = null;
  }

  function applyCap() {
    cap = market ? leverageCap(market.leverage, side, me().plan.max_leverage) : null;
    if (!cap) {
      const reason = market && market.leverage_error ? market.leverage_error : "";
      blocked(NO_KEY_TEXT, reason);
      return false;
    }
    if (!leverageTouched) {
      const current = side === "SHORT" ? market.leverage.short : market.leverage.long;
      leverage = card && card.calc ? card.calc.suggested_leverage : current;
    }
    leverage = clampLeverage(leverage, cap);
    el.blocked.hidden = true;
    el.form.hidden = false;
    el.result.hidden = false;
    return true;
  }

  // Тело preview из полей; ошибка ввода — подсветка у поля и null (запрос не уходит).
  function buildBody() {
    const bad = [];
    const value = (input, required) => {
      const raw = input.value.trim();
      const parsed = parseAmount(raw);
      const ok = parsed !== null || (!required && raw === "");
      input.classList.toggle("invalid", !ok && raw !== "");
      if (!ok) bad.push(input);
      return parsed;
    };
    const stop = value(el.stop, true);
    const take = value(el.take, false);
    const risk = value(el.risk, true);
    const limit = entry === "LIMIT" ? value(el.limit, true) : null;
    if (bad.length || !cap) return null;
    return {
      symbol, side, entry_type: entry,
      stop: { kind: stopKind, value: stop },
      take: take === null ? null : { kind: takeKind, value: take },
      risk: { kind: riskKind, value: risk },
      leverage: clampLeverage(leverage, cap),
      limit_price: limit,
      expiry_minutes: entry === "LIMIT" ? Number(el.expiry.value) : null,
    };
  }

  // Тейк на 2R — цена сервера из последнего превью; нет (стопа нет или он не с той
  // стороны) — чип неактивен.
  function take2r() {
    return card && card.calc && card.calc.take_2r ? String(card.calc.take_2r) : null;
  }

  // --- живой пересчёт -------------------------------------------------------------------

  function cancelPreview() {
    if (previewTimer !== null) clearTimeout(previewTimer);
    previewTimer = null;
    previewSeq += 1;
    if (previewController) previewController.abort();
    previewController = null;
    pending = false;
    el.result.classList.remove("pending");
  }

  function previewState(message) {
    el.previewState.textContent = message || "";
    el.previewState.hidden = !message;
  }

  function schedulePreview(delay = PREVIEW_DEBOUNCE_MS) {
    if (!active || !cap) return;
    if (previewTimer !== null) clearTimeout(previewTimer);
    pending = true;
    el.result.classList.add("pending");
    openButton();
    previewTimer = setTimeout(runPreview, delay);
  }

  function showCard(next, body) {
    card = next;
    cardBody = body;
    renderPreview(el.out, card, { symbol, marginType: me().margin_type });
    if (chart) chart.setLines(chartLines(card));
    renderControls();
    openButton();
  }

  async function runPreview() {
    previewTimer = null;
    const body = buildBody();
    if (!body) {
      cancelPreview();
      showCard(null, null);
      previewState(el.stop.value.trim() === "" ? "Укажи стоп — без стопа сделку не открыть."
        : "Проверь поля, отмеченные красным.");
      return;
    }
    const seq = ++previewSeq;
    if (previewController) previewController.abort();
    previewController = new AbortController();
    let next;
    try {
      next = await api.post("/api/open/preview", body, { signal: previewController.signal });
    } catch (err) {
      if (seq !== previewSeq || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      if (err instanceof ApiError && err.status === 429) {
        schedulePreview(PREVIEW_RETRY_MS);
        return;
      }
      cancelPreview();
      showCard(null, null);
      previewState(err instanceof ApiError ? err.message : "Пересчёт не удался.");
      return;
    }
    if (seq !== previewSeq) return;            // пришёл ответ на устаревший ввод
    previewController = null;
    pending = false;
    el.result.classList.remove("pending");
    previewState("");
    const suggested = next.calc ? clampLeverage(next.calc.suggested_leverage, cap) : null;
    if (!leverageTouched && suggested !== null && suggested !== body.leverage) {
      // Плечо не трогали — предложенное сервером; меняет только маржу — пересчитать.
      leverage = suggested;
      card = next;
      renderControls();
      schedulePreview(0);
      return;
    }
    showCard(next, body);
  }

  function changed() {
    if (!active) return;
    renderControls();
    schedulePreview();
  }

  el.side.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-side]");
    if (!active || !button || button.dataset.side === side) return;
    side = button.dataset.side;
    leverageTouched = false;
    card = null;
    if (applyCap()) changed();
  });
  el.entry.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-entry]");
    if (!active || !button || button.dataset.entry === entry) return;
    entry = button.dataset.entry;
    // Цена лимита по умолчанию — текущая (строка сервера, без пересчёта).
    if (entry === "LIMIT" && !el.limit.value && market) el.limit.value = String(market.last);
    changed();
  });
  for (const [group, set] of [
    [el.stopKind, (kind) => { stopKind = kind; }],
    [el.takeKind, (kind) => { takeKind = kind; }],
    [el.riskKind, (kind) => { riskKind = kind; }],
  ]) {
    group.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-kind]");
      if (!active || !button) return;
      set(button.dataset.kind);
      changed();
    });
  }
  // Чипы ставят расстояние в % от входа (сторону задаёт Лонг/Шорт); «нет» — без тейка.
  for (const [chips, input, setKind] of [
    [el.stopChips, el.stop, () => { stopKind = "percent"; }],
    [el.takeChips, el.take, () => { takeKind = "percent"; }],
  ]) {
    chips.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-percent]");
      if (!active || !button) return;
      input.value = button.dataset.percent;
      if (button.dataset.percent) setKind();
      changed();
    });
  }
  el.takeChips.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-r]");
    const price = take2r();
    if (!active || !button || !price) return;
    el.take.value = price;
    takeKind = "price";
    changed();
  });
  for (const input of [el.limit, el.stop, el.take, el.risk]) {
    input.addEventListener("input", changed);
  }
  el.expiry.addEventListener("change", changed);
  el.leverage.addEventListener("input", () => {
    if (!active || !cap) return;
    leverageTouched = true;
    leverage = clampLeverage(el.leverage.value, cap);
    changed();
  });
  el.form.addEventListener("submit", (event) => event.preventDefault());
  el.retry.addEventListener("click", () => active && load());

  // --- рынок и график ------------------------------------------------------------------

  function marketPath() {
    return `/api/market/${encodeURIComponent(symbol)}?interval=${interval}`;
  }

  function chartState(message) {
    el.chartText.textContent = message || "";
    el.chartState.hidden = !message;
  }

  function stopPolling() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (controller) controller.abort();
    controller = null;
  }

  function schedule() {
    if (!active || document.hidden || timer !== null) return;
    const delay = failures ? Math.min(PRICE_POLL_MS * 2 ** failures, MAX_BACKOFF_MS)
      : PRICE_POLL_MS;
    timer = setTimeout(tick, delay);
  }

  async function loadCandles(myGen, signal, missing = null) {
    if (missing !== null) {
      asked = missing;
      askedAt = Date.now();
    }
    const data = await api.get(
      `/api/market/${encodeURIComponent(symbol)}/candles?interval=${interval}`, { signal });
    if (myGen !== gen || !chart) return;
    candlesAt = Date.now();
    chart.setCandles(data.candles);
    chartState(data.candles.length ? null : "Свечей нет.");
  }

  function showMarket(m) {
    market = m;
    el.last.textContent = `цена ${m.last}`;
  }

  async function load() {
    stopPolling();
    cancelPreview();
    const myGen = ++gen;
    failures = 0;
    asked = null;
    market = null;
    cap = null;
    el.stale.hidden = true;
    el.blocked.hidden = true;
    el.form.hidden = true;
    el.result.hidden = true;
    el.last.textContent = "";
    chartState("Загрузка…");
    renderControls();
    openButton();
    controller = new AbortController();
    const { signal } = controller;
    let m;
    try {
      m = await api.get(marketPath(), { signal });
    } catch (err) {
      if (myGen !== gen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      blocked("Не получилось", err instanceof ApiError ? err.message : "Ошибка на странице.");
      chartState(null);
      return;
    }
    if (myGen !== gen) return;
    showMarket(m);
    if (!chart) chart = createPriceChart(el.chart, { interval });
    chart.setTimeframe(interval);
    chart.setPrecision(m.contract.price_precision);
    chart.setType("candles");
    if (applyCap()) {
      renderControls();
      schedulePreview(0);
    }
    try {
      await loadCandles(myGen, signal, "none");
    } catch (err) {
      if (myGen !== gen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      chartState(err instanceof ApiError ? err.message : "График не загрузился.");
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
    controller = new AbortController();
    const { signal } = controller;
    try {
      const m = await api.get(marketPath(), { signal });
      if (myGen !== gen) return;
      const moved = !market || market.last !== m.last;
      const hadCap = cap;
      showMarket(m);
      failures = 0;
      el.stale.hidden = true;
      if (!applyCap()) {
        // Ключ отключили или максимум не прочитан — открытия нет, опрос идёт дальше.
      } else if (!hadCap || (moved && entry === "MARKET")) {
        renderControls();
        schedulePreview();
      } else {
        renderControls();
      }
      const lastTime = chart ? chart.lastTime() : null;
      const quoted = Date.parse(m.time) / 1000;
      const nextCandle = lastTime === null ? null : lastTime + intervalSeconds(interval);
      const missing = lastTime === null ? "none" : quoted >= nextCandle ? nextCandle : null;
      if (candlesDue({ missing, asked, askedAt, loadedAt: candlesAt, now: Date.now() })) {
        await loadCandles(myGen, signal, missing);
      } else if (missing === null && chart) {
        chart.updateLast(m.last);
      }
    } catch (err) {
      if (myGen !== gen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      failures += 1;
      const wait = Math.min(PRICE_POLL_MS * 2 ** failures, MAX_BACKOFF_MS) / 1000;
      el.stale.textContent = `${err instanceof ApiError ? err.message : "Нет связи."} `
        + `Цена может быть устаревшей — повторю через ${wait} с.`;
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
    } else if (active && market && timer === null && controller === null) {
      tick();
    }
  }

  // --- вход и уход -------------------------------------------------------------------

  return {
    // Перед show("trade"): монета и сторона с экрана рынка, поля — заново (риск из плана).
    open(nextSide, nextSymbol) {
      side = nextSide === "SHORT" ? "SHORT" : "LONG";
      symbol = nextSymbol;
      const saved = storage.load("interval");
      interval = INTERVALS.includes(saved) ? saved : "5m";
      resetFields();
    },
    enter() {
      if (active || !symbol) return;
      active = true;
      document.addEventListener("visibilitychange", onVisibility);
      if (native) tg.MainButton.onClick(onOpenClick);
      load();
    },
    leave() {
      if (!active) return;
      active = false;
      gen += 1;
      stopPolling();
      cancelPreview();
      document.removeEventListener("visibilitychange", onVisibility);
      if (native) {
        tg.MainButton.offClick(onOpenClick);
        tg.MainButton.setParams({ is_visible: false });
      } else {
        el.fallback.hidden = true;
        el.fallbackSecondary.hidden = false;
      }
      if (chart) {
        chart.destroy();
        chart = null;
      }
    },
  };
}
