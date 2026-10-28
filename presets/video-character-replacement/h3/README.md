# H3 单人视频替换

配方 ID：`minimax-h3-replacement`。模块 ID：`video-character-replacement`。首发采用 MiniMax H3 基础 R2V，不使用 Turbo、LoRA、ControlNet 或第三方自定义节点。资源可随 wheel 脱离源码读取。

平台已接入媒体导入、最终执行图物化、真实提交和自动收片。Agent 不能因 preflight passed 宣称已出片；必须等待原目标 history 明确成功，且收片版本 ready、技术检查通过。部署实测与未验范围见项目的 docs/h3-validation.md，人工审片与采用由用户决定。

## 资源和版本

| 文件 | 用途 |
| --- | --- |
| recipe.yaml | 输入、参数、模型、输出绑定 |
| api.json | 18 节点、17 种核心节点的执行图模板；以此为预检和冻结依据 |
| canvas.json | 对应基础链路的可视画布，去除项目私有素材与说明；尚未验证当前前端导入/再导出往返 |
| upstream-r2v.json | 未修改的官方 R2V 模板原件，包含额外分支，不作为此配方执行图 |
| environment.lock.json | 核心提交、节点类型、模型修订/大小/哈希、环境参照与验收边界 |
| target-evidence.example.json | 单目标环境证据模板，不能当作安装成功回执 |
| LICENSE.workflow-templates | 官方模板 MIT 版权与许可全文 |

