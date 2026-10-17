// Показ формы сделки (§5а, экран 2): ответ POST /api/open/preview → строки экрана.
// Объём, риск $ с комиссией, RR, маржа, ликвидация, нарушения — только из ответа
// сервера (строки, уже округлённые): здесь ничего не считается, только подписи.
// Плечо: потолок формы — min(максимум стороны контракта по ключу, max_leverage плана);
// нет ключа или максимум не прочитан — потолка нет, открытия нет (решение владельца 17.10).

export const NO_KEY_TEXT = "Подключи ключи биржи";

const SIDE_TEXT = { LONG: "ЛОНГ", SHORT: "ШОРТ" };
const MARGIN_TEXT = { ISOLATED: "изол.", CROSSED: "кросс" };

function text(value) {
  return value === null || value === undefined ? "—" : String(value);
}

// Ввод пользователя: «1,5» с русской клавиатуры iOS → «1.5»; не число или ≤ 0 — null
// (подсветка у поля, запрос не уходит). Значение уходит на сервер строкой.
export function parseAmount(raw) {
  const value = String(raw ?? "").trim().replace(",", ".");
  if (!/^\d+(\.\d+)?$/.test(value) || /^0*(\.0*)?$/.test(value)) return null;
  return value;
}

// Потолок плеча формы: min(максимум стороны, план). leverage — Leverage из /api/market
// (null — нет ключа или сбой чтения): потолка нет — форма открытие не даёт.
export function leverageCap(leverage, side, planMax) {
  if (!leverage) return null;
  const sideMax = side === "SHORT" ? leverage.max_short : leverage.max_long;
  if (!Number.isInteger(sideMax) || sideMax < 1) return null;
  return Number.isInteger(planMax) && planMax >= 1 ? Math.min(sideMax, planMax) : sideMax;
}

// Выше потолка выбрать нельзя: и ползунок (max = потолок), и тело запроса — через это.
export function clampLeverage(value, cap) {
  const n = Math.trunc(Number(value));
  if (!Number.isFinite(n) || n < 1) return 1;
  return Math.min(n, cap);
}

export function baseAsset(symbol) {
  return String(symbol || "").replace(/-USDT$/, "");
}

// Главная кнопка по решению 05.10: есть BLOCK (или отказ) — кнопки нет; есть WARN —
// «Открыть всё равно»; иначе «Открыть ЛОНГ 52 NEAR».
export function openAction(card, side, symbol) {
  if (!card || !card.calc || !card.can_open) return { visible: false, text: "", warn: false };
  if (card.has_warnings) return { visible: true, text: "Открыть всё равно", warn: true };
  return {
    visible: true,
    text: `Открыть ${SIDE_TEXT[side]} ${card.calc.quantity} ${baseAsset(symbol)}`,
    warn: false,
  };
}

// Линии компактного графика — из ответа preview (не из полей ввода, §5а).
export function chartLines(card) {
  if (!card || !card.calc || !card.levels) return [];
  const lines = [
    { kind: "entry", price: card.calc.entry_price, title: "вход" },
    { kind: "stop", price: card.levels.stop_loss, title: "стоп" },
    { kind: "take", price: card.levels.take_profit, title: "тейк" },
    { kind: "liq", price: card.calc.liq_estimate, title: "ликв. ≈" },
  ];
  return lines.filter((line) => line.price !== null && line.price !== undefined);
}

function issueItem(icon, message) {
  const item = document.createElement("li");
  item.textContent = `${icon} ${message}`;
  return item;
}

