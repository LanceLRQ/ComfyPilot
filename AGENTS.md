# ComfyPilot

与本地 AI Agent 一起操作 ComfyUI 的创作与审片工作台。

## 约定

- 功能边界与验收以 `docs/mvp-scope.md` 为准；需求和架构分别见 `docs/product-requirements.md`、`docs/architecture.md`。变更已确认的设计时，同步核对这些文档的一致性。
- 只修改当前任务必需的内容，保留无关文件和已有改动。
- Git 写操作必须取得用户明确指示；暂存和提交分两次执行。
- 项目进度通过 kanban-hub 维护；文档同步范围为根目录 Markdown 和 `docs/` 第一层 Markdown。

## 完成标准

- 首次开发运行 `make setup` 安装锁定依赖。交付前必须运行并通过 `make check`，其中包含服务端 Ruff 诊断和格式检查、前端 Biome、TypeScript 类型检查、Web 构建和 pytest。
- 服务端源码在 `server/comfypilot/`，测试在 `tests/`，前端源码在 `apps/web/src/`。Web 构建输出到 `server/comfypilot/web/`，属于被忽略的生成文件，不手工修改或提交。
- 修改打包、启动入口或静态资源路径后，执行 `make package` 并在脱离源码的干净虚拟环境中安装 wheel，验证 CLI、API、Worker 与页面；不能只验证源码目录可运行。
- 运行服务必须显式指定绝对 `--project-root`；测试使用独立临时项目，不把开发仓库或用户现有项目作为可随意修改的测试数据。
- 临时验收项目、截图、日志和验证脚本统一放在系统 `/tmp` 下，不在用户数据目录或仓库旁堆放；需要无符号链接路径时使用其物理路径。

## 文档

- 公开文档放在 `docs/`。
- 过程文档（research、discussion、specs、plans、audit、reference、ui）放在 `docs/_internal/<category>/`。该目录已被 git 忽略：其中的文件和内容不进入提交、拉取请求和公开文件。
- 索引是 `docs/_internal/README.md`。创建或查找过程文档之前先读它，新文件在其中登记。
- 工作流类 skill 产出的规格和计划放到 `docs/_internal/specs/` 和 `docs/_internal/plans/`，不论该 skill 默认写到哪里。
- 本机备注（当前阶段、硬约束、本机路径）在 `docs/_internal/LOCAL.md`。该文件存在时，开工前先读它。

## 语言

文档使用简体中文，回复始终使用简体中文。代码标识、命令与路径保持原样。
