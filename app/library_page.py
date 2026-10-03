"""曲库页：作品列表、搜索、收藏、播放。"""

from PySide6.QtCore import Qt, QSize, QTimer, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.logger import get_logger

log = get_logger("library")


class ClipCard(QFrame):
    """单曲卡片。"""

    def __init__(self, clip: dict, favorite: bool, parent=None) -> None:
        super().__init__(parent)
        self.clip = clip
        self.setObjectName("card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        self.cover = QLabel("♪")
        self.cover.setObjectName("cover")
        self.cover.setFixedSize(56, 56)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)

        info = QVBoxLayout()
        info.setSpacing(2)
        title = QLabel(clip["title"])
        title.setObjectName("clipTitle")
        title.setWordWrap(True)
        title.setMinimumWidth(0)
        title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        tags = QLabel(clip["tags"] or " ")
        tags.setObjectName("clipTags")
        tags.setWordWrap(True)
        tags.setMinimumWidth(0)
        tags.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        info.addWidget(title)
        info.addWidget(tags)

        try:
            dur = int(float(clip.get("duration") or 0))
        except (TypeError, ValueError):
            dur = 0
        dur_label = QLabel(f"{dur // 60}:{dur % 60:02d}" if dur else "")
        dur_label.setObjectName("clipTags")

        self.fav_btn = QPushButton("♥" if favorite else "♡")
        self.fav_btn.setObjectName("favBtn")
        self.fav_btn.setFixedSize(32, 32)
        self.fav_btn.setCheckable(True)
        self.fav_btn.setChecked(favorite)
        self.dl_btn = QPushButton("下载")
        self.dl_btn.setObjectName("downloadBtn")
        self.dl_btn.setFixedSize(56, 32)
        self.dl_btn.setToolTip("加入下载队列")

        layout.addWidget(self.cover)
        layout.addLayout(info, 1)
        layout.addWidget(dur_label)
        layout.addWidget(self.dl_btn)
        layout.addWidget(self.fav_btn)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()


class LibraryPage(QWidget):
    download_requested = Signal(dict)
    download_all_requested = Signal(list)

    def __init__(self, api, storage, player, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self.storage = storage
        self.player = player
        self._clips: list[dict] = []
        self._current_id: str | None = None
        self._cards: list[ClipCard] = []
        self._favorites_only = False
        self._nam = QNetworkAccessManager(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(14)

        title = QLabel("曲库")
        title.setObjectName("pageTitle")

        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索标题或风格…")
        self.fav_only = QPushButton("只看收藏")
        self.fav_only.setCheckable(True)
        refresh_btn = QPushButton("刷新")
        refresh_btn.setObjectName("primary")
        download_all_btn = QPushButton("全部下载")
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.fav_only)
        toolbar.addWidget(download_all_btn)
        toolbar.addWidget(refresh_btn)

        self.status = QLabel("点击刷新加载你的作品")
        self.status.setObjectName("pageDesc")

        self.list = QListWidget()
        self.list.setObjectName("clipList")
        self.list.setSpacing(8)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setVerticalScrollMode(
            QListWidget.ScrollMode.ScrollPerPixel
        )

        layout.addWidget(title)
        layout.addLayout(toolbar)
        layout.addWidget(self.status)
        layout.addWidget(self.list, 1)

        refresh_btn.clicked.connect(self.reload)
        download_all_btn.clicked.connect(
            lambda: self.download_all_requested.emit(list(self._clips))
        )
        self.search.textChanged.connect(self._render)
        self.fav_only.toggled.connect(self._on_fav_only_toggled)
        self.list.itemDoubleClicked.connect(self._play_item)

        api.feed_loaded.connect(self._on_feed)
        api.fetch_failed.connect(self._on_fail)

    # ---- 数据 ----

    def reload(self) -> None:
        self.status.setText("加载中…")
        self.api.fetch_feed()

    def _on_feed(self, clips: list) -> None:
        log.info("on_feed: %d clips", len(clips))
        self._clips = clips
        self.status.setText(f"共 {len(clips)} 首（双击播放）")
        try:
            self._render()
            log.info("render done, list=%d", self.list.count())
        except Exception:
            log.exception("render failed")

    def _on_fail(self, reason: str) -> None:
        self.status.setText(f"加载失败：{reason}")

    def _on_fav_only_toggled(self, checked: bool) -> None:
        self._favorites_only = checked
        self._render()

    # ---- 渲染 ----

    def _render(self) -> None:
        kw = self.search.text().strip().lower()
        favs = self.storage.favorite_ids()
        self.list.clear()
        self._cards.clear()
        for clip in self._clips:
            if kw and kw not in (clip["title"] + clip["tags"]).lower():
                continue
            is_fav = clip["id"] in favs
            if self._favorites_only and not is_fav:
                continue
            item = QListWidgetItem(self.list)
            card = ClipCard(clip, is_fav)
            card.set_active(clip["id"] == self._current_id)
            item.setSizeHint(QSize(self.list.viewport().width() or 300, 72))
            self.list.addItem(item)
            self.list.setItemWidget(item, card)
            self._cards.append(card)
            card.fav_btn.clicked.connect(
                lambda _c=False, cid=clip["id"], btn=card.fav_btn: (
                    btn.setText(
                        "♥" if self.storage.toggle_favorite(cid) else "♡"
                    )
                )
            )
            card.dl_btn.clicked.connect(
                lambda _c=False, c=clip: self.download_requested.emit(c)
            )
            self._load_cover(card)
        self.list.viewport().update()
        QTimer.singleShot(0, self._sync_item_sizes)

    def _sync_item_sizes(self) -> None:
        width = self.list.viewport().width()
        if width <= 0:
            return
        for i in range(self.list.count()):
            item = self.list.item(i)
            card = self.list.itemWidget(item)
            if not card:
                continue
            height = card.layout().totalHeightForWidth(width)
            if height <= 0:
                height = card.sizeHint().height()
            item.setSizeHint(QSize(width, max(height, 72)))
        self.list.doItemsLayout()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._sync_item_sizes)

    def _load_cover(self, card: ClipCard) -> None:
        url = card.clip.get("image_url")
        if not url:
            return
        reply = self._nam.get(QNetworkRequest(QUrl(url)))
        reply.finished.connect(lambda r=reply, c=card: self._on_cover(r, c))

    def _on_cover(self, reply: QNetworkReply, card: ClipCard) -> None:
        data = reply.readAll().data()
        reply.deleteLater()
        pix = QPixmap()
        if pix.loadFromData(data):
            card.cover.setPixmap(pix.scaled(
                56, 56,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            ))
            card.cover.setText("")

    def _play_item(self, item) -> None:
        card = self.list.itemWidget(item)
        if card:
            self._current_id = card.clip["id"]
            self._refresh_active()
            self.player.play_clip(card.clip, self._clips)

    def _refresh_active(self) -> None:
        for card in self._cards:
            card.set_active(card.clip["id"] == self._current_id)
