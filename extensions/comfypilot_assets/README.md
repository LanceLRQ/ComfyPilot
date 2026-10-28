# ComfyPilot 素材桥接

将整个目录复制至用户指定的 ComfyUI custom_nodes 下，空闲时按既有方式重启。

入口只注册素材路由，不增加生成节点。protocol=1；通过 `/comfypilot/v1/capabilities` 验证。依赖 ComfyUI 已有 aiohttp 和标准库，无新增 Python 包或模型。

文件限定在 ComfyUI input/output 的 ComfyPilot/<项目UUID> 子目录；上传校验SHA-256，下载需要源指纹，已知副本保存在项目input子目录台账。不提供shell、任意路径或删除接口。

完整配置、安装、验证与回退见仓库 docs/remote-assets.md。卸载时在任务结束后把此目录移到 custom_nodes 外备份，再重启；不要删除用户素材目录。
