# 视频、参考图与显式片段

日期：2026-10-28（Asia/Shanghai）。M2/2.3 提供项目内素材准备；尚不向 ComfyUI 上传媒体、提交生成或自动收集输出。

## 使用入口

网页“素材与显式片段”支持选择视频或人物参考图，直接发送文件字节，显示上传进度，再轮询后台解码状态。素材卡可预览并展开哈希、来源和时间映射。此处是输入素材预览；逐帧对照、反馈和采用仍属于后续审片功能。

Agent 使用以下 MCP 工具，始终携带绝对 `project_root` 与显式连接的 `connection_ref`：

| 工具 | 参数与结果 |
| --- | --- |
| `import_asset` | `source_path` 为平台所在机器的普通文件绝对路径；`kind` 为 video/image；返回媒体任务 |
| `extract_clip` | 指定原片 `source_asset_id`、起止秒数，可绑定 `reference_asset_id`；返回媒体任务 |
| `get_media_job` | 按 `job_id` 查询排队、处理、成功或错误 |
| `get_asset` | 按成功任务的 `asset_id` 读取完整素材记录 |
| `list_assets` | 最近 100 项素材、最近 50 项任务及当前导入限额；更早记录用已保存 ID 单独查询 |

导入/提取并非幂等命令，每次成功产生独立素材身份。请求超时或回执丢失时先查列表；已取得任务 ID 时查该任务，不自动重复提交。

媒体任务与生成 Run 独立。`queued` 仅代表已保存任务，Worker 串行执行复制/提取与真实解码，成功状态为 `succeeded`、阶段为 `ready`，此时 `get_asset` 才可读取。上传进度只表示已传字节，不表示媒体检查完成。退出 MCP 后平台继续处理；生成的手动派发暂停不会暂停本地素材准备。

## 格式、限额与存储

平台运行环境需要 PATH 中有 `ffmpeg` 与 `ffprobe`，FFmpeg 需支持 libx264。工具只在操作时检测，MCP 待机不执行探测。当前接受 MP4/MOV、MKV/WebM 视频以及 PNG/JPEG/WebP 图片，按真实容器和解码验证，不信任扩展名。其他容器、播放列表和网络媒体引用不接纳。原片保留全部原始字节与音轨；工作片只保留视频。

项目有效配置支持以下字节上限，设置页可修改；手改 YAML 后须显式接纳配置：

```yaml
import_limits:
  video_bytes: 2147483648
  image_bytes: 52428800
  workflow_bytes: 5242880
```

上限允许范围分别为 1 字节至 64 GiB、1 GiB、32 MiB。工作流上限用于资源扫描的单文件，资源批次仍限制 32 MiB、1000 文件、解析目录 30 MiB，也适用于预设文件。媒体每块最多处理 1 MiB，不将完整文件读入 Python 内存。已知大小提前检查，流式无长度请求按累计字节检查，超限清理未接纳的临时副本；磁盘另预留至少 16 MiB。

原件成功导入后位于 `.comfypilot/artifacts/inputs/<SHA-256>`，元数据和任务保存在 SQLite，处理中间文件在 `.comfypilot/staging/`。同内容可共享字节，每次导入仍有不同 asset ID、来源与创建时间；不合并业务引用。修改或移走外部原文件不影响成功导入副本。复制期间原件发生变化则拒绝登记。

素材文件只能通过项目内登记 ID 读取；受管目录和本机来源不接纳符号链接、特殊文件或输出根逃逸。文件名仅用于展示，不作为落盘路径。HTTP 写操作沿用回环、同源与实例校验，SQL 使用参数绑定，FFmpeg 使用参数数组及本地协议/格式白名单。

## 显式范围与时间映射

必须从已导入的原片指定 `start_seconds`（含）和 `end_seconds`（不含）。默认 24 FPS、768×448；可显式选择 1–120 的整数 FPS、32–4096 的偶数宽高。等比缩放并补边，输出 H.264/yuv420p MP4，无音轨。当前不接受对工作片再次嵌套提取，也不自动识别镜头或人物。

时间原点是原片第一视频帧。先归一到指定 CFR，再按帧范围提取；起止秒数必须对齐输出帧网格，平台不静默取整。例：24 FPS 的 1–3 秒对应原片归一时间线 `[24,72)`，共 48 帧。保存：

- 原片 asset ID、原始时间请求与 `time_map`，含 origin、mode、FPS、起止帧和实际秒数。
- 解码后的实际帧数、尺寸、FPS、时长和视频起始时间。
- `input_order` 按 video → reference 固定保存工作片与可选参考图的 asset ID 和内容哈希。

变量帧率按归一时间线映射，不声称对应原始编码帧序号。范围超过时长、时间未对齐或实际帧数不符时拒绝登记。一般工作片规格不等于配方可运行条件；H3 仍要求其固定规格与 124 帧，生成链路接入前不能据此声称已可提交。

## HTTP 与失败处理

HTTP 统一返回项目/实例身份与 data，错误为结构化 code/message：

| 接口 | 用途 |
| --- | --- |
| `GET /api/assets` | 列表和限额 |
| `POST /api/assets/import` | JSON 本机路径导入 |
| `POST /api/assets/upload?kind=image&filename=reference.png` | 原始文件请求体流式上传，支持无 Content-Length；不接 multipart/base64 |
| `POST /api/assets/clips` | JSON 显式片段参数 |
| `POST /api/assets/jobs/get`、`POST /api/assets/get` | 按任务/素材 ID 查询 |
| `GET /api/assets/{asset_id}/content` | 受管文件读取与 Range 预览 |
| `GET /api/assets/{asset_id}/poster` | 受管视频或图片的 JPEG 预览 |
| `GET /api/versions/{version_id}/poster?view=candidate\|source` | 候选播放代理首帧，或对应原片/输入片段起点预览 |

poster 为按需生成、最多 480×270 的真实 JPEG，缓存在 `.comfypilot/cache/posters/`，可重建；不替换正式原件、代理或内容哈希，不增加素材和生成记录。读取前仍校验源 ID 和受管路径。FFmpeg 缺失、无法解码或超时会返回明确错误，缩略图显示不可用，视频内容和审片决定仍走原入口；不填充虚构预览。原片预览偏移只取已核实映射，未知时使用输入片段 0 秒。实现与审片入口见[第三批 UI](review-workbench.md#第三批手机专注审片与预览时间轴)。

写操作需 `X-ComfyPilot-Instance`。缺工具提示安装并加入平台 PATH；无空间提示释放空间；损坏媒体、错误类型和越界范围给出具体原因。外部进程探测最长 30 秒，解码/提取每次最长 300 秒，超时任务阻塞，用户可先在外部缩短素材再重新提交。

停止平台会取消媒体子进程；在途任务标记 blocked，重启遗留 running 也标 blocked 并清理所属临时文件，不自动重放。正常 queued 任务持久保存，Worker 下次启动继续领取。失败任务不登记素材；可修复原因后重新提交，历史错误保留。

本轮已验证 macOS、本机 FFmpeg、真实解码、浏览器操作、stdio MCP、HTTP 分块、独立副本和路径边界。大文件上限采用小文件与配置缩限验证，未声称实际完成 2 GiB 压力测试；Linux、双宿主完整验收、真实 ComfyUI 传输/生成/收片及 H3 安装回退继续保留。
