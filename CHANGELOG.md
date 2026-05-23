# 更新日志

## 2026-05-24

### New API 视频渠道适配

- 新增 `/v1/videos` JSON 请求兼容，保留 multipart 上传方式，同时支持 New API 常见的 JSON 视频创建请求。
- 视频任务返回增加本地缓存链接字段，便于 New API 和下游客户端拿到可直接访问的视频地址。
- 新增四个 New API 展示模型别名：
  - `grok-imagine-0.8-video-6s`
  - `grok-imagine-0.8-video-10s`
  - `grok-imagine-1.0-video-6s`
  - `grok-imagine-1.0-video-10s`
- 四个别名统一转发到 `grok-imagine-video`，并自动归一化清晰度、时长和画幅：`0.8` 对应 `480p`，`1.0` 对应 `720p`，`6s/10s` 对应对应视频时长，`16:9/9:16` 对应横竖屏尺寸。
- 新增中文教程 `docs/newapi-video-channel.zh.md`，说明 grok2api 作为 New API 视频专用渠道时的 Base URL、API Key、模型列表、模型映射和排错方式。
