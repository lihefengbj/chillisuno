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

    # ---- 内部 ----

    def _fetch(self, url: str, callback) -> None:
        page = self.auth.acquire_page()
        if page is None:
            # 探测页尚未加载：稍后重试，而不是立即失败
            log.info("page not ready, retry in 800ms")
            QTimer.singleShot(800, lambda: self._fetch(url, callback))
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
        if clips:
            log.info(
                "first clip raw: %s",
                json.dumps(clips[0], ensure_ascii=False)[:2000],
            )
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
