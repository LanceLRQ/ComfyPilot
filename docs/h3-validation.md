# H3 真实链路验收

日期：2026-10-28（Asia/Shanghai）。验收配方为 `minimax-h3-replacement`，固定 768×448、24 fps、124 帧、20 步，seed=42。使用用户明确授权的视频和参考图；素材、结果、服务地址及原始环境记录留在独立创作项目，不进入公开仓库或看板文档同步。

## 部署与证据范围

| 平台部署 | 素材适配器 | 结果 |
| --- | --- | --- |
| macOS / Python 3.12.13 → 远程 Linux ComfyUI | bridge，协议1 | 真实生成、重启认领原回执、自动收片、完整解码、Range 206、浏览器播放至结束通过 |
| Linux / Python 3.12.3，与 ComfyUI 同主机 | local，主机实际 input/output 绝对路径 | 真实生成、工具缺失阻塞、恢复命令幂等重放、完整解码、独立版本、Range 206与浏览器播放至结束通过 |

两个独立创作项目分别从 wheel 安装，API/Worker 在源码目录外运行，使用同一个真实 GPU/ComfyUI 实例，按顺序生成。该结果验证两种平台部署和素材通道；不代表两台硬件、不同 ComfyUI 版本或全部宿主均已验证。

目标为 ComfyUI 0.38.0，源码提交 `6b747c0428c343e1417219641db93a4fb7cb69ae`；Python 3.13.15、PyTorch 2.13.0+cu130、PyAV 18.1.0、aiohttp 3.14.3、safetensors 0.8.0，GPU 为 RTX 3090 / 24 GiB。17 种配方节点属于该核心。四个模型的实际大小和 SHA-256 全部与 `presets/video-character-replacement/h3/environment.lock.json` 相符。平台侧 FFmpeg/ffprobe 分别为 macOS 8.1.1、Linux 6.1.1；桥接文件哈希也单独留存。

## 复现步骤

1. 创建独立绝对项目目录，从 wheel 安装 CLI/MCP/Skill。只枚举工具时保持待机；显式 init、start、connect 后再操作项目。
2. 选择用户指定目标，配置 local 目录或按[桥接说明](remote-assets.md)安装扩展。记录目标核心提交、实际解释器/包、模型大小与哈希，不能仅依据 HTTP 自报版本。
3. 导入用户授权原片与参考图。片段不足规格时，按用户明确范围调用 extract_clip，保留时间映射；本次样例已经是 768×448、24 fps、124 帧。
4. refresh_resources，读取 manifest/API 图及输入顺序，probe_target 后 preflight_recipe。预检只覆盖 schema；通过不代表已经生成。
5. 经 MCP create_task/create_plan，记录用户范围、素材 ID 和哈希、完整参数、目标与最多一次提交；activate_plan、execute_plan，再显式 resume_dispatch。Agent 不直接 POST /prompt。
6. 确认 Run 已保存 intent 和 prompt_id 后退出 MCP/浏览器。以 get_execution、list_versions 或同源 API 复核后台状态，不能把采样完成当作可审。
7. 在 ComfyUI 仍运行时停止平台，确认原 prompt_id 仍在远端队列；重新启动并连接平台。派发默认暂停，但旧意图继续对账并收片，节点事件使用同一持久 client_id。
8. 只有原 history 明确 success/completed，才进入收片。检查输出指纹、字节、完整解码、配方规格、版本 ready；核对 Range 206 与浏览器实际播放。
9. 通过仅影响验证平台的受限 PATH 制造 ffmpeg/ffprobe 不可用。确认已有成功证据保留、收片 blocked 且无 ready 版本；恢复平台工具环境后，以新 command_id 调用 resume_collection，仅同一次恢复重放使用相同 ID。
10. 比对恢复前后 Run、prompt_id、额度、版本和目标 history 的项目/Run/意图标记；一个输出槽位只登记一次。保存证据后停止验证平台，保留生成结果供用户审片。

## 证据与复核

平台数据库保存最终 API 图及 graph_hash、输入副本和指纹、目标/资源快照、提交意图/回执、原始成功 history、收片检查点、独立版本和技术检查。验收另外将环境记录保存在项目 `.comfypilot/environment/<target_id>/receipt.json`，将操作前后快照归档为验证记录；这些是验收采集，不是新产品接口。

复核 SQLite 时使用只读连接，查询 execution_runs、execution_plans、output_versions、output_checks，检查 integrity_check 和 foreign_key_check。两个项目分别为一个 Run、一次提交额度、一个版本、一个检查记录；输出均为 H.264/MP4、124 帧、24/1 fps、5.166667 秒、一个模型生成音轨，技术检查无规格差异。

远程输出为278,055字节，SHA-256为 `d6f6b6ed606e78885560dde2e9ea72ac2eee27d177d276dde43941b5311ace04`；同机输出为 `e271f20033a56c02a6099dc211d62dfc39e96651fe764aee1fc12f2da8d4655c`。输入相同仍保存独立来源与版本，不要求 GPU 运算逐字节确定。验收所冻结配方哈希为 `0c3ab83504d2b849913204642d6f4ab7aa6b8d2750d482f278b584bbfd33ac79`；后续修正随包操作说明会产生新资源哈希，历史 Run 的图和资源保持原样，说明更新没有改变 API 图或采样参数。

| 验收范围 | 证据来源 |
| --- | --- |
| AC-04、AC-14、AC-19、AC-35 | 真实 GPU 回执/history、平台重启、自动收片、完整解码与实际浏览器播放 |
| AC-07–13 | 资源/输入/目标冻结与范围门禁测试，真实项目的输入副本和最终图；同内容保持独立身份 |
| AC-15 | 注入已接收丢响应、意图后退出及 history/queue 竞态的自动测试，按 HTTP POST 计数证明不补发 |
| AC-16、AC-36、AC-37 | 收片中断、有限重试、指纹变化、空间/工具缺失、媒体损坏与恢复幂等测试；另在真实输出上验证工具阻塞后的恢复 |

真实目标 history 和本地额度核对证明该项目只保留原提交；故障测试另直接计数 HTTP POST。两类证据分别记录，不把替身输出当真实 GPU 结果，也不以标准 API 宣称服务端恰好执行一次。

## 保留项

Claude Code/Codex 的自动 Skill 发现、权限交互和双宿主/双项目完整矩阵仍属 M1/1.7。新硬件或不同核心/模型版本需重新验收。模型与核心的全新安装/实际回退、画布导入再导出、长期负载和视觉/音频质量仍保留；已有依赖满足时无需为了验收重装目标。

技术 ready 仅表示文件可播放且符合配方规格。用户是否接受人物身份、服装、动作、背景和生成音频，需要人工审片；反馈、采用、局部返工和完整监控面板继续由 M3 实现，物理素材清理由 M4 实现。
