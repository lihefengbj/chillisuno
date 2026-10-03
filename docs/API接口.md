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
> `studio-api-prod.suno.com`（2026-10 实测）。

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
| `GET https://studio-api-prod.suno.com/api/feed/v2?page=N` | 作品列表（分页） |
| `GET https://studio-api-prod.suno.com/api/clip/{id}` | 单曲详情（含音频/封面/歌词地址） |

新版曲库中 `audio_url` 为 `.../api/forbidden` 占位符，真实音频在
`media_urls[].url`，是 **AES 加密的 m4a-opus**。播放需解密：

1. `POST https://studio-api-prod.suno.com/api/mango/rights`
   body: `{"content_params":{"content_id":"{clipId}","content_type":"clip"}}`
   → 返回 `{key, iv}`
2. `userKey = SHA-256(Clerk JWT)`（AES-GCM）
3. 用 AES-GCM(`additionalData=clipId`) 解开 key/iv
4. 下载 `media_urls[].url`，AES-CTR 解密整段（counter=16B，
   IV 取解出 iv 的前 12 字节）
5. 结果 Blob URL 交给 HTMLAudioElement 播放

### M3 下载

下载可复用 `mango/rights` 解密字节直接落盘；MP3/WAV 走官网下载接口
（M3 期间核实，见 BetterSuno 参考的 `/api/gen/{id}/wav_file/` 与
`/api/download/clip/{id}?format=...`）。

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
