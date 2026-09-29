# -*- coding: utf-8 -*-
"""statemachine —— 本地原生 Agent 框架的「最小可跑状态机内核」（v1）。

三个入口：
    from statemachine import WorkOrder, WorkOrderStateMachine, build_default_adapter
命令行：
    python -m statemachine.cli contract
    python -m statemachine.cli gates --workdir DIR --target f.py
    python -m statemachine.cli run --workdir DIR --target f.py --task "..." [--test test_f.py]

契约文档：``docs/工单状态机-契约-v1.md``
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
_COMPAT = os.path.join(_PROJECT, "compatibility")
for p in (_HERE, _COMPAT):
    if p not in sys.path:
        sys.path.insert(0, p)

from contract import (  # noqa: E402,F401
    FALLBACK_RULES, NODE_CHAIN, NODE_TOOLSETS, STATUS_DONE, STATUS_ESCALATED,
    STATUS_FAILED, STATUS_NEEDS_INPUT, STATUS_SECURITY_ABORT, GateIssue, GateResult,
    Step, WorkOrder, contract_summary, node_contract_table,
)
from engine import RunResult, WorkOrderStateMachine, check_contract_sync  # noqa: E402,F401
from gates import run_gate  # noqa: E402,F401
from tools import REGISTRY, ToolContext  # noqa: E402,F401

__all__ = [
    "WorkOrder", "WorkOrderStateMachine", "RunResult", "GateResult", "GateIssue", "Step",
    "NODE_CHAIN", "NODE_TOOLSETS", "FALLBACK_RULES", "contract_summary",
    "node_contract_table", "check_contract_sync", "REGISTRY", "ToolContext", "run_gate",
    "STATUS_DONE", "STATUS_ESCALATED", "STATUS_FAILED", "STATUS_NEEDS_INPUT",
    "STATUS_SECURITY_ABORT", "build_default_adapter",
]

__version__ = "0.1.0"

from adapter_factory import build_default_adapter  # noqa: E402
