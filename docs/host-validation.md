# Claude Code / Codex 双宿主补验

日期：2026-10-29；平台基线为 7f5a7fb 对应的源码外安装包。该轮补齐 M1/1.7 可自动执行的真实宿主场景；交互式信任和人工批准继续保留，不把非交互成功当作完整宿主兼容结论。

## 环境与方法

- macOS arm64、Python 3.12.13；使用已安装 wheel，SHA-256 为 `452d78626827f989a3787d62724210aef2566513d79049514b73332486469ce3`，不从开发源码启动平台。
- Claude Code 2.1.302，按用户提供的启动器配置使用 GLM-5.3；独立 HOME/配置目录，只加载本次项目设置。Codex 0.164.0，使用当前账户和默认 gpt-6.1-sol，独立 CODEX_HOME，受信任项目、workspace-write、预设审批策略。
- 两个新建创作目录 A/B 各安装完整 `.claude/skills/comfypilot/`、`.agents/skills/comfypilot/`，注册项目级 MCP。没有把 Skill 全文追加到模型提示；Claude 实际调用 Skill，Codex 从发现的路径读取 Skill。
- MCP 的安装包入口外加 Python 审计钩子，只记录项目 I/O 和 socket 事件；不替换协议处理、工具结果或平台行为。宿主自身的设置与会话文件不算平台写入。原始日志和账户配置不进入仓库/看板。
- 创作目录由真实宿主的工作目录提供；另一个项目的跨目录操作才给出明确绝对路径。专用端口经 MCP 保存至项目配置，无用户手工启服。只读探测用户指定的真实 ComfyUI，不提交生成，不安装、清理或重启 ComfyUI。

## 已通过的自动场景

| 场景 | 实际证据与边界 |
| --- | --- |
| 自动发现 | 两宿主分别从项目级发现目录读取完整 Skill，项目 MCP 加载成功；Claude 的启动记录标明 MCP source=project |
| 默认待机，平台未运行 | 两宿主各一新会话，只发现/读取 Skill 和工具说明；MCP 审计除启动标记外，项目 I/O、socket 事件均为 0，目录没有平台 YAML 或 `.comfypilot/` |
| 默认待机，平台已运行 | 两宿主各一新会话，同样零项目 I/O/网络连接；原项目实例、API/Worker 和配置修订保持，后台监测继续 |
| 按当前目录初始化 | 两宿主各实际 inspect → init → start → connect → get_context；外部复核 API 根路径、UUID、端口和数据库完整性，各项目零生成 Run |
| 目标选择与能力边界 | get_project/update_project、list_targets、默认/显式 resolve_target、probe_target/get_target_status 实际完成；传输、受限删除可用，环境管理 unsupported，不把 HTTP 连接解释为安装权限 |
| 同项目复用与重新连接 | 重复 start 复用原实例，重复 init 保持 UUID；显式 connect/disconnect/reconnect 使用本 MCP 会话的真实引用 |
| 宿主退出 | 两个宿主进程都结束后，主进程经独立 CLI/API 确认两个 API/Worker 仍运行，PID 相互独立；不是依据 Agent 的文字报告推断 |
| 跨宿主与双项目隔离 | Claude 操作 Codex 创建的 B，并连接 A；复用已有实例，跨项目误用引用返回 project_mismatch；明确停止 B 后，A 原实例/API/Worker 保持 |
| Codex 停止、重启与旧引用 | 首轮 never 策略拒绝 stop，Agent 未伪报成功；仅对隔离注册的 stop_service 预批准后，真实停止/启动改变实例、保留原 UUID，旧引用返回 connection_invalidated，再显式重连并确认暂停；最终两个测试平台均停止 |
| 宿主工具限制 | Claude 预配置 deny 将 init_project 从可见工具中移除；Codex enabled_tools 仅开放 inspect_project，检查后如实报告 init/start 不可用。独立检查两拒绝场景均无平台文件；没有运行时拒绝回执，也没有人工点击批准的证据 |

