"""Suno 登录会话管理。

内嵌 QWebEngineView 加载 suno.com，用户用官方页面完成 OAuth 登录。
登录态由持久化 QWebEngineProfile 保持。

会话令牌获取（2026-10-03 起改用此方案）：
新版 Suno 页面不暴露 window.Clerk，且 Chromium 拦截器会过滤
Authorization 敏感头（无法直接捕获）。改为在页面上下文中调用 Clerk
认证接口 auth.suno.com/v1/client（页面自身就在调，CORS/cookie 天然可用），
从响应中取会话 JWT，再调 studio-api-prod.suno.com 的 billing 接口取额度。
"""

import json
import re
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QStandardPaths, QTimer, QUrl, Signal
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import (
    QWebEngineProfile,
    QWebEnginePage,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QVBoxLayout

from app.logger import get_logger

log = get_logger("auth")

SUNO_HOME = "https://suno.com"
CLERK_CLIENT_API = "https://auth.suno.com/v1/client"
BILLING_API = "https://studio-api-prod.suno.com/api/billing/info/"


def _chrome_like_ua(profile: QWebEngineProfile) -> str:
    """取 QtWebEngine 真实 UA，仅移除 QtWebEngine 标识。

    保持 Chromium 版本号与内核一致——UA 与真实指纹不符会触发
    Cloudflare Turnstile 反复人机验证。
    """
    return re.sub(r"QtWebEngine/\S+\s*", "", profile.httpUserAgent())


class TokenInterceptor(QWebEngineUrlRequestInterceptor):
    """捕获发往 studio-api 的请求中的 Bearer 令牌。

    注意：interceptRequest 运行在 IO 线程，不能触碰 UI；
    通过信号（队列连接）把令牌交回主线程。
    """

    tokenCaptured = Signal(str)
    hostSeen = Signal(str, str)  # (host, resource_type) 诊断用

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._seen: set[str] = set()

    def interceptRequest(self, info) -> None:
        host = info.requestUrl().host()
        if host not in self._seen:
            self._seen.add(host)
            self.hostSeen.emit(host, str(info.resourceType()))
        if "suno" not in host:
            return
        headers = info.httpHeaders()
        auth = headers.value(b"Authorization") or headers.value(b"authorization")
        if auth:
            token = bytes(auth).decode(errors="replace").removeprefix("Bearer ")
            if token:
                self.tokenCaptured.emit(host + "|" + token)


# 注意：QWebEnginePage.runJavaScript 不会等待 Promise 完成，
# 因此分两段执行：启动任务把结果写入 window.__chilliResult，延迟后再读取。
#
# 流程：auth.suno.com/v1/client（带 cookie）-> 会话 JWT -> billing 额度。
_START_SESSION_JS = (
    """
window.__chilliResult = null;
(async () => {
  const dbg = {url: location.href, title: document.title || ""};
  try {
    if (dbg.title.toLowerCase().includes("too many"))
      throw {rate_limited: true};
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
    if (!jwt)
      throw {reason: sessions.length ? "no-jwt-in-session" : "no-session"};
    const resp = await fetch("%s", {
      headers: { "Authorization": "Bearer " + jwt }
    });
    if (!resp.ok)
      throw {reason: "billing-http-" + resp.status};
    const data = await resp.json();
    window.__chilliResult = JSON.stringify({
      logged_in: true,
      credits: data.total_credits_left ?? null,
      dbg
    });
  } catch (e) {
    window.__chilliResult = JSON.stringify(Object.assign(
      {logged_in: false, dbg}, (e && (e.reason || e.rate_limited)) ? e
                               : {error: String(e)}));
  }
})();
"started"
"""
    % (CLERK_CLIENT_API, BILLING_API)
)

_READ_RESULT_JS = "window.__chilliResult"


class LoginDialog(QDialog):
    def __init__(self, profile: QWebEngineProfile, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("登录 Suno")
        self.setWindowIcon(QIcon("assets/icon.png"))
        self.resize(520, 760)
        self.view = QWebEngineView(self)
        self.view.setPage(QWebEnginePage(profile, self.view))
        self.view.setUrl(QUrl(SUNO_HOME + "/sign-in"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)


class AuthService(QObject):
    session_changed = Signal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        data_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        profile_dir = Path(data_dir) / "webprofile"
        self.profile = QWebEngineProfile("chillisuno", self)
        self.profile.setPersistentStoragePath(str(profile_dir))
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        ua = _chrome_like_ua(self.profile)
        self.profile.setHttpUserAgent(ua)
        log.info("UA: %s", ua)

        # 请求拦截器：捕获 API 令牌
        self._token: str | None = None
        self._interceptor = TokenInterceptor(self)
        self._interceptor.tokenCaptured.connect(
            self._on_token, Qt.ConnectionType.QueuedConnection
        )
        self._interceptor.hostSeen.connect(
            lambda host, rtype: log.debug("intercept host: %s (%s)", host, rtype),
            Qt.ConnectionType.QueuedConnection,
        )
        self.profile.setUrlRequestInterceptor(self._interceptor)

        self._dialog: LoginDialog | None = None
        self._info: dict = {"logged_in": False}
        self._probe_loaded = False

        # 后台探测页：懒加载，避免与登录窗口同时请求 suno.com 触发限流
        self._probe = QWebEnginePage(self.profile, self)
        self._probe.loadFinished.connect(self._on_probe_loaded)

        # 登录窗口打开期间轮询（5s 一次，避免触发 Too Many Requests）
        self._poll = QTimer(self)
        self._poll.setInterval(5000)
        self._poll.timeout.connect(self.refresh_credits)

        # 被限流时的退避重载
        self._backoff = QTimer(self)
        self._backoff.setSingleShot(True)
        self._backoff.setInterval(30000)
        self._backoff.timeout.connect(self._reload_probe)

    # ---- 对外操作 ----

    def login(self) -> None:
        log.info("open login dialog")
        if self._dialog is None:
            self._dialog = LoginDialog(self.profile, self.parent())
            self._dialog.finished.connect(self._on_dialog_closed)
            self._dialog.view.loadFinished.connect(self._on_dialog_page_loaded)
        else:
            self._dialog.view.setUrl(QUrl(SUNO_HOME + "/sign-in"))
        self._dialog.show()
        self._poll.start()

    def logout(self) -> None:
        log.info("logout")
        self.profile.cookieStore().deleteAllCookies()
        self.profile.clearHttpCache()
        self._token = None
        self._info = {"logged_in": False}
        self._probe_loaded = False
        self.session_changed.emit(self._info)

    def import_cookies(self, cookie_str: str) -> int:
        """从浏览器复制的 Cookie 字符串导入（"k1=v1; k2=v2" 格式）。

        用于 Google 封禁内嵌 WebView 登录时的兜底：用户在自己的浏览器
        登录 suno.com 后，F12 -> 应用 -> Cookie 全量复制粘贴进来。
        返回导入的 cookie 数量。
        """
        store = self.profile.cookieStore()
        count = 0
        for pair in cookie_str.split(";"):
            name, sep, value = pair.partition("=")
            name, value = name.strip(), value.strip()
            if not sep or not name:
                continue
            cookie = QNetworkCookie(name.encode(), value.encode())
            cookie.setDomain(".suno.com")
            cookie.setPath("/")
            store.setCookie(cookie, QUrl(SUNO_HOME))
            count += 1
        log.info("imported %d cookies", count)
        self._probe_loaded = False
        self.refresh_credits()
        return count

    def refresh_credits(self) -> None:
        page = self._active_page()
        if page is None:
            return
        page.runJavaScript(_START_SESSION_JS)
        QTimer.singleShot(1500, lambda: self._read_result(page))

    # ---- 内部 ----

    def _on_token(self, token: str) -> None:
        host, _, token = token.partition("|")
        if token != self._token:
            log.info("captured api token from %s (len=%d)", host, len(token))
            self._token = token
            if not self._info.get("logged_in"):
                self._set_state({"logged_in": True, "credits": None})
            self.refresh_credits()

    def _ensure_page(self) -> None:
        if self._dialog is not None and self._dialog.isVisible():
            return
        if not self._probe_loaded:
            log.info("lazy-load probe page: %s/create", SUNO_HOME)
            self._probe.load(QUrl(SUNO_HOME + "/create"))

    def _active_page(self) -> QWebEnginePage | None:
        """优先用登录窗口页面（用户实际操作页），否则用后台探测页。"""
        if self._dialog is not None and self._dialog.isVisible():
            url = self._dialog.view.url()
            if "suno.com" in url.host():
                return self._dialog.view.page()
            return None
        if not self._probe_loaded:
            self._ensure_page()
            return None
        return self._probe

    def _read_result(self, page: QWebEnginePage) -> None:
        try:
            page.runJavaScript(_READ_RESULT_JS, self._on_probe_result)
        except RuntimeError:
            pass

    def _on_probe_loaded(self, ok: bool) -> None:
        self._probe_loaded = True
        log.info(
            "probe page loaded ok=%s url=%s", ok, self._probe.url().toString()
        )
        self.refresh_credits()

    def _on_dialog_page_loaded(self, ok: bool) -> None:
        log.info(
            "login dialog page loaded ok=%s url=%s",
            ok,
            self._dialog.view.url().toString(),
        )

    def _on_probe_result(self, raw) -> None:
        log.debug("billing probe raw: %s", raw)
        try:
            info = json.loads(raw) if raw else {}
        except (TypeError, json.JSONDecodeError):
            log.warning("probe result not JSON: %r", raw)
            return

        if info.get("rate_limited"):
            log.warning("rate limited (429), backoff 30s")
            self._probe_loaded = False
            self._backoff.start()
            return
        if info.get("logged_in"):
            log.info("credits=%s", info.get("credits"))
            self._set_state({"logged_in": True, "credits": info.get("credits")})
            if self._dialog and self._dialog.isVisible():
                log.info("login confirmed, close dialog")
                self._dialog.accept()
        else:
            reason = info.get("reason") or info.get("error")
            log.info("not logged in: %s dbg=%s", reason, info.get("dbg"))
            if reason in ("no-session", "no-jwt-in-session"):
                self._set_state({"logged_in": False, "credits": None})

    def _set_state(self, state: dict) -> None:
        merged = {"logged_in": False, "email": None, "credits": None}
        merged.update({k: v for k, v in state.items() if v is not None or k == "logged_in"})
        merged = {k: merged.get(k) for k in ("logged_in", "email", "credits")}
        if merged != self._info:
            self._info = merged
            self.session_changed.emit(merged)

    def _reload_probe(self) -> None:
        log.info("reload probe page after backoff")
        self._probe.load(QUrl(SUNO_HOME + "/create"))

    def _on_dialog_closed(self, _code) -> None:
        log.info("login dialog closed")
        self._poll.stop()
        self.refresh_credits()
