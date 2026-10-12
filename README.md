# chillisuno — Suno 桌面辅助工具

chillisuno 是 Suno.com 的桌面端辅助工具（非官方），支持 Windows / macOS。
帮助用户在桌面完成 Suno 账号的日常操作：登录/退出、曲库管理、在线播放、
单曲与批量下载、音频上传，以及后期的音频混淆预处理。

不做自有音乐生成，所有内容均来自用户的 Suno 账号。

## 技术栈

Python 3.12 + PySide6 (Qt6)，内嵌 QWebEngine (Chromium) 作为 Suno 接入引擎。
详见 [docs/架构.md](docs/架构.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/架构.md](docs/架构.md) | 架构与技术选型决策 |
| [docs/API接口.md](docs/API接口.md) | Suno 非官方接口说明 |
| [docs/UI设计.md](docs/UI设计.md) | UI 布局与页面设计 |
| [docs/路线图.md](docs/路线图.md) | 里程碑计划 |
| [docs/打包发布.md](docs/打包发布.md) | 双平台打包发布 |
| [docs/进度.md](docs/进度.md) | 开发进度（每次代码变动后同步更新） |

## 运行

项目使用 Python 3.12；这是当前 CI 验证过的版本。更高版本尚未验证，建议使用 3.12。

### 安装 Python

从 [Python 官方下载页](https://www.python.org/downloads/) 安装 Python 3.12。安装完成后，在终端检查版本：

- macOS：`python3 --version`
- Windows：`py -3.12 --version`

### macOS

在项目根目录运行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

### Windows（PowerShell）

在项目根目录运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

激活成功后，命令行提示符会显示 `(.venv)`。以后再次运行项目时，先进入项目目录并激活虚拟环境；完成后可运行 `deactivate` 退出。

## 风险提示

- 基于非官方接口，Suno 页面结构变动可能导致功能失效，需跟随维护
- 自动化批量操作与混淆上传可能触发平台风控，仅限个人使用，勿分发售卖
