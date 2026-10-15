// Запросы к /api/*: заголовок «Authorization: tma <initData>», ответ — JSON.
// Ошибка — ApiError {status, code, message}; message сервера показывается как есть
// (тот же текст, что в чате). Числа приходят строками — здесь не считаются.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export const NETWORK_TEXT = "Нет связи с сервером — повтори.";
const BAD_RESPONSE_TEXT = "Сервер ответил непонятно — повтори.";

export function createApi(initData, fetchImpl = window.fetch.bind(window)) {
  async function request(method, path, body, signal) {
    const headers = { Authorization: `tma ${initData}` };
    const init = { method, headers, signal, credentials: "omit", cache: "no-store" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetchImpl(path, init);
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      throw new ApiError(0, "NETWORK", NETWORK_TEXT);
    }
    let data = null;
    try {
      data = await response.json();
    } catch {
      throw new ApiError(response.status, "BAD_RESPONSE", BAD_RESPONSE_TEXT);
    }
    if (!response.ok) {
      const code = data && typeof data.code === "string" ? data.code : "HTTP_" + response.status;
      const message = data && typeof data.message === "string" ? data.message : BAD_RESPONSE_TEXT;
      throw new ApiError(response.status, code, message);
    }
    return data;
  }

  return {
    get: (path, { signal } = {}) => request("GET", path, undefined, signal),
    post: (path, body, { signal } = {}) => request("POST", path, body ?? {}, signal),
  };
}
