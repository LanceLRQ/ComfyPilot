# ComfyPilot

与本地 AI Agent 一起操作 ComfyUI 的创作与审片工作台。

产品目标：用户在 Claude Code、Codex 等工具里提需求，Agent 按 Skill 在当前项目目录启动 ComfyPilot，通过 MCP 管理任务与执行。用户打开浏览器看进度、审片和提出修改意见。生成可以交给本机或远程 ComfyUI。

当前已实现 M1/1.1–1.6 和 M2 第一条视频链路：项目服务、默认待机的 stdio MCP、显式连接、多目标配置、资源快照、素材导入与显式片段、H3 配方、持久提交/对账、[自动收片和基础预览](docs/collection.md)、幂等恢复及远程素材桥接。[真实链路验收](docs/h3-validation.md)已覆盖 macOS 远程桥接与 Linux 同机目录模式，包含重启接续和工具缺失后的恢复。M3 已接入逐段审片、截图圈选、返工与明确采用、共享监控、MCP 控制和 Agent 新会话接续；[M3 联合验收](docs/m3-validation.md)区分已验证场景与保留项。M1/1.7 双宿主完整矩阵、M2/2.2 安装回退等实测项仍保留；M4 清理交付继续开发。文档更新于 2026-10-28（Asia/Shanghai）。

## 本地开发与运行

需要 Python 3.12+、uv、Node.js 22.12+、npm 和 Make；素材处理另需 FFmpeg/ffprobe（含 libx264）。开发环境已在 macOS/Python 3.12/Node.js 24 验证；Linux/Python 3.12 的 wheel 安装、独立平台进程和同机生成收片也已实测，安装包运行无需 Node.js。

在源码根目录安装锁定依赖并构建页面：

```sh
make setup
make web
```

选择自己的创作目录，将以下 `/absolute/path/to/project` 替换为其绝对路径。在源码根目录执行：

```sh
uv run --locked comfypilot init --project-root /absolute/path/to/project
uv run --locked comfypilot start --project-root /absolute/path/to/project
```

打开 <http://127.0.0.1:8765> 查看当前项目的名称、目录、UUID、数据格式和 Worker 心跳。项目与监测每 5 秒刷新，收片预览通过[业务事件与 SSE](docs/review-workbench.md)实时更新。`start` 返回 JSON 状态后退出，API 和 Worker 在各自独立的后台会话中继续运行；同项目重复或并发启动复用一个实例。

```sh
uv run --locked comfypilot status --project-root /absolute/path/to/project
uv run --locked comfypilot stop --project-root /absolute/path/to/project
```

`status` 和 `stop` 不会隐式启动服务。复用和停止先核对项目、实例、进程启动信息和健康回执；端口或身份冲突时拒绝接管。后台日志位于项目的 `.comfypilot/logs/service.log`。开发调试仍可使用 `serve` 前台运行，按 Ctrl+C 退出；它与后台实例共用项目实例锁。

Worker 单独退出后，仍运行的 API 会以 1 秒检查间隔恢复它；连续失败最多重试 3 次，稳定运行 5 秒后重置计数。显式停止时取消监督并收尾自己的 Worker，不启动或停止 ComfyUI。API 意外消失但 Worker 仍存活时，命令会保留现场并报告诊断，当前不提供无健康回执的残留进程接管。

初始化、启动及停止默认保存手动派发暂停。下面的命令只解除这个手动开关：

```sh
uv run --locked comfypilot resume-dispatch --project-root /absolute/path/to/project
```

重启后仍须显式恢复派发；Worker 单独重启保留开关状态。指示范围、次数限制与串行门禁已实现；解除开关后 Worker 可消费已生效方案的入队运行，生成前仍须通过全部门禁；自动收片、逐段审片和监控协作已接入。详见[生成提交与对账](docs/generation.md)。

CLI 必须显式传入绝对项目目录，不会猜测当前项目。`init` 可重复执行，保留已有项目标识、配置和数据；其他命令只加载已经初始化的项目。默认仅监听 `127.0.0.1`，无需登录；可显式开启[局域网审片](docs/lan-review.md)，管理操作仍限本机。进程管理使用 POSIX 文件锁和会话机制，macOS/Linux 的安装包平台启停已实测，双宿主完整矩阵仍在 M1/1.7，暂不支持 Windows。

初始化产生的文件全部位于指定项目内：

| 路径 | 用途 |
| --- | --- |
| `comfypilot.yaml` | 项目名称、回环地址和端口配置 |
| `.comfypilot/project.json` | 稳定 UUID、创建时间和数据格式 |
| `.comfypilot/state.sqlite` | SQLite/WAL 元数据与 Worker 心跳 |
| `.comfypilot/runtime.json` | 本次实例标识、端口、API/Worker PID 与创建时间；不作为可移植备份 |
| `.comfypilot/artifacts/`、`runs/`、`staging/`、`logs/`、`locks/` | 为后续产物、运行记录及进程管理准备的目录，均位于 `.comfypilot/` 内 |
| `workflows/`、`custom_nodes/`、`modules/`、`skills/` | 为项目资源预留的目录，当前不附带工作流、节点或 Skill |

