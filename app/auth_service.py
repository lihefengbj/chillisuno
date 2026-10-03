"""Suno 登录会话管理。

内嵌 QWebEngineView 加载 suno.com，用户用官方页面完成 OAuth 登录。
登录态由持久化 QWebEngineProfile 保持；登录状态与额度通过页面内的
window.Clerk 会话令牌调用 studio-api.suno.ai 获得（与官网前端同链路）。

检测策略：登录窗口打开期间直接在窗口页面上检测（用户实际操作的就是
这个页面，状态最准确）；窗口关闭后用后台探测页检测。
"""

import json
import re
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, QTimer, QUrl, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QVBoxLayout

from app.logger import get_logger

log = get_logger("auth")

SUNO_HOME = "https://suno.com"
BILLING_API = "https://studio-api.suno.ai/api/billing/info/"


def _chrome_like_ua(profile: QWebEngineProfile) -> str:
    """取 QtWebEngine 真实 UA，仅移除 QtWebEngine 标识。

    保持 Chromium 版本号与内核一致——UA 与真实指纹不符会触发
    Cloudflare Turnstile 反复人机验证。
    """
    return re.sub(r"QtWebEngine/\S+\s*", "", profile.httpUserAgent())


# 注意：QWebEnginePage.runJavaScript 不会等待 Promise 完成，
# 因此分两段执行：_START 启动异步任务并把结果写入 window.__chilliResult，
# 延迟后再用 _READ 读取。
_START_SESSION_JS = (
    """
window.__chilliResult = null;
(async () => {
  const dbg = {url: location.href, title: document.title || "",
               clerk: !!window.Clerk,
               session: !!(window.Clerk && window.Clerk.session)};
  try {
    if (dbg.title.toLowerCase().includes("too many"))
      throw {rate_limited: true};
    if (!dbg.session)
      throw {reason: "no-clerk-session"};
    const token = await window.Clerk.session.getToken();
    if (!token)
      throw {reason: "no-token"};
    const resp = await fetch("%s", {
      headers: { "Authorization": "Bearer " + token }
    });
    if (!resp.ok)
      throw {reason: "billing-http-" + resp.status, logged_in: true};
    const data = await resp.json();
    window.__chilliResult = JSON.stringify({
      logged_in: true,
      credits: data.total_credits_left ?? null,
      email: (window.Clerk.user && window.Clerk.user.primaryEmailAddress
              && window.Clerk.user.primaryEmailAddress.emailAddress) || null,
      dbg
    });
  } catch (e) {
    window.__chilliResult = JSON.stringify(Object.assign(
      {logged_in: false, dbg}, (e && e.reason) ? e : {error: String(e)}));
  }
})();
"started"
"""
    % BILLING_API
)

_READ_SESSION_JS = "window.__chilliResult"


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

        self._dialog: LoginDialog | None = None
        self._info: dict = {"logged_in": False}
        self._probe_loaded = False

        # 后台探测页：懒加载，避免与登录窗口同时请求 suno.com 触发限流
        self._probe = QWebEnginePage(self.profile, self)
        self._probe.loadFinished.connect(self._on_probe_loaded)

        # 登录窗口打开期间轮询登录态（5s 一次，避免触发 Too Many Requests）
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
        self._info = {"logged_in": False}
        self._probe_loaded = False
        self.session_changed.emit(self._info)

    def refresh_credits(self) -> None:
        page = self._active_page()
        if page is None:
            return
        page.runJavaScript(_START_SESSION_JS)
        QTimer.singleShot(1500, lambda: self._read_result(page))

    def _read_result(self, page: QWebEnginePage) -> None:
        # 页面可能已跳转，读取时确认页面仍然有效
        try:
            page.runJavaScript(_READ_SESSION_JS, self._on_probe_result)
        except RuntimeError:
            pass

    # ---- 内部 ----

    def _active_page(self) -> QWebEnginePage | None:
        """优先用登录窗口页面（用户实际操作页），否则用后台探测页。"""
        if self._dialog is not None and self._dialog.isVisible():
            url = self._dialog.view.url()
            if "suno.com" in url.host():
                return self._dialog.view.page()
            log.debug("login dialog page not on suno.com yet: %s", url.toString())
            return None
        if not self._probe_loaded:
            log.info("lazy-load probe page: %s", SUNO_HOME)
            self._probe.load(QUrl(SUNO_HOME + "/create"))
            return None
        return self._probe

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
        log.debug("session probe raw: %s", raw)
        try:
            info = json.loads(raw) if raw else {"logged_in": False}
        except (TypeError, json.JSONDecodeError):
            log.warning("probe result not JSON: %r", raw)
            return

        if info.get("rate_limited"):
            # 命中 429 页面：30 秒后重新加载探测页
            log.warning("rate limited (429), backoff 30s")
            self._probe_loaded = False
            self._backoff.start()
            info = {"logged_in": False}
        elif not info.get("logged_in"):
            log.info(
                "not logged in: reason=%s dbg=%s",
                info.get("reason"),
                info.get("dbg"),
            )
        else:
            log.info(
                "logged in: email=%s credits=%s (billing reason=%s)",
                info.get("email"),
                info.get("credits"),
                info.get("reason"),
            )

        state = {k: info.get(k) for k in ("logged_in", "email", "credits")}
        if state != self._info:
            self._info = state
            self.session_changed.emit(state)
        if state.get("logged_in") and self._dialog and self._dialog.isVisible():
            log.info("login detected, close dialog")
            self._dialog.accept()

    def _reload_probe(self) -> None:
        log.info("reload probe page after backoff")
        self._probe.load(QUrl(SUNO_HOME + "/create"))

    def _on_dialog_closed(self, _code) -> None:
        log.info("login dialog closed")
        self._poll.stop()
        self.refresh_credits()
