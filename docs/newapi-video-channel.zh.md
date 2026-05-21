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

建议在 New API 里只添加这四个视频模型：

| New API 展示模型 | 映射到 grok2api 上游模型 | 推荐尺寸 |
| :-- | :-- | :-- |
| `grok-imagine-1.1-video-landscape-10s` | `grok-imagine-video` | `1280x720` |
| `grok-imagine-1.1-video-portrait-10s` | `grok-imagine-video` | `720x1280` |
| `grok-imagine-0.8-video-landscape-6s` | `grok-imagine-video` | `1280x720` |
| `grok-imagine-0.8-video-portrait-6s` | `grok-imagine-video` | `720x1280` |

比例说明：

| size | 比例 | 场景 |
| :-- | :-- | :-- |
| `1280x720` | `16:9` | 横屏视频 |
| `720x1280` | `9:16` | 竖屏视频 |

模型名只负责在 New API 里区分价格、时长和横竖屏；真正转发到 grok2api 时统一映射为 `grok-imagine-video`。

## 4. JSON 请求示例

横屏 10 秒：

```bash
curl https://grokapi.example.com/v1/videos \
  -H "Authorization: Bearer YOUR_GROK2API_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-imagine-video",
    "prompt": "A clean product showcase video, steady camera, no text, no watermark.",
    "duration": 10,
    "size": "1280x720"
  }'
```

竖屏 6 秒，带参考图：

```bash
curl https://grokapi.example.com/v1/videos \
  -H "Authorization: Bearer YOUR_GROK2API_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-imagine-video",
    "prompt": "Generate a vertical fashion product video based on the reference image.",
    "duration": 6,
    "size": "720x1280",
    "images": ["https://example.com/reference.jpg"]
  }'
```

`duration` 会被 grok2api 映射为 `seconds`。`images` 支持图片 URL、base64 或 data URL，并会作为参考图传入视频生成流程。

## 5. 查询任务与下载视频

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

## 6. 常见问题

### 请求到了 grok2api，但返回 `Field required: model/prompt`

通常是上游按 JSON 提交，而旧版本 grok2api 只解析 multipart form。请使用包含 JSON 兼容补丁的版本。

### 竖屏提示词生成了横屏视频

检查 New API 选择的模型和 `size` 是否一致：

- 横屏模型使用 `1280x720`
- 竖屏模型使用 `720x1280`

提示词里写“竖屏 9:16”不能替代 `size` 参数；最终比例由 `size` 映射出的 `aspectRatio` 决定。

### New API 没有请求到 grok2api

重点检查：

- 模型是否存在于 New API 渠道模型列表
- 模型是否映射到 `grok-imagine-video`
- 渠道是否启用且分组可用
- Base URL 是否能从 New API 容器访问
- API Key 是否和 grok2api 的 `app.api_key` 一致
