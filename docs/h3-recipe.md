# 首发人物替换配方与环境验收

日期：2026-10-28（Asia/Shanghai）。M2/2.2 交付 `minimax-h3-replacement`：基于 MiniMax H3 原生 R2V 的单人替换配方。资源发现、API 图校验、参数冻结与目标标准预检已经接入；完整生成、安装重启/回退和画布往返仍保留真实验收。

## 使用方式与范围

Agent 在当前创作项目显式连接平台后，先 `list_resources`，再以当前资源修订调用 `refresh_resources`，从配方记录读取 manifest 和 instructions 对应的内容哈希，通过 `get_resource_file` 读取随包说明。资源不依赖开发仓库路径；源码入口为 [完整配方说明](../presets/video-character-replacement/h3/README.md)。

1. 用户指定执行端，Agent 用 `resolve_target` 确认身份；不因缺模型就切换目标。
2. `probe_target(target_id)` 后立即 `preflight_recipe(recipe_id="minimax-h3-replacement",target_id)`；预检按配方哈希与目标分别保存，15 秒过期或配置/实例变化后重探测。
3. 只读 HTTP 可验证目录和标准 schema；环境管理 capability 缺失时报告缺少目标终端/SSH/容器管理通道，不在本机代装，不调用云 API。
4. 有管理通道后按完整说明记录原环境、队列、固定核心提交、模型哈希和依赖；在已有用户安装/停机指示范围内执行，保留回退环境。运行/等待队列非空时不自动重启。

| 项 | 首发规格 |
| --- | --- |
| 输入 | 单段已准备视频 → 单张人物参考图；分别为 H3 的 Video 1 / Picture 1 |
| 默认输出 | 768×448、124 帧、24 fps，约 5.167 秒；MP4 / H.264、8 bit、sRGB |
| 采样 | 20 步，res_multistep / simple，denoise=1，seed 默认 42 |
| 可调整 | 提示词、JSON 可精确表示范围内的 seed；尺寸、帧数、步数固定 |
| 音频 | H3 生成音频；原片声音/配乐需单独后处理，不默认保留 |
| 输出绑定 | SaveVideo 节点 92，history 的 images 键，按视频处理 |
| 模板占位 | 两个媒体文件名及输出前缀待物化，当前不能把模板直接提交生成 |
| 限制 | 单人替换，不自动识别人、拆片或扩为多角色；不保证遮挡/转身/背景和身份完全保留 |

视频在进入配方前明确原片时间偏移，转为 CFR 24 fps 后取 124 帧，等比缩放补边至 768×448；配方从准备素材第 0 帧读取，不静默裁掉人物或沿用历史项目偏移。完整说明给出 FFmpeg 与 ffprobe 检查命令。素材导入、裁剪映射、上传和最终图哈希由 M2/2.3 及后续执行链实现。

## 固定资源与安装证据

预设包含 API 图、派生画布、未修改的官方模板、环境锁、四模型绑定和单目标证据模板。ComfyUI 核心固定 0.38.0 / `6b747c0428c343e1417219641db93a4fb7cb69ae`；17 种节点全部为核心，无额外自定义节点。四模型为 R2V INT8 convrot 主模型、Qwen3-VL NVFP4 编码器、视频 INT8 convrot VAE、音频 FP32 VAE，来源修订、大小、下载 URL、SHA-256 与模型许可独立记录。

模型文件约 40.1 GB；需要额外环境/缓存/输出空间和合适 offload。当前 3090/24 GiB 的可达性不证明该规格一定不会 OOM。CPU/GPU、PyTorch CUDA wheel、PyAV 与 FFmpeg 都在目标环境核验；不能用开发机版本代替远端实测。模型卡推荐的量化条件和许可链接见完整说明，不捆绑权重。

环境安装说明包含：用户选择 target_id → 确认管理通道与目标解释器/目录 → 备份 Git/依赖/启动方式 → 检查运行和等待队列 → 隔离安装固定核心与模型 → 哈希/包/编码器检查 → 按授权重启 → 重探测/预检 → 完整生成验收；失败切回原目录/venv/镜像并重新核验，不清队列、不删除模型、不改其他目标。

目标标准证据由平台按 recipe_hash + target_id 保存；Agent 的安装证据另存在当前项目 `.comfypilot/environment/<target_id>/receipt.json`，模板随预设交付，不进全局配置。手写 receipt 不会被平台自动采信为安装成功；本阶段没有远程安装适配器。

## 验证结果与保留项

| 验证 | 结果与边界 |
| --- | --- |
| 资源与冻结 | 内置配方可发现；输入顺序、参考图/视频连线、参数、四个模型及 SaveVideo 输出键绑定有效；内容按快照读取 |
| V3 标准节点 | 支持 COMBO 选项、AUTOGROW 点分槽位和 DYNAMICCOMBO 选中分支；非法槽位、缺必填输入、非法枚举与范围拒绝；其他未知类型保留 unknown |
| 媒体输入 | 上传选择器仅校验槽位，materialization 仍 pending，不以已有文件枚举拒绝待上传素材，也不冒称素材存在 |
| 目标隔离 | 标准接口替身中制造单目标缺模型，另一目标通过，分别保存；passed 不代表安装版本或权重 bytes 已验证 |
| 真实远程 ComfyUI | HTTP/WebSocket、971 节点和基础监测通过；H3 标准预检 passed，缺节点/模型/绑定错误均为 0；完整监测经 MCP 返回约 748 KiB |
| 工程与安装包 | make check 通过，109 项 pytest；make package 和源码外干净 venv 的 CLI/Skill/MCP、双项目、API/Worker/Web、H3 资源/冻结、SQLite 完整性与同目标真实预检均通过 |
| 预处理示例 | FFmpeg/ffprobe 8.1.1 对合成素材执行有偏移的转换/补边/取帧，通过 768×448、24/1 fps、124 帧检查；不代表目标已装 FFmpeg |
| 目标环境和生成 | 源码提交/模型实际哈希、依赖安装、重启与回退、画布导入再导出、真实显存/视觉/音频质量仍未实测 |
| 其他环境 | Codex/Claude Code 双宿主、Linux 项目平台、本机 ComfyUI 保留 M1/1.7；完整生成/收片保留 M2/2.9 |

预检不会执行 VALIDATE_INPUTS、自定义 Python、权重读取或 POST /prompt。标准预检通过时仍返回 `version_verification: unknown`、`materialization: pending`、`real_test: not_run`。后续完整验收需保存素材/配方/最终图哈希、prompt_id、目标依赖证据、history 和 ffprobe 结果，再由用户审片，不能用参考项目历史成片替代当前交付的实测。
