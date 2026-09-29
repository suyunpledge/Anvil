# -*- coding: utf-8 -*-
"""types.py —— 结果载体（模型无关层）。

``fill_slot`` / ``ask`` 都返回 :class:`FillResult`。它承载「这次调用到底发生了什么」，
kind 取值是 R12 钉死的失败分类全集。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# R12：失败分类全集（外加 no_call / tool_call 两个成功分支）
KINDS = (
    "tool_call",          # 成功：拿到已规范化 + 已过安全闸的调用
    "no_call",            # 成功：空 tool_calls（反问 / 无需工具）——不是失败
    "schema_violation",   # 失败：参数不合 schema / 调用了不存在的工具（格式噪声）
    "unauthorized_tool",  # 失败：调用了「在 registry 里、但不在本节点授权集」的工具（越权/安全事件）
    "unsafe_argument",    # 失败：危险参数命中，判死
    "context_overflow",   # 失败：上下文超限
    "transport_error",    # 失败：出网 / 解码层
    "exhausted",          # 失败：best-of-N 用尽仍无结果
)


class FillResult:
    """一次槽位填充的返回值 —— 框架侧唯一结果载体。

    成功与否看 ``ok``；具体形态看 ``kind``：

    ================  ======  ==========================================
    kind              ok      含义 / 框架应对
    ================  ======  ==========================================
    tool_call         True    ``calls`` 已规范化且过安全闸，可直接执行
    no_call           True    空 tool_calls，交回框架决定反问 / 无需工具（R10）
    schema_violation  False   参数不合法或调了不存在的工具（格式噪声），可换温度重试
    unauthorized_tool False   越权：调了「在 registry 里、不在本节点授权集」的工具（安全事件，判死）
    unsafe_argument   False   危险参数，判死，不回炉（R2）
    context_overflow  False   上下文超限，应走检索精简（R5）
    transport_error   False   出网 / 解码失败
    exhausted         False   best-of-N 用尽
    ================  ======  ==========================================
    """

    def __init__(
        self,
        node: str,
        ok: bool,
        kind: str,
        calls: Optional[List[Dict[str, Any]]] = None,
        content: str = "",
        thinking: str = "",
        error: Optional[str] = None,
        sec: Optional[float] = None,
        attempts: int = 1,
        temp: Optional[float] = None,
        ctx: Optional[int] = None,
    ) -> None:
        self.node = node
        self.ok = ok
        self.kind = kind
        self.calls = calls or []
        self.content = content or ""
        self.thinking = thinking or ""
        self.error = error
        self.sec = sec
        self.attempts = attempts
        self.temp = temp
        self.ctx = ctx

    def to_dict(self) -> Dict[str, Any]:
        """可 JSON 序列化的摘要（正文只留长度，避免把大段代码塞进日志）。"""
        return {
            "node": self.node, "ok": self.ok, "kind": self.kind,
            "calls": self.calls,
            "content_len": len(self.content), "thinking_len": len(self.thinking),
            "error": self.error, "sec": self.sec,
            "attempts": self.attempts, "temp": self.temp, "ctx": self.ctx,
        }

    def __repr__(self) -> str:
        return "<FillResult %s %s kind=%s calls=%d%s>" % (
            self.node, "OK" if self.ok else "FAIL", self.kind, len(self.calls),
            "" if not self.error else " err=%s" % str(self.error)[:60],
        )
