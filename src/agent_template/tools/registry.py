"""Turn ordinary python functions into tools the model can call.

A tool is a plain function: its type hints become the JSON Schema, its
docstring becomes the description. The registry validates incoming arguments,
runs the handler under a timeout, and converts failures into plain text so the
model can read the error and correct itself instead of crashing the process.

将普通 Python 函数转换为模型可调用的工具。
工具本质就是一个普通函数：函数的类型提示自动生成 JSON Schema，函数文档字符串作为工具描述。
工具注册器会校验入参，在超时限制下执行处理函数，并把异常转为纯文本，让模型能够读取错误信息并自行修正，避免整个进程崩溃。
"""

from __future__ import annotations

import asyncio
import inspect # 核心反射库：读取函数签名、参数名、docstring，用来自动生成工具描述和参数列表。
import json # 把 Pydantic schema 转成 JSON Schema，给到 LLM 的 tool definition。
import logging
from dataclasses import dataclass
from typing import Any, Callable, get_type_hints
# - `Callable[..., Any]`：任意可调用对象，入参任意，返回任意类型
# - `get_type_hints`：读取函数上的类型注解（比直接读 `__annotations__` 更完善，能解析前向引用）


from pydantic import BaseModel, ConfigDict, ValidationError, create_model
# - `create_model`：**动态创建 Pydantic 模型**，根据函数参数 + 类型注解，在运行时生成校验模型
# - `BaseModel`：数据模型基类
# - `ValidationError`：参数校验失败异常（模型传过来的工具参数不对时捕获）
# - `ConfigDict`：动态模型的配置项


from agent_template.llm.base import ToolSpec



logger = logging.getLogger("agent.tools") # 日志器，名字 `agent.tools`，专门打印工具注册、参数校验、执行异常、超时等日志。

Handler = Callable[..., Any]

class RawArguments(BaseModel):
    "用于由外部（MCP）定义的工具的通用参数模型。"
    model_config = ConfigDict(extra="allow") # 默认行为：`extra="forbid"`，传入未定义字段直接抛校验错误 ;`allow`允许传入模型中没用声明的字段
    # 原样接收 MCP 工具返回的任意参数字典，不做强校验，只做简单类型包装



@dataclass(slots=True) # 开启 `__slots__`，节省内存，实例属性固定，不允许随意新增实例字段。
class Tool:
    name: str
    description: str
    handler: Handler # 工具真正执行函数
    args_model: type[BaseModel]
    # Pydantic 模型类，用来校验模型传过来的工具入参
    # 本地 Python 工具：动态生成的 Pydantic 
    # 模型MCP 工具：`RawArguments`

    source: str = "local" # `local` or `mcp:<server>`
    schema_override: dict[str, Any] | None = None

    def spec(self) -> ToolSpec:
        """Describe this tool to the model"""
        if self.schema_override is not None:
            # 如果设置了 `schema_override`：直接使用这份手动写好的 schema，跳过自动生成。
            # 适合 MCP 场景或者需要自定义 schema 的场景。
            parameters = self.schema_override
        else:
            # 没有覆盖 schema：调用 `args_model.model_json_schema()`，
            # 从 Pydantic 模型自动生成 JSON Schema；
            # 并且**删掉顶层的 `title` 字段**（很多 LLM 网关不喜欢这个多余字段）。
            parameters = self.args_model.model_json_schema()
            parameters.pop("title", None)
        return ToolSpec(
            name=self.name, description=self.description, parameters=parameters
        )


class ToolRegistry:
    def __init__(self, *, timeout_s: float = 30.0) -> None:
        self.timeout_s = timeout_s
        self._tools: dict[str, Tool] = {}


    # -------------------------------------------------------------------- regiter
    def register(
            self,
            func: Handler | None = None,
            *,
            name: str | None = None,
            description: str | None = None,
    ):
        """ Decorator: @registry.register, with optional name/description override"""

        def decorate(fn: Handler) -> Handler:
            # name 参数是"改名"用的：不传才退回函数名。
            # args_model_for 也要用改名后的名字，否则生成的参数模型类名对不上。
            tool_name = name or fn.__name__
            self._tools[tool_name] = Tool(
                name=tool_name,
                description=description or first_line(fn.__doc__),
                handler=fn,
                args_model=args_model_for(tool_name, fn),
            )

            return fn

        return decorate(func)  if func is not None else decorate

    def add_described(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Handler,
        source: str = "external",
    ) -> None:
        """Register an already-described tool. This is the MCP bridge seam."""
        # """注册一个已经预先定义好描述的工具。这是对接 MCP 的适配层。"""
        self._tools[name] = Tool(
            name=name,
            description=description,
            handler=handler,
            args_model=RawArguments,
            source=source,
            schema_override=parameters,
        )

# ------------------------------------------------------------------------- query
    def specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool in self._tools.values()]

    def entries(self) -> list[Tool]:
        """返回全部工具对象。

        给 CLI 这类需要展示"来源"的场景用：specs() 是给模型看的，
        里面只有 name/description/parameters，没有 source。
        """
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> str:
        """Human-readable inventory, handy for CLI output and prompts."""
        return "\n".join(
            f"- {tool.name} ({tool.source}): {tool.description}"
            for tool in self._tools.values()
        )

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools    


# ------------------------------------------------------------------------- call
    async def call(self, name:str, arguments: dict[str, Any]) -> str:
        """执行工具。永远不抛出异常：所有失败都会转为文本返回给模型
        Run a tool. Never raises: failures come back as text for the model.
        """
        tool = self._tools.get(name)
        if tool is None:
            available = ",".join(self.names()) or "none"
            return f"Error: unknown tool `{name}`. Available tools: {available}"

        try:
            vailabled = tool.args_model.model_validate(arguments)
        except ValidationError as exc:
            return f"Error: invalid arguments for `{name}`: {exc.errors(include_url=False)}"


        kwargs = dict(vailabled)
        try:
            if inspect.iscoroutinefunction(tool.handler):
                result = await asyncio.wait_for(tool.handler(**kwargs), self.timeout_s)
            else:
                # Sync handlers run in a worker thread so the timeout still holds
                result = await asyncio.wait_for(
                    asyncio.to_thread(tool.handler, **kwargs), self.timeout_s
                )
        except asyncio.TimeoutError:
            return f"Error: `{name}` timed out after {self.timeout_s}s"
        except Exception as exc:  # noqa: BLE001 - report, do not crash the loop
            logger.warning("tool %s failed: %s", name, exc)
            return f"Error: `{name}` raised {type(exc).__name__}: {exc}"
        return as_text(result)


        
def first_line(doc: str | None) -> str:
    """First non-empty line of a docstring, used as the tool description."""
    for line in (doc or "").splitlines():
         stripped = line.strip()
         if stripped:
             return stripped
    return ""


def args_model_for(name: str, fn: Handler) -> type[BaseModel]:
    """Build a pydantic model from a function signature, for validation."""
    signature = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except:
        hints = {}

    fields: dict[str, Any] = {}
    for param_name, param in signature.parameters.items():
        # 跳过 *args、 **kwargs 可变参数
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        # 获取参数的类型注解， 无注解则兜底为str类型
        annotation = hints.get(param_name, param.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = str

        # 判断参数是否有默认值： ...代表必填字段
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[param_name] = (annotation, default)

    # 生成pydantic模型类名： 下划线转大驼峰 + arg后缀
    model_name = "".join(part.title() for part in name.split("_")) + "Args"
    # 动态创建并返回pydantic模型类
    return create_model(model_name, **fields)


def as_text(result: Any) -> str:
    """Tool output goes dack to the model as text"""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)
