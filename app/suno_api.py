"""Suno 数据接口：在页面上下文中取 Clerk JWT 后调用 studio-api。

复用 auth_service 的两段式 JS 模式（runJavaScript 不等待 Promise）。
"""

import json

from PySide6.QtCore import QObject, QTimer, Signal

from app.logger import get_logger

log = get_logger("api")

CLERK_CLIENT_API = "https://auth.suno.com/v1/client"
API_BASE = "https://studio-api-prod.suno.com"

# 通用两段式：先取 JWT 再请求目标接口，结果写 window.__chilliApi
_START_FETCH_JS = (
    """
window.__chilliApi = null;
(async () => {
  try {
    const cr = await fetch("%s", { credentials: "include" });
    if (!cr.ok) throw {reason: "clerk-http-" + cr.status};
    const client = await cr.json();
    const sessions = (client && client.response && client.response.sessions)
                     || (client && client.sessions) || [];
    let jwt = null;
    for (const s of sessions) {
      const t = s.last_active_token && s.last_active_token.jwt;
      if (t) { jwt = t; break; }
    }
    if (!jwt) throw {reason: "no-session"};
    const r = await fetch("%s", {
      headers: { "Authorization": "Bearer " + jwt }
    });
    if (!r.ok) throw {reason: "http-" + r.status};
    const data = await r.json();
    window.__chilliApi = JSON.stringify({ok: true, data: data});
  } catch (e) {
    window.__chilliApi = JSON.stringify(Object.assign(
      {ok: false}, (e && e.reason) ? e : {error: String(e)}));
  }
})();
"started"
"""
)

_READ_JS = "window.__chilliApi"

_START_TOKEN_JS = (
    """
window.__chilliToken = null;
(async () => {
  try {
    const cr = await fetch("%s", { credentials: "include" });
    if (!cr.ok) throw {reason: "clerk-http-" + cr.status};
    const client = await cr.json();
    const sessions = (client && client.response && client.response.sessions)
                     || (client && client.sessions) || [];
    let jwt = null;
    for (const s of sessions) {
      const t = s.last_active_token && s.last_active_token.jwt;
      if (t) { jwt = t; break; }
    }
    if (!jwt) throw {reason: "no-session"};
    window.__chilliToken = JSON.stringify({ok: true, token: jwt});
  } catch (e) {
    window.__chilliToken = JSON.stringify(
      Object.assign({ok: false}, (e && e.reason) ? e : {error: String(e)})
    );
  }
})();
"started"
"""
    % CLERK_CLIENT_API
)

_READ_TOKEN_JS = "window.__chilliToken"

_START_REQUEST_JS = (
    """
window.__chilliReq = null;
(async () => {
  try {
    const cr = await fetch("%s", { credentials: "include" });
    if (!cr.ok) throw {reason: "clerk-http-" + cr.status};
    const client = await cr.json();
    const sessions = (client && client.response && client.response.sessions)
                     || (client && client.sessions) || [];
    let jwt = null;
    for (const s of sessions) {
      const t = s.last_active_token && s.last_active_token.jwt;
      if (t) { jwt = t; break; }
    }
    if (!jwt) throw {reason: "no-session"};
    const r = await fetch("%s", {
      method: "%s",
      headers: { "Authorization": "Bearer " + jwt, "Content-Type": "application/json" },
      body: JSON.stringify(%s)
    });
    const text = await r.text();
    window.__chilliReq = JSON.stringify({ok: r.ok, status: r.status, text: text});
  } catch (e) {
    window.__chilliReq = JSON.stringify({
      ok: false, status: 0, text: "",
      error: (e && (e.reason || e.message)) || String(e)
    });
  }
})();
"started"
"""
)

_READ_REQUEST_JS = "window.__chilliReq"


