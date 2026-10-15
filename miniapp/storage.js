// localStorage по пользователю (§1 п.1, §5а): только поля формы, избранное,
// таймфрейм — никогда данные авторизации. Ключ — по telegram_id пользователя.
// Хранилище может быть недоступно (приватный режим, запрет) — тогда молча
// работаем без него: это удобство, не состояние.

const PREFIX = "ma";

export function createStorage(telegramId) {
  const scope = `${PREFIX}:${telegramId ?? "anon"}:`;
  let store = null;
  try {
    store = window.localStorage;
  } catch {
    store = null;
  }

  return {
    load(name, fallback = null) {
      if (!store) return fallback;
      try {
        const raw = store.getItem(scope + name);
        return raw === null ? fallback : JSON.parse(raw);
      } catch {
        return fallback;
      }
    },
    save(name, value) {
      if (!store) return;
      try {
        store.setItem(scope + name, JSON.stringify(value));
      } catch {
        // переполнено или запрещено — без сохранения
      }
    },
    remove(name) {
      if (!store) return;
      try {
        store.removeItem(scope + name);
      } catch {
        // без хранилища — нечего удалять
      }
    },
  };
}
