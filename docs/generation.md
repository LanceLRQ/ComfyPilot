# 生成提交与对账

Worker 在用户显式生效方案、入队并解除手动暂停后消费 Run。每个输入需同时提供 `inputs` 中的 SHA-256 与 `asset_ids` 中的素材身份；旧的仅哈希草稿不能执行。配置本机素材适配器后，按配方顺序复制输入，核对既有副本指纹，绑定本项目路径及 Run 专属输出前缀。

提交前重新探测目标，事务内核对当前方案、执行指示、暂停原因、次数、串行限制、外部空队列及最终图 schema；最终图和哈希、输入副本、配方资源、目标、client_id、提交意图与额度先持久保存，之后才发送一次 `POST /prompt`。回执保存在 Run.document.receipt。明确的标准配方校验拒绝成为失败，其余异常或不完整回执进入 reconciling。提交意图始终占用一次额度，不能靠重连或重启重发。

Worker 优先处理已有意图，使用保存的目标地址和 prompt_id。没有 prompt_id 时仅认领同时匹配项目 UUID、Run 与意图 ID 的队列/history 条目。查询按 history→queue→history 进行，消除任务刚从队列移入历史的空窗；仍未确认则保持待核对并阻止新生成。修改目标配置或停止旧方案不会改写在途提交的目的地。

只有 history 明确声明 `status_str=success` 且 `completed=true` 才记录原始历史和外部成功证据、进入 collecting。单个节点完成、采样100%、队列消失均不代表生成成功。外部失败记录原始证据并遵守 on_failure。Worker 不清队列、不调用全局 interrupt，不接管陌生任务。

停止平台或 Worker 中断可能留下未发出但已预留的意图；无法确认时宁可保持待核对。标准接口没有幂等提交保证，因此本平台保证不自动重复发送，不能声称服务端恰好执行一次。核对响应限16MiB、超时25秒，不跟随重定向，不向日志输出凭据。

M2/2.5 已接通持久提交与对账；自动收片由后续任务接入。当前仍以实际执行端能力为准，HTTP 可达不代表具有视频传输或节点安装能力。真实本机/远程 H3 完整链路待独立验收，测试替身不替代 GPU 生成。

接口依据：[ComfyUI 官方路由说明](https://docs.comfy.org/development/comfyui-server/comms_routes)、[官方服务实现](https://github.com/comfyanonymous/ComfyUI/blob/master/server.py)。
