// Тема Mini App (§9а «Дизайн Mini App», Д.2): светлый или тёмный набор токенов style.css.
//
// - Набор выбирает Telegram: colorScheme клиента (решение владельца 18.10) — цвета страницы
//   свои, цвета themeParams не берём. Клиент без темы (браузер, стенд без themeParams в
//   хеше) — системная prefers-color-scheme. Выбор — атрибут data-theme у <html>; до него
//   (и на экране boot.js без SDK) style.css берёт ту же системную тему.
// - Шапка и фон окна Telegram — цветом бумаги: setBackgroundColor (6.1+), setHeaderColor
//   hex (6.9+; раньше — только ключ themeParams, шапка остаётся цветом клиента), полоса
//   нижних кнопок — setBottomBarColor (7.10+).
// - Нативные кнопки (MainButton/SecondaryButton) — через paintButton(): роль кнопки
//   запоминается и перекрашивается при смене темы.
// - После смены темы — событие THEME_EVENT на document: график перекрашивается по нему
//   (data-theme уже стоит, токены — новые).

export const THEME_EVENT = "ma:theme";

const ROLES = {
  main: ["--ink", "--paper"],          // заливка чернилами, текст цветом бумаги
  short: ["--paper", "--red-text"],    // «Шорт»: контура у нативной кнопки нет — красный текст
  live: ["--red-text", "--paper"],     // подтверждение на реальном счёте
  confirm: ["--blue", "--paper"],      // «Да» карточки действия на демо (К6): не цвет превью
};

const painted = new Map();             // нативная кнопка → роль
let tg = null;
let media = null;

export function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function buttonColors(role) {
  const [color, text] = ROLES[role] || ROLES.main;
  return { color: token(color), text_color: token(text) };
}

// Параметры нативной кнопки с цветами роли; роль запоминается для смены темы.
export function paintButton(button, role, params) {
  painted.set(button, role);
  button.setParams({ ...params, ...buttonColors(role) });
}

export function scheme() {
  if (tg && tg.themeParams && tg.themeParams.bg_color) {
    return tg.colorScheme === "dark" ? "dark" : "light";
  }
  return media && media.matches ? "dark" : "light";
}

function paintTelegram() {
  if (!tg) return;
  const paper = token("--paper");
  if (tg.isVersionAtLeast("6.1")) tg.setBackgroundColor(paper);
  if (tg.isVersionAtLeast("6.9")) tg.setHeaderColor(paper);
  if (tg.isVersionAtLeast("7.10")) tg.setBottomBarColor(paper);
  for (const [button, role] of painted) button.setParams(buttonColors(role));
}

export function applyTheme() {
  document.documentElement.dataset.theme = scheme();
  paintTelegram();
  document.dispatchEvent(new Event(THEME_EVENT));
}

export function initTheme(webApp) {
  tg = webApp;
  media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  applyTheme();
  if (tg) tg.onEvent("themeChanged", applyTheme);
  if (media) media.addEventListener("change", applyTheme);
}
