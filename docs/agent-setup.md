# Agent 接入与 M1 验收

日期：2026-10-28（Asia/Shanghai）。当前交付项目平台、36 个 MCP 工具和操作 Skill，包含资源快照、标准预检、[素材导入与显式片段](media-assets.md)及[目标副本查询](asset-adapters.md)。本机目录及[远程素材桥接](remote-assets.md)适配器、[生成提交和对账](generation.md)、[自动收片与基础预览](collection.md)已接入；两种部署的[真实 H3 链路](h3-validation.md)已验证，完整审片仍在开发。

## 1. 安装与 Skill 定位

先构建或取得 wheel，再由用户选择的 Python 工具环境安装。例如在独立工具环境安装本地 wheel：

```sh
uv tool install --python 3.12 /absolute/path/to/comfypilot-0.1.0.dev0-py3-none-any.whl
comfypilot skill-path
```

`skill-path` 返回包含 name/path 的 JSON，path 指向随包交付的完整 Skill 目录。这个命令仅读取安装资源，不初始化项目、不联网、不启动平台。源码开发可用 `uv run --locked comfypilot skill-path`。安装后的 CLI、MCP 和 Skill 均不需要源码目录；运行平台无需 Node.js。

将返回目录中的全部内容复制到所选宿主的 Skill 目录，保持 SKILL.md 与 references 的相对关系。源文件在仓库的 `packages/skills/comfypilot/`。已有同名 Skill 时先比较内容并保留用户修改，不直接覆盖。

| 宿主 | 当前创作项目内的 Skill 路径 | 调用方式 |
| --- | --- | --- |
| Codex | `.agents/skills/comfypilot/SKILL.md` | `$comfypilot` 或要求使用 ComfyPilot |
| Claude Code | `.claude/skills/comfypilot/SKILL.md` | `/comfypilot` 或要求使用 ComfyPilot |

