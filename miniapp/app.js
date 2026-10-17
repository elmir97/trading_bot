// Mini App: Telegram WebApp, экраны, шапка счёта.
// Своей торговой логики нет — только ввод, показ и вызов API (§1). Экран рынка —
// market.js (2.3), форма сделки — form.js + render.js (2.4), подтверждение — confirm.js (2.5).
// Экран с подписками (опрос, график, кнопки Telegram) отдаёт {enter, leave}: show()
// зовёт leave у прежнего экрана до enter у нового — график снимается при уходе.

import { ApiError, createApi } from "./api.js";
import { createConfirmScreen } from "./confirm.js";
import { createTradeScreen } from "./form.js";
import { createMarketScreen } from "./market.js";
import { createStorage } from "./storage.js";

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
const TV_URL = "https://www.tradingview.com/";
const SCREENS = ["loading", "market", "trade", "confirm", "error"];
// BackButton — с Bot API 6.1; на старом клиенте (и вне Telegram SDK говорит 6.0) не трогаем.
const backButton = tg && tg.isVersionAtLeast("6.1") ? tg.BackButton : null;
const api = createApi(tg ? tg.initData : "");
const telegramId = tg && tg.initDataUnsafe && tg.initDataUnsafe.user
  ? tg.initDataUnsafe.user.id : null;
const storage = createStorage(telegramId);

const history = [];
let current = "loading";
let screens = {};
let me = null;               // /api/me: план (риск, max_leverage), сроки лимита, режим маржи

function $(id) {
  return document.getElementById(id);
}

function show(name, { push = true } = {}) {
  if (!SCREENS.includes(name)) throw new Error(`нет экрана ${name}`);
  if (push && current !== name && current !== "loading" && current !== "error") {
    history.push(current);
  }
  if (current !== name && screens[current]) screens[current].leave();
  current = name;
  for (const screen of SCREENS) {
    $(`screen-${screen}`).hidden = screen !== name;
  }
  if (screens[name]) screens[name].enter();
  if (backButton) {
    // На экране рынка «Назад» Telegram закрывает приложение (§5а).
    backButton.show();
  }
}

function back() {
  // Пока «Открыть» в полёте или идёт проверка итога — с экрана не уходим.
  if (screens[current] && screens[current].busy && screens[current].busy()) return;
  const previous = history.pop();
  if (previous) {
    show(previous, { push: false });
  } else if (tg) {
    tg.close();
  }
}

// Числа приходят строкой, уже округлёнными сервером, — показываются как есть
// (как fmt_amount в чате: без групп разрядов); нет значения — «—».
function text(value) {
  return value === null || value === undefined ? "—" : String(value);
}

function renderAccount(me) {
  const header = $("account");
  const live = me.account_mode === "LIVE";
  header.classList.toggle("live", live);
  $("account-mode").textContent = live ? "⚠️ Реальный счёт" : "DEMO";
  $("account-equity").textContent = text(me.equity);
  $("account-available").textContent = text(me.available);
  const note = $("account-note");
  const notes = [];
  if (me.balance_error) notes.push(me.balance_error);
  if (me.flags && me.flags.dry_run) notes.push("Сухой прогон — на биржу не уйдёт.");
  note.textContent = notes.join(" ");
  note.hidden = notes.length === 0;
  header.hidden = false;
}

function showError(err, retry) {
  const expired = err instanceof ApiError && err.status === 401;
  $("error-title").textContent = expired ? "Сессия истекла" : "Не получилось";
  $("error-text").textContent = expired
    ? "Закрой приложение и открой заново из бота."
    : err instanceof ApiError ? err.message : "Ошибка на странице — открой заново.";
  const button = $("error-retry");
  button.hidden = expired || !retry;
  button.onclick = retry || null;
  show("error", { push: false });
}

// «Лонг»/«Шорт» с экрана рынка — форма сделки (2.4).
function openTrade(side, symbol) {
  screens.trade.open(side, symbol);
  show("trade");
}

// «Открыть» с формы — подтверждение: тело и ответ последнего превью (2.5).
function openConfirm(payload) {
  screens.confirm.start(payload);
  show("confirm");
}

// С итога — на рынок; история экранов заново (форма и подтверждение позади).
function toMarket() {
  history.length = 0;
  show("market", { push: false });
}

function openExternal(url) {
  if (tg && typeof tg.openLink === "function") {
    tg.openLink(url);
  } else {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

async function start() {
  history.length = 0;
  show("loading", { push: false });
  try {
    me = await api.get("/api/me");
    renderAccount(me);
    show("market");
  } catch (err) {
    if (!(err instanceof ApiError)) console.error(err);
    showError(err, start);
  }
}

function init() {
  // Без SDK запасного режима нет (без initData нет авторизации): экран boot.js.
  if (!tg || window.maBoot.failed()) {
    window.maBoot.fail();
    return;
  }
  tg.ready();
  tg.expand();
  if (backButton) backButton.onClick(back);
  screens = {
    market: createMarketScreen({
      api, tg, storage, onSide: openTrade,
      onFatal: (err) => showError(err, start),
    }),
    trade: createTradeScreen({
      api, tg, storage, me: () => me, onOpen: openConfirm,
      onFatal: (err) => showError(err, start),
    }),
    confirm: createConfirmScreen({
      api, tg, me: () => me, onBack: back, onMarket: toMarket,
      onFatal: (err) => showError(err, start),
    }),
  };
  $("tv-attribution").addEventListener("click", (event) => {
    event.preventDefault();
    openExternal(TV_URL);
  });
  start();
}

init();