项目设置页与 MCP 显式更新配置后产生新修订。直接修改 `comfypilot.yaml` 属于磁盘草稿，须使用设置页“接纳文件修改”或 MCP `refresh_project` 后生效；非法草稿保留原有效配置。服务端口的修改须停止再启动，`start --port 8766` 或 `serve --port 8766` 也可临时覆盖端口。多个项目使用不同端口。数据库仅接受已识别的本机文件系统；初始化和启动要求至少 16 MiB 可用空间。

已有数据库增量升级至格式 3，保留项目 UUID、Worker 记录与用户表，增加实例归属、手动派发开关和不可变配置修订。旧项目首次升级时将原 YAML 接纳为修订 1，之后不自动读取文件修改。项目配置与身份 JSON 的 `schema_version` 仍为 1，和 SQLite 的 `user_version` 分别管理。

这些 CLI 命令也用于开发验证。最终用户由 Agent 依据 Skill 管理项目服务；完整 Skill 与 Claude Code/Codex 双宿主实测在 M1/1.7 接入。

## stdio MCP

安装 wheel 后，宿主通过 `comfypilot-mcp` 启动桥接器。下列是通用配置片段，按宿主配置格式使用，并替换为实际安装路径：

```json
{
  "command": "/absolute/path/to/venv/bin/comfypilot-mcp",
  "args": []
}
```

源码开发也可在仓库根目录运行 `uv run --locked comfypilot-mcp`。入口不接受项目参数；启动、初始化握手和工具枚举均保持待机，不读取项目、不探测平台或 ComfyUI、不创建项目文件。stdout 仅用于 MCP 协议。

每次工具调用都显式传入绝对 `project_root`，和宿主启动 MCP 时的工作目录无关：

| 工具 | 当前行为 |
| --- | --- |
| `inspect_project` | 只读检查是否初始化及项目身份，不创建目录或迁移数据库 |
| `init_project` | 初始化目录、配置、身份和数据库；已有数据保留 |
| `start_service` | 后台启动或复用 API/Worker；可传 `port`，默认读项目配置；不建立业务连接 |
| `connect_service` | 校验已运行实例，返回 `connection_ref`、项目 UUID、实例 ID 和页面 URL；不启动服务 |
| `get_project_status` | 传入 `connection_ref`，读取 API/Worker 与网页共用的项目概览及派发开关 |
| `disconnect_service` | 传入 `connection_ref`，仅丢弃该引用，不停止服务或影响其他连接 |
| `stop_service` | 暂停派发并停止给定项目；传引用时只允许停止绑定实例，省略引用时明确停止该目录的当前实例 |

典型顺序为 `inspect_project → init_project（需要时）→ start_service → connect_service → get_project_status`。例如连接的参数是 `{"project_root":"/absolute/path/to/project"}`；收到引用后，查询传 `{"project_root":"/absolute/path/to/project","connection_ref":"返回的引用"}`。

工具返回结构化 `{ "ok": true, "data": { ... } }`；失败返回 `{ "ok": false, "error": { "code": "...", "message": "下一步提示" } }`，并设置 MCP `isError`。未连接返回 `service_not_connected`；引用属于其他目录或 UUID 不符返回 `project_mismatch`；重启或进程身份变化后返回 `connection_invalidated`，需要重新显式连接。旧引用不会自动切换到新实例。

连接引用只存在于当前 MCP 进程中，允许同时连接多个项目。同项目的多个 MCP 进程复用平台实例，各自管理引用；退出宿主、终止 MCP 或断开连接后，后台 API/Worker 继续运行。

配置工具还包括 `get_project`、`update_project`、`refresh_project`、`list_targets`、`update_target` 和 `resolve_target`，均要求显式项目与连接引用。写入传 `expected_revision`，配置冲突返回 `conflict`；网页保留当前草稿，可自行合并或明确重新读取。监测工具包括 `probe_target`、`get_target_status` 和 `get_monitor_status`。执行工具包括 `get_execution`、`create_task`、`create_plan`、`activate_plan`、`execute_plan`、`stop_plan`、`pause_dispatch` 和 `resume_dispatch`，资源工具包括 `list_resources`、`refresh_resources`、`get_resource_file`、`preflight_recipe`，另提供逐段审片、反馈/采用、运行/收片查询、方案暂停/恢复、指定未提交步骤取消、`get_context`、`read_events` 和 `wait_for_events`，当前共 53 个工具。生成、对账、收片与事件流已接入，完整操作见随包 Skill 和[审片工作台](docs/review-workbench.md)。

