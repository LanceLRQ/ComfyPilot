# 自动收片与基础预览

外部 history 明确成功后，独立 Worker 按冻结配方的输出节点、key 和槽位读取文件声明，不扫描整个 ComfyUI output。当前每个槽位必须声明一个文件，并落在该 Run 的受管前缀下。本机素材适配器负责指纹与流式复制，Agent 退出、断开 MCP 或暂停新派发不会停止收片；平台重启会接续已保存的成功证据。

收片按等待输出、传输、校验、播放代理、登记、可审推进，实际字节数可查询，不推算整体百分比。网页“收片与预览”和 MCP `list_versions` 读取同一数据。版本最多返回最近100条，收片队列最多50条；保存的版本 ID 可直接访问媒体。当前为基础播放，逐段反馈、采用与对比属于 M3。

视频须经 ffprobe 获取帧数及画面信息，再经 FFmpeg 完整解码；图片也须真解码。输出可声明固定规格，例如：

```yaml
outputs:
  main:
    node_id: '92'
    key: images
    kind: video
    filename_prefix: {node_id: '92', input_name: filename_prefix}
    spec:
      width: 768
      height: 448
      fps: 24
      frame_count: 124
      duration_seconds: 5.166666666666667
```

未声明的规格不猜测。尺寸/帧数必须一致，FPS误差上限0.001，时长容差一帧；H3预设显式声明其固定输出规格。解码或规格失败只保留检查点、输出副本及失败证据，不生成可审版本。

原始结果保持其编码；非 H.264/MP4 的视频另生成 H.264、yuv420p、MP4 播放代理，并独立解码、核对尺寸和帧数、计算哈希。原始与代理分别保存，只有必需检查通过才登记 Version、Check、输出引用并在事务内发出 clip_ready。

每个 Run/输出槽位唯一，相同内容可共享本地字节，但不同 Run 拥有独立版本身份和输入来源。A 进入收片后不占用生成并发，B 可继续生成。媒体内容端点支持 Range；前端使用浏览器原生视频/图片控件，基础播放不代表人工通过或已采用。

接口：`GET /api/versions`，`GET /api/versions/{version_id}/content?variant=preview|original`，MCP `list_versions`。待收片状态仍可通过 `get_execution` 查看完整检查点。临时读取/传输错误进入 retry_wait，默认最多3次额外退避重试（间隔2、4、8秒）；截止时间、尝试次数、输出指纹、暂存token、已传字节与错误持久保存。耗尽后 collecting/blocked；缺空间或工具直接阻塞。确定的解码/规格失败使Run进入failed，不能恢复收片绕过。

修复环境后，通过业务命令 `resume_collection(run_id, command_id, expected_revision)` 在原Run恢复 blocked 收片。仅已保存外部成功证据的Run可恢复；首次恢复创建新的命令UUID；仅重放同一次恢复时复用该ID和原参数返回原回执，不增加POST /prompt或生成额度。新一次恢复会重置本轮收片尝试计数并增加恢复次数，保留原输出指纹/证据。修改媒体后的指纹不符仍阻塞，不覆盖旧副本。

当前是检查点恢复：丢弃该Run已记录的不完整暂存文件后重传，已登记槽位直接复用；并非任意文件系统或HTTP字节断点续传。暂停新派发不停止收片，停止平台后重启可接续进行中的阶段。
