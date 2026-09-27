"""零依赖的小工具集：足以验证函数调用这条链路是通的。

这两个工具同时是"写工具"的最小样板：
    * 只依赖标准库；
    * 参数用类型注解声明，登记表会自动把它转成 JSON Schema；
    * docstring 会**直接成为模型看到的工具描述**，所以要写清能做什么、
      参数有什么要求——它的措辞比 schema 本身更影响模型的调用质量；
    * 出错时返回错误文本而不是抛异常，让模型有机会自己改参数重试。
"""

from __future__ import annotations

import ast # 抽象语法树，安全解析数学表达式 `ast` 模块可以把一段**字符串形式的 Python 代码**，解析成一棵抽象语法树（AST），也就是把代码拆成一个个节点对象。
import operator # 内置运算符函数
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError # 时区支持

_OPS = {
    ast.Add: operator.add, # +
    ast.Sub: operator.sub, # -
    ast.Mult: operator.mul, # *
    ast.Div: operator.truediv, # /
    ast.FloorDiv: operator.floordiv, # //
    ast.Mod: operator.mod, # % 取模
    ast.Pow: operator.pow, # ** 幂
    ast.USub: operator.neg, # 一元负号 -5
    ast.UAdd: operator.pos, # 一元正号 +5
}


def get_current_time(timezone: str = "Asia/Shanghai") -> str:
    """返回指定 IANA 时区的当前日期和时间。"""
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        # Windows系统本身没有内置IANA时区数据库；如果没有安装tzdata包，
        # 所有时区查询都会失败。降级使用本机本地时钟并告知用户，
        # 而不是直接报错中断Agent的执行步骤。
        local = datetime.now().astimezone()
        offset = local.strftime("%z")
        pretty = f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "local"
        return (
            f"{local.strftime('%Y-%m-%d %H:%M:%S')} "
            f"({pretty}, local clock; unknown timezone `{timezone}`)"
        )
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")



def calculator(expression: str) -> str:
    """计算一个基础算术表达式，支持 + - * / // % ** 和括号。"""
    try:
        tree = ast.parse(expression, mode="eval")
        # ast.parse 在解析字符串的时候，就已经按照 Python 的语法规则（运算符优先级、括号），建好 AST 树结构了。
        # expression = "3 + 4 *2
        # Expression(
        #   body=BinOp(
        #     left=Constant(value=3),
        #     op=Add(),
        #     right=BinOp(
        #       left=Constant(value=4),
        #       op=Mult(),
        #       right=Constant(value=2)
        #     )
        #   )
        # )
    except SyntaxError as exc:
        return f"Error: cannot parse `{expression}` : {exc}"
    try:
        # 递归遍历AST求值，并转为字符串返回
        return str(_evaluate(tree.body))
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        return f"Error: {type(exc).__name__}: {exc}"



def _evaluate(node: ast.AST) -> float:
    """递归求值，只放行出现在 _OPS 白名单里的节点。

    白名单是这里的安全边界：不在表内的节点类型一律抛 ValueError，
    所以 `__import__("os").system("...")` 这类表达式在求值之前就被拦下，
    永远走不到执行那一步。这也是不用 eval() 的原因。
    """
    # 如果是int/float常量，直接返回数值
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    # 二元运算，递归计算左右操作数，执行对应运算
    if isinstance(node, ast.BinOp):
        return _OPS[type(node.op)](_evaluate(node.left), _evaluate(node.right))
    # 一元运算（负号/正号）
    if isinstance(node, ast.UnaryOp):
        return _OPS[type(node.op)](_evaluate(node.operand))
    # 任何不在白名单的节点，直接抛出不支持异常
    raise ValueError(f"unsupported expression element: {type(node).__name__}")
