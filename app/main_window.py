from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.auth_service import AuthService
from app import theme
from app.logger import bus as log_bus
from app.library_page import LibraryPage
from app.player import PlayerBar
from app.storage import Storage
from app.suno_api import SunoApi


class LogPage(QWidget):
    MAX_LINES = 2000

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(12)

        title = QLabel("日志")
        title.setObjectName("pageTitle")
        self.view = QPlainTextEdit()
        self.view.setObjectName("logView")
        self.view.setReadOnly(True)

        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self.view.clear)

        layout.addWidget(title)
        layout.addWidget(self.view, 1)
        layout.addWidget(clear_btn, 0, Qt.AlignmentFlag.AlignRight)

        log_bus.record.connect(self._append)

    def _append(self, level: str, message: str) -> None:
        self.view.appendPlainText(f"[{level}] {message}")
        doc = self.view.document()
        while doc.blockCount() > self.MAX_LINES:
            cursor = self.view.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()


def _placeholder_page(text: str) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    label = QLabel(text)
    label.setObjectName("placeholder")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(label)
    return page


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("chillisuno — Suno 桌面助手")
        self.resize(1180, 760)
        self.setStyleSheet(theme.QSS)

        self.auth = AuthService(self)
        self.auth.session_changed.connect(self._on_session_changed)
        self.storage = Storage()
        self.api = SunoApi(self.auth, self)
        self.player = PlayerBar(self)

        # ---------- 侧边栏 ----------
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(0)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(20, 20, 20, 0)
        brand_row.setSpacing(10)
        logo_icon = QLabel()
        logo_icon.setPixmap(
            QPixmap("assets/logo.png").scaled(
                34,
                34,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        logo_icon.setFixedSize(34, 34)
        brand_row.addWidget(logo_icon)
        brand_row.addSpacing(0)

        logo = QLabel("chillisuno")
        logo.setObjectName("logo")
        brand_row.addWidget(logo)
        brand_row.addStretch(1)

        logo_sub = QLabel("Suno 桌面助手")
        logo_sub.setObjectName("logoSub")

        nav = QListWidget()
        nav.setObjectName("nav")
        for name in ("账户", "曲库", "下载中心", "上传", "设置", "日志"):
            QListWidgetItem(name, nav)
        nav.setCurrentRow(0)
        nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        side_layout.addLayout(brand_row)
        side_layout.addWidget(logo_sub)
        side_layout.addWidget(nav, 1)

        # ---------- 页面栈 ----------
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_account_page())
        self.pages.addWidget(LibraryPage(self.api, self.storage, self.player))
        self.pages.addWidget(_placeholder_page("下载中心 · M3 开发中"))
        self.pages.addWidget(_placeholder_page("上传 · M4 开发中"))
        self.pages.addWidget(_placeholder_page("设置 · 待实现"))
        self.pages.addWidget(LogPage())

        nav.currentRowChanged.connect(self.pages.setCurrentIndex)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self.pages, 1)
        right.addWidget(self.player)

        body.addLayout(right, 1)
        container = QWidget()
        container.setLayout(body)
        self.setCentralWidget(container)

    # ---------- 账户页 ----------

    def _build_account_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(16)

        title = QLabel("账户")
        title.setObjectName("pageTitle")
        desc = QLabel("管理你的 Suno 账号会话与额度")
        desc.setObjectName("pageDesc")
        layout.addWidget(title)
        layout.addWidget(desc)

        # 账户卡片
        card = QFrame()
        card.setObjectName("card")
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(24, 24, 24, 24)
        card_layout.setSpacing(16)

        self.avatar = QLabel("–")
        self.avatar.setObjectName("avatar")
        self.avatar.setFixedSize(52, 52)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)

        info = QVBoxLayout()
        info.setSpacing(4)
        self.name_label = QLabel("未登录")
        self.name_label.setObjectName("accountName")
        self.status_label = QLabel("登录后可管理曲库与下载")
        self.status_label.setObjectName("accountSub")
        info.addWidget(self.name_label)
        info.addWidget(self.status_label)

        credits_box = QVBoxLayout()
        credits_box.setSpacing(2)
        self.credits_value = QLabel("—")
        self.credits_value.setObjectName("creditsValue")
        self.credits_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        credits_label = QLabel("剩余额度")
        credits_label.setObjectName("creditsLabel")
        credits_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        credits_box.addWidget(self.credits_value)
        credits_box.addWidget(credits_label)

        card_layout.addWidget(self.avatar)
        card_layout.addLayout(info, 1)
        card_layout.addLayout(credits_box)
        layout.addWidget(card)

        # 操作按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        self.login_btn = QPushButton("登录 Suno")
        self.login_btn.setObjectName("primary")
        self.import_btn = QPushButton("导入 Cookie")
        self.logout_btn = QPushButton("退出登录")
        self.logout_btn.setObjectName("danger")
        self.refresh_btn = QPushButton("刷新额度")
        btn_row.addWidget(self.login_btn)
        btn_row.addWidget(self.import_btn)
        btn_row.addWidget(self.refresh_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.logout_btn)
        layout.addLayout(btn_row)

        layout.addStretch(1)

        self.login_btn.clicked.connect(self.auth.login)
        self.import_btn.clicked.connect(self._show_import_dialog)
        self.logout_btn.clicked.connect(self.auth.logout)
        self.refresh_btn.clicked.connect(self.auth.refresh_credits)
        self._update_account_ui({"logged_in": False})
        return page

    def _show_import_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("从浏览器导入 Cookie")
        dlg.resize(560, 320)
        layout = QVBoxLayout(dlg)
        hint = QLabel(
            "适用于 Google 无法在内嵌窗口登录的情况。\n"
            "注意：登录凭证都是 HttpOnly，必须从网络请求头复制，"
            "且要复制发往 auth.suno.com 的请求（它才带完整的会话凭证）：\n"
            "1. 在常用浏览器中登录 suno.com 并保持在该页面\n"
            "2. 按 F12 → 网络(Network) → 筛选框输入 auth.suno.com → 刷新页面\n"
            "   （没有请求出现就等约 1 分钟，Clerk 会定期发）\n"
            "3. 点一条 v1/client 请求 → 标头(Headers) → 请求头 Cookie: "
            "→ 复制整行值（应包含 __client 和 __session）\n"
            "4. 粘贴到下面，点击导入"
        )
        hint.setWordWrap(True)
        edit = QPlainTextEdit()
        edit.setPlaceholderText("name1=value1; name2=value2; ...")
        ok = QPushButton("导入")
        ok.setObjectName("primary")

        def do_import() -> None:
            text = edit.toPlainText()
            if "__client" not in text:
                hint.setText(
                    "⚠ 粘贴的内容中没有 __client（长期会话凭证）。\n"
                    "请确认复制的是发往 auth.suno.com 的 v1/client 请求的 "
                    "Cookie 头（不是 suno.com 页面请求的）。"
                )
                return
            n = self.auth.import_cookies(text)
            if n:
                dlg.accept()

        ok.clicked.connect(do_import)
        layout.addWidget(hint)
        layout.addWidget(edit, 1)
        layout.addWidget(ok, 0, Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    def _on_session_changed(self, info: dict) -> None:
        self._update_account_ui(info)

    def _update_account_ui(self, info: dict) -> None:
        if info.get("logged_in"):
            email = info.get("email") or "Suno 用户"
            self.name_label.setText(email)
            self.status_label.setText("● 已连接")
            self.status_label.setStyleSheet("color: #3FB950; font-size: 13px;")
            self.avatar.setText(email[0].upper())
            credits = info.get("credits")
            self.credits_value.setText(str(credits) if credits is not None else "…")
            self.login_btn.setEnabled(False)
            self.logout_btn.setEnabled(True)
        else:
            self.name_label.setText("未登录")
            self.status_label.setText("登录后可管理曲库与下载")
            self.status_label.setStyleSheet("")
            self.avatar.setText("–")
            self.credits_value.setText("—")
            self.login_btn.setEnabled(True)
            self.logout_btn.setEnabled(False)
