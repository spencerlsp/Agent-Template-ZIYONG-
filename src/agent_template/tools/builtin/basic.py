"""Small dependency-free tools. Enough to prove function calling works"""

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
    """Return the current date and time in the given IANA timezone."""
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
    """Evaluate a basic arithmetic experssion: + - * / // % ** and parentheses"""
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
    """Evaluate a parsed expression, allowing only arithmetic nodes"""
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