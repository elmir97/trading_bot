// График экрана рынка (§5а): Lightweight Charts из своей статики, свечи или линия.
//
// - Встроенный логотип выключен (attributionLogo: false у каждого графика): он
//   вставляет <style>, а CSP style-src 'self' его режет; атрибуция — своя ссылка
//   в подвале страницы (§5а, решение владельца 15.10).
// - Время свечей приходит в UTC (unix-секунды) и в серию идёт как есть; на оси и в
//   перекрестье — локальное время пользователя через форматтеры, данные не сдвигаются.
//   Метки дат библиотека ставит по суткам UTC, подпись у них — локальная дата.
// - Цвета — из Telegram themeParams (запасные — переменные style.css), перекраска по
//   themeChanged; размер — autoSize (ResizeObserver библиотеки: поворот экрана, смена
//   высоты окна). destroy() снимает подписки и зовёт chart.remove().
// - Числа приходят строкой; Number — только чтобы нарисовать, расчётов здесь нет.
// - Линии уровней (вход, стоп, тейк, ликвидация) — setLines(): у формы значения превью,
//   у рынка — открытой позиции (П.2). Рисуются на видимой серии: скрытая серия
//   (visible: false) прячет и свои price lines, поэтому setType переносит их на линию и
//   обратно. Что нарисовано — в data-lines / data-lines-on контейнера (сценарии).

const LWC = window.LightweightCharts;

const INTERVAL_SECONDS = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 };

