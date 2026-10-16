// Mini App: Telegram WebApp, экраны, шапка счёта.
// Своей торговой логики нет — только ввод, показ и вызов API (§1). Экран рынка —
// market.js (2.3); формы и подтверждения — подэтапы 2.4–2.5.
// Экран с подписками (опрос, график, кнопки Telegram) отдаёт {enter, leave}: show()
// зовёт leave у прежнего экрана до enter у нового — график снимается при уходе.

import { ApiError, createApi } from "./api.js";
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

// «Лонг»/«Шорт» с экрана рынка — форма сделки (подэтап 2.4).
function openTrade(side, symbol) {
  $("trade-title").textContent = `${symbol} · ${side === "LONG" ? "Лонг" : "Шорт"}`;
  show("trade");
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
    const me = await api.get("/api/me");
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
  };
  $("tv-attribution").addEventListener("click", (event) => {
    event.preventDefault();
    openExternal(TV_URL);
  });
  start();
}

init();
