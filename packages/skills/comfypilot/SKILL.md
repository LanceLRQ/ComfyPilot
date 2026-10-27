---
name: comfypilot
description: 在用户指定的本地创作目录，通过 ComfyPilot MCP 管理项目平台的启停、连接、ComfyUI 执行端、监测和受用户指示约束的方案。用户要求使用 ComfyPilot 工作台时使用；开发平台源码或单纯操作其他 ComfyUI 工具时不使用。
---

# ComfyPilot 项目操作

帮助用户在当前创作目录使用本地平台。MCP 是由宿主管理的 stdio 桥接器，平台 API/Worker 是独立后台进程；不要管理 MCP 进程的生命周期。

## 按请求建立项目连接

安装 MCP、启动宿主、发现 Skill 和枚举工具都不构成启动或连接平台的指示。这些情况下保持待机。用户要求使用工作台开展项目时，可以按其已表达的范围完成初始化、平台启动与连接，无需逐步重复批准。

1. 以用户指定目录或 Agent 当前明确的创作工作目录为根，取得规范绝对路径。目录不明确时询问；不要从 MCP 进程 cwd 猜测，不把平台源码目录自动作为创作项目。
2. `inspect_project(project_root)` 检查项目。未初始化且用户要求在这里开展项目时，调用 `init_project`；遇到不完整状态不覆盖用户文件。
3. 调用 `start_service` 启动或复用这个目录的实例，再单独调用 `connect_service`。已有实例也需要显式连接。服务未运行时，`connect_service` 不会启动它。
4. 保存返回的项目 UUID、实例 ID、URL 和本 MCP 进程的 `connection_ref`，业务调用始终带 `project_root` 与此引用。打开返回 URL 供用户查看；不要硬编码端口。
5. 使用 `get_project`、`get_execution`、`get_monitor_status` 了解当前配置、业务与带时间的实际监测。告知用户当前已有工作及阻止派发的原因。

工具在宿主中可能带 MCP 名称前缀，使用实际发现的 schema 和名称。工具缺失时说明安装版本/注册缺项，不编造成功回执。

## 选择目标与处理缺项

- 用 `list_targets` 和 `resolve_target` 选择用户明确指定或配置默认的执行端。缺地址、默认目标或必要凭据时集中询问；不扫描网络、不猜本机 8188、不在不可达时切换目标。
- 目标配置可随时通过 `update_target` 或 `update_project` 更新，传最新配置 `expected_revision`；手改 YAML 后显式 `refresh_project`。非法草稿不替换有效配置。发生 conflict 时保留用户草稿，读取新状态后再合并。
- 用户选择目标后用 `probe_target(target_id)` 验证标准接口与节点证据。`get_target_status` 读取快照；fresh、stale、unknown、unsupported 分别解释，零值和未知值不能混为一谈。
- 可选凭据只放当前项目 `.comfypilot/secrets.local.yaml`，配置仅保存 `credential_ref`。按用户指定的认证方式管理本地文件，限制读取权限，不把秘密写入普通配置、对话摘要或版本库。
- 连接平台、连接 ComfyUI 与安装节点/模型是不同能力。只有 HTTP 时不能声称有环境安装、通用视频传输或清理权限。列明缺少的节点/模型、安装说明或管理通道；未经用户要求不安装、启动或停止 ComfyUI。
- 本机/远程只是位置标签，不能作为能力证据。项目中的 workflows/custom_nodes/modules 可以保留用户资源；当前 M1 尚无模块注册、实际资源校验或自动安装，不宣称“放入即已可执行”。

## 方案与工作状态

准备或调整方案前读 [操作约定](references/operations.md)，按用户已有目标、输入、配方、参数范围、次数和停止条件建立执行指示。缺项明确补齐，不扩大次数或替换输入；范围内连续工作不需要逐步骤确认。

当前实现仅支持方案草稿、生效、幂等入队及持久提交门禁，Worker 尚不派发真实生成。用户要求渲染或审片时说明 M2 尚待接入，可以准备草稿；不要把 queued 说成正在渲染，也不要直接 POST /prompt 补齐未实现链路。

监测由 Worker 程序持续进行，无需另开大模型监控 Agent。节点步数只来自收到的真实 WebSocket 事件，外部队列不代表本项目任务；不推算整体百分比/ETA，不因没有事件判失败或补发生成。

## 断开、停止与恢复

- 用户只要求断开连接时调用 `disconnect_service`；这不会暂停或停止平台。宿主或 MCP 退出后，后台平台继续运行。
- 用户明确要求停止平台时调用 `stop_service`，携带引用则只控制绑定实例；不要顺带停止 ComfyUI 或全局中断外部任务。
- 重启后调用 `connect_service` 取得新引用，再读业务与监测状态；普通重连不等于恢复派发。`connection_invalidated` 不允许把旧引用自动绑定新实例。
- 用户要求恢复派发时调用 `resume_dispatch`；它仅解除 manual_pause，不能清除 review_hold 或 reconciliation_required。保留并解释这些限制，不能编辑数据库绕过。
- 对旧提交意图、未知外部任务或不明回执保留原身份与证据，不自动重发、清空队列或换地址认领。当前收片/对账尚未实现，明确说明缺项。

完成后告诉用户项目目录、面板 URL、连接/目标状态、已做操作及仍需补齐的能力。没有用户停止指示时，让独立平台继续运行。