class SunoApi(QObject):
    """通过 AuthService 的页面上下文执行接口调用。"""

    feed_loaded = Signal(list)       # list[dict] 归一化后的 clip
    fetch_failed = Signal(str)

    def __init__(self, auth, parent=None) -> None:
        super().__init__(parent)
        self.auth = auth

    # ---- 公开接口 ----

    def fetch_feed(self, page: int = 0) -> None:
        url = f"{API_BASE}/api/feed/v2?page={page}"
        self._fetch(url, self._on_feed_raw)

    def fetch_token(self, callback, attempt: int = 0) -> None:
        """在页面上下文中取 Clerk JWT，异步回调 callback(token|None)。"""
        page = self.auth.acquire_page()
        if page is None:
            if attempt >= 40:
                log.warning("token page never ready")
                callback(None)
                return
            QTimer.singleShot(
                800, lambda: self.fetch_token(callback, attempt + 1)
            )
            return
        page.runJavaScript(_START_TOKEN_JS)
        self._wait_token(page, callback, 0)

    def _wait_token(self, page, callback, n: int) -> None:
        if n > 20:
            log.warning("token fetch timeout")
            callback(None)
            return

        def read(raw) -> None:
            if raw in (None, "", "null"):
                QTimer.singleShot(
                    500, lambda: self._wait_token(page, callback, n + 1)
                )
                return
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                callback(None)
                return
            callback(payload.get("token") if payload.get("ok") else None)

        try:
            page.runJavaScript(_READ_TOKEN_JS, read)
        except RuntimeError:
            QTimer.singleShot(
                500, lambda: self._wait_token(page, callback, n + 1)
            )

    def request_json(self, method, url, body, callback, attempt: int = 0) -> None:
        """在页面上下文发起 JSON 请求，规避 Python TLS 指纹被 Suno 拦截。

        callback 收到 dict：{ok, status, text}；非 2xx 时 ok=False。
        """
        page = self.auth.acquire_page()
        if page is None:
            if attempt >= 40:
                log.warning("request page never ready")
                callback({"ok": False, "status": 0, "text": "no-page"})
                return
            QTimer.singleShot(
                800, lambda: self.request_json(
                    method, url, body, callback, attempt + 1
                )
            )
            return
        # fetch 的 body 不接受普通对象，否则浏览器会把它序列化成
        # "[object Object]"，Suno 返回 400 "Cannot parse request body"。
        body_js = "undefined" if body is None else json.dumps(body)
        page.runJavaScript(
            _START_REQUEST_JS % (CLERK_CLIENT_API, url, method.upper(), body_js)
        )
        self._wait_request(page, callback, 0)

    def _wait_request(self, page, callback, n: int) -> None:
        if n > 20:
            log.warning("request timeout")
            callback({"ok": False, "status": 0, "text": "timeout"})
            return

        def read(raw) -> None:
            if raw in (None, "", "null"):
                QTimer.singleShot(
                    500, lambda: self._wait_request(page, callback, n + 1)
                )
                return
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                payload = {"ok": False, "status": 0, "text": raw}
            callback(payload)

        try:
            page.runJavaScript(_READ_REQUEST_JS, read)
        except RuntimeError:
            QTimer.singleShot(
                500, lambda: self._wait_request(page, callback, n + 1)
            )

    # ---- 内部 ----

    def _fetch(self, url: str, callback, attempt: int = 0) -> None:
        page = self.auth.acquire_page()
        if page is None:
            if attempt >= 40:  # 40 * 800ms = 32s
                log.warning("page never ready, giving up")
                self.fetch_failed.emit("no-page")
                return
            # 探测页尚未加载：稍后重试，而不是立即失败
            QTimer.singleShot(
                800, lambda: self._fetch(url, callback, attempt + 1)
            )
            return
        log.info("fetch %s", url)
        page.runJavaScript(_START_FETCH_JS % (CLERK_CLIENT_API, url))
        self._wait_result(page, callback, 0)

    def _wait_result(self, page, callback, n: int) -> None:
        if n > 20:  # 20 * 500ms = 10s 超时
            log.warning("fetch timeout")
            self.fetch_failed.emit("timeout")
            return

        def read(raw) -> None:
            # window.__chilliApi 尚未就绪时为 null / 空串
            if raw in (None, "", "null"):
                QTimer.singleShot(
                    500, lambda: self._wait_result(page, callback, n + 1)
                )
            else:
                callback(raw)

        try:
            page.runJavaScript(_READ_JS, read)
        except RuntimeError:
            QTimer.singleShot(
                500, lambda: self._wait_result(page, callback, n + 1)
            )

    def _on_feed_raw(self, raw) -> None:
        log.debug("feed raw len=%s", len(raw) if raw else 0)
        try:
            payload = json.loads(raw) if raw else {}
        except (TypeError, json.JSONDecodeError):
            self.fetch_failed.emit("bad-json")
            return
        if not payload.get("ok"):
            reason = payload.get("reason") or payload.get("error") or "unknown"
            log.warning("feed failed: %s", reason)
            self.fetch_failed.emit(reason)
            return
        data = payload.get("data")
        clips = data.get("clips") if isinstance(data, dict) else data
        if not isinstance(clips, list):
            log.warning("feed unexpected shape: %s", type(data))
            self.fetch_failed.emit("unexpected-shape")
            return
        result = [self._normalize(c) for c in clips if isinstance(c, dict)]
        result = [c for c in result if c]
        log.info("feed loaded: %d clips", len(result))
        self.feed_loaded.emit(result)

    @staticmethod
    def _normalize(clip: dict) -> dict | None:
        cid = clip.get("id")
        if not cid:
            return None
        meta = clip.get("metadata") or {}
        # 真实音频在 media_urls（progressive 直链），audio_url 常为
        # .../api/forbidden 占位符。优先选可播放的容器（mp3/aac），
        # 否则退回第一个 progressive（m4a-opus，需 Chromium 播放）。
        media_urls = clip.get("media_urls") or []
        audio_url = ""
        for m in media_urls:
            if not isinstance(m, dict) or m.get("delivery") != "progressive":
                continue
            u = m.get("url") or ""
            if not u:
                continue
            if not audio_url:
                audio_url = u
            ct = (m.get("content_type") or "").lower()
            if "mp3" in ct or "aac" in ct or "m4a" in ct and "opus" not in ct:
                audio_url = u
                break
        if not audio_url:
            raw = clip.get("audio_url") or ""
            if raw and "forbidden" not in raw:
                audio_url = raw
        return {
            "id": cid,
            "title": clip.get("title") or "未命名",
            "audio_url": audio_url,
            "media_urls": media_urls,
            "image_url": clip.get("image_url")
            or f"https://cdn2.suno.ai/image_{cid}.jpeg",
            "tags": meta.get("tags") or "",
            "duration": meta.get("duration") or 0,
            "created_at": clip.get("created_at") or "",
        }
