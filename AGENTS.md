# ComfyPilot

与本地 AI Agent 一起操作 ComfyUI 的创作与审片工作台。

## 约定

- 功能边界与验收以 `docs/mvp-scope.md` 为准；需求和架构分别见 `docs/product-requirements.md`、`docs/architecture.md`。变更已确认的设计时，同步核对这些文档的一致性。
- 只修改当前任务必需的内容，保留无关文件和已有改动。
- Git 写操作必须取得用户明确指示；暂存和提交分两次执行。
- 项目进度通过 kanban-hub 维护；文档同步范围为根目录 Markdown 和 `docs/` 第一层 Markdown。

## 完成标准

尚未定义检查命令。项目有了之后写在这里。

## 文档

- 公开文档放在 `docs/`。
- 过程文档（research、discussion、specs、plans、audit、reference、ui）放在 `docs/_internal/<category>/`。该目录已被 git 忽略：其中的文件和内容不进入提交、拉取请求和公开文件。
- 索引是 `docs/_internal/README.md`。创建或查找过程文档之前先读它，新文件在其中登记。
- 工作流类 skill 产出的规格和计划放到 `docs/_internal/specs/` 和 `docs/_internal/plans/`，不论该 skill 默认写到哪里。
- 本机备注（当前阶段、硬约束、本机路径）在 `docs/_internal/LOCAL.md`。该文件存在时，开工前先读它。

## 语言

文档使用简体中文，回复始终使用简体中文。代码标识、命令与路径保持原样。