// Отрисовка ответа preview. el — элементы блока результата; meta — {symbol, marginType}.
export function renderPreview(el, card, { symbol, marginType }) {
  const c = card ? card.calc : null;
  const base = baseAsset(symbol);
  el.quantity.textContent = c ? `${c.quantity} ${base} ≈ ${c.notional} $` : "—";
  el.margin.textContent = c ? `${c.margin} $ ${MARGIN_TEXT[marginType] || ""}`.trim() : "—";
  el.rr.textContent = c ? text(c.rr) : "—";
  el.liq.textContent = c && c.liq_estimate !== null
    ? `≈ ${c.liq_estimate}${c.liq_ratio !== null ? ` (${c.liq_ratio}× дистанции)` : ""}` : "—";
  el.fee.textContent = c ? `≈ ${c.fee_usd} $` : "—";
  el.risk.textContent = c ? `${c.risk_usd} $ (${c.risk_percent}%)` : "—";

  el.issues.replaceChildren();
  if (card && card.refusal) el.issues.append(issueItem("🚫", card.refusal.message));
  for (const issue of card ? card.issues : []) {
    el.issues.append(issueItem(issue.level === "BLOCK" ? "🚫" : "⚠️", issue.message));
  }
  el.issues.hidden = el.issues.childElementCount === 0;

  const rounded = card ? card.rounded.map((r) => `${r.label} округлён с ${r.before} до ${r.after}`)
    : [];
  el.rounded.textContent = rounded.join(" · ");
  el.rounded.hidden = rounded.length === 0;
}

// Подсказка у плеча: потолок формы и максимум при этом стопе (сервер).
export function leverageHint(card, cap) {
  const parts = [`потолок ${cap}×`];
  if (card && card.calc) {
    parts.unshift(`макс при стопе ${card.calc.max_leverage_for_stop}×`);
  }
  return parts.join(" · ");
}

// Подсказка у риска: что сервер насчитал из ввода ($ ↔ %) и риск по плану.
export function riskHint(card, kind, planRisk) {
  const parts = [];
  if (card && card.calc) {
    parts.push(kind === "usd" ? `= ${card.levels.risk_percent}%` : `= ${card.calc.risk_usd} $`);
  }
  if (planRisk !== null && planRisk !== undefined) parts.push(`по плану ${planRisk}%`);
  return parts.join(" · ");
}

// --- подтверждение (2.5) ----------------------------------------------------------------

export const LIVE_TEXT = "⚠️ Реальный счёт";

// Вопрос showConfirm перед «Открыть»: сторона, объём, риск $ — строки карточки сервера;
// реальный счёт — первой строкой (решение владельца 17.10).
export function confirmQuestion(card, side, symbol, live) {
  const c = card.calc;
  const ask = `Открыть ${SIDE_TEXT[side]} ${c.quantity} ${baseAsset(symbol)}, риск ${c.risk_usd} $?`;
  return live ? `${LIVE_TEXT}\n${ask}` : ask;
}

export function cancelQuestion(opening, live) {
  const ask = `Отменить лимит ${opening.symbol} ${SIDE_TEXT[opening.side]} по ${opening.limit_price}?`;
  return live ? `${LIVE_TEXT}\n${ask}` : ask;
}

const DIFF_FIELDS = [
  ["quantity", "объём"], ["entry_price", "вход"], ["risk_usd", "риск $"],
  ["liq_estimate", "ликвидация ≈"],
];

// Карточка (свежие данные биржи) разошлась с превью — «было → стало»; сравнение строк
// сервера, без расчётов.
export function cardDiff(preview, card) {
  if (!preview || !preview.calc || !card || !card.calc) return [];
  return DIFF_FIELDS
    .filter(([key]) => String(preview.calc[key]) !== String(card.calc[key]))
    .map(([key, label]) => `${label}: ${text(preview.calc[key])} → ${text(card.calc[key])}`);
}

// Итог открытия: текст сервера и что стоит на бирже.
export function resultLines(opening, serverText) {
  const lines = [serverText || opening.text];
  if (opening.trade_id) lines.push(`Сделка #${opening.trade_id}`);
  if (opening.status === "DONE") {
    lines.push(`Стоп ${opening.stop_placed ? "✓" : "—"} · тейк ${opening.take_placed ? "✓"
      : opening.take_profit === null ? "нет" : "—"}`);
  }
  return lines;
}
