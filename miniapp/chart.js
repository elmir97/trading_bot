// График экрана рынка (§5а): Lightweight Charts из своей статики, свечи или линия.
//
// - Встроенный логотип выключен (attributionLogo: false у каждого графика): он
//   вставляет <style>, а CSP style-src 'self' его режет; атрибуция — своя ссылка
//   в подвале страницы (§5а, решение владельца 15.10).
// - Время свечей приходит в UTC (unix-секунды) и в серию идёт как есть; на оси и в
//   перекрестье — локальное время пользователя через форматтеры, данные не сдвигаются.
//   Метки дат библиотека ставит по суткам UTC, подпись у них — локальная дата.
// - Цвета — токены style.css текущей темы (theme.js), перекраска по THEME_EVENT; размер —
//   autoSize (ResizeObserver библиотеки: поворот экрана, смена высоты окна). destroy()
//   снимает подписки и зовёт chart.remove().
// - Числа приходят строкой; Number — только чтобы нарисовать, расчётов здесь нет.
// - Линии уровней (вход, стоп, тейк, ликвидация) — setLines(): у формы значения превью,
//   у рынка — открытой позиции (П.2). Рисуются на видимой серии: скрытая серия
//   (visible: false) прячет и свои price lines, поэтому setType переносит их на линию и
//   обратно. Что нарисовано — в data-lines / data-lines-on контейнера (сценарии).
// - Автомасштаб цены включает вход, стоп и тейк (решение владельца 17.10) и «новый» экрана 5:
//   уровень за краем графика не виден. Ликвидацию — нет: она бывает далеко и ужимает график; у рынка
//   её значение — строкой в блоке «Позиция».

import { THEME_EVENT, token } from "./theme.js";

const LWC = window.LightweightCharts;
// Подписи осей — моноширинным, как числа страницы (fonts.css; до загрузки — системный).
const MONO = '"IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace';

function candleColors(colors) {
  return { upColor: colors.ink, downColor: colors.red, wickUpColor: colors.ink,
    wickDownColor: colors.red };
}

export const SCALED_KINDS = ["entry", "stop", "take", "new-stop", "new-take"];

// Диапазон цены серии, расширенный уровнями SCALED_KINDS. Нет данных — как у библиотеки.
export function scaledRange(range, levels) {
  if (!range || !range.priceRange) return range;
  const prices = levels.filter((level) => SCALED_KINDS.includes(level.kind))
    .map((level) => Number(level.price)).filter(Number.isFinite);
  if (!prices.length) return range;
  return {
    ...range,
    priceRange: {
      minValue: Math.min(range.priceRange.minValue, ...prices),
      maxValue: Math.max(range.priceRange.maxValue, ...prices),
    },
  };
}

const INTERVAL_SECONDS = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 };

export function intervalSeconds(interval) {
  return INTERVAL_SECONDS[interval] ?? 300;
}

// Цвета — токены style.css текущей темы (theme.js): график на карточке, рост — чернилами,
// падение — красным (макет «Рынок»).
export function themeColors() {
  return {
    background: token("--card"),
    text: token("--muted"),
    grid: token("--chart-grid"),
    border: token("--rule"),
    ink: token("--ink"),
    red: token("--red"),
    blue: token("--blue"),
  };
}

