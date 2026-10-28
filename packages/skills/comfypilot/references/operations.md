# 操作约定

## 三种修订与命令回执

配置写入采用 `get_project.revision`；资源刷新采用 `list_resources.revision`（初始 0）；业务命令采用 `get_execution.revision`。不能混用。资源内容无变化时刷新不增加修订。业务命令包括方案、派发、收片恢复、反馈、用户决定及评价写入；控制也支持 pause_plan、resume_plan 和 cancel_queued_runs。

每次新的业务操作生成 UUID 字符串作为 command_id，传 expected_revision；回执包含 command_id、revision、event_id 和 result；控制还包含 affected_steps 与 control 中仍有效的暂停、审片挂起、待核对和授权额度。重复原操作重用同一个 ID 与原参数，取回原回执；同 ID 换请求返回 command_mismatch。网络超时不证明操作未执行，先查状态或按原 ID 重读回执，不换新 ID 重复创建。新命令冲突时保留草稿，读取状态并重新确认其仍符合用户意图。

来源 agent/web_user 只是操作来源标签，不是可靠身份认证。

## 方案数据

| 操作 | 必要内容 | 结果 |
| --- | --- | --- |
| create_task | goal、acceptance | task_id |
| create_plan | task_id、steps、mandate | 不可变草稿 plan_id |
| activate_plan | plan_id | 草稿显式生效，取消旧方案无 intent 的 queued 运行 |
| execute_plan | plan_id、action_indices | 幂等 Run ID；索引从 0 开始 |
| stop_plan | plan_id | 取消未提交运行，保留在途意图 |
| resume_collection | run_id、命令与业务修订 | 仅恢复已确认外部成功的blocked收片，原Run/原额度；failed不可恢复 |
| pause_plan / resume_plan | plan_id、命令与业务修订 | 暂停后续派发；暂停随同任务新修订继承，恢复仅解除此方案开关 |
| cancel_queued_runs | plan_id、明确 run_ids | 整批仅取消queued且无intent；混入已提交项整批拒绝 |
| propose_plan | 同create_plan | 同一应用用例建立草稿，不自动生效 |
| pause_dispatch / resume_dispatch | 命令与业务修订 | 修改 manual_pause，其他限制保留 |

每个 step 明确 recipe_id、recipe_hash、inputs、asset_ids、parameters、target_id 和 required_nodes。recipe_hash 从已刷新配方记录读取，覆盖图、说明及关联资源，不是单独 API 文件哈希。inputs 是输入槽位到媒体 SHA-256 的映射，asset_ids 是同槽位到导入/提取后独立素材ID的映射；两者必须完整对应且类型、哈希一致；不要把可变文件路径当版本证据。所有输入和参数必须完整提供。required_nodes 必须与 API 图节点类型集合一致。

mandate 保存用户的 instruction 和 source，以及 allowed_recipes（配方 ID→哈希）、allowed_targets、allowed_inputs（输入 ID→哈希）、allowed_parameters（参数名→允许值列表）、max_submissions 和 stop_conditions。参数当前只支持明确候选值枚举，保留原值；不支持范围表达式。即使没有可调参数，也显式传 parameters 与 allowed_parameters 的空对象。

stop_conditions 支持 on_failure（失败后停止）与 after_limit；最大次数始终是硬限制。方案最多 100 个步骤，最大提交次数 100；次数来自用户指示，不因软件允许 100 就自行提高。本机适配器已接通最终图与提交：显式配置 assets.mode=local 和 input_dir/output_dir，Worker 按 input_order 准备真实素材、Run专属前缀，并持久保存最终图、输入/目标/依赖及意图后提交一次。旧版方案没有资源快照时需重建，已有 intent 保留。

## 本地媒体准备

媒体任务不使用配置/业务 expected_revision 或 command_id。每次 import_asset/extract_clip 返回新任务，其 id 是 job_id；预分配 asset_id 在任务 succeeded 前不可用。get_media_job 读取 status/phase/error，成功后 get_asset 保存原片与参考图的身份、哈希、来源和媒体信息。list_assets 只列最近100素材/50任务，旧记录可按保存ID读取。

