# New API 视频渠道接入教程

本文说明如何把 grok2api 作为 New API 的视频专用渠道使用。示例中的域名、密钥和 Cookie 都是占位值，部署时请替换为你自己的配置，不要把真实密钥提交到 Git。

## 1. grok2api 基础配置

部署 grok2api 后，先在后台或运行时配置中设置以下项目：

| 配置项 | 说明 | 示例 |
| :-- | :-- | :-- |
| `app.app_key` | Admin 后台登录密钥 | `change-this-admin-key` |
| `app.api_key` | New API 调用 grok2api 时使用的 API Key | `change-this-api-key` |
| `app.app_url` | grok2api 对外访问地址，用于生成本地媒体链接 | `https://grokapi.example.com` |
| `features.video_format` | 视频返回格式 | 推荐 `local_url` |

`app.app_url` 必须是浏览器和 New API 都能访问的 HTTPS 地址，否则图片、视频的本地缓存链接可能无法下载。

## 2. New API 新增视频渠道

在 New API 后台新增一个 OpenAI 兼容渠道：

| 项目 | 填写方式 |
| :-- | :-- |
| 渠道类型 | OpenAI 兼容 |
| Base URL | `https://grokapi.example.com` 或容器内地址 `http://grok2api:8000` |
| API Key | grok2api 的 `app.api_key` |
| 渠道分组 | 按你的 New API 分组填写 |
| 模型范围 | 只添加视频模型，图片模型不要混到这个渠道 |

如果 New API 和 grok2api 在同一台服务器的同一个 Docker 网络内，可以用容器内地址作为 Base URL；如果跨服务器访问，使用 HTTPS 域名。

## 3. 视频模型与映射

建议在 New API 里只展示这四个视频模型，不再用模型名区分横竖屏：

| New API 展示模型 | 映射到 grok2api 上游模型 | 清晰度 | 时长 |
| :-- | :-- | :-- | :-- |
| `grok-imagine-0.8-video-6s` | `grok-imagine-video` | `480p` | `6s` |
| `grok-imagine-0.8-video-10s` | `grok-imagine-video` | `480p` | `10s` |
| `grok-imagine-1.0-video-6s` | `grok-imagine-video` | `720p` | `6s` |
| `grok-imagine-1.0-video-10s` | `grok-imagine-video` | `720p` | `10s` |

比例由请求参数决定：

| size | aspect_ratio | 场景 |
| :-- | :-- | :-- |
| `1280x720` | `16:9` | 横屏视频 |
| `720x1280` | `9:16` | 竖屏视频 |

模型名只负责区分清晰度和时长；真正转发到 grok2api 时统一映射为 `grok-imagine-video`。

grok2api 会对这些别名做兜底归一化：

- `0.8` 固定写入 `resolution=480p` 和 `resolution_name=480p`
- `1.0` 固定写入 `resolution=720p` 和 `resolution_name=720p`
- `6s/10s` 固定写入 `seconds` 和 `duration`
- `aspect_ratio=16:9` 会写入 `size=1280x720`
- `aspect_ratio=9:16` 会写入 `size=720x1280`
- 旧的 `landscape/portrait` 别名仍可兼容，但不建议继续展示

## 4. New API 渠道配置要点

渠道模型列表填写：

```text
grok-imagine-0.8-video-6s,grok-imagine-0.8-video-10s,grok-imagine-1.0-video-6s,grok-imagine-1.0-video-10s
```

模型映射填写：

```json
{
  "grok-imagine-0.8-video-6s": "grok-imagine-video",
  "grok-imagine-0.8-video-10s": "grok-imagine-video",
  "grok-imagine-1.0-video-6s": "grok-imagine-video",
  "grok-imagine-1.0-video-10s": "grok-imagine-video"
}
```

如果 New API 开启了模型价格过滤，需要给这四个模型配置固定价格或倍率，否则 `/v1/models` 可能不会展示它们。

## 5. JSON 请求示例

横屏 720p 10 秒：

```bash
curl https://grokapi.example.com/v1/videos \
  -H "Authorization: Bearer YOUR_GROK2API_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-imagine-1.0-video-10s",
    "prompt": "A clean product showcase video, steady camera, no text, no watermark. 视频比例：16:9 横屏。",
    "duration": 10,
    "size": "1280x720",
    "aspect_ratio": "16:9"
  }'
```

竖屏 480p 6 秒，带参考图：

```bash
curl https://grokapi.example.com/v1/videos \
  -H "Authorization: Bearer YOUR_GROK2API_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-imagine-0.8-video-6s",
    "prompt": "Generate a vertical fashion product video based on the reference image. 视频比例：9:16 竖屏。",
    "duration": 6,
    "size": "720x1280",
    "aspect_ratio": "9:16",
    "images": ["https://example.com/reference.jpg"]
  }'
```

`images` 支持图片 URL、base64 或 data URL。兼容字段包括 `images`、`image_urls`、`input_reference`、`input_references`。

## 6. 查询任务与下载视频

提交成功后会得到任务 ID：

```json
{
  "id": "task_xxx",
  "object": "video",
  "model": "grok-imagine-video",
  "status": "queued",
  "progress": 0
}
```

查询任务：

```bash
curl https://grokapi.example.com/v1/videos/task_xxx \
  -H "Authorization: Bearer YOUR_GROK2API_API_KEY"
```

完成后返回字段示例：

```json
{
  "id": "task_xxx",
  "object": "video",
  "status": "completed",
  "progress": 100,
  "video_url": "https://assets.grok.com/.../generated_video.mp4",
  "url": "https://grokapi.example.com/v1/files/video?id=xxxx",
  "content_url": "https://grokapi.example.com/v1/files/video?id=xxxx"
}
```

字段含义：

| 字段 | 含义 |
| :-- | :-- |
| `video_url` | Grok 上游原始 MP4 链接，方便第三方中转或解析软件读取 |
| `url` | grok2api 本地缓存下载链接 |
| `content_url` | grok2api 本地缓存下载链接，兼容 New API 任务结果读取 |

也可以直接下载：

```bash
curl -L "https://grokapi.example.com/v1/files/video?id=xxxx" -o output.mp4
```

## 7. 常见问题

### 请求到了 grok2api，但返回 `Field required: model/prompt`

通常是上游按 JSON 提交，而旧版本 grok2api 只解析 multipart form。请使用包含 JSON 兼容补丁的版本。

### 竖屏提示词生成了横屏视频

检查请求里的 `size` 或 `aspect_ratio`：

- 横屏使用 `size=1280x720` 或 `aspect_ratio=16:9`
- 竖屏使用 `size=720x1280` 或 `aspect_ratio=9:16`

提示词里写“竖屏 9:16”不能替代 `size/aspect_ratio` 参数；最终比例由请求参数决定。

### New API 用户侧看不到新模型

重点检查：

- 模型是否存在于 New API 渠道模型列表
- 模型是否映射到 `grok-imagine-video`
- `abilities` 是否已经同步到新模型
- 模型是否配置了固定价格或倍率
- 渠道是否启用且分组可用
- Base URL 是否能从 New API 容器访问
- API Key 是否和 grok2api 的 `app.api_key` 一致
