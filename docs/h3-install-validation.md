# H3 独立安装、失败回退与画布往返

日期：2026-10-29（Asia/Shanghai）。本轮补验 M2/2.2，原有环境保留，新建独立 ComfyUI 目标。已验证全新核心和 Python 依赖安装、缺节点修复、两种失败路径及实际浏览器画布往返。真实单次生成、自动收片及原环境恢复也已完成；四个既有模型保持只读共享，未重复下载。

## 环境隔离与安装

先保存原容器 ID、镜像 ID、启动配置、队列及素材哈希。原队列为空后，按用户授权停止原容器，再运行新环境；原容器使用 stop/start，保留原容器身份和目录。

新环境参考现有分类目录的布局，但使用独立容器名称、端口与目录：

| 内容 | 本轮处理 |
| --- | --- |
| 基础镜像 | 固定到既有镜像的不可变 image ID，仅复用系统/CUDA 基础层；覆盖镜像默认入口，不执行生产 pre-start |
| ComfyUI | 从上游重新 fetch 并 checkout `6b747c0428c343e1417219641db93a4fb7cb69ae`，版本 0.38.0；新源码目录，无生产源码复制 |
| Python | 3.13.15，新建空白 venv，未使用 system-site-packages；实际 sys.path 不包含系统 site-packages，user site 关闭 |
| 依赖 | 从官方 PyTorch cu130 源安装 torch 2.13.0+cu130 / torchvision 0.28.0+cu130，再从 PyPI 安装固定核心 requirements；pip check 通过，完整 freeze 单独留证 |
| 实测包 | PyAV 19.0.2、aiohttp 3.14.5、safetensors 0.8.0、transformers 5.21.0、comfy-kitchen 0.2.36、comfy-aimdo 0.5.5 |
| 节点 | 17 种 H3 节点全为新核心提供；独立 custom_nodes 只额外安装随包素材桥接，未复用生产自定义节点 |
| 模型 | 只读挂载既有 models，通过 extra_model_paths 配置；四文件大小及 SHA-256 与 environment.lock.json 完全一致 |
| 其他目录 | input/output/user、配置、缓存、HF/Torch 缓存、临时文件和 venv 均独立；模型目录是唯一生产挂载 |
| 设备/接口 | RTX 3090 / 24 GiB；CUDA 实际运算通过，FFmpeg/ffprobe 7.1.5、PyAV libx264 编码器可用；HTTP/WS 与 963 种节点目录可用 |

模型约 40.1 GB，本轮按用户要求共享已有模型，未重新下载权重。不能将此结果写成从空磁盘下载模型、下载中断恢复或所有硬件均已验证。全新安装指新核心、空白 venv、独立节点/配置和目标模型映射。目标是否可用必须逐项检查，不能仅凭基础镜像启动成功。

## 缺依赖与失败回退

所有故障只发生在新环境，操作前检查其运行/等待队列为空。

1. **真实缺节点**：暂存新核心的 nodes_minimax_h3.py 到加载路径之外后重启。目标启动完成后，MCP probe_target / preflight_recipe 明确返回 missing_dependencies，唯一缺节点为 MiniMaxH3ReferenceToVideo，缺模型为 0。恢复该固定提交文件、检查 Git diff、重启并重探测后，预检 passed。启动尚未就绪期间返回 unknown，不能沿用旧目录的通过结论。
2. **依赖安装失败**：在另一空白候选 venv 中安装不存在的 comfyui-frontend-package==0.0.0，pip 真实退出 1；活动 venv 不变，原可用测试服务仍可读。失败候选不自动启用。
3. **启动失败后的回退**：故障演练主动将测试启动指向上述不完整候选，真实进程因 ModuleNotFoundError 退出 1。切回保留的可用 venv 后，同一测试容器及端口重新启动，恢复 0.38.0、节点目录和 passed 预检。未在活动环境 pip uninstall，也未改模型、清队列或重提生成。

标准预检仍只覆盖 schema，version_verification、materialization 与 real_test 的含义保持不变；终端环境记录另存对应目标回执。不同目标预检证据继续按 recipe_hash + target_id 隔离，已有双目标自动测试不以此次安装结果互相赋值。

## 实际前端画布往返

