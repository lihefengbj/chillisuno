# Suno 非官方接口说明

> 基于页面内 JS 上下文调用，与 Suno 官网前端同链路。
> 接口无官方文档，随官网变动，需跟随维护。

## 认证

Suno 使用 Clerk 认证，但页面**不暴露 window.Clerk 全局对象**。
在页面上下文中调用 Clerk 认证接口取会话 JWT：

```js
const r = await fetch("https://auth.suno.com/v1/client", {credentials:"include"});
const client = await r.json();
const jwt = client.response.sessions[0].last_active_token.jwt;
// 之后所有请求带 Header: Authorization: Bearer <jwt>
```

JWT 有有效期（约 1 分钟级），每次调用前重新走一遍该流程即可。

> 备注：Chromium 请求拦截器会过滤 Authorization 头，无法在 Qt 侧
> 直接捕获令牌，必须走页面内 JS。真实 API 域名为
> `studio-api-prod.suno.com`（2026-10 实测），`studio-api.prod.suno.com`
> 用于媒体流。

## 接口清单（按里程碑使用）

### M1 账户

| 接口 | 说明 |
|---|---|
| `GET https://auth.suno.com/v1/client` | Clerk 会话，取 JWT |
| `GET https://studio-api-prod.suno.com/api/billing/info/` | 账单与额度，`total_credits_left` 为剩余额度 |

用户信息取自 `window.Clerk.user`（邮箱、头像、用户名）。

### M2 曲库

| 接口 | 说明 |
|---|---|
| `GET https://studio-api.suno.ai/api/feed/v2?...` | 作品列表（分页） |
| `GET https://studio-api.suno.ai/api/clip/{id}` | 单曲详情（含音频/封面/歌词地址） |

播放地址通常为 `cdn1.suno.ai/{id}.mp3`。

### M3 下载

下载不走接口：拿到 CDN 地址后用 requests 直接下载，支持并发与断点续传。
MP3 为默认；WAV 需官网转换接口（M3 期间核实）。

### M4 上传

| 接口 | 说明 |
|---|---|
| `POST https://studio-api.suno.ai/api/uploads/audio/` | 上传音频（用于 Extend/Cover），M4 期间核实参数 |

上传前经过本地混淆管道（见 roadmap M4）。

### M5 创作

| 接口 | 说明 |
|---|---|
| `POST https://studio-api.suno.ai/api/generate/v2/` | 提交生成任务（prompt/歌词/风格） |
| `GET  https://studio-api.suno.ai/api/feed/v2?ids=...` | 轮询生成状态 |

## 调用方式约定

所有接口调用统一在 QWebEngine 页面上下文内执行（`runJavaScript`），
不在 Python 侧裸发 HTTP，以保证浏览器指纹与 Cloudflare 校验通过。
例外：CDN 文件下载不受此限。
