# 安装、兼容与升级回退

当前产物为开发构建 `comfypilot-0.1.0.dev0-py3-none-any.whl`，尚未发布正式版。版本号相同的开发 wheel 以 Git 提交和 SHA-256 区分，保留原 wheel、锁文件及私有项目备份。

## 构建与干净安装

开发环境执行 make setup、make check、make package。dist/ 同时输出 wheel 和 requirements-runtime.txt；后者由 uv.lock 导出，含所有运行依赖的精确版本、平台条件及分发哈希，不含 pytest/aiohttp 等测试依赖。改依赖后重新导出 packages/distribution/requirements-runtime.txt，检查命令会校验其与 uv.lock 一致。

在源码目录之外使用 Python 3.12 安装，替换所有示例路径：

```sh
uv venv --python 3.12 /absolute/comfypilot-env
uv pip install --python /absolute/comfypilot-env/bin/python --require-hashes -r /absolute/dist/requirements-runtime.txt
uv pip install --python /absolute/comfypilot-env/bin/python --no-deps /absolute/dist/comfypilot-0.1.0.dev0-py3-none-any.whl
/absolute/comfypilot-env/bin/comfypilot distribution-path
/absolute/comfypilot-env/bin/comfypilot skill-path
/absolute/comfypilot-env/bin/comfypilot bridge-path
```

distribution-path 仅读取路径，不建项目、不联网。安装版目录含锁文件、protocols.json、本说明及 MIT LICENSE。服务、Worker、Web、stdio MCP、平台 Skill/参考、内置 H3 模块与配方、素材桥接都在 wheel 内，安装运行无需 Node.js。模型、用户媒体、项目配置及运行状态不在包中；模型/节点许可另按配方说明核对。

将完整 Skill 安装到用户所选宿主的发现目录，注册待机 comfypilot-mcp，按[Agent 接入](agent-setup.md)验证。用户提出创作请求后，由 Agent 显式指定项目根，初始化/启动/连接平台。无需手动启动服务；安装及工具枚举本身不启动或连接平台。无需账号或平台密钥；[LAN 审片](lan-review.md)必须显式配置并重启。

## 协议与兼容范围

| 项目 | 当前值与实际范围 |
| --- | --- |
| Python / 系统 | 最低 3.12；macOS arm64 / 3.12.13、Linux x86_64 / 3.12.3 源码外安装与 API/Worker/Web/stdio MCP 生命周期已验；其他组合不承诺 |
| 进程管理 | POSIX 文件锁/会话；Windows 未支持，不以 pure-Python wheel 名称推断可运行 |
| 运行依赖 | uv.lock 与随包 requirements-runtime.txt；FastAPI 0.162.0、Uvicorn 0.54.0、Pydantic 2.14.1、PyYAML 6.0.3、psutil 7.2.2、MCP SDK 2.3.0、httpx2 2.16.0、websockets 17.3；传递依赖同样锁定 |
| 项目 / 数据库 / 备份 | 配置与身份 schema=1；SQLite user_version=14；目录备份 format=1。SQLite 只接受已识别的本机文件系统 |
| 媒体工具 | 平台 PATH 需要 FFmpeg/ffprobe，视频处理需 libx264；macOS FFmpeg 8.1.1、Linux FFmpeg 6.1.1 已验，缺项有阻塞原因；H.264 MP4 为浏览器基准 |
| ComfyUI / 生成配方 | 已验 ComfyUI 0.38.0 / RTX 3090 的 H3 人物替换；模型、节点与固定依赖见 h3-recipe.md，生成证据见 h3-validation.md；不是任意节点/工作流兼容声明 |
| 素材桥接 | protocol=1；upload/download/stat/known 为传输基线；清理另需 delete。2026-10-29 已完成既有生产桥接升级、原容器重启和两新项目受限删除；原件与既有媒体哈希保持，详见 remote-assets.md |
| 宿主 | Codex 0.164.0、Claude Code 2.1.302 / GLM-5.3 的 macOS 双宿主非交互发现、待机、目录、连接与生命周期已补验；首次信任/人工批准、交互式退出及 Linux 真实双宿主路径保留，见 agent-setup.md |
| 本机与远程 | 两种部署已完成真实生成/收片；当前两具名执行端同一 GPU 地址，独立 ComfyUI 实例切换仍保留 |

FFmpeg 版本以验收记录为准，系统工具不由 Python lock 安装。GPU 温度/利用率、CPU/远端磁盘、完整日志或环境管理没有采集/管理通道时为 unsupported。标准 HTTP 不保证通用视频传输或物理清理；[远程桥接](remote-assets.md)只通过用户已指定通道安装，不自动降级 SSH。平台只读项目资源，不导入自定义节点源码。

## 升级与回退

1. 回读运行/收片状态，等待已提交工作完成或明确处理其证据。停止项目平台，保留原 wheel 与原工具环境；不要管理宿主持有的 MCP 进程。
2. 用当前版本可用的导出功能保存校验备份，并单独保管本机秘密文件；旧版没有导出时，在平台停止后完整复制整个项目目录作私有备份，保留权限。先验证备份可读，再升级。秘密不随公开导出传播。
3. 在新的独立工具环境用随包锁文件安装新 wheel，不覆盖原环境。先对备份的独立恢复副本验证 CLI、Web、Worker、待机 MCP、Skill 和版本，再让所选宿主重新加载安装路径；项目连接必须重新建立。
4. 新程序加载项目时可能增量升级数据库。较旧程序遇到较新格式会拒绝启动；不支持逆向迁移。回退使用原环境及升级前完整项目副本，先停止新服务、归档新目录；不能把旧 wheel 指向已经升级的数据库，也不能并行运行两个具有相同 UUID 的副本。
5. 新版 export/restore 生成新 UUID、暂停、禁用目标，不继承旧远端所有权；该恢复不等于把已升级库降级。用户需要接续时按当前版本重新预检和明确提出方案，不能恢复后自动重放。

ComfyUI 扩展与平台升级分别处理：先核对外部队列空闲及原启动方式，保存既有扩展，再换入 bridge-path 目录并重启 ComfyUI、验证 capabilities。只有 HTTP 地址时不能安装。既有生产桥接已补验升级与受限删除；其他部署仍须逐一核对，不能由此推断均已升级。失败时移出本次扩展并恢复原文件/启动方式，不删除用户素材。

M4 交付不自动上传 PyPI、不创建公开 Release、不推送 Git。首版完整发布判定由逐条 AC 验收及保留项决定。
