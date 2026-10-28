# 远程素材桥接

本机目录适配器与素材桥接是两种显式选择：`assets.mode=local` 使用平台可访问的 input/output 绝对路径；`assets.mode=bridge` 使用目标 ComfyUI 内的扩展。位置标签不授予任何能力。未安装或协议不兼容时显示 unsupported，连接异常显示 unknown；不回退到标准图片上传冒充视频传输。

## 安装与回退

在平台环境运行 `comfypilot bridge-path`，取得随 wheel 交付的完整 `comfypilot_assets` 目录。该命令只读，不初始化项目、不联网、不导入扩展代码。将此目录复制到用户已指定的 ComfyUI `custom_nodes/` 下。只有用户提供的终端/SSH/容器管理通道才能安装，HTTP 可达不构成主机管理权限。

扩展只依赖 ComfyUI 已有的 Python、aiohttp 和标准库，无新增模型或节点依赖。先核对目标地址与目录、当前队列和启动方式；已有同名目录先比较并保留其修改。保存安装文件哈希，在队列空闲时按该环境的原有方式重启 ComfyUI，访问 `/comfypilot/v1/capabilities`，应返回 protocol=1、namespace=ComfyPilot，以及 upload/download/stat/known。再在项目目标中设置：

```yaml
assets:
  mode: bridge
```

用 `probe_target` 核对执行能力与素材传输能力。运行新建独立测试项目的上传、指纹、下载检查后再生成。回退时先等待本项目及外部任务结束，将本次安装目录移到 custom_nodes 外的备份位置，再按原方式重启；素材命名空间与已有模型/节点保持独立。平台不暴露安装、shell、重启或删除远端文件的 MCP 工具。

## 受限接口

| 接口 | 范围 |
| --- | --- |
| GET /comfypilot/v1/capabilities | 协议、素材能力与单文件上限 |
| PUT /comfypilot/v1/projects/{project_uuid}/input/{asset_uuid}.ext | 受支持视频/图片扩展名；流式上传；SHA-256校验，不覆盖未知文件 |
| GET .../projects/{project_uuid}/stat?role=input\|output&path=... | 指定项目命名空间内的大小、哈希和文件指纹 |
| GET .../projects/{project_uuid}/download?path=... | 仅output；必须带上一次stat的X-ComfyPilot-Fingerprint |
| GET .../projects/{project_uuid}/known | 最近1000条已记录副本，不扫描未知文件 |

根目录由 ComfyUI 的 folder_paths 提供。所有素材路径限于 `input/ComfyPilot/{project_uuid}/` 或 `output/ComfyPilot/{project_uuid}/`；平台进一步要求输出属于原 Run 的前缀与 history 声明。扩展拒绝绝对路径、`..`、跨项目路径与符号链接，不提供任意路径读取、shell或删除。已知副本台账在对应input项目子目录内，记录指纹，变化时拒绝覆盖；cleanup_eligible始终false，清理由M4单独实现。

上传及下载按1MiB分块，单文件服务端上限64GiB，实际输入仍受项目import_limits限制。大小和SHA-256必须与项目素材相符；复制前后复核源指纹。下载带指纹条件，传输后再次stat核对；改变的输出不能覆盖旧版本。元数据响应限制4MiB，能力探测2秒，传输整体超时300秒、空闲读取30秒；重试规则沿用[收片检查点](collection.md)，不承诺字节续传。

扩展沿用现有 ComfyUI 的监听网络及其网关认证。平台已有 credential_ref 时使用该项目凭据，不跟随重定向，不另建账号。不要将未受信任网络中的 ComfyUI 当作可随意开放的素材存储。

实现依据：[ComfyUI 标准路由](https://docs.comfy.org/development/comfyui-server/comms_routes)、[aiohttp 服务端流式响应](https://docs.aiohttp.org/en/stable/web_quickstart.html)。安装/重启、真实大文件传输和生成结果应单独记录实测，替身测试不代表GPU运行验收。
