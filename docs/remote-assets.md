# 远程素材桥接

本机目录适配器与素材桥接是两种显式选择：`assets.mode=local` 使用平台可访问的 input/output 绝对路径；`assets.mode=bridge` 使用目标 ComfyUI 内的扩展。位置标签不授予任何能力。未安装或协议不兼容时显示 unsupported，连接异常显示 unknown；不回退到标准图片上传冒充视频传输。

## 安装与回退

在平台环境运行 `comfypilot bridge-path`，取得随 wheel 交付的完整 `comfypilot_assets` 目录。该命令只读，不初始化项目、不联网、不导入扩展代码。将此目录复制到用户已指定的 ComfyUI `custom_nodes/` 下。只有用户提供的终端/SSH/容器管理通道才能安装，HTTP 可达不构成主机管理权限。

扩展只依赖 ComfyUI 已有的 Python、aiohttp 和标准库，无新增模型或节点依赖。先核对目标地址与目录、当前队列和启动方式；已有同名目录先比较并保留其修改。保存安装文件哈希，在队列空闲时按该环境的原有方式重启 ComfyUI，访问 `/comfypilot/v1/capabilities`，应返回 protocol=1、namespace=ComfyPilot，以及 upload/download/stat/known/delete。旧桥接没有 delete 时仍可传输，清理需升级。再在项目目标中设置：

```yaml
assets:
  mode: bridge
```

用 `probe_target` 核对执行能力与素材传输能力。运行新建独立测试项目的上传、指纹、下载检查后再生成。回退时先等待本项目及外部任务结束，将本次安装目录移到 custom_nodes 外的备份位置，再按原方式重启；素材命名空间与已有模型/节点保持独立。平台不暴露安装、shell 或重启 MCP 工具；清理须先生成固定清单，再通过 execute_cleanup 执行，见[素材维护](asset-cleanup.md)。

## 受限接口

| 接口 | 范围 |
| --- | --- |
| GET /comfypilot/v1/capabilities | 协议、素材能力与单文件上限 |
| PUT /comfypilot/v1/projects/{project_uuid}/input/{asset_uuid}.ext | 受支持视频/图片扩展名；流式上传；SHA-256校验，不覆盖未知文件 |
| GET .../projects/{project_uuid}/stat?role=input\|output&path=... | 指定项目命名空间内的大小、哈希和文件指纹 |
| GET .../projects/{project_uuid}/download?path=... | 仅output；必须带上一次stat的X-ComfyPilot-Fingerprint |
| GET .../projects/{project_uuid}/known | 最近1000条已记录副本，不扫描未知文件 |
| DELETE .../projects/{project_uuid}/delete | 台账内固定项目/路径/指纹与幂等 operation_id，物理删除该副本 |

根目录由 ComfyUI 的 folder_paths 提供。所有素材路径限于 `input/ComfyPilot/{project_uuid}/` 或 `output/ComfyPilot/{project_uuid}/`；平台进一步要求输出属于原 Run 的前缀与 history 声明。扩展拒绝绝对路径、`..`、跨项目路径与符号链接，不提供任意路径读取或 shell。已知副本台账在对应 input 项目子目录内，记录指纹，变化时拒绝覆盖。同一素材串行协调上传、读取与删除；业务引用和本地原件由平台在执行前复核，桥接本身不推断可清理状态。

上传及下载按1MiB分块，单文件服务端上限64GiB，实际输入仍受项目import_limits限制。大小和SHA-256必须与项目素材相符；复制前后复核源指纹。下载带指纹条件，传输后再次stat核对；改变的输出不能覆盖旧版本。元数据响应限制4MiB，能力探测2秒，传输整体超时300秒、空闲读取30秒；重试规则沿用[收片检查点](collection.md)，不承诺字节续传。

扩展沿用现有 ComfyUI 的监听网络及其网关认证。平台已有 credential_ref 时使用该项目凭据，不跟随重定向，不另建账号。不要将未受信任网络中的 ComfyUI 当作可随意开放的素材存储。

实现依据：[ComfyUI 标准路由](https://docs.comfy.org/development/comfyui-server/comms_routes)、[aiohttp 服务端流式响应](https://docs.aiohttp.org/en/stable/web_quickstart.html)。安装/重启、真实大文件传输和生成结果应单独记录实测，替身测试不代表GPU运行验收。

## 生产桥接升级补验（2026-10-29）

在用户已授权的原管理通道中，核对外部队列为空、旧文件与历史版本哈希一致，保存完整旧扩展到 custom_nodes 之外，再复制完整新版目录并按原 Docker 容器方式重启。容器身份、镜像和模型挂载保持；启动后确认扩展导入成功，capabilities 返回 protocol=1 与 upload/download/stat/known/delete。旧目录保留用于回退，本轮未执行生产回退。

两个新 UUID 的独立测试项目实际完成上传、指纹检查、下载和平台 prepare_cleanup → execute_cleanup。每个项目首次删除输入、输出副本合计 320,100,014 字节；相同命令返回原回执。重传相同输入后，重放同命令不会删除新文件，新命令执行旧清单返回 changed；重新预览后可清理该测试输入。所有本地原件哈希保持，升级前登记的 15 个既有受管媒体文件哈希在升级及清理后均保持。该核验只覆盖登记的文件，不推断全盘未变化；未提交真实 GPU 生成。

监控中的 managed_delete 现在跟随实际能力：新版桥接 available，旧桥接无 delete 时 unsupported，连接或协议请求失败时按原因显示 unknown/unsupported。本机目录需要已验证的输入写/输出读权限及输出写权限；未配置适配器不宣称支持。能力可用仍需清单归属、当前引用及本地原件复核，不等于任意删除权限。