## Agent Skill

安装包包含 `comfypilot` Skill，运行 `comfypilot skill-path` 只读取得目录；将完整目录复制到所选宿主的项目 Skill 位置，再注册 stdio MCP。详细步骤、当前能力与待实测矩阵见 [Agent 接入与 M1 验收](docs/agent-setup.md)。安装/枚举后保持待机，用户要求使用平台时才由 Agent 初始化、显式启动和连接。

## 执行端与秘密文件

设置页可以添加/停用本机或远程执行端，指定名称、HTTP(S) 地址和默认值。每个执行端有稳定 ID；本机/远程标签不代表已经探测能力。`resolve_target` 优先使用显式 `target_id`，否则使用已配置默认值；没有明确目标时返回 `needs_input`，不猜测或自动切换。结果携带配置修订与完整目标快照；修改地址不会重写历史配置，不能用新地址上的同名外部任务替代原目标。

URL 不允许内嵌用户名、密码、查询参数或片段。可选认证仅填 `credential_ref`，让 Agent 在项目 `.comfypilot/secrets.local.yaml` 中管理对应请求头：

```yaml
render-auth:
  Authorization: Bearer <由用户在本地填写>
```

支持 `Authorization` 或 `X-API-Key`；公开配置和响应只显示引用及是否已配置。秘密文件应限制为本人可读并加入项目忽略规则；不要把凭据粘贴到普通配置或工作流。重复地址会给出合并提示，平台不会自动合并目标身份。项目服务运行且执行端启用时，由 Worker 开始采集；MCP 待机不采集，也不启动平台。

## 执行端监测

Worker 默认每 5 秒读取标准 `/system_stats`、`/queue`，节点清单只在建立监测或显式 `probe_target` 时获取；网络请求有 2 秒时限、4 MiB 响应上限，失败逐步退避至 60 秒。HTTP 与 WebSocket 均不跟随重定向或使用系统代理。

网页和 MCP 共享 `/api/status` 快照。每个指标包含来源、采集时间、15 秒过期时间、单位与可用性；缺值是 unknown，标准接口未提供的 GPU 利用率、温度、CPU、磁盘和完整日志是 unsupported，实际零值保留。WebSocket 只展示收到的真实节点事件；外部队列不冒充本项目任务，不计算整体百分比或 ETA，不因无事件判失败或补发生成。

格式 4 引入的监测表按配置修订、服务实例和 Worker PID 隔离最新样本，重启未采集时为 unknown；样本覆盖不创建配置修订或业务事件。能力预检 available 仅表明标准接口和节点清单可用。节点事件订阅跟随原提交的持久 client_id，重启后可恢复，已验证真实采样进度；其他客户端的全部进度未必可见。

## 方案与执行范围

`create_task` 记录用户目标和验收要求，`create_plan` 保存不可变草稿及执行指示：来源、允许的配方/输入内容哈希、目标、参数候选值、最多提交次数与停止条件。当前参数范围使用明确候选值枚举，尚不支持范围表达式；`on_failure` 表示失败后停止，额度上限始终生效。缺少指示或步骤越界会被拒绝。

`activate_plan` 显式生效；它与旧方案未提交运行的取消处在同一事务，草稿本身不替换当前方案。`execute_plan` 按 action_index 幂等入队，同方案/动作仅一个 Run。停止不修改已有提交意图、不调用外部中断。所有业务命令传 UUID `command_id` 和 `get_execution` 的业务 `expected_revision`；这与配置修订是两个独立计数。重复命令返回原回执，新命令遇到旧修订返回 conflict；同 ID 改请求返回 command_mismatch。回执含命令 ID、结果、业务修订和持久事件 ID。

SQLite 格式 5 保存任务、方案、Run、回执、事件与独立挂起原因。内部领取边界重新检查方案/指示、目标快照、最新能力与所需节点、外部空队列、所有暂停原因和项目总并发 1；额度预留、提交意图与事件同事务。旧意图仅返回 reused，不成为再次发送的许可。普通恢复只解除 manual_pause，保留 review_hold 与 reconciliation_required。CLI 恢复派发也记录命令回执。

Worker 已接通监测、媒体准备、生成提交、回执对账与自动收片。新方案必须引用已刷新且声明完整的配方哈希，冻结参数化图模板、输入与关联资源；刷新不改旧 Run。派发允许且门禁通过后，Worker 才向 ComfyUI 提交；queued 仅代表等待，Agent 不直接 POST /prompt 绕过平台。详见[模块与资源](docs/resources.md)、[提交与对账](docs/generation.md)和[收片](docs/collection.md)。

## 素材准备