ComfyUI 固定为 `0.38.0`，提交 `6b747c0428c343e1417219641db93a4fb7cb69ae`，节点实现采用其 GPL-3.0 许可；本包只分发图与说明，不捆绑 ComfyUI 源码。API 图按既有 H3 实践的基础链路整理，源模板来自 [Comfy-Org/workflow_templates 固定提交](https://github.com/Comfy-Org/workflow_templates/tree/e7cd011d4ded3411c2f481200544f0be6fdc962e)，SHA-256 见锁文件。原件与派生画布/API 图分别留存，平台不从 widgets 数组猜测执行图。

全部节点均属于核心：UNETLoader、CLIPLoader、VAELoader、LoadImage、LoadVideo、GetVideoComponents、PrimitiveStringMultiline、MiniMaxH3ReferenceToVideo、RandomNoise、KSamplerSelect、BasicScheduler、BasicGuider、SamplerCustomAdvanced、VAEDecode、VAEDecodeAudio、CreateVideo、SaveVideo。`custom_nodes: []`，不需要 ComfyUI-Manager、VHS 或云端 API Key。版本号可见不等于目标源码提交已核验。

四个权重均固定到 Comfy-Org/MiniMax-H3 的同一修订 `e5eb578a89295337b8ff433a035929ce0279e0b6`，下载 URL、LFS SHA-256 及字节数见 recipe.yaml / environment.lock.json：

| 模型文件 | 放置于目标 ComfyUI 的目录 | 图中选择器 |
| --- | --- | --- |
| minimax_h3_ref2va_pruned_int8_convrot.safetensors | models/diffusion_models/ | 127.unet_name |
| qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors | models/text_encoders/ | 128.clip_name，type=minimax |
| minimax_h3_video_vae_int8_convrot.safetensors | models/vae/ | 119.vae_name |
| minimax_h3_audio_vae_fp32.safetensors | models/vae/ | 120.vae_name |

模型许可采用模型仓库声明的 MiniMax H3 Community License Agreement，具体固定链接见 recipe.yaml；项目 MIT 许可不覆盖模型。量化文本编码器的原来源与关联条款见[固定修订模型卡](https://huggingface.co/Comfy-Org/MiniMax-H3/blob/e5eb578a89295337b8ff433a035929ce0279e0b6/README.md)，其中指向 Qwen3-VL-32B NVFP4 转换来源。不捆绑权重、不绕过下载条件、不把 ref2va 换成 fl2va，也不自动换量化格式。

## 输入、参数与输出

输入顺序固定为 **Video 1 → Picture 1**。`<Video 1>`、`<Picture 1>` 是 H3 的提示词引用标记，不是随意编号。

| 槽位/参数 | 绑定 | 约定 |
| --- | --- | --- |
| video | 200.file → 201.video → 136.ref_videos.ref_video_0 | 已裁剪、转为 24 fps 的单段视频 |
| reference | 137.image → 136.ref_images.ref_image_0 | 一张清晰人物参考图，身份与服装描述需明确 |
| prompt | 138.value → 136.prompt | 默认只替换前景主角，保留其他人、镜头和背景；多个人物时必须写清位置/外观 |
| seed | 129.noise_seed | 默认 42，可为 0–2^53−1 的整数，保持网页 JSON 整数精度 |
| width / height / length | 136 同名输入 | 首发固定 768 / 448 / 124，不接受任意网格或帧数 |
| steps | 124.steps | 首发固定 20；res_multistep / simple / denoise=1 |

宽高为 32 的倍数，帧数满足 17k+5；124 帧 / 24 fps 约为 5.167 秒。此版不提供时长、尺寸、FPS、采样器或模型自由组合；需要改规格时创建新项目配方并重新预检和验收。`ref_image_size=match` 限制参考编码开销，不能承诺任意片段在 24 GiB 显存上成功。模型文件约 40.1 GB，还需环境、缓存和输出空间；实测参考机器为 RTX 3090 24 GiB / 约 64 GiB 内存，实际 offload、驱动与启动参数需另记。

图本身没有剪辑和时间偏移功能。输入必须预先完成以下处理：

1. 用户/Agent 明确原片 `start_seconds`，不默认沿用参考项目的历史偏移。配方消费准备后素材的第 0 帧；原片偏移另存在项目任务元数据。
2. 先转 CFR 24 fps，再取连续 124 帧。足够时长约 5.167 秒；不足不默认补帧或循环。可用平台 extract_clip 显式提取，保留原片时间映射。
3. 保持画面比例，缩放后补边至 768×448，默认不裁掉人物。若要裁剪，明确裁剪框并保留原片→工作片映射，不拉伸人物、不静默改 FPS。
4. 参考图仅用于身份/服装，不自动分割、抠图、定位人物或创建多角色参考。

下面的命令只作为准备步骤示例。Agent 应先填好本项目实际路径和偏移；输出必须是新的工作副本，命令不覆盖已有文件。空间路径均保持引号，参数通过参数数组或可靠 shell 引用传入。

```sh
ffmpeg -n -i "$CP_SOURCE" -ss "$CP_START_SECONDS" \
  -vf "fps=24,scale=768:448:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=768:448:(ow-iw)/2:(oh-ih)/2,setsar=1" \
  -frames:v 124 -an -c:v libx264 -pix_fmt yuv420p "$CP_PREPARED"
ffprobe -v error -count_frames -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate,nb_read_frames -of json "$CP_PREPARED"
```

要求结果宽 768、高 448、r_frame_rate=24/1、nb_read_frames=124。输入素材顺序和 prompt 标记需与图绑定一起核对。两个加载节点中的示例文件名只是占位，枚举中不存在也不能认为上传已完成。

输出为 `92`（SaveVideo）保存的 MP4 / H.264，24 fps、8 bit、sRGB。history 的 UI 文件键是 **images**，来自核心 PreviewVideo，不是 videos；下游必须按媒体类型识别为视频。默认前缀 `comfypilot/unmaterialized` 仅为模板占位，后续执行链必须替换为项目 UUID/Run 专属前缀并保存最终图哈希，不直接提交模板。

图的音频来自 H3 → VAEDecodeAudio → CreateVideo，是**模型生成音频**，不是保留原片声音。若要原片配乐/对白，需要单独授权和后处理、时长同步，再产生新版本。审片检查身份、服装、动作、其他人物、背景、时序连续性及音频；遮挡、多人靠近、快速转身可能失败，不保证逐像素背景或表演完全保留。

## Agent 安装流程：始终定位单个执行端

1. 从 `get_project` / `resolve_target` 取得用户选择的 `target_id` 和地址。记录项目 UUID、配置修订、目标身份与该目标的管理方式。没有用户明确选择，不采用另一台机器或全局默认配置。
2. `probe_target` 检查 HTTP / WebSocket、节点、队列和能力。只有 HTTP 时，`environment_management` 为 unsupported；模型选项出现只证明选择器可见。报告缺少“目标终端/SSH/容器管理通道”，保留缺项；不能在 Agent 所在电脑安装一份来冒充远端已安装。本配方没有新增远程管理适配器。
3. 有目标管理通道后，确认 ComfyUI 的绝对目录、运行用户、真实解释器/venv 或容器、启动命令、模型路径映射、GPU/驱动、磁盘余量；不要使用本机默认 python/pip。读目标 Git SHA/dirty 状态、`pip freeze`、版本和模型 SHA-256，保存该目标原状。
4. 安装前再次读 `/queue`。运行或等待不为空时，等待该目标空闲或请用户安排停机窗口，不执行 `/interrupt`、清队列或杀其他进程。安装/更新会影响所有使用该 ComfyUI 的项目；HTTP 可达本身不是重启授权。可用依赖已满足时跳过安装和重启。
5. 优先在目标的新隔离目录/venv 安装固定核心提交。已有目录存在未保存修改时不覆盖、不 reset。CPU/驱动不支持所需量化时保留失败，另立配方调整，不能静默降级。

以下仅在已确认的目标终端执行；`CP_COMFY_ROOT` 为新目录，`CP_PY` 为它的解释器。路径由用户/管理通道确认，不从 HTTP URL 推导。

```sh
git clone --no-checkout https://github.com/Comfy-Org/ComfyUI.git "$CP_COMFY_ROOT"
git -C "$CP_COMFY_ROOT" checkout --detach 6b747c0428c343e1417219641db93a4fb7cb69ae
# 使用目标上选定的 Python 创建独立 venv，再将 CP_PY 指向其解释器。
"$CP_PY" -m pip install -r "$CP_COMFY_ROOT/requirements.txt"
"$CP_PY" -m pip check
"$CP_PY" -m pip freeze
git -C "$CP_COMFY_ROOT" rev-parse HEAD
```

Python/PyTorch 依赖以固定核心的 requirements.txt 为基线，GPU wheel 按目标驱动选择；INT8 convrot 参照模型卡使用 cu130。观察到的运行环境为 Python 3.13.15 / PyTorch 2.13.0+cu130；这是 HTTP 自报环境参照，不是已在本项目重装复现的依赖锁。不要把 requirements 安装成功等同于模型可运行，保存目标解析后的完整 freeze 与驱动版本后再生成验证。

6. 模型按锁文件的固定 URL 下载到新文件，逐项比对大小和 SHA-256 后放入上述目标目录；路径映射也可用现有 extra_model_paths，必须记录实际文件位置。保留旧文件，不按同名直接覆盖。权重无需进入创作项目资源目录或 Git。
7. 素材预处理环境需要 `ffmpeg` / `ffprobe`、libx264、缩放/补边/fps 滤镜；当前本机工具为 8.1.1。目标若承担预处理也需相同能力。核心 LoadVideo/SaveVideo 采用 PyAV，检查目标 `import av` 与 H.264 编码能力，不能用本机 ffmpeg 版本替代目标证据。目标命令行 FFmpeg、PyAV/编码器及驱动尚待管理通道核验。
8. 核心或依赖变更需要重启目标 ComfyUI。再次确认队列为空，按其原启动方式只重启该实例；不要改变另一目标或平台生命周期。保留原命令、venv/镜像和日志。只加模型时按实例支持的方式刷新模型清单，无效时按同样停机流程重启。

## 安装后的验证与回退

验证按证据层次执行，并写入当前项目 `.comfypilot/environment/<target_id>/receipt.json`（由 Agent 管理的本机过程证据，平台当前不自动导入）：

1. 目标终端：Git SHA/dirty、解释器和包版本、`pip check`、`import torch, av`、CUDA 可用性、四个权重实际 SHA-256、FFmpeg/ffprobe/编码器。`environment.lock.json` 内列的是上游期望值，不是目标实测回执。
2. 平台：显式 `refresh_resources`，选择该配方；`probe_target(target_id)` 后立即 `preflight_recipe(recipe_id,target_id)`。预检有效期 15 秒，过期重探测。失败逐项报告缺节点、缺模型、无效绑定或 unknown；缺项不得通过切换另一执行端来掩盖。
3. 画布：在目标实际前端导入 canvas.json，检查动态参考端口与 SaveVideo 参数，保存/导出新的 API 图后对照 api.json。当前画布往返未实测，默认执行依据仍为独立 API 图。上游原件包含其他分支，不能直接顶替本配方。
4. 完整链路：M2/2.9 用用户授权的普通测试人物素材，经平台最终图提交，记录 prompt_id、最终图/素材哈希、节点日志、history 文件、原片偏移、模型/环境证据与输出 ffprobe；核对 124 帧、24 fps、768×448 和音频来源，再审查视觉质量。当前不使用 `/prompt` 充当只校验接口，不绕过平台先跑生成。

`resource_preflights` 按 **recipe_hash + target_id** 保存标准预检证据；实例、Worker、配置或目录失效后标 stale。一个目标通过不会修改另一目标的状态。receipt.json 只写对应 target_id 的环境记录，状态分为 expected / verified / unknown；不含 SSH 密钥、令牌、完整启动 argv 或用户素材文件列表。当前平台不把手写 receipt 自动当作安装状态。

回退先停止新生成派发，保存错误和目标证据；等待该 ComfyUI 队列空闲。只在已选目标切回原目录/venv 或原镜像/启动命令，不在活动环境 `pip uninstall` 猜回旧版本；已保留的模型原路径继续使用，新模型暂留便于诊断，不批量删除或影响其他项目。重启后重新读 Git SHA、HTTP/WS、节点与模型目录，重做预检；旧预检失效，不修改历史 Run 快照。回退重启仍受停机授权约束，无管理通道则报告待处理，不宣称已回退。

## 依据与待验收

节点参数依据固定核心的 [H3 实现](https://github.com/Comfy-Org/ComfyUI/blob/6b747c0428c343e1417219641db93a4fb7cb69ae/comfy_extras/nodes_minimax_h3.py)、[V3 输入展开](https://github.com/Comfy-Org/ComfyUI/blob/6b747c0428c343e1417219641db93a4fb7cb69ae/comfy_api/latest/_io.py)、[视频保存](https://github.com/Comfy-Org/ComfyUI/blob/6b747c0428c343e1417219641db93a4fb7cb69ae/comfy_extras/nodes_video.py)、[PreviewVideo 输出](https://github.com/Comfy-Org/ComfyUI/blob/6b747c0428c343e1417219641db93a4fb7cb69ae/comfy_api/latest/_ui.py)与[官方教程](https://docs.comfy.org/tutorials/video/minimax/minimax-h3)。

实测范围与结果见公开 `docs/h3-validation.md`、`docs/h3-recipe.md` 和 `docs/agent-setup.md`。新增目标仍须单独验证依赖、哈希和生成能力，不能借用其他目标的结果。模型/核心全新安装与回退、画布往返、双宿主完整矩阵及用户视觉/音频质量验收继续保留；技术检查通过不代表用户采用。
