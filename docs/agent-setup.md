# Agent 接入与 M1 验收

日期：2026-10-29（Asia/Shanghai）。当前交付项目平台、59 个 MCP 工具和操作 Skill，包含资源快照、标准预检、[素材导入与显式片段](media-assets.md)及[目标副本查询](asset-adapters.md)。本机目录及[远程素材桥接](remote-assets.md)适配器、[生成提交和对账](generation.md)、[自动收片与基础预览](collection.md)已接入；两种部署的[真实 H3 链路](h3-validation.md)已验证，[逐段审片与版本反馈](review-workbench.md)已接入，返工、明确采用和来源清单已接入，共享生产监控已接入，MCP状态与工作控制已接入，新会话上下文和有界事件等待已接入。[素材清理](asset-cleanup.md)、[LAN 审片](lan-review.md)和[备份恢复](project-backup.md)已交付；锁定安装与回退见[安装说明](installation.md)。

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

让宿主重新加载配置并查看工具。此时桥接器可以运行并枚举工具，但平台仍不启动，也没有任何项目连接。不要把“宿主已连接 MCP”误认为已连接项目平台。接入初验时核对了 Codex 0.164.0 与 Claude Code 2.1.302 的命令帮助，没有修改本机已有宿主配置；2026-10-29 已在隔离配置中完成两宿主自动发现、默认待机和双项目生命周期补验，见[双宿主记录](host-validation.md)。

## 3. 让 Agent 使用平台

可以向宿主说明：

> 使用 ComfyPilot，在我当前的创作目录初始化并打开工作台。先询问我本机/远程 ComfyUI 的地址和默认执行端，再探测可用能力。

Agent 按 Skill 取得规范绝对根目录，依次 inspect、必要时 init、start、connect，再用返回的 connection_ref 查询配置和状态。start 返回后台运行的 API/Worker，Agent 给出面板实际 URL，无需用户手工拉服务。安装/枚举 MCP、打开宿主或发现 Skill 本身都不触发上述过程。

业务工具均要求显式根目录和连接引用。重启失效后重新 connect；多个项目/桥接器分别保存引用。用户只断开时 disconnect，明确停止平台时 stop；退出 MCP 不停止 API/Worker，也不控制 ComfyUI。

