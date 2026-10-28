import json
from graphlib import CycleError, TopologicalSorter
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

Name = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")]
Text = Annotated[str, Field(min_length=1, max_length=4096)]


def content_hash(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    kind: str
    id: Name
    name: Text
    version: Text
    description: Text
    instructions: Text


class Module(Declaration):
    kind: Literal["module"]
    inputs: dict[Name, Literal["video", "image", "text"]] = Field(min_length=1)
    outputs: dict[Name, Literal["video", "image"]] = Field(min_length=1)
    checks: list[Text] = Field(min_length=1)
    feedback: list[Text] = Field(min_length=1)


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: Text
    input_name: Text


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["video", "image", "text"]
    bindings: list[Binding] = Field(min_length=1)


class Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    type: Literal["integer", "number", "string", "boolean"]
    default: bool | int | float | str
    bindings: list[Binding] = Field(min_length=1)
    enum: list[bool | int | float | str] | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None


class MediaSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fps: float | None = Field(default=None, gt=0)
    frame_count: int | None = Field(default=None, gt=0)
    duration_seconds: float | None = Field(default=None, gt=0)


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: Text
    key: Name
    kind: Literal["video", "image"]
    filename_prefix: Binding | None = None
    spec: MediaSpec | None = None


class ModelDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Text
    source: Text
    license: Text
    node_id: Text
    input_name: Text
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None


class Recipe(Declaration):
    kind: Literal["recipe"]
    module_id: Name
    workflow: Text | None = None
    api_graph: Text | None = None
    input_order: list[Name]
    inputs: dict[Name, Input] = Field(min_length=1, max_length=32)
    parameters: dict[Name, Parameter] = Field(default_factory=dict, max_length=64)
    outputs: dict[Name, Output] = Field(min_length=1)
    custom_nodes: list[Name] = Field(default_factory=list)
    models: list[ModelDependency] = Field(default_factory=list)


class CustomNode(Declaration):
    kind: Literal["node"]
    source: Text
    license: Text
    python_dependencies: list[Text]
    system_dependencies: list[Text]
    installation: Text
    verification: Text
    rollback: Text
    restart_required: bool


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    class_type: Text
    inputs: dict[str, bool | int | float | str | list[str | int]]
    meta: dict = Field(default_factory=dict, alias="_meta", exclude=True)


def validate_parameter(parameter: Parameter, value) -> None:
    types = {"integer": (int,), "number": (int, float), "string": (str,), "boolean": (bool,)}
    if type(value) not in types[parameter.type]:
        raise ValueError("参数类型与配方声明不一致")
    if parameter.enum is not None and not any(
        type(value) is type(item) and value == item for item in parameter.enum
    ):
        raise ValueError("参数不在配方枚举范围内")
    if type(value) in (int, float) and (
        parameter.minimum is not None
        and value < parameter.minimum
        or parameter.maximum is not None
        and value > parameter.maximum
    ):
        raise ValueError("参数超出配方范围")


def validate_graph(recipe: Recipe, value: dict) -> dict:
    if not isinstance(value, dict) or not value or "nodes" in value or "links" in value:
        raise ValueError("需要独立的 API 图；画布原件不能直接执行")
    nodes = TypeAdapter(dict[str, GraphNode]).validate_python(value)
    graph = {}
    dependencies = {}
    for key, node in nodes.items():
        if not isinstance(key, str) or not key:
            raise ValueError("API 图节点 ID 必须是字符串")
        # ComfyUI 的 _meta 仅供画布说明，不影响可执行图。
        graph[key] = node.model_dump()
        dependencies[key] = []
        for item in node.inputs.values():
            if isinstance(item, list):
                if (
                    len(item) != 2
                    or not isinstance(item[0], str)
                    or type(item[1]) is not int
                    or item[1] < 0
                    or item[0] not in value
                ):
                    raise ValueError("API 图连线必须引用已有节点和非负输出序号")
                dependencies[key].append(item[0])
    try:
        tuple(TopologicalSorter(dependencies).static_order())
    except CycleError as error:
        raise ValueError("API 图含循环依赖") from error
    if len(set(recipe.input_order)) != len(recipe.inputs) or set(recipe.input_order) != set(
        recipe.inputs
    ):
        raise ValueError("input_order 必须完整且不重复地声明输入顺序")
    occupied = set()
    for declaration in [*recipe.inputs.values(), *recipe.parameters.values()]:
        if isinstance(declaration, Parameter):
            validate_parameter(declaration, declaration.default)
        for binding in declaration.bindings:
            pair = (binding.node_id, binding.input_name)
            if (
                pair in occupied
                or binding.node_id not in graph
                or binding.input_name not in graph[binding.node_id]["inputs"]
                or isinstance(graph[binding.node_id]["inputs"][binding.input_name], list)
            ):
                raise ValueError("输入或参数绑定不存在、重复或覆盖了连线")
            occupied.add(pair)
    if any(output.node_id not in graph for output in recipe.outputs.values()):
        raise ValueError("输出规则引用了不存在的节点")
    for output in recipe.outputs.values():
        binding = output.filename_prefix
        if binding:
            pair = (binding.node_id, binding.input_name)
            if (
                pair in occupied
                or binding.node_id not in graph
                or not isinstance(graph[binding.node_id]["inputs"].get(binding.input_name), str)
            ):
                raise ValueError("输出前缀绑定必须是未占用的字符串输入")
            occupied.add(pair)
    for model in recipe.models:
        if (
            model.node_id not in graph
            or graph[model.node_id]["inputs"].get(model.input_name) != model.name
        ):
            raise ValueError("模型声明必须绑定 API 图中的实际模型选择值")
    return graph
