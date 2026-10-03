"""商务专业深色主题（Qt StyleSheet）。"""

ACCENT = "#4F7CFF"
ACCENT_HOVER = "#6B90FF"
BG_MAIN = "#1B1E26"
BG_SIDEBAR = "#14161D"
BG_CARD = "#232733"
BORDER = "#2E3342"
TEXT = "#E8EAF0"
TEXT_DIM = "#8B91A3"

QSS = f"""
QMainWindow, QWidget {{
    background: {BG_MAIN};
    color: {TEXT};
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 14px;
}}

/* ---------- 侧边栏 ---------- */
#sidebar {{
    background: {BG_SIDEBAR};
    border-right: 1px solid {BORDER};
}}
#sidebar QLabel {{
    background: transparent;
}}
#logo {{
    font-size: 20px;
    font-weight: 700;
    color: {TEXT};
    padding: 0;
}}
#logoSub {{
    font-size: 11px;
    color: {TEXT_DIM};
    padding: 6px 20px 18px 20px;
}}
#nav QListWidget, QListWidget#nav {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget#nav::item {{
    height: 42px;
    padding-left: 20px;
    color: {TEXT_DIM};
    border-left: 3px solid transparent;
}}
QListWidget#nav::item:hover {{
    background: #1D2029;
    color: {TEXT};
}}
QListWidget#nav::item:selected {{
    background: #232A3D;
    color: {TEXT};
    border-left: 3px solid {ACCENT};
}}

/* ---------- 主区标题 ---------- */
#pageTitle {{
    font-size: 22px;
    font-weight: 600;
    padding: 4px 0;
}}
#pageDesc {{
    color: {TEXT_DIM};
    font-size: 13px;
}}

/* ---------- 卡片 ---------- */
#card {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
#card QLabel {{
    background: transparent;
    border: none;
}}
#card QLabel#avatar {{
    background: {ACCENT};
    border-radius: 26px;
}}
#avatar {{
    background: {ACCENT};
    color: white;
    font-size: 22px;
    font-weight: 700;
    border-radius: 26px;
}}
#statusDot {{ font-size: 12px; }}
#accountName {{ font-size: 17px; font-weight: 600; }}
#accountSub {{ color: {TEXT_DIM}; font-size: 13px; }}
#creditsValue {{
    font-size: 30px;
    font-weight: 700;
    color: {ACCENT};
}}
#creditsLabel {{ color: {TEXT_DIM}; font-size: 12px; }}

/* ---------- 按钮 ---------- */
QPushButton {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 9px 18px;
    color: {TEXT};
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: #1D2029; }}
QPushButton#primary {{
    background: {ACCENT};
    border: none;
    color: white;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#danger:hover {{ border-color: #E5534B; color: #E5534B; }}

/* ---------- 占位页 ---------- */
#placeholder {{
    color: {TEXT_DIM};
    font-size: 15px;
}}

/* ---------- 日志页 ---------- */
#logView {{
    background: {BG_SIDEBAR};
    border: 1px solid {BORDER};
    border-radius: 8px;
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: 12px;
    color: {TEXT_DIM};
    padding: 8px;
}}

QToolTip {{ background: {BG_CARD}; color: {TEXT}; border: 1px solid {BORDER}; }}
"""