执行端地址和默认值可以随时修改。文件修改必须显式 refresh 才成为有效修订；写入使用 get_project 的配置 revision。任务/方案命令使用 get_execution 的业务 revision 与 UUID command_id，不能混用。详见安装包 Skill 的 references/operations.md 和 [README](../README.md#方案与执行范围)。

连接 ComfyUI 不表示能安装节点/模型、传输任意视频或清理磁盘；没有对应通道时返回明确缺项。Worker 已消费获授权且通过门禁的生成队列，queued 仅代表等待；原目标明确成功后自动收片，ready 且检查通过才提供基础预览。逐段审片、共享监控、MCP 控制、局域网审片、清理和备份已交付；当前发布保留项见 [M4 验收](m4-validation.md)。

## 4. 实测矩阵

| 环境/范围 | 现有证据 | 状态 |
| --- | --- | --- |
| macOS，Python 3.12，Node.js 24 开发环境 | 全套检查、真实独立 API/Worker、源码外 wheel 安装与页面资源 | 已验证当前实现 |
| 官方 SDK stdio 客户端 auto/legacy | 默认待机审计、显式连接、双项目/双桥接器、失效引用和退出后平台存活 | 已验证协议/进程层 |
| 标准 ComfyUI HTTP/WebSocket 替身 | 指标/缺项、断连、2/8 步事件、超时退避、队列与生成门禁、零生成提交 | 已验证替身行为 |
| Codex 0.164.0 / Claude Code 2.1.302，macOS | [双宿主补验](host-validation.md)：项目 Skill/MCP 自动发现，未运行/已运行四待机场景、当前目录初始化、显式连接、目标选择、跨宿主复用、引用隔离和宿主退出 | 非交互场景已验证；首次信任/人工批准与交互式退出仍保留 |
| Claude Code 2.1.302 / GLM-5.3，macOS | 使用独立 HOME、源码外 wheel MCP，将已安装 Skill/参考作为上下文，12 轮实际完成初始化、启服、显式连接、视频/参考图导入、1–3秒片段和48帧/映射/输入顺序核对；主进程复核数据库、内容哈希与退出后平台存活 | 本地素材历史子场景已验证；后续发现/双项目补验见上行，交互项仍保留 |
| Claude Code 2.1.302 / GLM-5.3，M3 新会话 | 在独立项目读取已安装的项目 Skill，显式连接并 get_context，根据离线反馈 propose_plan；逐条工具记录与实际草稿经主进程复核，执行与采用由已有明确指示另行完成 | [该接续场景已验证](m3-validation.md)；不替代双宿主完整矩阵 |
| Linux 项目平台，Python 3.12.3 | 独立 wheel/venv、真实 stdio MCP 初始化/启停/连接、退出后 Worker 存活和同机生成收片 | 该部署已实测；宿主完整矩阵仍保留 |
| 真实远程 ComfyUI，Linux / 0.38.0 / RTX 3090 | macOS 平台通过桥接上传、真实 H3 生成、原 client_id 节点事件、平台重启后原回执对账及自动收片 | 技术检查、Range 与浏览器完整播放通过 |
| 真实同机 ComfyUI | Linux 平台通过实际目录上传/收片，工具缺失阻塞、恢复命令重放和唯一版本/额度核对 | 该部署已实测，与远程部署使用同一 GPU 实例 |

自动发现、当前目录选择和非交互宿主退出已有真实双宿主证据；不能替代首次信任、人工批准与交互式退出。M1/1.7 保留复核状态，不把非交互成功等同整个 M1 完成。

2026-10-28 远程验证使用独立临时创作目录和官方 SDK stdio 客户端：枚举工具没有创建项目；随后经 MCP 初始化、启动平台、显式连接、配置目标、探测并读取统一监测。HTTP 与 WebSocket 均为 fresh，目标声明 ComfyUI 0.38.0，节点目录 971 项，采样时运行/等待队列均为 0；当时统一监测经 MCP 返回约 415 KiB；M3 已将节点目录/schema 从公开监测投影移除，完整资源按引用获取。断开并退出 MCP 后 API/Worker 继续存活，最后只停止测试平台并清理临时目录。

上述 M1 连接验证没有提交生成。随后 M2 在用户提供 SSH 通道后安装独立素材桥接，空队列时按原方式重启；再分别完成远程桥接和 Linux 同机目录模式的真实生成。当时 GPU 温度、利用率、磁盘、完整日志及环境管理/清理按缺失能力显示 unsupported；后续素材桥接传输与受限删除已单独补验，当前以实际能力快照为准；无渲染事件时进度显示 unknown。部署地址、进程标识、用户媒体和原始环境记录留在本机过程证据，不写入公开文档。

## 5. 非交互审批与 M1 剩余环境验收

Codex 的 approval_policy=never 表示不弹出批准请求，不表示自动允许全部 MCP 操作。本轮实际观察到 stop_service 因 destructive_hint 被宿主拒绝：MCP tool call requires approval, but approval policy is never。平台仍运行，Agent 准确报告未停止。

只有用户已明确授权启停、且使用隔离测试注册时，本轮才针对 stop_service 配置工具级预批准，其他工具及沙箱策略保持原值：

```toml
[mcp_servers.comfypilot.tools.stop_service]
approval_mode = "approve"
```

配置格式依据 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)的工具级审批设置。本设置作用于该 MCP 注册的整个工具，并非 project_root 访问控制；本轮仅写独立测试项目的注册，不修改日常宿主设置，不作为默认安装配置。普通交互使用应按宿主提示批准；Agent 不应删除工具风险标注、绕过拒绝或声称已停止。完整实测与边界见[双宿主补验](host-validation.md)。

剩余需在真实交互环境中记录操作系统、宿主/安装包版本、两个创作目录、命令回执与实际界面步骤，不记录秘密值：

1. 两个宿主首次进入未经预置信任的新项目，验证项目/MCP 信任提示、拒绝后仍待机、用户允许后加载及执行。已预配 allow/deny 或 enabled_tools 的非交互测试不替代人工操作。
2. 交互式退出、Ctrl+C、终端关闭后的平台/Worker 存活与明确停止；本轮仅覆盖非交互宿主自然退出。
3. Linux 的真实双宿主交互路径。现有 Linux 平台、SDK、同机生成/收片及 macOS 双宿主非交互证据各按自身范围有效。
4. AC-04 的真实回执对账与重启收片沿用 M2/2.9；第二独立 ComfyUI 切换仍在 M3/3.8，不以同实例两个名称替代。

M1/1.7 保持 review；AC-03 的完整宿主场景继续保留，按实际证据勾选，不扩大环境兼容声明。
