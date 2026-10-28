# ComfyUI 素材适配器与副本归属

M2/2.4 提供本机目录适配器、最终图准备和副本/引用查询；真实生成派发由后续 M2/2.5 接入。位置标签不代表目录访问能力，必须由用户明确提供目录：

```yaml
targets:
  - id: render
    name: 我的 ComfyUI
    base_url: http://127.0.0.1:8188
    assets:
      mode: local
      input_dir: /absolute/path/to/ComfyUI/input
      output_dir: /absolute/path/to/ComfyUI/output
```

目录必须已存在、可由平台访问、为绝对路径且不经符号链接。设置页与 MCP 配置共用有效修订；修改后旧方案保持原目标快照，不会被切换。只有 HTTP 地址且没有素材适配器时，不能声称通用视频可上传。

## 身份、路径与引用

项目原件仍按 SHA-256 存在本项目。目标输入写入 `input/ComfyPilot/<project_uuid>/<asset_id>.<ext>`；先逐块复制并复核大小/哈希，再以不覆盖方式就位。当前本机目标文件系统须支持同目录硬链接就位。已登记副本复用前重新核对哈希、大小、mtime 和 inode；未知同名文件不接管，不覆盖。传输按1MiB块并检查空间，不将完整媒体读入内存。

SQLite 格式8的 asset_replicas 保存目标快照/指纹、项目UUID、素材身份、类型、相对位置、大小及来源证据，asset_references 分别保存引用它的Run。只查询本项目已知记录，不扫描全ComfyUI或将陌生文件登记为己有。`list_replicas` MCP及`GET /api/assets/replicas`共用记录；目前共34个MCP工具。物理清理由M4实现，当前所有副本的cleanup_eligible均为false。

## 最终输入与输出

新执行方案的每个step通过`asset_ids`指定槽位对应的独立素材ID，`inputs`继续保存用户允许的SHA-256。保存方案时核对身份/类型/哈希；实际准备要求覆盖全部输入，旧的仅哈希草稿不能自动挑选来源，应创建完整新方案。准备按配方input_order复制和绑定，保存到Run的`document.materialized`：最终graph/graph_hash、顺序输入副本、目标及依赖快照、输出规则。

配方可对每个输出声明实际的前缀字符串绑定，例如H3：

```yaml
outputs:
  main:
    node_id: '92'
    key: images
    kind: video
    filename_prefix: {node_id: '92', input_name: filename_prefix}
```

准备时将其改成`ComfyPilot/<project_uuid>/<run_id>/<output_slot>`。绑定缺失不会伪造受管前缀；真实history输出仍需落在该Run目录内才能作为已知副本读取。越界、符号链接和指纹变化拒绝；声明范围外结果只保留证据，不能自动获得清理资格。准备期间方案被替换/停止则不保存可提交图。

本机目录与文件系统边界已在独立临时ComfyUI目录验证。真实本机ComfyUI及远程部署、安装回退、完整生成收片仍按M2后续任务验收；不以临时目录测试替代真实GPU验收。

远程目标现可选择assets.mode=bridge，通过[受限素材桥接](remote-assets.md)传输视频和图片、核对输出；本机目录模式继续使用显式目录，二者不自动切换。能力探测按实际配置和连接更新，并独立于主机安装/删除权限。
