# 操作约定

## 三种修订与命令回执

配置写入采用 `get_project.revision`；资源刷新采用 `list_resources.revision`（初始 0）；业务命令采用 `get_execution.revision`。不能混用。资源内容无变化时刷新不增加修订。业务命令包括 create_task、create_plan、activate_plan、execute_plan、stop_plan、pause_dispatch 和 resume_dispatch。

每次新的业务操作生成 UUID 字符串作为 command_id，传 expected_revision；回执包含 command_id、revision、event_id 和 result。重复原操作重用同一个 ID 与原参数，取回原回执；同 ID 换请求返回 command_mismatch。网络超时不证明操作未执行，先查状态或按原 ID 重读回执，不换新 ID 重复创建。新命令冲突时保留草稿，读取状态并重新确认其仍符合用户意图。

来源 agent/web_user 只是操作来源标签，不是可靠身份认证。

## 方案数据

| 操作 | 必要内容 | 结果 |
| --- | --- | --- |
| create_task | goal、acceptance | task_id |
| create_plan | task_id、steps、mandate | 不可变草稿 plan_id |
| activate_plan | plan_id | 草稿显式生效，取消旧方案无 intent 的 queued 运行 |
| execute_plan | plan_id、action_indices | 幂等 Run ID；索引从 0 开始 |
| stop_plan | plan_id | 取消未提交运行，保留在途意图 |
| pause_dispatch / resume_dispatch | 命令与业务修订 | 修改 manual_pause，其他限制保留 |

每个 step 明确 recipe_id、recipe_hash、inputs、parameters、target_id 和 required_nodes。recipe_hash 从已刷新配方记录读取，覆盖图、说明及关联资源，不是单独 API 文件哈希。inputs 是输入槽位到媒体 SHA-256 的映射；不要把可变文件路径当版本证据。所有输入和参数必须完整提供。required_nodes 必须与 API 图节点类型集合一致。

mandate 保存用户的 instruction 和 source，以及 allowed_recipes（配方 ID→哈希）、allowed_targets、allowed_inputs（输入 ID→哈希）、allowed_parameters（参数名→允许值列表）、max_submissions 和 stop_conditions。参数当前只支持明确候选值枚举，保留原值；不支持范围表达式。即使没有可调参数，也显式传 parameters 与 allowed_parameters 的空对象。

stop_conditions 支持 on_failure（失败后停止）与 after_limit；最大次数始终是硬限制。方案最多 100 个步骤，最大提交次数 100；次数来自用户指示，不因软件允许 100 就自行提高。M2/2.1 已验证配方声明与冻结图模板；输入媒体本体尚待导入阶段验证。旧版方案没有资源快照时需重建，已有 intent 保留。

## 门禁与缺项

- 草稿不暗中替换当前方案。参数、输入、配方或目标扩大范围时，先取得用户新的指示，再创建和生效新修订。
- 同方案/action_index 只有一个 Run；不能靠新 command_id 重跑同一动作，另一次尝试需要明确的新指示与新方案。
- 项目总生成并发为 1；内部预留意图会再检查所有暂停原因、次数、目标身份、fresh 的生成能力/节点和外部空队列。监测过期时等待或显式探测，不当作正常。
- 目标改址或停用后旧方案不会偷偷换端。保存新方案前明确目标；旧在途记录始终保持原快照。
- 内部 intent、额度和事件同事务。返回 reused 的原 intent 是待对账证据，不是再次发送许可。不明提交占用一次额度。
- 当前没有实际生成消费、回执对账、收片、清理、审片结论或事件流工具；不要调用设计文档中的未来工具，也不要编辑 SQLite 完成这些操作。