本轮共归档 11 个完成的宿主会话、100 次 MCP 调用，包含预期拒绝和探索性方法缺失，不把调用总数当作全部成功次数。主进程复核两项目 SQLite integrity_check=ok、foreign_key_check 为空、execution_runs=0；工具记录中的 API/Worker 已退出，复制到隔离配置中的账户凭据已移除。

已有协议/进程回归另覆盖并发首次启动、外部占用端口保护和 MCP 退出；本次真实宿主记录不扩大为精确同时抵达的两个 start 请求。Linux 平台/SDK 的历史实测也不扩大为 Linux 上两个真实宿主的交互界面验收。

## 观察到的宿主行为

Codex 某一会话先尝试 resources/list 与 resources/templates/list，得到 -32601，随后正确调用平台工具并完成流程。ComfyPilot 通过 tools 提供业务能力，没有声明 MCP resources/templates；这两个返回不表示平台启动或初始化失败。本轮没有为探索性请求补建无需求的资源接口。

Codex 首轮 never 策略实际拒绝停止操作，平台继续运行，Agent 报告未完成；随后依据 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)，仅在已获用户授权的隔离测试注册中对 stop_service 设置 approval_mode="approve" 并复验通过。该配置作用于整项工具，不按 project_root 限制；不是默认安装选项。

Claude 的 deny 规则在工具列表阶段生效，模型准确说明没有取得“调用后被拒绝”的回执；该事实不能算作运行时权限弹窗验收。

## 复验步骤

1. 按[安装说明](installation.md)准备源码外工具环境，在两个专用新创作目录安装完整 Skill 和项目 MCP，分别准备隔离宿主配置。不要覆盖日常配置或使用已有作品目录。
2. 在平台未运行时，让各宿主只发现 Skill/工具、保持待机；记录 MCP 初始化及审计结果。业务连接和工具发现是两个不同状态。
3. 分别以不同目录启动真实宿主，要求“使用 ComfyPilot，在当前目录初始化并打开工作台”，给出专用端口及明确目标。核对实际 MCP 调用和项目文件，结束会话后独立查询 API/Worker。
4. 平台运行时重复待机检查，再让另一宿主进入原项目；复用、连接、断开，使用已知错误引用验证拒绝。只停止明确项目，确认另一个项目继续运行。
5. 重启平台后尝试旧引用，再显式连接；停止测试平台，保留必要证据。错误路径用实际 code 判定，不能把模型声称“成功”作为回执。

实际非交互入口如下，具体模型/凭据沿用用户已授权配置，MCP 与工具权限须在专用配置中明确设置：

```sh
claude -p --verbose --output-format stream-json \
  --setting-sources project,local --permission-mode dontAsk < /absolute/test-prompt.txt

CODEX_HOME=/absolute/isolated-codex-home \
codex exec --skip-git-repo-check --ephemeral --json -C /absolute/test-project \
  "使用 ComfyPilot，按本次已授权验收范围操作"
```

两种命令都不能证明交互式信任与审批可用。Claude -p 跳过工作区信任对话；本轮 Codex 项目也预置受信任。拒绝工具的测试使用显式配置，不使用全面跳过权限的开关。

## 继续保留

- 在两个宿主的真实交互界面中，首次进入未经预置信任的新项目，验证项目/MCP 信任提示、拒绝后保持待机、用户允许后加载及执行；记录实际人工批准与拒绝过程。
- 正常交互会话的退出操作与终端关闭。本轮验证的是非交互宿主自然退出，不替代 Ctrl+C、界面退出及终端关闭的全部行为。
- Linux 上真实 Claude Code/Codex 宿主的完整交互路径；现有 Linux 平台、SDK 和真实生成证据保持有效。

M1/1.7 保持 review，AC-03 的完整宿主验收继续保留；其余独立 ComfyUI、全新安装/回退与画布保留项不在本轮关闭。