例如用户指定原片1–3秒、24 FPS和一张人物参考图：分别 import_asset(video/image)，等两项 succeeded；extract_clip 传原片/参考图asset_id、start_seconds=1、end_seconds=3；成功后核对 time_map 为 first_video_frame/normalized_cfr、帧区间[24,72)、frame_count=48，input_order 为 video→reference。工作片无音轨，等比补边到指定偶数宽高。该48帧示例不满足H3固定124帧生成要求，不调用生成接口补齐。

默认导入限额：视频2GiB、图片50MiB、工作流5MiB；通过有效项目配置 import_limits 修改。工作流仍走资源刷新，媒体存于独立受管目录。图片必须真实解码，视频完整检查；错误码区分超限、路径、缺工具、空间、损坏或中断。修改原文件不影响成功导入副本；处理中修改原件则拒绝。

## 门禁与缺项

- 草稿不暗中替换当前方案。参数、输入、配方或目标扩大范围时，先取得用户新的指示，再创建和生效新修订。
- 同方案/action_index 只有一个 Run；不能靠新 command_id 重跑同一动作，另一次尝试需要新方案且仍在原指示剩余额度内；扩大范围/次数需明确新指示。
- 项目总生成并发为 1；内部预留意图会再检查所有暂停原因、次数、目标身份、fresh 的生成能力/节点和外部空队列。监测过期时等待或显式探测，不当作正常。
- 目标改址或停用后旧方案不会偷偷换端。保存新方案前明确目标；旧在途记录始终保持原快照。
- 内部 intent、额度和事件同事务。返回 reused 的原 intent 是待对账证据，不是再次发送许可。不明提交占用一次额度。
- 实际生成消费与回执对账已接入。意图已存在时仅核对原目标、原prompt_id；丢失prompt_id时按项目/Run/意图标记匹配，不明状态保留并阻止新提交。只有history明确success且completed才进入collecting，节点完成不算成功。后台已自动收片，list_versions 返回当前收片阶段、字节/错误及已完整解码并通过配方规格检查的ready版本。非H.264/MP4视频会有独立检查的播放代理；原片和代理不能混淆。不同Run即使字节相同也是独立版本，不合并来源。当前支持反馈、技术评价、明确用户采用及来源清单；提供get_context/read_events/wait_for_events接续和有界事件等待，清理与备份恢复已接入，按下方范围和复核流程操作；不要编辑 SQLite 绕过平台。

收片 checkpoint 保存阶段、已传字节、输出指纹、暂存token、尝试次数与下次时间。临时读取错误最多3次额外重试，耗尽后blocked；缺工具/空间直接blocked；媒体损坏或规格不符使Run.failed。按既有用户指示修复环境后使用resume_collection，首次恢复使用新的UUID command_id；同一次resume_collection请求回执丢失时才复用这次恢复的ID。原execute_plan及先前恢复轮次的command_id均不能拿来做新恢复，否则会command_mismatch或只返回旧回执。沿用的是run_id，不用execute_plan重跑。部分文件会从头重传，非通用字节续传；同名文件变化仍阻塞，不能借恢复覆盖旧指纹。

远程素材配置assets.mode=bridge，comfypilot bridge-path返回完整扩展目录；只通过用户指定的管理通道安装，核对队列空闲后按原方式重启并probe_target验证。桥接仅允许项目命名空间下输入上传、声明输出下载、stat与known列表；受限清理另需 capabilities.features 含 delete，没有 shell 或任意主机控制。未安装unsupported、断线unknown，不自动切换目标。原Run输出依旧须完整传输/解码/配方检查后才可审。


## 调整后续工作

先用 get_system_status/list_runs/get_run/get_collection_status 回读同一项目的修订、门禁、运行与收片。用户要求暂停某方案时 pause_plan；暂停整个项目后续派发时 pause_dispatch。二者不停止监测、既有intent对账或收片；恢复只解除对应开关，不清review_hold、待核对或失败停止。取消须提供明确plan_id/run_ids；取消结果若为conflict或run_not_cancellable，重新读取，不改为全局清队列。

