// Показ формы сделки (§5а, экран 2): ответ POST /api/open/preview → строки экрана.
// Объём, риск $ с комиссией, RR, маржа, ликвидация, нарушения — только из ответа
// сервера (строки, уже округлённые): здесь ничего не считается, только подписи.
// Плечо: потолок формы — min(максимум стороны контракта по ключу, max_leverage плана);
// нет ключа или максимум не прочитан — потолка нет, открытия нет (решение владельца 17.10).

export const NO_KEY_TEXT = "Подключи ключи биржи";

const SIDE_TEXT = { LONG: "ЛОНГ", SHORT: "ШОРТ" };
const SIDE_WORD = { LONG: "лонг", SHORT: "шорт" };
const MARGIN_TEXT = { ISOLATED: "изол.", CROSSED: "кросс" };

function text(value) {
  return value === null || value === undefined ? "—" : String(value);
}

// Разряды — только в показе (решение 17.10, шапка B): строка сервера «84172.35» →
// «84 172.35» (неразрывный пробел), число не пересчитывается и не округляется. Не число
// («—», текст) — как есть.
const NBSP = String.fromCharCode(160);

export function groupDigits(value) {
  const raw = value === null || value === undefined ? "" : String(value);
  const m = /^([+\-−]?)(\d+)(\.\d+)?$/.exec(raw);
  if (!m) return raw;
  return m[1] + m[2].replace(/\B(?=(\d{3})+(?!\d))/g, NBSP) + (m[3] || "");
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
// «Открыть всё равно»; иначе «Открыть лонг · 52 NEAR» (макет «Форма», Д.4). Объём —
// строка сервера (calc.quantity), здесь только разряды.
export function openAction(card, side, symbol) {
  if (!card || !card.calc || !card.can_open) return { visible: false, text: "", warn: false };
  if (card.has_warnings) return { visible: true, text: "Открыть всё равно", warn: true };
  return {
    visible: true,
    text: `Открыть ${SIDE_WORD[side]} · ${groupDigits(card.calc.quantity)} ${baseAsset(symbol)}`,
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

// Сумма со знаком $ — строка сервера с разрядами; нет значения — «—».
function usd(value) {
  return value === null || value === undefined ? "—" : `${groupDigits(value)} $`;
}

// Отрисовка ответа preview — квитанция «РАСЧЁТ» (Д.4). el — элементы блока результата;
// meta — {symbol, marginType}. Все числа — строки сервера (calc), здесь только разряды,
// подписи и единицы: ни одного расчёта.
export function renderPreview(el, card, { symbol, marginType }) {
  const c = card ? card.calc : null;
  const base = baseAsset(symbol);
  el.quantity.textContent = c ? `${groupDigits(c.quantity)} ${base}` : "—";
  el.notional.textContent = c ? usd(c.notional) : "—";
  el.marginLabel.textContent = `маржа${MARGIN_TEXT[marginType] ? `, ${MARGIN_TEXT[marginType]}` : ""}`;
  el.margin.textContent = c ? usd(c.margin) : "—";
  el.rr.textContent = c && c.rr !== null && c.rr !== undefined ? `1 : ${c.rr}` : "—";
  el.liq.textContent = c && c.liq_estimate !== null
    ? `≈ ${groupDigits(c.liq_estimate)}${c.liq_ratio !== null ? ` · ${c.liq_ratio}× дальше стопа` : ""}`
    : "—";
  el.fee.textContent = c ? `≈ ${usd(c.fee_usd)}` : "—";
  el.risk.textContent = c ? `${usd(c.risk_usd)} · ${c.risk_percent}%` : "—";

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

// Подсказка у риска (пометка «от руки», Д.4): что сервер насчитал из ввода ($ ↔ %) и
// риск по плану.
export function riskHint(card, kind, planRisk) {
  const parts = [];
  if (card && card.calc) {
    parts.push(kind === "usd" ? `= ${card.levels.risk_percent}%` : `= ${usd(card.calc.risk_usd)}`);
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

// Карточка (свежие данные биржи) разошлась с превью — «было → стало»: оба значения —
// строки сервера (ответ preview и ответ /api/open), сравнение строк и разряды, без расчётов
// (решение владельца 18.10, Д.5).
export function cardDiff(preview, card) {
  if (!preview || !preview.calc || !card || !card.calc) return [];
  return DIFF_FIELDS
    .filter(([key]) => String(preview.calc[key]) !== String(card.calc[key]))
    .map(([key, label]) => `${label}: ${digitsOrDash(preview.calc[key])} → `
      + `${digitsOrDash(card.calc[key])}`);
}

function digitsOrDash(value) {
  return value === null || value === undefined ? "—" : groupDigits(value);
}

// Действие с позицией (экран 5): карточка на свежих данных разошлась с превью — «было →
// стало» по строкам с той же подписью; значения — строки сервера, сравнение строк.
export function actionDiff(preview, card) {
  if (!preview || preview.refusal || !card || card.refusal) return [];
  const before = new Map(preview.rows.map((r) => [r.label, r.value]));
  return card.rows
    .filter((r) => before.has(r.label) && before.get(r.label) !== r.value)
    .map((r) => `${r.label}: ${before.get(r.label)} → ${r.value}`);
}

// --- «ЗАПИСЬ» подтверждения (Д.5, макет «Подтверждение») -------------------------------

const ENTRY_TEXT = { MARKET: "маркет", LIMIT: "лимит" };

// Номер записи — только реальный id открытия с сервера (opening_id); нет — без номера
// (решение владельца 18.10: не выдумывать и не нумеровать на фронте).
export function recordNumber(openingId) {
  return Number.isInteger(openingId) && openingId > 0 ? `ЗАПИСЬ № ${openingId}` : "ЗАПИСЬ";
}

// «маркет ≈ 1.4950 · изол. 10×» — тип входа, цена (calc.entry_price / levels.limit_price),
// маржа и плечо из карточки сервера.
export function recordSubtitle(card, marginType) {
  if (!card || !card.calc || !card.levels) return "";
  const lv = card.levels;
  const price = lv.entry_type === "LIMIT" ? digitsOrDash(lv.limit_price)
    : `≈ ${digitsOrDash(card.calc.entry_price)}`;
  const margin = MARGIN_TEXT[marginType] ? `${MARGIN_TEXT[marginType]} ` : "";
  return `${ENTRY_TEXT[lv.entry_type] || lv.entry_type} ${price} · ${margin}${lv.leverage}×`;
}

// Строки записи: [подпись, значение, класс подписи, класс значения]. Все числа — строки
// карточки сервера; здесь только разряды, единицы и подписи.
export function recordRows(card, symbol) {
  if (!card || !card.calc || !card.levels) return [];
  const c = card.calc;
  const lv = card.levels;
  const stopTail = c.distance_percent === null || c.distance_percent === undefined ? ""
    : ` · ${c.distance_percent}%`;
  return [
    ["объём", `${groupDigits(c.quantity)} ${baseAsset(symbol)}`, "", "strong"],
    ["стоп", `${groupDigits(lv.stop_loss)}${stopTail}`, "text-red", ""],
    ["тейк", lv.take_profit === null ? "нет" : groupDigits(lv.take_profit), "text-blue", ""],
    ["риск с комиссией", `${usd(c.risk_usd)} · ${c.risk_percent}%`, "", "text-red strong"],
    ["RR", c.rr === null || c.rr === undefined ? "—" : `1 : ${c.rr}`, "", "text-blue strong"],
    ["ликвидация", c.liq_estimate === null ? "—" : `≈ ${groupDigits(c.liq_estimate)}`, "", ""],
    ["комиссия", `≈ ${usd(c.fee_usd)}`, "", ""],
  ];
}

// Нарушения и округления карточки — строками (как в форме).
export function recordNotes(card) {
  if (!card) return [];
  const notes = [];
  if (card.refusal) notes.push(`🚫 ${card.refusal.message}`);
  for (const issue of card.issues) {
    notes.push(`${issue.level === "BLOCK" ? "🚫" : "⚠️"} ${issue.message}`);
  }
  for (const r of card.rounded) notes.push(`ℹ️ ${r.label} округлён с ${r.before} до ${r.after}`);
  return notes;
}

// Отсчёт «1:57» до expires_at сервера; left — целые секунды.
export function clockLeft(left) {
  const s = Math.max(0, left);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
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