视频与参考图支持受管导入、真实解码、显式片段提取和持久媒体任务。网页可流式上传并看状态，MCP 提供 import_asset、extract_clip、get_media_job、get_asset、list_assets。原片改动不影响已导入副本；同内容共享存储，每次导入身份独立。时间范围、输入顺序和哈希可回读，详见[素材操作说明](docs/media-assets.md)。

本机目录和远程桥接适配器支持独立素材 ID 绑定、最终图/受管输出前缀与副本查询；当前 SQLite 格式12、53个MCP工具。配置和边界见[素材适配器](docs/asset-adapters.md)及[远程桥接](docs/remote-assets.md)。收片仅在原目标明确成功后执行，技术检查通过后提供基础预览；失败恢复沿用原 Run，不新增生成。

## 检查与打包

```sh
make check
make package
```

`make check` 依次执行 Ruff 诊断与格式检查、Biome、TypeScript 类型检查、Web 构建和服务端测试。测试覆盖项目与进程隔离、迁移、恢复、持久暂停、静态资源边界，以及真实 stdio 客户端的待机零副作用、显式连接、失效引用、多个桥接器和宿主退出。

`make package` 先构建页面，再输出 `dist/comfypilot-0.1.0.dev0-py3-none-any.whl`。wheel 包含页面静态资源，安装运行时无需 Node.js；仍需 Python 及包声明的运行依赖。安装包已在脱离源码的独立虚拟环境中完成初始化、服务和页面冒烟验证。

## 文档入口

| 文档 | 内容 |
| --- | --- |
| [模块与资源](docs/resources.md) | 声明格式、显式刷新、覆盖映射、目标预检与冻结快照 |
| [H3 首发配方与环境验收](docs/h3-recipe.md) | 输入输出、固定版本/模型、安装验证/回退、真实环境保留项 |
| [H3 真实链路验收](docs/h3-validation.md) | 两种部署、重启接续、收片恢复、技术检查及证据复核 |
| [M3 审片协作联合验收](docs/m3-validation.md) | 真实 GPU 并行审阅、离线反馈、Agent 返工、用户采用和监控恢复 |
| [素材导入与显式片段](docs/media-assets.md) | 网页/MCP 操作、任务状态、限额、时间映射和失败处理 |
| [Agent 接入与 M1 验收](docs/agent-setup.md) | Skill 安装、双宿主注册、实际操作与待实测矩阵 |
| [产品需求](docs/product-requirements.md) | 已确认约束、协作流程、模块划分与产品规则 |
| [首版范围与验收](docs/mvp-scope.md) | 第一版功能边界、40 条验收要求与开发顺序 |
| [架构设计](docs/architecture.md) | 项目目录、进程生命周期、模块契约、执行恢复与素材清理 |
| [技术调研与分期](docs/research-and-roadmap.md) | 官方方案参考、简化画布设计、分期与仍需实测的问题 |

范围以首版范围与验收为准。实现状态和实测范围以上方说明及验收记录为准；M3 联合验收的独立执行端实测保留，M4 清理交付仍按里程碑推进。

## 第一版

- 一个项目目录对应一个平台实例；配置、元数据、工作流、自定义节点资源与产物都保存在该目录。
- 宿主管理 MCP 进程。MCP 默认待机，不主动连接或启动平台；Agent 按 Skill 显式管理启停和连接。
- 默认监听本机，无账号、无需平台访问密钥；用户要求时可开放局域网审片。
- 保存多个 ComfyUI 执行端，按用户指定的目标与规则调度；首版项目生成串行执行。
- 优先完成视频人物替换：片段准备、参考素材、生成、技术检查、实时审片、局部返工、采用与交付。
- 每段合格结果自动收集后立即可审；反馈绑定具体版本、时间范围和截图圈选。
- 审片同时查看平台、执行端、生成与收片状态；Agent 可通过 MCP 查询、暂停/恢复和调整后续方案。
- 模块携带工作流、依赖清单与 Agent 操作说明；允许项目自定义资源，节点实际安装由 Agent 操作目标环境。
- 支持本机目录与远端素材桥接；用户要求清理时，先生成清单并复核引用，再物理删除符合条件的 ComfyUI 素材副本。
- 保留提交意图、回执与运行证据；服务重启接续对账和收片，不盲目重投。

第二期增加 Agent 编排、用户拖拽微调的业务流程图；后续分批接入图片生成与编辑、视频生成及剧本分镜。首版不建设内置聊天、多 Agent 编排或自动 GPU 集群调度。

## 设计来源

继承新宝岛视频项目在审片、版本管理和执行恢复方面的经验，重新建立独立的项目模型。旧项目路径、历史状态和凭据不作为新项目的运行依赖。

## 许可证

[MIT License](LICENSE)

M2/2.7–2.8 已提供有限重试、原Run幂等恢复及[远程素材桥接](docs/remote-assets.md)。`comfypilot bridge-path`定位随包扩展，需经用户指定管理通道安装；真实环境生成验收以看板记录为准。
