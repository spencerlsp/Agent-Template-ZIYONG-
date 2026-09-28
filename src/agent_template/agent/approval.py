"""工具审批：把"这个操作要不要执行"的决定权交给人。

为什么单独一个模块：审批牵涉三方——

    * 循环（发起询问）：只需要一行 `await self._approve(request)`
    * 决策源（真的去问人）：CLI 用终端提问，测试用假实现，将来 API 用挂起/恢复
    * 决定本身的语义：批准、拒绝，以及将来的超时、记住本次会话

三者放在一起，循环里就不用知道"人是被谁问的"。

三种实现形态（选型见 docs）：
    A. 注入回调：循环 await 一个函数，函数里问人 —— 适合 CLI（当前选择）
    B. 两阶段：循环存下待审批状态并结束本轮，答复是下一次请求 —— 适合普通 HTTP
    C. 长连接：决策来自同一条 WebSocket 上的另一条消息 —— 适合实时 Web UI

现在实现 A，但接口按 B 的信息需求设计（见 ApprovalRequest 的字段），
将来换实现时循环那部分不用改。
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable


from agent_template.llm.base import ToolCall

class ApprovalDecision(str, Enum):
    """审批结果。

    用枚举而不是布尔值，是刻意的：很快就会多出
    TIMEOUT（人不在）、APPROVED_FOR_SESSION（本次会话内不再问）这些状态。
    布尔值是最容易后悔的接口，而枚举能加成员、能序列化（继承 str 是为了直接进 JSON）。
    """
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(slots=True)
class ApprovalRequest:
    """一次待审批的请求。

    除了调用本身，还带上 session_id 和 step——现在用不到，但**留着是为了将来换
    两阶段方案**：那时要靠这两个字段把挂起状态存下来，并在下一次请求里恢复。
    接口现在设计对，将来就不用重构。
    """

    call: ToolCall
    session_id: str
    step: int

@dataclass(slots=True)
class ApprovalResult:
    """一次审批的结论：决定 + 理由。

    为什么不用 (decision, reason) 元组：元组要求每个调用点都记得解包，少写一次
    不会报错——`decision = await ...` 拿到的是整个元组，而元组永远不等于
    `ApprovalDecision.APPROVED`，于是"批准"会静默失效。dataclass 没有这个问题。
    """

    decision: ApprovalDecision
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.decision is ApprovalDecision.APPROVED
    
# 决策源的类型，给他一个请求，他给出一个决定（异步， 因为问人可能很慢）
ApprovalHandler = Callable[[ApprovalRequest], Awaitable[ApprovalResult]]

async def approve_all(request: ApprovalRequest) -> ApprovalResult:
    """默认决策源：全部批准。

    这是刻意的默认值——**不注入审批时，行为与之前完全一致**，
    现有的测试、demo、脚本都不受影响。想开启审批才显式注入。
    这也符合"新增的能力要能被关掉"这条原则。
    """
    return ApprovalResult(ApprovalDecision.APPROVED)

def rejected_message(call: ToolCall, reason: str = "") -> str:
    """工具被人工拒绝时，回给模型的那段话。

    这段文案不是"随便写写"——它直接决定模型接下来的行为，每句话都有用意：

    * "已被人工拒绝"：说清性质。不写这句，模型会以为是自己参数写错了，
      然后换个参数重试。
    * "请不要重试"：明确禁止重试。
    * "也不要换别的方式完成同一件事"：**堵住绕道**。写文件被拒之后，模型完全
      可能去调"执行命令"来达到同样目的——那样审批就形同虚设了。
    * 最后给两条出路：说明为什么需要这个权限，或者提出替代方案。
      给模型一个"体面的下一步"，它才不会继续撞墙。
    """
    arguments = json.dumps(call.arguments, ensure_ascii=False)
    lines = [
        f"操作已被人工拒绝：`{call.name}({arguments})`。",
        "这是用户的决定，不是技术故障——请不要重试，也不要换别的方式完成同一件事。",
    ]
    if reason:
        lines.append(f"用户给出的理由是：{reason}")
        lines.append("请据此调整做法，不要试图绕过这条理由。")
    lines.append("你可以向用户说明为什么需要这项权限，或者提出不需要它的替代方案。")
    return "\n".join(lines)