这些是宿主自己的发现目录。平台配置与数据库仍只在创作项目的 comfypilot.yaml 和 .comfypilot/ 中，安装程序不会创建用户级平台服务。目录规则见 [Codex Skills 官方文档](https://learn.chatgpt.com/docs/build-skills)和 [Claude Code Skills 官方文档](https://code.claude.com/docs/en/skills)。

## 2. 注册待机 MCP

先用 `command -v comfypilot-mcp` 确认安装后的绝对路径。下面的路径均须替换为本机实际值。MCP 启动命令不能带平台 start/serve，不能写死创作根目录；Agent 每次调用工具显式传根目录。

Codex 可在受信任创作项目的 `.codex/config.toml` 中合并以下配置，不覆盖文件里的其他设置：

```toml
[mcp_servers.comfypilot]
command = "/absolute/path/to/comfypilot-mcp"
args = []
```

若用户选择在 Codex 多项目间共用注册，可改用 `codex mcp add comfypilot -- /absolute/path/to/comfypilot-mcp`。这写入宿主配置，不创建全局 ComfyPilot 服务。项目配置需要宿主信任，详见 [Codex MCP 官方文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

Claude Code 在创作目录使用项目级注册：

```sh
claude mcp add --transport stdio --scope project comfypilot -- /absolute/path/to/comfypilot-mcp
```

该命令创建或更新项目 `.mcp.json`；按宿主提示完成项目信任。已有同名注册时先检查并合并，避免重复添加。详见 [Claude Code MCP 官方文档](https://code.claude.com/docs/en/mcp)。

让宿主重新加载配置并查看工具。此时桥接器可以运行并枚举工具，但平台仍不启动，也没有任何项目连接。不要把“宿主已连接 MCP”误认为已连接项目平台。本次只核对了 Codex 0.164.0 与 Claude Code 2.1.302 的命令帮助，没有修改本机已有宿主配置。

## 3. 让 Agent 使用平台

可以向宿主说明：

> 使用 ComfyPilot，在我当前的创作目录初始化并打开工作台。先询问我本机/远程 ComfyUI 的地址和默认执行端，再探测可用能力。

Agent 按 Skill 取得规范绝对根目录，依次 inspect、必要时 init、start、connect，再用返回的 connection_ref 查询配置和状态。start 返回后台运行的 API/Worker，Agent 给出面板实际 URL，无需用户手工拉服务。安装/枚举 MCP、打开宿主或发现 Skill 本身都不触发上述过程。

业务工具均要求显式根目录和连接引用。重启失效后重新 connect；多个项目/桥接器分别保存引用。用户只断开时 disconnect，明确停止平台时 stop；退出 MCP 不停止 API/Worker，也不控制 ComfyUI。

执行端地址和默认值可以随时修改。文件修改必须显式 refresh 才成为有效修订；写入使用 get_project 的配置 revision。任务/方案命令使用 get_execution 的业务 revision 与 UUID command_id，不能混用。详见安装包 Skill 的 references/operations.md 和 [README](../README.md#方案与执行范围)。

连接 ComfyUI 不表示能安装节点/模型、传输任意视频或清理磁盘；没有对应通道时返回明确缺项。Worker 已消费获授权且通过门禁的生成队列，queued 仅代表等待；原目标明确成功后自动收片，ready 且检查通过才提供基础预览。局域网、完整审片与清理仍按后续里程碑交付。

## 4. 实测矩阵

| 环境/范围 | 现有证据 | 状态 |
| --- | --- | --- |
| macOS，Python 3.12，Node.js 24 开发环境 | 全套检查、真实独立 API/Worker、源码外 wheel 安装与页面资源 | 已验证当前实现 |
| 官方 SDK stdio 客户端 auto/legacy | 默认待机审计、显式连接、双项目/双桥接器、失效引用和退出后平台存活 | 已验证协议/进程层 |
| 标准 ComfyUI HTTP/WebSocket 替身 | 指标/缺项、断连、2/8 步事件、超时退避、队列与生成门禁、零生成提交 | 已验证替身行为 |
| Codex 0.164.0 / Claude Code 2.1.302 | CLI 帮助与官方接入格式核对 | 待实际宿主按 Skill 验收 |
| Claude Code 2.1.302 / GLM-5.3，macOS | 使用独立 HOME、源码外 wheel MCP，将已安装 Skill/参考作为上下文，12 轮实际完成初始化、启服、显式连接、视频/参考图导入、1–3秒片段和48帧/映射/输入顺序核对；主进程复核数据库、内容哈希与退出后平台存活 | 本地素材子场景已验证；自动 Skill 发现、权限交互、双宿主/双项目完整矩阵仍待验 |
| Linux 项目平台，Python 3.12.3 | 独立 wheel/venv、真实 stdio MCP 初始化/启停/连接、退出后 Worker 存活和同机生成收片 | 该部署已实测；宿主完整矩阵仍保留 |
| 真实远程 ComfyUI，Linux / 0.38.0 / RTX 3090 | macOS 平台通过桥接上传、真实 H3 生成、原 client_id 节点事件、平台重启后原回执对账及自动收片 | 技术检查、Range 与浏览器完整播放通过 |
| 真实同机 ComfyUI | Linux 平台通过实际目录上传/收片，工具缺失阻塞、恢复命令重放和唯一版本/额度核对 | 该部署已实测，与远程部署使用同一 GPU 实例 |

当前测试证据不替代实际宿主的 Skill 选择、权限提示、目录获取和退出行为。M1/1.7 保留复核状态，不能因为交付了 Skill 就把整个 M1 标为完成。

2026-10-28 远程验证使用独立临时创作目录和官方 SDK stdio 客户端：枚举工具没有创建项目；随后经 MCP 初始化、启动平台、显式连接、配置目标、探测并读取统一监测。HTTP 与 WebSocket 均为 fresh，目标声明 ComfyUI 0.38.0，节点目录 971 项，采样时运行/等待队列均为 0；统一监测经 MCP 返回约 415 KiB。断开并退出 MCP 后 API/Worker 继续存活，最后只停止测试平台并清理临时目录。

上述 M1 连接验证没有提交生成。随后 M2 在用户提供 SSH 通道后安装独立素材桥接，空队列时按原方式重启；再分别完成远程桥接和 Linux 同机目录模式的真实生成。GPU 温度、利用率、磁盘、完整日志及环境管理/清理仍按实际能力显示 unsupported，素材传输单独探测；无渲染事件时进度显示 unknown。部署地址、进程标识、用户媒体和原始环境记录留在本机过程证据，不写入公开文档。

## 5. M1 剩余环境验收

在可用环境中记录操作系统、Python、安装包与宿主版本、执行端部署/节点版本、两个创作目录、API/Worker PID 与实例、命令回执和检查结果；不记录秘密值。

1. 在 Claude Code 与 Codex 各加载 Skill 和 MCP；分别在平台未运行/已运行时枚举，确认没有项目写入或业务连接（AC-01、AC-02）。
2. 让各宿主在两个临时创作目录按 Skill 初始化、启停、显式连接和选择目标；同项目复用，不同项目隔离。断开/退出宿主后 API/Worker 继续，明确 stop 才停止（AC-01、AC-03）。
3. macOS/Linux 安装包及两种素材通道已实测；其他系统、目标版本与宿主仍需按实际环境核对指标、节点事件、地址变更和门禁（AC-05、AC-06）。
4. AC-04 的真实回执对账与重启收片已由 M2/2.9 补验；这不替代第1、2项的双宿主自动 Skill 发现、权限与目录选择流程。

有不满足项保留记录与状态，按实际证据逐项勾选看板，不用协议替身冒充双宿主/Linux/真实生成验收。