export function intervalSeconds(interval) {
  return INTERVAL_SECONDS[interval] ?? 300;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// themeParams Telegram (bg_color, text_color, …); вне Telegram — переменные style.css.
export function themeColors(tg) {
  const p = (tg && tg.themeParams) || {};
  return {
    background: p.bg_color || cssVar("--bg"),
    text: p.hint_color || cssVar("--hint"),
    grid: p.section_separator_color || p.secondary_bg_color || cssVar("--section"),
    line: p.link_color || p.button_color || cssVar("--link"),
    up: cssVar("--long"),
    down: cssVar("--short"),
    liq: cssVar("--liq"),
  };
}

// Вход — пунктир цветом текста, стоп — красная, тейк — зелёная, ликвидация — оранжевая точками.
function lineStyle(kind, colors) {
  const S = LWC.LineStyle;
  if (kind === "stop") return { color: colors.down, lineStyle: S.Solid };
  if (kind === "take") return { color: colors.up, lineStyle: S.Solid };
  if (kind === "liq") return { color: colors.liq, lineStyle: S.Dotted };
  return { color: colors.text, lineStyle: S.Dashed };
}

function formatters(interval) {
  const daily = interval === "1d";
  const crosshair = new Intl.DateTimeFormat("ru-RU", daily
    ? { day: "numeric", month: "short", year: "numeric" }
    : { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const clock = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const clockSeconds = new Intl.DateTimeFormat("ru-RU",
    { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const day = new Intl.DateTimeFormat("ru-RU", { day: "numeric" });
  const month = new Intl.DateTimeFormat("ru-RU", { month: "short" });
  const year = new Intl.DateTimeFormat("ru-RU", { year: "numeric" });
  const T = LWC.TickMarkType;
  const local = (time) => new Date(time * 1000);
  return {
    timeFormatter: (time) => crosshair.format(local(time)),
    tickMarkFormatter: (time, type) => {
      const date = local(time);
      if (type === T.Year) return year.format(date);
      if (type === T.Month) return month.format(date);
      if (type === T.DayOfMonth) return day.format(date);
      if (type === T.TimeWithSeconds) return clockSeconds.format(date);
      return clock.format(date);
    },
  };
}

function priceFormat(precision) {
  return { type: "price", precision, minMove: Number(`1e-${precision}`) };
}

export function createPriceChart(container, { tg = null, precision = 4, interval = "5m" } = {}) {
  let colors = themeColors(tg);
  let current = formatters(interval);
  let last = null;            // последняя свеча (числа) — её обновляет цена
  let destroyed = false;
  let levels = [];            // уровни: [{kind, price, title}] строками сервера
  let priceLines = [];        // [[серия, price line]] — снимать с той, на которой нарисованы
  let type = "candles";

  const chart = LWC.createChart(container, {
    autoSize: true,
    layout: {
      attributionLogo: false,
      background: { type: LWC.ColorType.Solid, color: colors.background },
      textColor: colors.text,
      fontSize: 11,
    },
    grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
    rightPriceScale: { borderColor: colors.grid },
    timeScale: {
      borderColor: colors.grid, timeVisible: interval !== "1d", secondsVisible: false,
      tickMarkFormatter: (time, type) => current.tickMarkFormatter(time, type),
    },
    localization: {
      locale: "ru-RU",
      timeFormatter: (time) => current.timeFormatter(time),
    },
    handleScale: { axisPressedMouseMove: { time: true, price: false } },
  });

  const candles = chart.addSeries(LWC.CandlestickSeries, {
    upColor: colors.up, downColor: colors.down, wickUpColor: colors.up,
    wickDownColor: colors.down, borderVisible: false,
    priceFormat: priceFormat(precision), lastValueVisible: true, priceLineVisible: true,
  });
  const line = chart.addSeries(LWC.LineSeries, {
    color: colors.line, lineWidth: 2, visible: false,
    priceFormat: priceFormat(precision), lastValueVisible: true, priceLineVisible: true,
  });

  function applyTheme() {
    if (destroyed) return;
    colors = themeColors(tg);
    chart.applyOptions({
      layout: { background: { type: LWC.ColorType.Solid, color: colors.background },
        textColor: colors.text },
      grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
      rightPriceScale: { borderColor: colors.grid },
      timeScale: { borderColor: colors.grid },
    });
    candles.applyOptions({ upColor: colors.up, downColor: colors.down,
      wickUpColor: colors.up, wickDownColor: colors.down });
    line.applyOptions({ color: colors.line });
    drawLines();
  }

  function drawLines() {
    for (const [series, priceLine] of priceLines) series.removePriceLine(priceLine);
    const host = type === "line" ? line : candles;
    priceLines = levels.map((level) => [host, host.createPriceLine({
      price: Number(level.price), title: level.title, lineWidth: 1, axisLabelVisible: true,
      ...lineStyle(level.kind, colors),
    })]);
    container.dataset.lines = levels.map((level) => `${level.kind}:${level.price}`).join(" ");
    container.dataset.linesOn = host === line ? "line" : "candles";   // где нарисованы на деле
  }

  // В Telegram тема приходит событием; в браузере стенда — от системной темы.
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  if (tg) tg.onEvent("themeChanged", applyTheme);
  if (media) media.addEventListener("change", applyTheme);

  return {
    setTimeframe(next) {
      interval = next;
      current = formatters(next);
      chart.applyOptions({ timeScale: { timeVisible: next !== "1d" } });
    },
    setPrecision(next) {
      candles.applyOptions({ priceFormat: priceFormat(next) });
      line.applyOptions({ priceFormat: priceFormat(next) });
    },
    setType(next) {
      if (destroyed) return;
      type = next === "line" ? "line" : "candles";
      candles.applyOptions({ visible: type === "candles" });
      line.applyOptions({ visible: type === "line" });
      drawLines();
    },
    // rows — [{time, open, high, low, close}] строками от сервера, время UTC.
    setCandles(rows) {
      const data = rows.map((r) => ({
        time: r.time, open: Number(r.open), high: Number(r.high),
        low: Number(r.low), close: Number(r.close),
      }));
      candles.setData(data);
      line.setData(data.map((c) => ({ time: c.time, value: c.close })));
      last = data.length ? { ...data[data.length - 1] } : null;
      chart.timeScale().fitContent();
    },
    // Последняя (незакрытая) свеча от свежей цены — §5а. Новая свеча не
    // достраивается здесь: её приносит перезагрузка свечей.
    updateLast(value) {
      if (!last) return;
      const close = Number(value);
      if (!Number.isFinite(close)) return;
      last = { ...last, close, high: Math.max(last.high, close), low: Math.min(last.low, close) };
      candles.update(last);
      line.update({ time: last.time, value: close });
    },
    lastTime() {
      return last ? last.time : null;
    },
    setLines(next) {
      if (destroyed) return;
      levels = next;
      drawLines();
    },
    applyTheme,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      if (tg) tg.offEvent("themeChanged", applyTheme);
      if (media) media.removeEventListener("change", applyTheme);
      delete container.dataset.lines;
      delete container.dataset.linesOn;
      chart.remove();
    },
  };
}
