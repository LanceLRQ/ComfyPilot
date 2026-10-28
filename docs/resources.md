# 模块、配方与项目资源

日期：2026-10-28（Asia/Shanghai）。M2/2.1 已实现声明、显式发现、目标标准预检和不可变快照；M2/2.2 已加入 [H3 首发人物替换配方与环境说明](h3-recipe.md)。生成、素材上传和收片尚未接入。

## 目录与来源

平台只读取当前项目的 `modules/`、`workflows/`、`custom_nodes/` 和安装包预设。普通 JSON、说明、节点源码均可保存。`module.yaml`、`recipe.yaml`、`node.yaml` 分别识别为模块、配方、自定义节点声明；其他文件只作为原件，不导入 Python、不运行 JavaScript、不安装依赖。

来源示例为 `builtin:video-character-replacement/module.yaml` 和 `project:modules/my-module/recipe.yaml`。界面/MCP 展示来源、版本、内容哈希、说明、绑定与依赖。内置 H3 配方 ID 为 `minimax-h3-replacement`，配方、API 图、画布、上游原件、锁文件和说明随包交付；真实生成仍待后续执行链。

声明中的文件引用始终相对该来源根目录：项目声明使用 `modules/my-module/README.md` 等路径；内置声明相对预设根目录。不能使用绝对路径、`..`、反斜杠或跨来源引用。资源目录内不接受符号链接。隐藏文件、依赖环境、node_modules、__pycache__、pyc/tmp/bak 不收录。模型本体与媒体应放资源目录外。

单文件最多 5 MiB，一次刷新最多 1000 个文件、合计 32 MiB；解析后的目录最多 30 MiB。超过限制或文件不可读时明确报错，保留已有修订。资源字节写入当前项目 SQLite，不作为包内模板的可变引用。

## 声明契约

所有声明必填 `kind/id/name/version/description/instructions`，未知字段拒绝。ID 使用字母、数字、下划线、点、连字符；版本由作者声明，内容变化以 SHA-256 为准。

| 声明 | 额外字段 |
| --- | --- |
| Module，`kind: module` | `inputs`（槽位→video/image/text）、`outputs`（槽位→video/image）、`checks`、`feedback` |
| Recipe，`kind: recipe` | `module_id`、`input_order`、`inputs`、`parameters`、`outputs`；可选 `workflow`、`api_graph`、`custom_nodes`、`models` |
| CustomNode，`kind: node` | `source`、固定 `version`、`license`、`python_dependencies`、`system_dependencies`、`installation`、`verification`、`rollback`、`restart_required` |

Recipe 输入与输出的名称、类型必须匹配模块。输入必须完整列在 `input_order`，每个输入含 `kind` 与非空 `bindings`；绑定字段为 `node_id/input_name`。参数支持 integer/number/string/boolean，包含 `type/default/bindings`，可选 `enum/minimum/maximum`。每个输出包含 `node_id/key/kind`，其中 key 是后续 history 输出读取键。自定义节点 ID 引用对应 node 声明。模型声明包含 `name/source/license/node_id/input_name`，可选 `sha256`，绑定到图中的实际模型选择值；哈希声明不等于目标模型文件已校验。

例如以下是项目配方声明的结构，节点 ID、文件和依赖须替换为实际导出的资源，本例不是内置可运行配方：

```yaml
kind: recipe
id: my-recipe
module_id: my-module
name: 项目视频配方
version: "1"
description: 按项目实际工作流填写用途与限制
instructions: modules/my-module/README.md
workflow: modules/my-module/canvas.json
api_graph: modules/my-module/api.json
input_order: [video]
inputs:
  video:
    kind: video
    bindings: [{node_id: "实际输入节点ID", input_name: video}]
parameters:
  seed:
    type: integer
    default: 0
    minimum: 0
    bindings: [{node_id: "实际采样节点ID", input_name: seed}]
outputs:
  main: {node_id: "实际输出节点ID", key: videos, kind: video}
custom_nodes: []
models: []
```

画布原件可以独立入库。API 图必须是节点 ID 到 `class_type/inputs` 的映射，连线采用 `[上游节点ID, 输出序号]`；校验不存在的节点、循环、重复/缺失绑定及输出引用。平台不转换 widgets 数组或复杂子图。没有 API 图、说明不完整或图损坏时，资源仍可发现，但不能新建可执行方案。

## 显式刷新与冲突

1. `list_resources` 读取有效快照，初始资源修订为 0。启动服务、打开页面、枚举 MCP 工具不会扫描资源。
2. Agent 写入资源后，页面“刷新磁盘资源”或 MCP `refresh_resources(expected_revision)` 才接纳。无内容变化则复用原修订；并发旧修订返回 conflict。配置修订、资源修订、业务修订独立计数。
3. 同类型同 ID 的不同来源拒绝刷新。通过 `update_project` 或手改配置后 `refresh_project` 接纳 `resource_overrides`，再刷新资源：