在新实例的 **ComfyUI 前端 1.53.6** 实际加载随包 canvas.json，待参考图/视频预览就绪，使用前端 graphToPrompt 导出画布及 API 图；重新加载该导出画布，再次等待预览就绪并导出。两次稳定 API 导出相同，18 个节点、22 条连线、Video 1 / Picture 1、提示词、seed、尺寸/帧数、20 步和 MP4/H.264 均保持。实际画布无缺节点或缺素材错误；素材使用独立测试副本，未执行浏览器队列提交。

前端导出和随包 api.json 需要区分以下字段：

| 字段 | 实测行为与处理 |
| --- | --- |
| 每节点 _meta.title | 前端补入本地化显示标题，比较执行语义时不计入 |
| CreateVideo.color_space=sRGB、codec=none | 前端补入目标 schema 的默认值，未改变随包图的默认语义 |
| SaveVideo.format.codec.encoding=auto、codec=auto | 前者为当前 H.264 分支默认编码模式；后者为隐藏兼容输入，实际使用 format.codec=h264 |
| LoadVideo.video-preview="" | 前端预览控件误带入 API 输入，目标 schema 未定义；平台明确报 `200.video-preview 不在目标节点输入定义中`，不会自动提交 |

Agent 将导出图作为项目配方前，应保留画布原件，仅删除已确认的 **LoadVideo.inputs.video-preview**，再按实际目标 schema 重新预检。本轮仅删除这一 UI 字段后，真实目标预检 passed；不能无条件删除未知输入。按上述四个默认值和标题归一后，导出执行图与随包 api.json 完全一致。API 图仍单独保存和冻结，不从 widgets 数组猜测，也不把原始前端导出直接覆盖内置配方。

两个模板媒体名仍为占位；正式执行需由平台物化项目/Run 输入及输出前缀。已有 Run 的资源和图哈希保持，说明更新会产生新的资源快照哈希。

## 验收结论与保留范围

通过现有安装版 ComfyPilot 的真实 stdio MCP 在源码外新 UUID 项目操作：导入用户已授权的原片和参考图，创建最多一次提交的范围与方案，显式恢复派发。新环境真实 H3 执行约 404.21 秒，原 history 成功后平台自动收片；仅一个 Run、一个 prompt_id、一次额度及一个 ready 版本，未自动采用。

输出为 H.264 / MP4、768×448、24/1 fps、124 帧、5.166667 秒，一个 H3 生成音轨，完整解码和规格检查通过。文件 278,055 字节，SHA-256 为 `cf998242de606b05260cf8891c16cd969f52cedfbcfedd221ffe687a1a02f9b0`。项目 SQLite integrity_check=ok、foreign_key_check 无错误。此前生成记录仍保持自己的资源哈希；本轮只改随包说明，未修改 API 图、画布或参数。

测试队列清空后停止测试容器，恢复原容器。原 ID、镜像 ID、挂载及核心 diff 均保持；22 个既有受管素材、2 个配置文件、4 个模型逐项复核大小和 SHA-256，28 项全部未变。原服务 0.38.0 恢复健康且队列为空，原采用作品及项目未改动。独立测试环境保留为停止状态，未删除或接替原服务。

工程检查：make check 的 Ruff/格式、Biome、TypeScript、Web 单测/构建及 **237 项 pytest 通过、2 项压力测试默认跳过**；新增真实前端预览字段拒绝的回归用例。make package 后在源码外空白 venv 使用带哈希依赖安装，逐文件核对 wheel，CLI/API/独立 Worker/Web 静态资源及 59 个 stdio MCP 工具、显式连接、恢复后原目标能力探测通过。候选 wheel 为 306,327 字节，SHA-256：`4d9b7f3fec8301e6d194f5ab5fb5069937e0195f98b18cbfd1d15e48df231923`；版本号未变，与历史候选分开记录。真实 GPU 生成使用更新说明前的既有安装版，运行代码与图保持一致。

AC-08 的真实缺节点展示、固定依赖补齐后重新可用已有证据；AC-09 增补实际画布往返及不合法前端导出的拒绝。安装、缺节点修复、回退和往返原始日志/JSON/截图保存在同步范围外，不向仓库或看板上传服务地址和用户素材。

本轮新样片播放至结束的额外浏览器补验被后台节能及切页打断，未记通过；完整解码已通过，历史播放验收保持独立。

继续保留：从零下载四个权重及下载失败恢复、其他 GPU/驱动/核心版本、长期负载、未获用户明确结论的新候选视觉/音频质量；M1 双宿主交互式验收与 M3 双实例在途切换另行验收。新增独立实例本身不代表完成在途切换。
