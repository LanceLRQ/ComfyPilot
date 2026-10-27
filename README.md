# ComfyPilot

与本地 AI Agent 一起操作 ComfyUI 的创作与审片工作台。

产品目标：用户在 Claude Code、Codex 等工具里提需求，Agent 按 Skill 在当前项目目录启动 ComfyPilot，通过 MCP 管理任务与执行。用户打开浏览器看进度、审片和提出修改意见。生成可以交给本机或远程 ComfyUI。

当前已实现 M1/1.1：项目初始化、回环 API、独立 Worker 和项目概览页。MCP、Agent Skill、ComfyUI 连接及视频审片尚未接入。文档更新于 2026-10-27（Asia/Shanghai）。

## 本地开发与运行

需要 Python 3.12+、uv、Node.js 22.12+、npm 和 Make。当前已在 macOS 上验证 Python 3.12 与 Node.js 24；其他系统的实际运行验收随后补充。

在源码根目录安装锁定依赖并构建页面：

```sh
make setup
make web
```

选择自己的创作目录，将以下 `/absolute/path/to/project` 替换为其绝对路径。在源码根目录执行：

```sh
uv run --locked comfypilot init --project-root /absolute/path/to/project
uv run --locked comfypilot serve --project-root /absolute/path/to/project
```

打开 <http://127.0.0.1:8765> 查看当前项目的名称、目录、UUID、数据格式和 Worker 心跳。页面每 5 秒读取一次实际状态。服务在前台运行，按 Ctrl+C 停止 API 和 Worker；后台启停、实例复用和异常恢复属于后续 M1/1.2。

CLI 必须显式传入绝对项目目录，不会猜测当前项目。`init` 可重复执行，保留已有项目标识、配置和数据；`serve` 只加载已经初始化的项目，不隐式创建项目。默认仅监听 `127.0.0.1`，无需登录；本阶段尚不支持局域网监听。

初始化产生的文件全部位于指定项目内：

| 路径 | 用途 |
| --- | --- |
| `comfypilot.yaml` | 项目名称、回环地址和端口配置 |
| `.comfypilot/project.json` | 稳定 UUID、创建时间和数据格式 |
| `.comfypilot/state.sqlite` | SQLite/WAL 元数据与 Worker 心跳 |
| `.comfypilot/artifacts/`、`runs/`、`staging/`、`logs/`、`locks/` | 为后续产物、运行记录及进程管理准备的目录，均位于 `.comfypilot/` 内 |
| `workflows/`、`custom_nodes/`、`modules/`、`skills/` | 为项目资源预留的目录，当前不附带工作流、节点或 Skill |

修改 `comfypilot.yaml` 的 `name` 或 `server.port` 后重新启动生效；`serve --port 8766` 可临时覆盖端口，不改配置。多个项目同时运行时使用不同端口。当前数据库仅接受已识别的本机文件系统，拒绝 NFS/SMB 等网络存储及未知类型；初始化和启动要求至少 16 MiB 可用空间，这只是基础启动门槛。

这些 CLI 命令用于当前开发阶段的验证。最终用户由 Agent 依据 Skill 管理项目服务，MCP 默认待机的产品约定保持不变。

## 检查与打包

```sh
make check
make package
```

`make check` 依次执行 Ruff 诊断与格式检查、Biome、TypeScript 类型检查、Web 构建和服务端测试。测试覆盖双项目隔离、重复初始化、存储拒绝、数据库迁移、真实 API/Worker 进程及静态资源访问边界。

`make package` 先构建页面，再输出 `dist/comfypilot-0.1.0.dev0-py3-none-any.whl`。wheel 包含页面静态资源，安装运行时无需 Node.js；仍需 Python 及包声明的运行依赖。安装包已在脱离源码的独立虚拟环境中完成初始化、服务和页面冒烟验证。

## 文档入口

| 文档 | 内容 |
| --- | --- |
| [产品需求](docs/product-requirements.md) | 已确认约束、协作流程、模块划分与产品规则 |
| [首版范围与验收](docs/mvp-scope.md) | 第一版功能边界、40 条验收要求与开发顺序 |
| [架构设计](docs/architecture.md) | 项目目录、进程生命周期、模块契约、执行恢复与素材清理 |
| [技术调研与分期](docs/research-and-roadmap.md) | 官方方案参考、简化画布设计、分期与仍需实测的问题 |

范围以首版范围与验收为准。除上方明确列出的 M1/1.1 能力外，文档中的目录、工具名和接口仍是设计约定，不代表已经实现。

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
