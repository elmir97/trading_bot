// Экран «Рынок» (§5а, как в Walt): монета и поиск, цена и изменение за свечу,
// график, таймфреймы, детали, кнопки «Лонг»/«Шорт».
//
// Обновление — без частого опроса. Цена: GET /api/market раз в PRICE_POLL_MS (3 с,
// §5а) — следующий запрос только после ответа на предыдущий (setTimeout, не
// setInterval), один в полёте. Сервер держит общий кэш процесса: цена 2 с, свечи 1m
// 5 с и прочие 30 с, фандинг/ОИ 60 с, плечо 5 мин (app/api/market.py) — сколько бы
// вкладок ни смотрели монету, к BingX уходит не больше ~20 ticker + ≤12 klines +
// 2 premium/ОИ в минуту на символ (≈6 за 10 с при окне публичных ручек 500 за 10 с —
// около 1 %) и плечо (приватная, 5/с) раз в 5 мин на пользователя. Чаще 2 с опрос
// бесполезен — та же цена из кэша. Свечи (GET …/candles) — только на смене свечи
// (время котировки дошло до конца последней) и раз в CANDLES_REFRESH_MS; между ними
// последнюю свечу двигает цена. Новая свеча запрошена, а не пришла (или свечей нет) —
// повтор не чаще CANDLE_RETRY_MS, не на каждом опросе (candlesDue); сервер держит
// свечи не дольше закрытия последней. Ошибка — пауза растёт 6 → 12 → 24 → 30 с; вкладка
// скрыта (visibilitychange) или ушли с экрана — опрос стоит, запрос в полёте
// отменяется (AbortController).

import { ApiError } from "./api.js";
import { createPriceChart, intervalSeconds } from "./chart.js";

export const INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"];
export const PRICE_POLL_MS = 3000;
export const CANDLES_REFRESH_MS = 60000;
export const CANDLE_RETRY_MS = 10000;
export const MAX_BACKOFF_MS = 30000;
export const SEARCH_DEBOUNCE_MS = 300;
const DEFAULT_INTERVAL = "5m";
const RECENT_LIMIT = 8;
const INTERVAL_LABELS = { "1m": "1м", "5m": "5м", "15m": "15м", "1h": "1ч", "4h": "4ч", "1d": "1д" };

function $(id) {
  return document.getElementById(id);
}

// --- показ чисел: строка сервера как есть, только знак/стрелка/единицы ---------

function text(value) {
  return value === null || value === undefined ? "—" : String(value);
}

function isZero(value) {
  return /^[-+]?0*(\.0*)?$/.test(String(value));
}

function signed(value) {
  const s = String(value);
  return s.startsWith("-") || isZero(s) ? s : `+${s}`;
}

function direction(value) {
  if (value === null || value === undefined || isZero(value)) return "flat";
  return String(value).startsWith("-") ? "down" : "up";
}

// Объём/ОИ — как open_interest_text в чате: млрд — 2 знака, млн и тыс. — 1.
export function compactUsd(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} млрд $`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} млн $`;
  return `${(n / 1e3).toFixed(1)} тыс. $`;
}

export function utcOffset(date) {
  const minutes = -date.getTimezoneOffset();
  const abs = Math.abs(minutes);
  const tail = abs % 60 ? `:${String(abs % 60).padStart(2, "0")}` : "";
  return `UTC${minutes < 0 ? "−" : "+"}${Math.floor(abs / 60)}${tail}`;
}

// Как funding_text в чате: «через 2 ч 15 мин», прошло — «начисление сейчас».
export function fundingLeft(nextIso, now = Date.now()) {
  const seconds = Math.floor((Date.parse(nextIso) - now) / 1000);
  if (!Number.isFinite(seconds)) return "";
  if (seconds <= 0) return "начисление сейчас";
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  return hours ? `через ${hours} ч ${minutes % 60} мин` : `через ${minutes} мин`;
}

// Тянуть ли свечи на этом опросе. missing — какой свечи не хватает: начало новой
// (unix, с), "none" — на графике свечей нет, null — всё на месте. Недостающую — сразу,
// но ту же самую повторно не чаще CANDLE_RETRY_MS; иначе — раз в CANDLES_REFRESH_MS.
export function candlesDue({ missing, asked, askedAt, loadedAt, now }) {
  if (missing === null) return now - loadedAt >= CANDLES_REFRESH_MS;
  return missing !== asked || now - askedAt >= CANDLE_RETRY_MS;
}