```yaml
resource_overrides:
  recipe:my-recipe: project:workflows/replacement/recipe.yaml
```

映射值必须是发现的明确来源，界面显示被覆盖来源。不会按路径顺序、项目优先或文件时间静默覆盖。损坏声明与图作为错误资源保存；修复后再次刷新，不沿用旧图假装成功。

## 五种独立证据

| 状态 | 证据 |
| --- | --- |
| 已发现 | 显式扫描保存的文件与内容哈希 |
| 声明完整 | schema、文件、模块/节点声明、API 图和绑定校验通过 |
| 缺依赖/未知 | 指定目标缺节点、模型选项，或标准接口无法确认 |
| 目标预检通过 | 当前配置/实例/Worker 下，标准节点 schema 与图、参数、连线和输出节点相符 |
| 实测通过 | 必须来自后续真实生成与媒体检查；当前始终 `not_run` |

先选择目标并调用 `probe_target`，再 `preflight_recipe(recipe_id,target_id)`。预检不联网、不入队，使用刚采集的 `/object_info` 及连接快照；连接与节点目录证据最多 15 秒有效。过期后重新探测，不因心跳仍正常而沿用旧目录。预检记录按配方内容哈希和目标分别保存，查询会标记过期、配置/实例/Worker 变化；一个目标通过不修改其他目标。

本地标准预检不能执行节点的自定义 VALIDATE_INPUTS，不能证明已安装的节点提交版本、Python/系统依赖或模型文件哈希，也不能检查媒体实际内容。版本证据明确为 unknown。媒体输入当前只校验绑定槽位，最终路径与可读性在上传阶段验证。标准接口未列出的自定义字面类型保留 unknown。

V3 COMBO 按 options 处理；AUTOGROW 按声明前缀/名称与数量展开点分输入；DYNAMICCOMBO 只接纳选中分支的子输入并校验必填项。未覆盖的动态类型不推测。媒体绑定到 image_upload/video_upload 选择器时允许待上传文件名，仍标 pending；普通枚举不获得这种豁免。保存目录仅保留预检所需的类型、范围、选项与分支，不保存节点 UI 默认提示词。

## 冻结方案与运行

`create_plan` 的 recipe_hash 必须来自当前有效配方记录，完整传入输入与参数；required_nodes 与实际图类型集合完全一致。参数按配方 schema 校验，不能凭任意字符串哈希绕过。草稿冻结参数化图模板、输入哈希、配方 manifest、资源修订、关联文件内容哈希和 graph_hash。

`execute_plan` 复制草稿快照到 Run，不再读磁盘或新的安装包模板。资源刷新和配方覆盖不会改写旧 Run；按 `get_resource_file(resource_hash)` 始终读取该内容版本。内部领取再校验冻结图哈希和当前目标 schema，失败不预留额度。已有 intent 仍按原身份返回，不重发。旧方案没有资源快照时需创建新方案，历史数据保留。

当前 `materialization: pending` 表示图模板尚未替换成上传后的媒体路径；graph_hash 是模板哈希，后续提交链路需要另存最终执行图。当前 Worker 不消费生成队列、不调用 `/prompt`。

## 已实现接口与验证边界

| MCP 工具 | HTTP |
| --- | --- |
| list_resources | GET /api/resources |
| refresh_resources | POST /api/resources/refresh，expected_revision |
| get_resource_file | POST /api/resources/file，resource_hash |
| preflight_recipe | POST /api/resources/preflight，recipe_id、target_id |

均要求显式项目连接；写请求核对本机来源与实例。页面与 MCP 共用应用用例、修订和预检记录。原件按纯文本显示，不执行 HTML/节点代码。内容按哈希读取，不接受任意文件路径；二进制只返回元数据。资源修订与 blob 保留历史，本轮不做回收，M4 备份与资产治理需纳入其空间占用。

M2/2.1 用标准接口替身验证发现、冲突、图、依赖、过期、冻结、迁移与真实 stdio/HTTP 接口；M2/2.2 使用去除用户素材信息的实际 H3 节点 schema 验证新版输入。远程 ComfyUI 连接与监测已实测；本机 ComfyUI、完整人物替换生成、Linux 项目平台及 Claude Code/Codex 双宿主仍保留待实测。

协议依据：[官方路由](https://docs.comfy.org/development/comfyui-server/comms_routes)、[API 图示例](https://github.com/Comfy-Org/ComfyUI/blob/master/script_examples/basic_api_example.py)、[服务端验证实现](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_execution/validation.py)。`POST /prompt` 同时承担验证与入队，因此此处不将其作为无副作用预检。