// Вход — пунктир чернилами, стоп — красная, тейк — синяя, ликвидация — серая точками;
// стоп/тейк после действия (экран 5, new_level сервера) — пунктир своим цветом.
function lineStyle(kind, colors) {
  const S = LWC.LineStyle;
  if (kind === "stop") return { color: colors.red, lineStyle: S.Solid };
  if (kind === "take") return { color: colors.blue, lineStyle: S.Solid };
  if (kind === "new-stop") return { color: colors.red, lineStyle: S.Dashed };
  if (kind === "new-take") return { color: colors.blue, lineStyle: S.Dashed };
  if (kind === "liq") return { color: colors.text, lineStyle: S.Dotted };
  return { color: colors.ink, lineStyle: S.Dashed };
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

// Высота ценовой плашки на шкале (шрифт 11 + поля): деление ближе к плашке линии или
// последней цены по вертикали выглядывало из-под неё хвостами («1.5500» под «1.5631») —
// такое деление не подписываем.
export const LABEL_GAP_PX = 18;

export function createPriceChart(container, { precision = 4, interval = "5m" } = {}) {
  let colors = themeColors();
  let current = formatters(interval);
  let last = null;            // последняя свеча (числа) — её обновляет цена
  let destroyed = false;
  let levels = [];            // уровни: [{kind, price, title}] строками сервера
  let priceLines = [];        // [[серия, price line]] — снимать с той, на которой нарисованы
  let type = "candles";
  let digits = precision;
  let host = null;            // серия, на которой линии (свечи или «линия»); до серий — null
  let ticks = [];             // последние подписи делений — для сценариев (--scenario)

  const chart = LWC.createChart(container, {
    autoSize: true,
    layout: {
      attributionLogo: false,
      background: { type: LWC.ColorType.Solid, color: colors.background },
      textColor: colors.text,
      fontSize: 11,
      fontFamily: MONO,
    },
    grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
    rightPriceScale: { borderColor: colors.border },
    timeScale: {
      borderColor: colors.border, timeVisible: interval !== "1d", secondsVisible: false,
      tickMarkFormatter: (time, type) => current.tickMarkFormatter(time, type),
    },
    localization: {
      locale: "ru-RU",
      timeFormatter: (time) => current.timeFormatter(time),
      // Только деления шкалы: плашки линий и последней цены форматирует серия.
      tickmarksPriceFormatter: (prices) => tickLabels(prices),
    },
    handleScale: { axisPressedMouseMove: { time: true, price: false } },
  });

  const autoscaleInfoProvider = (original) => scaledRange(original(), levels);
  const candles = chart.addSeries(LWC.CandlestickSeries, {
    ...candleColors(colors), borderVisible: false, autoscaleInfoProvider,
    priceFormat: priceFormat(precision), lastValueVisible: true, priceLineVisible: true,
  });
  const line = chart.addSeries(LWC.LineSeries, {
    color: colors.ink, lineWidth: 2, visible: false, autoscaleInfoProvider,
    priceFormat: priceFormat(precision), lastValueVisible: true, priceLineVisible: true,
  });

  host = candles;

  // Подписи делений: ближе LABEL_GAP_PX к плашке (уровень линии или последняя цена) — пусто.
  function tickLabels(prices) {
    const busy = levels.map((level) => Number(level.price));
    if (last) busy.push(last.close);
    const covered = host ? busy.map((p) => host.priceToCoordinate(p)).filter((y) => y !== null)
      : [];
    ticks = prices.map((price) => {
      const y = host ? host.priceToCoordinate(price) : null;
      const hidden = y !== null && covered.some((c) => Math.abs(c - y) < LABEL_GAP_PX);
      return { price, y, text: hidden ? "" : price.toFixed(digits) };
    });
    return ticks.map((t) => t.text);
  }

  // Для сценариев (--scenario): подписи делений (текст — последнего форматирования,
  // координата — сейчас: не пересчитались после смены линий — видно) и координаты плашек.
  // Серия — своя, по видимому типу, а не `host` форматтера: иначе ошибка в `host` не видна.
  container.tickLabels = () => {
    const shown = type === "line" ? line : candles;
    const y = (p) => shown.priceToCoordinate(p);
    return {
      type,
      ticks: ticks.map((t) => ({ ...t, y: y(t.price) })),
      labels: [...levels.map((level) => Number(level.price)), ...(last ? [last.close] : [])]
        .map(y),
    };
  };

  // Для сценариев (--scenario): какие уровни сейчас в видимом диапазоне цены.
  container.levelsInView = () => {
    const host = type === "line" ? line : candles;
    const height = chart.paneSize().height;
    return levels.map((level) => {
      const y = host.priceToCoordinate(Number(level.price));
      return `${level.kind}:${y !== null && y >= 0 && y <= height ? "in" : "out"}`;
    });
  };

  function applyTheme() {
    if (destroyed) return;
    colors = themeColors();
    chart.applyOptions({
      layout: { background: { type: LWC.ColorType.Solid, color: colors.background },
        textColor: colors.text },
      grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
      rightPriceScale: { borderColor: colors.border },
      timeScale: { borderColor: colors.border },
    });
    candles.applyOptions(candleColors(colors));
    line.applyOptions({ color: colors.ink });
    drawLines();
  }

  function drawLines() {
    for (const [series, priceLine] of priceLines) series.removePriceLine(priceLine);
    host = type === "line" ? line : candles;
    priceLines = levels.map((level) => [host, host.createPriceLine({
      price: Number(level.price), title: level.title, lineWidth: 1, axisLabelVisible: true,
      ...lineStyle(level.kind, colors),
    })]);
    container.dataset.lines = levels.map((level) => `${level.kind}:${level.price}`).join(" ");
    container.dataset.linesOn = host === line ? "line" : "candles";   // где нарисованы на деле
  }

  // Смену темы (Telegram или системную) разбирает theme.js и шлёт THEME_EVENT — data-theme
  // к этому моменту уже стоит, токены новые.
  document.addEventListener(THEME_EVENT, applyTheme);

  return {
    setTimeframe(next) {
      interval = next;
      current = formatters(next);
      chart.applyOptions({ timeScale: { timeVisible: next !== "1d" } });
    },
    setPrecision(next) {
      digits = next;
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
      document.removeEventListener(THEME_EVENT, applyTheme);
      delete container.dataset.lines;
      delete container.dataset.linesOn;
      delete container.levelsInView;
      delete container.tickLabels;
      chart.remove();
    },
  };
}