返工用propose_plan/create_plan，feedback_ids明确关联需处理的反馈；沿用segment_id保持同片段候选归属。草稿不解除挂起，activate_plan后才标记关联反馈addressed；这不等于用户verified。沿用同一指示共享authorization_id、累计次数和失败停止条件；扩大目标/配方/素材/参数/次数必须先取得用户新原话，再设置new_instruction=true。

用户明确通过才record_user_decision，保留user_statement与来源；Agent评价用record_evaluation，不能代用户采用。get_delivery_manifest只包含已采用且技术合格的片段与来源。截图、媒体通过引用读取，不在MCP中传base64。


## 上下文和事件接口

get_context使用同一业务读取事务保存revision、event_cursor、有效方案授权累计、运行、收片、候选、采用与反馈；配置/资源单独携带自己的revision，不混作业务revision。候选窗口100条，近期运行20条；保存的版本/运行ID仍可按对应工具直接访问。node_schemas/node_classes不在公共目标监测中，配方标准预检仍在服务端使用完整catalog。

read_events默认after=0、limit=50，上限100；wait_for_events同样分页，timeout_seconds默认20、范围0–25。超时返回timed_out，关闭平台会返回interrupted；无事件时游标不前进。等待不阻塞其他MCP工具，客户端退出不停止Worker，离线期间产生的业务事件仍可补读。HTTP为GET /api/context、POST /api/events/read和/api/events/wait；原GET /api/events和网页SSE继续可用。

get_run/list_runs提供references，完整图、原始history和checks通过独立GET /api/execution/runs/{run_id}/evidence?kind=graph|history|checks读取；未产生时返回evidence_unavailable。context只给日志路径，不返回日志内容或凭据。所有HTTP引用相对于connect_service的面板URL；使用项目机器上的既有管理通道读取，不绕过实例或源限制。


## 素材维护、LAN 与备份

| 用户目标 | 操作与可核对结果 |
| --- | --- |
| 清理指定执行端副本 | inspect_storage(target_id, replica_ids?, roles?) 只盘点；明确范围后 prepare_cleanup(user_instruction, replica_ids) 保存固定 plan_id；execute_cleanup(plan_id, command_id) 才物理删除 |
| 回执不明或失败 | list_cleanup 读取计划/回执；同命令返回原结果。失败项用新 command_id 重试同 plan_id，仍复核引用与指纹。changed/protected 不当作可删，不能扩大清单 |
| 其他设备审片 | update_project 设置 server.host=0.0.0.0 及实际 IPv4 lan_hosts，停止再启动；本机 MCP 继续回环连接，远端只审片和脱敏监测 |
| 导出 | export_project(destination) 写项目外尚不存在的新目录；复制完整 bundle，不含平台秘密/PID/锁/暂存半文件 |
| 恢复 | restore_project(project_root, bundle_path) 无需旧服务连接；目标是父目录已存在的新目录，返回新 UUID、暂停和禁用目标。旧运行是历史，不自动重放或接管旧远端资产 |

维护保护活动 Run、未来方案、待审/待核对、未确认反馈及未知引用；项目原件须可读且哈希匹配。删除字节为文件大小估算，磁盘空闲变化独立记录，不把移动到同盘回收站当释放。只有标准 HTTP 或旧桥接时解释 cleanup_unavailable，按用户已给管理通道升级扩展，不自行换 SSH 或扫描其他项目。

恢复先验证清单/路径/hash/SQLite完整性/来源引用；失败保留证据，改用新目录，不覆盖现有项目。恢复后重新检查媒体工具、目标和有效方案；不能通过重放旧command或改库继续旧生成。移动原项目先stop，整目录移动后使用新绝对路径start/connect，UUID保持。升级前备份并保留旧环境，较旧程序不能读较新数据库；需要回退时使用升级前副本。comfypilot distribution-path 返回安装版锁文件、协议和安装说明。
