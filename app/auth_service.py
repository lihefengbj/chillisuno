"""Suno 登录会话管理。

内嵌 QWebEngineView 加载 suno.com，用户用官方页面完成 OAuth 登录。
登录态由持久化 QWebEngineProfile 保持；登录状态与额度通过页面内的
window.Clerk 会话令牌调用 studio-api.suno.ai 获得（与官网前端同链路）。
"""

import json
import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, QTimer, QUrl, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QVBoxLayout

SUNO_HOME = "https://suno.com"
BILLING_API = "https://studio-api.suno.ai/api/billing/info/"

# 使用真实 Chrome UA：QtWebEngine 默认 UA 带标识，易被限流/风控
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# 在 suno.com 页面上下文中执行：取 Clerk 会话令牌 -> 拉取账单/额度信息。
# 结果 JSON 序列化后返回给 Python。
_FETCH_SESSION_JS = """
(async () => {
  try {
    if (document.title && document.title.toLowerCase().includes("too many"))
      return JSON.stringify({logged_in:false, rate_limited:true});
    if (!window.Clerk || !window.Clerk.session)
      return JSON.stringify({logged_in:false, title:document.title||""});
    const token = await window.Clerk.session.getToken();
    if (!token) return JSON.stringify({logged_in:false});
    const resp = await fetch("%s", {
      headers: { "Authorization": "Bearer " + token }
    });
    if (!resp.ok) return JSON.stringify({logged_in:true, credits:null});
    const data = await resp.json();
    return JSON.stringify({
      logged_in: true,
      credits: data.total_credits_left ?? null,
      email: (window.Clerk.user && window.Clerk.user.primaryEmailAddress
              && window.Clerk.user.primaryEmailAddress.emailAddress) || null
    });
  } catch (e) {
    return JSON.stringify({logged_in:false, error:String(e)});
  }
})()
""" % BILLING_API


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
        self._migrate_profile_dir(profile_dir)
        self.profile = QWebEngineProfile("chillisuno", self)
        self.profile.setPersistentStoragePath(str(profile_dir))
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self.profile.setHttpUserAgent(CHROME_UA)
        self._dialog: LoginDialog | None = None
        self._info: dict = {"logged_in": False}
        self._probe_loaded = False

        # 后台探测页：懒加载，避免与登录窗口同时请求 suno.com 触发限流
        self._probe = QWebEnginePage(self.profile, self)

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
        if self._dialog is None:
            self._dialog = LoginDialog(self.profile, self.parent())
            self._dialog.finished.connect(self._on_dialog_closed)
        self._dialog.show()
        self._poll.start()

    def logout(self) -> None:
        self.profile.cookieStore().deleteAllCookies()
        self.profile.clearHttpCache()
        self._info = {"logged_in": False}
        self._probe_loaded = False
        self.session_changed.emit(self._info)

    def refresh_credits(self) -> None:
        if not self._probe_loaded:
            self._probe_loaded = True
            self._probe.load(QUrl(SUNO_HOME))
            return
        self._probe.runJavaScript(_FETCH_SESSION_JS, self._on_probe_result)

    # ---- 内部 ----

    @staticmethod
    def _migrate_profile_dir(new_dir: Path) -> None:
        """旧版本登录态目录（growmusic）自动迁移到 chillisuno。"""
        if new_dir.exists():
            return
        candidates = [
            Path.home() / "AppData/Roaming/growmusic/GROW MUSIC/webprofile",
            Path.home() / "AppData/Roaming/growmusic/webprofile",
            Path.home() / "AppData/Roaming/grow-music/webprofile",
            Path.home() / "AppData/Roaming/GROW MUSIC/webprofile",
        ]
        for old in candidates:
            if old.exists():
                new_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new_dir))
                return

    def _on_probe_result(self, raw) -> None:
        try:
            info = json.loads(raw) if raw else {"logged_in": False}
        except (TypeError, json.JSONDecodeError):
            return
        if info.get("rate_limited"):
            # 命中 429 页面：30 秒后重新加载探测页
            self._probe_loaded = False
            self._backoff.start()
            info = {"logged_in": False}
        if info != self._info:
            self._info = info
            self.session_changed.emit(info)
        if info.get("logged_in") and self._dialog and self._dialog.isVisible():
            self._dialog.accept()

    def _reload_probe(self) -> None:
        self._probe_loaded = True
        self._probe.load(QUrl(SUNO_HOME))

    def _on_dialog_closed(self, _code) -> None:
        self._poll.stop()
        self.refresh_credits()
