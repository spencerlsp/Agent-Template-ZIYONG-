"""审批决策源的测试：三条分支各自的默认行为。

这里守的是"无人确认时不能瞎执行"——所以非交互式那条分支必须被钉住。
"""

from __future__ import annotations

import asyncio
import sys

from agent_template.agent.approval import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalResult,
)
from agent_template.cli import Options, make_approval_handler
from agent_template.llm.base import ToolCall


def sample_request() -> ApprovalRequest:
    return ApprovalRequest(
        call=ToolCall(id="c0", name="write_thing", arguments={"path": "a.txt"}),
        session_id="s1",
        step=0,
    )


def run_handler(handler, request: ApprovalRequest) -> ApprovalResult:
    """跑一次决策源。包一层，省得每个用例都写 asyncio.run。"""
    return asyncio.run(handler(request))


def test_yes_flag_approves_everything() -> None:
    """--yes 是唯一一条"无人确认也执行"的路径，给脚本和 CI 用。"""
    handler = make_approval_handler(Options(assume_yes=True))

    assert run_handler(handler, sample_request()).approved is True


def test_non_interactive_defaults_to_reject(monkeypatch) -> None:
    """管道 / CI 里没有人能回答 → 必须拒绝，而不是挂住或瞎批准。"""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    handler = make_approval_handler(Options())

    result = run_handler(handler, sample_request())

    assert result.decision is ApprovalDecision.REJECTED
    assert result.reason          # 得说清为什么拒绝，否则用户一头雾水


def test_interactive_yes_approves_without_asking_a_reason(monkeypatch) -> None:
    """回答 y 就批准，而且**不该**再追问理由。"""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    handler = make_approval_handler(Options())

    prompts: list[str] = []

    def fake_ask(prompt, *args, **kwargs):
        prompts.append(str(prompt))
        return "y"

    monkeypatch.setattr("agent_template.cli.Prompt.ask", fake_ask)

    assert run_handler(handler, sample_request()).approved is True
    assert len(prompts) == 1, "批准时只该问一次，不该再追问理由"


def test_interactive_no_captures_the_reason(monkeypatch) -> None:
    """回答 n 之后要追问理由，并把理由带回给调用方。"""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    handler = make_approval_handler(Options())

    answers = iter(["n", "这份笔记还在用"])
    monkeypatch.setattr("agent_template.cli.Prompt.ask", lambda *a, **k: next(answers))

    result = run_handler(handler, sample_request())

    assert result.decision is ApprovalDecision.REJECTED
    assert result.reason == "这份笔记还在用"


def test_interactive_reason_is_optional_but_still_rejects(monkeypatch) -> None:
    """理由可以回车跳过——但跳过不等于批准。

    这条守的是"跳过理由"这条最常见的拒绝路径：它必须仍然被当成拒绝。
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    handler = make_approval_handler(Options())

    answers = iter(["n", ""])
    monkeypatch.setattr("agent_template.cli.Prompt.ask", lambda *a, **k: next(answers))

    result = run_handler(handler, sample_request())

    assert result.decision is ApprovalDecision.REJECTED
    assert result.reason == ""
