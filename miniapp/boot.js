// Старт без белого экрана: в index.html статичное «Загрузка…», SDK telegram.org и
// скрипты страницы — defer (разбор страницы их не ждёт). Этот файл — обычный
// синхронный скрипт из <head>, до SDK: если через SDK_TIMEOUT_MS нет Telegram.WebApp
// (SDK завис или не загрузился), показывает экран «Не загрузилось» с «Обновить».
// Запасного режима без SDK нет: без initData нет авторизации. SDK не загрузился
// (ошибка сети, блокировка) — тот же экран сразу: слушатель error в фазе перехвата
// (ошибка загрузки <script> не всплывает), без on*= в HTML — CSP не меняется.
// app.js без Telegram.WebApp тоже зовёт window.maBoot.fail() и не стартует.

(function () {
  var SDK_URL = "https://telegram.org/js/telegram-web-app.js";
  var SDK_TIMEOUT_MS = 10000;
  var failed = false;

  function hasSdk() {
    return !!(window.Telegram && window.Telegram.WebApp);
  }

  function fail() {
    if (failed) return;
    failed = true;
    if (document.readyState === "loading") {
      // Ошибка SDK может прийти до разбора <body> — показать, когда он разобран.
      document.addEventListener("readystatechange", show, { once: true });
    } else {
      show();
    }
  }

  function show() {
    var screens = document.querySelectorAll("main > .screen");
    for (var i = 0; i < screens.length; i += 1) {
      screens[i].hidden = screens[i].id !== "screen-sdk";
    }
    var account = document.getElementById("account");
    if (account) account.hidden = true;
    var fallback = document.getElementById("fallback-buttons");
    if (fallback) fallback.hidden = true;
    document.getElementById("sdk-reload").addEventListener("click", function () {
      window.location.reload();
    });
  }

  window.maBoot = {
    SDK_TIMEOUT_MS: SDK_TIMEOUT_MS,
    failed: function () { return failed; },
    fail: fail,
  };

  window.addEventListener("error", function (event) {
    var target = event.target;
    if (target && target.tagName === "SCRIPT" && target.src === SDK_URL) fail();
  }, true);

  setTimeout(function () {
    if (!hasSdk()) fail();
  }, SDK_TIMEOUT_MS);
})();