const clockFormat = new Intl.DateTimeFormat("ru-RU",
  { hour: "2-digit", minute: "2-digit", second: "2-digit" });

// --- экран -----------------------------------------------------------------------------

export function createMarketScreen({ api, tg, storage, onSide, onFatal }) {
  const native = tg && tg.isVersionAtLeast("7.10");   // MainButton + SecondaryButton
  const el = {
    body: $("market-body"),
    head: $("market-head"),
    symbolButton: $("market-symbol-button"),
    coin: $("market-coin"),
    symbol: $("market-symbol"),
    leverage: $("market-leverage"),
    favorite: $("market-favorite"),
    last: $("market-last"),
    change: $("market-candle-change"),
    changeValue: $("market-candle-value"),
    changeInterval: $("market-interval-label"),
    time: $("market-time"),
    stale: $("market-stale"),
    chart: $("market-chart"),
    chartState: $("market-chart-state"),
    chartText: $("market-chart-text"),
    chartRetry: $("market-chart-retry"),
    intervals: $("market-intervals"),
    types: $("market-types"),
    dayChange: $("market-day-change"),
    volume: $("market-volume"),
    openInterest: $("market-oi"),
    funding: $("market-funding"),
    detailsNote: $("market-details-note"),
    error: $("market-error"),
    errorText: $("market-error-text"),
    errorRetry: $("market-retry"),
    search: $("market-search"),
    searchInput: $("market-search-input"),
    searchClose: $("market-search-close"),
    searchState: $("market-search-state"),
    searchList: $("market-search-list"),
    fallback: $("fallback-buttons"),
    fallbackMain: $("fallback-main"),
    fallbackSecondary: $("fallback-secondary"),
  };

  let active = false;
  let symbol = storage.load("symbol", null);
  let interval = INTERVALS.includes(storage.load("interval")) ? storage.load("interval")
    : DEFAULT_INTERVAL;
  let chartType = storage.load("chartType") === "line" ? "line" : "candles";
  let chart = null;
  let contract = null;       // contract из /api/market (точность цены)
  let loaded = false;        // рынок по монете хотя бы раз показан
  let gen = 0;               // номер «загрузки»: смена монеты/ТФ делает старые ответы лишними
  let timer = null;
  let controller = null;
  let failures = 0;
  let candlesAt = 0;         // последняя успешная загрузка свечей
  let asked = null;          // какую недостающую свечу уже запрашивали (см. candlesDue)
  let askedAt = 0;
  let searchTimer = null;
  let searchController = null;
  let searchSeq = 0;

  // --- кнопки «Лонг»/«Шорт» --------------------------------------------------------

  const goLong = () => active && loaded && onSide("LONG", symbol);
  const goShort = () => active && loaded && onSide("SHORT", symbol);

  function sideButtons(visible) {
    if (native) {
      const long = getComputedStyle(document.documentElement).getPropertyValue("--long").trim();
      const short = getComputedStyle(document.documentElement).getPropertyValue("--short").trim();
      tg.MainButton.setParams({ text: "↗ Лонг", color: long, text_color: "#ffffff",
        is_active: true, is_visible: visible });
      tg.SecondaryButton.setParams({ text: "↘ Шорт", color: short, text_color: "#ffffff",
        position: "right", is_active: true, is_visible: visible });
    } else {
      el.fallbackMain.textContent = "↗ Лонг";
      el.fallbackSecondary.textContent = "↘ Шорт";
      el.fallback.hidden = !visible;
    }
  }

  // Обработчики статичных элементов вешаются один раз; пока экран не активен, молчат.
  el.fallbackMain.addEventListener("click", goLong);
  el.fallbackSecondary.addEventListener("click", goShort);

  // --- состояния -----------------------------------------------------------------

  function chartState(message, { retry = false } = {}) {
    el.chartText.textContent = message || "";
    el.chartRetry.hidden = !retry;
    el.chartState.hidden = !message;
  }

  function screenError(err) {
    el.errorText.textContent = err instanceof ApiError ? err.message
      : "Ошибка на странице — открой заново.";
    el.error.hidden = false;
    el.body.hidden = true;
    sideButtons(false);
  }

  function fatal(err) {
    // 401 — сессия истекла: экран ошибки приложения, без «Повторить».
    if (err instanceof ApiError && err.status === 401) {
      onFatal(err);
      return true;
    }
    return false;
  }

  function resetView() {
    el.error.hidden = true;
    el.body.hidden = false;
    el.stale.hidden = true;
    el.last.textContent = "—";
    el.change.textContent = "—";
    el.change.className = "pill flat";
    el.changeValue.textContent = "";
    el.time.textContent = "";
    for (const node of [el.dayChange, el.volume, el.openInterest, el.funding]) {
      node.textContent = "—";
    }
    el.detailsNote.hidden = true;
    el.leverage.hidden = true;
    chartState("Загрузка…");
  }

  // --- отрисовка -------------------------------------------------------------------

  function renderHead() {
    el.symbol.textContent = symbol || "Выбери монету";
    el.coin.textContent = symbol ? symbol.charAt(0) : "?";
    const favorite = symbol && favorites().includes(symbol);
    el.favorite.textContent = favorite ? "★" : "☆";
    el.favorite.setAttribute("aria-pressed", favorite ? "true" : "false");
    el.favorite.hidden = !symbol;
    for (const button of el.intervals.querySelectorAll("button")) {
      button.classList.toggle("selected", button.dataset.interval === interval);
    }
    for (const button of el.types.querySelectorAll("button")) {
      button.classList.toggle("selected", button.dataset.type === chartType);
    }
  }

  function renderMarket(m) {
    contract = m.contract;
    el.last.textContent = text(m.last);
    const pct = m.candle_change.percent;
    const dir = direction(pct);
    const arrow = dir === "up" ? "↑" : dir === "down" ? "↓" : "";
    el.change.textContent = pct === null ? "—" : `${arrow}${String(pct).replace(/^[-+]/, "")}%`;
    el.change.className = `pill ${dir}`;
    el.changeValue.textContent = m.candle_change.value === null ? ""
      : `${signed(m.candle_change.value)} $`;
    el.changeInterval.textContent = INTERVAL_LABELS[m.interval] || m.interval;
    const at = new Date(m.time);
    el.time.textContent = Number.isNaN(at.getTime()) ? ""
      : `Цена на ${clockFormat.format(at)} ${utcOffset(at)}`;

    const day = m.day_change;
    el.dayChange.textContent = day.percent === null ? "—"
      : `${signed(day.percent)}%${day.value === null ? "" : ` · ${signed(day.value)} $`}`;
    el.dayChange.className = direction(day.percent);
    el.volume.textContent = compactUsd(m.quote_volume);
    el.openInterest.textContent = compactUsd(m.open_interest);
    el.funding.textContent = m.funding_rate_percent === null ? "—"
      : `${signed(m.funding_rate_percent)}%`
        + (m.next_funding_time ? ` · ${fundingLeft(m.next_funding_time)}` : "");
    el.detailsNote.textContent = m.details_error || "";
    el.detailsNote.hidden = !m.details_error;

    // Максимум плеча контракта для счёта (по ключу BingX); нет ключа или сбой — плашки нет.
    const maxLeverage = m.contract.max_leverage;
    el.leverage.textContent = maxLeverage ? `${maxLeverage}×` : "";
    el.leverage.hidden = !maxLeverage;
  }

  // --- загрузка и опрос ------------------------------------------------------------

  function stopPolling() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (controller) controller.abort();
    controller = null;
  }

  function schedule() {
    if (!active || document.hidden || !symbol || timer !== null) return;
    const delay = failures ? Math.min(PRICE_POLL_MS * 2 ** failures, MAX_BACKOFF_MS)
      : PRICE_POLL_MS;
    timer = setTimeout(tick, delay);
  }

  function marketPath() {
    return `/api/market/${encodeURIComponent(symbol)}?interval=${interval}`;
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
    if (!data.candles.length) {
      chart.setCandles([]);
      chartState("Свечей нет — биржа не вернула данных за этот таймфрейм.");
      return;
    }
    chart.setCandles(data.candles);
    chartState(null);
  }

  // Полная загрузка: при входе, смене монеты и таймфрейма.
  async function load() {
    stopPolling();
    const myGen = ++gen;
    failures = 0;
    asked = null;
    loaded = false;
    resetView();
    renderHead();
    sideButtons(false);
    controller = new AbortController();
    const { signal } = controller;
    let m;
    try {
      m = await api.get(marketPath(), { signal });
    } catch (err) {
      if (myGen !== gen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      if (err instanceof ApiError && err.status === 404) {
        // Монеты больше нет на бирже — убрать из «последней» и открыть поиск.
        storage.remove("symbol");
        symbol = null;
        openSearch(err.message);
        return;
      }
      if (!(err instanceof ApiError)) console.error(err);
      screenError(err);
      return;
    }
    if (myGen !== gen) return;
    if (!chart) chart = createPriceChart(el.chart, { tg, interval });
    chart.setTimeframe(interval);
    chart.setPrecision(m.contract.price_precision);
    chart.setType(chartType);
    renderMarket(m);
    loaded = true;
    sideButtons(true);
    try {
      await loadCandles(myGen, signal, "none");
    } catch (err) {
      if (myGen !== gen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      chartState(err instanceof ApiError ? err.message : "График не загрузился.", { retry: true });
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
      renderMarket(m);
      failures = 0;
      el.stale.hidden = true;
      const lastTime = chart ? chart.lastTime() : null;
      const quoted = Date.parse(m.time) / 1000;
      const next = lastTime === null ? null : lastTime + intervalSeconds(interval);
      const missing = lastTime === null ? "none" : quoted >= next ? next : null;
      if (candlesDue({ missing, asked, askedAt, loadedAt: candlesAt, now: Date.now() })) {
        await loadCandles(myGen, signal, missing);
      } else if (missing === null) {
        // Пока новая свеча не пришла, цена её периода прошлую не двигает.
        chart.updateLast(m.last);
      }
    } catch (err) {
      if (myGen !== gen || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      failures += 1;
      const wait = Math.min(PRICE_POLL_MS * 2 ** failures, MAX_BACKOFF_MS) / 1000;
      el.stale.textContent = `${err instanceof ApiError ? err.message : "Нет связи."} `
        + `Цены могут быть устаревшими — повторю через ${wait} с.`;
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

  // --- монета: избранное, последние, поиск ---------------------------------------

  function favorites() {
    const list = storage.load("favorites", []);
    return Array.isArray(list) ? list.filter((s) => typeof s === "string") : [];
  }

  function recent() {
    const list = storage.load("recent", []);
    return Array.isArray(list) ? list.filter((s) => typeof s === "string") : [];
  }

  function choose(next) {
    symbol = next;
    storage.save("symbol", next);
    storage.save("recent", [next, ...recent().filter((s) => s !== next)].slice(0, RECENT_LIMIT));
    closeSearch();
    load();
  }

  function listItem(label, hint) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "search-item";
    button.dataset.symbol = label;
    const coin = document.createElement("span");
    coin.className = "coin";
    coin.textContent = label.charAt(0);
    const name = document.createElement("span");
    name.textContent = label;
    button.append(coin, name);
    if (hint) {
      const note = document.createElement("span");
      note.className = "muted small";
      note.textContent = hint;
      button.append(note);
    }
    item.append(button);
    return item;
  }

  function renderSearchList(groups, emptyText) {
    el.searchList.replaceChildren();
    let count = 0;
    for (const [title, symbols] of groups) {
      if (!symbols.length) continue;
      if (title) {
        const header = document.createElement("li");
        header.className = "search-group muted small";
        header.textContent = title;
        el.searchList.append(header);
      }
      for (const s of symbols) {
        el.searchList.append(listItem(s));
        count += 1;
      }
    }
    el.searchState.textContent = count ? "" : emptyText;
    el.searchState.hidden = count > 0;
  }

  function showSaved() {
    const fav = favorites();
    renderSearchList([["Избранное", fav], ["Последние", recent().filter((s) => !fav.includes(s))]],
      "Введи название монеты: xrp, btc, near…");
  }

  async function runSearch(query) {
    const seq = ++searchSeq;
    if (searchController) searchController.abort();
    searchController = new AbortController();
    el.searchState.textContent = "Ищу…";
    el.searchState.hidden = false;
    try {
      const found = await api.get(`/api/symbols?q=${encodeURIComponent(query)}`,
        { signal: searchController.signal });
      if (seq !== searchSeq) return;
      renderSearchList([[null, found.map((c) => c.symbol)]], "Не нашёл такой монеты на BingX.");
    } catch (err) {
      if (seq !== searchSeq || (err && err.name === "AbortError")) return;
      if (fatal(err)) return;
      el.searchList.replaceChildren();
      el.searchState.textContent = err instanceof ApiError ? err.message : "Поиск не удался.";
      el.searchState.hidden = false;
    }
  }

  function cancelSearch() {
    if (searchTimer !== null) clearTimeout(searchTimer);
    searchTimer = null;
    searchSeq += 1;
    if (searchController) searchController.abort();
    searchController = null;
  }

  function openSearch(note) {
    stopPolling();
    el.body.hidden = true;
    el.error.hidden = true;
    el.head.hidden = true;
    sideButtons(false);
    el.search.hidden = false;
    el.searchClose.hidden = !symbol;
    el.searchInput.value = "";
    showSaved();
    if (note) {
      el.searchState.textContent = note;
      el.searchState.hidden = false;
    }
  }

  function closeSearch() {
    cancelSearch();
    el.search.hidden = true;
    el.head.hidden = false;
    el.body.hidden = false;
  }

  el.symbolButton.addEventListener("click", () => active && openSearch());
  el.searchClose.addEventListener("click", () => {
    if (!active || !symbol) return;
    closeSearch();
    load();
  });
  el.searchInput.addEventListener("input", () => {
    if (!active) return;
    cancelSearch();
    const query = el.searchInput.value.trim();
    if (!query) {
      showSaved();
      return;
    }
    searchTimer = setTimeout(() => {
      searchTimer = null;
      runSearch(query);
    }, SEARCH_DEBOUNCE_MS);
  });
  el.searchList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-symbol]");
    if (active && button) choose(button.dataset.symbol);
  });
  el.favorite.addEventListener("click", () => {
    if (!active || !symbol) return;
    const fav = favorites();
    storage.save("favorites", fav.includes(symbol) ? fav.filter((s) => s !== symbol)
      : [symbol, ...fav]);
    renderHead();
  });

  // --- таймфрейм, тип графика, повтор --------------------------------------------

  el.intervals.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-interval]");
    if (!active || !button || button.dataset.interval === interval) return;
    interval = button.dataset.interval;
    storage.save("interval", interval);
    load();
  });
  el.types.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-type]");
    if (!active || !button) return;
    chartType = button.dataset.type === "line" ? "line" : "candles";
    storage.save("chartType", chartType);
    if (chart) chart.setType(chartType);
    renderHead();
  });
  el.errorRetry.addEventListener("click", () => active && load());
  el.chartRetry.addEventListener("click", () => active && load());

  // --- вход и уход ---------------------------------------------------------------

  return {
    enter() {
      if (active) return;
      active = true;
      document.addEventListener("visibilitychange", onVisibility);
      if (native) {
        tg.MainButton.onClick(goLong);
        tg.SecondaryButton.onClick(goShort);
      }
      // Старт (§5а): последняя монета, иначе первая из избранного, иначе поиск.
      if (!symbol) symbol = favorites()[0] || null;
      if (symbol) {
        el.search.hidden = true;
        el.head.hidden = false;
        load();
      } else {
        openSearch();
      }
    },
    leave() {
      if (!active) return;
      active = false;
      gen += 1;
      stopPolling();
      cancelSearch();
      document.removeEventListener("visibilitychange", onVisibility);
      sideButtons(false);
      if (native) {
        tg.MainButton.offClick(goLong);
        tg.SecondaryButton.offClick(goShort);
      }
      if (chart) {
        chart.destroy();
        chart = null;
      }
      loaded = false;
    },
    // С вкладки «Позиции» (П.1): монета позиции — следующий вход на экран её покажет.
    setSymbol(next) {
      if (typeof next !== "string" || !next) return;
      symbol = next;
      storage.save("symbol", next);
      storage.save("recent", [next, ...recent().filter((s) => s !== next)].slice(0, RECENT_LIMIT));
      if (active) {
        closeSearch();
        load();
      }
    },
    // «🔄 Обновить» из меню.
    refresh() {
      if (active && symbol && el.search.hidden) load();
    },
    symbol: () => symbol,
    contract: () => contract,
  };
}
