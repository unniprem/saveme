// SaveMe shared page helpers, used by app.js (link downloads) and studio.js (Clip Studio).
// Plain browser JavaScript, no dependencies. Loaded first (all scripts are deferred, so they
// run in document order) and exposed as one frozen object, window.SaveMe.
"use strict";

(() => {
  const MB = 1024 * 1024;
  const JOB_ID_RE = /^[A-Za-z0-9_-]{22}$/;
  const MSG_OFFLINE = "Can't reach SaveMe. Check that it's running, then try again.";
  const MSG_BAD_REPLY = "SaveMe sent an unexpected reply. Please try again.";

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const str = (v) => (typeof v === "string" ? v.trim() : "");
  const fmtMB = (bytes) => `${(bytes / MB).toFixed(1)} MB`;

  function fmtDuration(seconds) {
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
  }

  function httpUrl(value) {
    if (typeof value !== "string") return null;
    try {
      const u = new URL(value);
      return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
    } catch {
      return null;
    }
  }

  function setText(node, text) {
    const value = text == null ? "" : String(text);
    if (node.textContent !== value) node.textContent = value;
  }

  function show(node, visible) {
    node.hidden = !visible;
  }

  // A text line that is hidden when empty; tone "error" turns it red (data-[tone=error]: classes).
  function setLine(node, text, tone) {
    setText(node, text);
    show(node, Boolean(text));
    if (tone) node.dataset.tone = tone;
    else delete node.dataset.tone;
  }

  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  // The server's error body as an ApiError, for any non-2xx reply.
  function errorFrom(status, data) {
    const err = data && typeof data === "object" && data.error && typeof data.error === "object" ? data.error : {};
    return new ApiError(
      status,
      str(err.code) || `http_${status}`,
      str(err.message) || `Something went wrong (HTTP ${status}). Please try again.`,
    );
  }

  // GET when body is undefined, otherwise POST JSON. Errors carry the server's message.
  async function api(path, body) {
    const init = { cache: "no-store", headers: { Accept: "application/json" } };
    if (body !== undefined) {
      init.method = "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch {
      throw new ApiError(0, "network", MSG_OFFLINE);
    }
    let data = null;
    try {
      data = await res.json();
    } catch {
      // Not JSON; handled below.
    }
    if (!res.ok) throw errorFrom(res.status, data);
    if (!data || typeof data !== "object") throw new ApiError(res.status, "bad_response", MSG_BAD_REPLY);
    return data;
  }

  window.SaveMe = Object.freeze({
    MB, JOB_ID_RE, MSG_OFFLINE, MSG_BAD_REPLY,
    isNum, str, fmtMB, fmtDuration, httpUrl, setText, show, setLine,
    ApiError, errorFrom, api,
  });
})();
