# -*- coding: utf-8 -*-
"""contract.py —— 工单状态机的「宪法」（数据结构 + 节点契约 + 回退规则）。

这份文件的正文说明见 ``docs/工单状态机-契约-v1.md``。代码与文档必须同步改：
凡是契约里的字段、节点输入输出、回退规则，都在这里以数据形式写死，
引擎（engine.py）只允许按这里的数据行事，不允许自己发明流程。

v1 的硬边界（用户 2026-09-27 明确）：
  · 只针对「单文件 Python 修改 + 测试通过」这一个场景写死，不做通用化；
  · 五个节点固定为：读 → 改 → 语法 gate → 类型 gate → 测试 gate；
  · 写操作只允许落在工单声明的目标文件上（测试文件不在授权范围内，模型改不了测试来「作弊」）；
  · gate 是唯一真相源（R8）：模型说「已完成」不算数，只有三个 gate 全过才算完成。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ============================================================
# 节点链（v1 固定）
# ============================================================
NODE_CHAIN: tuple = ("recon", "edit", "gate_syntax", "gate_type", "gate_test")

NODE_LABEL_ZH: Dict[str, str] = {
    "recon": "读节点",
    "edit": "改节点",
    "gate_syntax": "语法 gate",
    "gate_type": "类型 gate",
    "gate_test": "测试 gate",
}

#: 非模型节点（程序化 gate）——不调用模型，只跑程序
PROGRAMMATIC_NODES: tuple = ("gate_syntax", "gate_type", "gate_test")
MODEL_NODES: tuple = ("recon", "edit")

# ============================================================
# R1 工具授权：每个节点的工具集（绝不暴露全集）
#   实测依据 E9（文件类节点看到 run_shell 会自己升级动作）、
#   E15（编辑节点同时给 apply_patch 与 write_file 会退化成整文件重写）。
# ============================================================
NODE_TOOLSETS: Dict[str, List[str]] = {
    "recon": ["read_file", "list_dir", "search_code"],       # 只读侦察
    "edit": ["read_file", "apply_patch", "search_code"],     # ★ 只给小改动，无 write_file
    "gate_syntax": [],
    "gate_type": [],
    "gate_test": [],
    # 非 v1 链上节点：预留给后续的 shell 节点，同时用于 R2 命令闸的回归测试
    "shell": ["run_shell"],
}

#: 写类工具。它们只能落在工单声明的目标文件上（见 WRITE_SCOPE）。
WRITE_TOOLS: tuple = ("apply_patch", "write_file")
WRITE_SCOPE: str = "target_only"

# ============================================================
# R3 / R5：节点策略（必须与 compatibility/mythos_core/config.py 一致）
# ============================================================
NODE_KIND: Dict[str, str] = {
    "recon": "slot",   # 关思考（E13：选工具/抽参数关思考不掉质量）
    "edit": "edit",    # ★ 开思考（E15：关思考会退化成整文件重写）
}
#: 修复轮用 repair（开思考，靠错误信息纠正）
REPAIR_NODE_KIND: str = "repair"

# ============================================================
# 回合预算（框架兜底，模型无权决定「跑多久」）
# ============================================================
MAX_RECON_TURNS: int = 3        # 读节点最多几轮模型调用
MAX_EDIT_TURNS: int = 3         # 改节点内最多几轮（含 E7：模型首轮只读不写的补救）
DEFAULT_MAX_REPAIR_ROUNDS: int = 3   # gate 失败最多回退几轮
VIOLATION_KILL_AFTER: int = 2   # 同一节点内结构性违规（越权工具）出现几次后终止整单
MAX_BEST_OF_N: int = 3

# ============================================================
# gate 失败回退规则（契约表；engine 按此执行，不另造逻辑）
# ============================================================
FALLBACK_RULES: Dict[str, Dict[str, Any]] = {
    "gate_syntax": {
        "goto": "edit",
        "node_kind": "repair",
        "feedback": "syntax",          # 把语法错误原文喂回改节点
        "reason": "语法错误几乎总是局部书写问题，退回改节点即可",
        "escalate_after": 2,           # 语法连续失败 2 次 → 先刷新上下文再改
        "escalate_to": "recon",
    },
    "gate_type": {
        "goto": "edit",
        "node_kind": "repair",
        "feedback": "type",
        "reason": "类型/名称问题多半在同一文件内可修",
        "escalate_after": 2,
        "escalate_to": "recon",
    },
    "gate_test": {
        "goto": "edit",
        "node_kind": "repair",
        "feedback": "test",
        "reason": "测试失败先退回改节点；若同样的失败签名重复出现，说明模型没抓住要点，"
                  "退出改节点、重新读一遍上下文（recon）再改",
        "escalate_after": 2,
        "escalate_to": "recon",
    },
}

# ============================================================
# 工单状态
# ============================================================
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"                    # 五节点全走通、三个 gate 全过、变更已原子落地
STATUS_NEEDS_INPUT = "needs_input"      # 模型反问（R10 空调用合法）→ 需要人补信息
STATUS_ESCALATED = "escalated"          # 修复轮用尽 → 降级人审
STATUS_FAILED = "failed"                # 确定性失败（上下文超限 / 传输层反复失败）
STATUS_SECURITY_ABORT = "security_abort"  # 触发安全边界（危险参数 / 反复越权）

FINAL_STATUSES: tuple = (STATUS_DONE, STATUS_NEEDS_INPUT, STATUS_ESCALATED,
                         STATUS_FAILED, STATUS_SECURITY_ABORT)


# ============================================================
# 数据结构
# ============================================================
@dataclass
class GateIssue:
    """gate 报出的单条问题。``line`` 允许为 0（无行号信息）。"""
    type: str
    message: str
    line: int = 0
    col: int = 0
    hint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "message": self.message,
                "line": self.line, "col": self.col, "hint": self.hint}


@dataclass
class GateResult:
    """一个 gate 的结果 —— 契约里规定的**通过/失败信号**。

    signal 只有两个取值：``"ok"`` / ``"fail"``。框架其它部件只允许看 ``ok``，
    不允许解析 ``raw`` 里的自然语言（那是给人看的证据，不是判断依据）。
    """
    gate: str
    ok: bool
    issues: List[GateIssue] = field(default_factory=list)
    sec: float = 0.0
    raw: Optional[Dict[str, Any]] = None
    error: str = ""

    @property
    def signal(self) -> str:
        return "ok" if self.ok else "fail"

    def to_dict(self) -> Dict[str, Any]:
        return {"gate": self.gate, "ok": self.ok, "signal": self.signal,
                "issues": [i.to_dict() for i in self.issues],
                "sec": round(self.sec, 3), "error": self.error,
                "raw": self.raw or {}}

    def feedback_text(self) -> str:
        """给模型看的失败原文（喂回改节点用）。"""
        if self.error:
            return "[%s] %s" % (self.gate, self.error)
        lines = ["[%s 未通过]" % self.gate]
        for i in self.issues[:8]:
            where = ("第 %d 行" % i.line) if i.line else "—"
            lines.append("- (%s, %s) %s%s" % (i.type, where, i.message,
                                              ("；建议：%s" % i.hint) if i.hint else ""))
        if len(self.issues) > 8:
            lines.append("- …另有 %d 条" % (len(self.issues) - 8))
        return "\n".join(lines)


@dataclass
class Step:
    """一次节点动作的历史记录（工单的「历史尝试记录」由一串 Step 组成）。"""
    node: str
    round_no: int
    turn: int
    kind: str                # adapter 的 FillResult.kind / 或 "tool" / "gate"
    ok: bool
    detail: str = ""
    tool: str = ""
    sec: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"node": self.node, "round": self.round_no, "turn": self.turn,
                "kind": self.kind, "ok": self.ok, "tool": self.tool,
                "detail": self.detail[:400], "sec": self.sec}


@dataclass
class WorkOrder:
    """工单 —— 状态机的唯一输入型数据结构。

    用户列的必备字段：目标文件、任务描述、当前节点、历史尝试记录、gate 结果。
    其余是 v1 必需的执行参数（沙箱根、测试文件、预算、产物）。
    """
    wo_id: str
    task: str                      # 任务描述（自然语言，给模型看）
    workdir: str                   # 沙箱根（绝对路径）
    target: str                    # 目标文件（相对 workdir）
    test_path: Optional[str] = None  # 测试文件（相对 workdir），测试 gate 用它
    test_cmd: Optional[List[str]] = None  # 覆盖默认测试命令（默认见 gates.py）
    node: str = NODE_CHAIN[0]      # 当前节点
    round_no: int = 0              # 修复轮次（gate 失败回退的次数）
    status: str = STATUS_PENDING
    history: List[Step] = field(default_factory=list)
    gate_results: List[Dict[str, Any]] = field(default_factory=list)
    violations: List[Dict[str, Any]] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    constraints: Dict[str, Any] = field(default_factory=lambda: {
        "max_repair_rounds": DEFAULT_MAX_REPAIR_ROUNDS,
        "best_of_n": 1,
        "write_scope": WRITE_SCOPE,
        "allow_git_commit": False,
    })
    created_at: float = field(default_factory=time.time)

    # ---------------- 便捷视图 ----------------
    def budget(self, key: str, default: Any = None) -> Any:
        return self.constraints.get(key, default)

    def add_step(self, step: Step) -> Step:
        self.history.append(step)
        return step

    def add_gate(self, g: GateResult) -> Dict[str, Any]:
        d = g.to_dict()
        d["round"] = self.round_no
        self.gate_results.append(d)
        return d

    def add_violation(self, node: str, rule: str, detail: str) -> Dict[str, Any]:
        v = {"node": node, "rule": rule, "detail": detail, "round": self.round_no,
             "ts": time.time()}
        self.violations.append(v)
        return v

    def last_gate(self, name: str) -> Optional[Dict[str, Any]]:
        for d in reversed(self.gate_results):
            if d.get("gate") == name:
                return d
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "wo_id": self.wo_id, "task": self.task, "workdir": self.workdir,
            "target": self.target, "test_path": self.test_path,
            "node": self.node, "round": self.round_no, "status": self.status,
            "constraints": self.constraints,
            "history": [s.to_dict() for s in self.history],
            "gate_results": self.gate_results,
            "violations": self.violations,
            "questions": self.questions,
            "artifacts": self.artifacts,
            "created_at": self.created_at,
        }


# ============================================================
# 节点契约表（给 CLI / 文档 / 验证脚本用的机器可读版本）
# ============================================================
def node_contract_table() -> List[Dict[str, Any]]:
    """五节点的输入 / 输出 / 通过信号 —— 契约的机器可读视图。"""
    return [
        {
            "node": "recon", "label": NODE_LABEL_ZH["recon"], "kind": "model",
            "node_type": "recon", "node_kind": NODE_KIND["recon"],
            "tools": NODE_TOOLSETS["recon"],
            "input": ["工单任务描述", "沙箱目录清单（框架注入）", "目标文件源码（框架确定性读取）"],
            "output": "ContextBundle{target_source, search_hits, notes}",
            "pass_signal": "拿到 ContextBundle（模型可 no_call 结束；空调用合法 R10）",
        },
        {
            "node": "edit", "label": NODE_LABEL_ZH["edit"], "kind": "model",
            "node_type": "edit", "node_kind": NODE_KIND["edit"],
            "tools": NODE_TOOLSETS["edit"],
            "input": ["ContextBundle", "gate 失败反馈（修复轮）", "补丁格式说明"],
            "output": "Patch{path, mode, diff, new_source}",
            "pass_signal": "在暂存区成功应用一次最小补丁（写范围 = 目标文件）",
        },
        {
            "node": "gate_syntax", "label": NODE_LABEL_ZH["gate_syntax"], "kind": "program",
            "input": ["暂存区目标文件"],
            "output": "GateResult{gate, ok, signal, issues[], sec}",
            "pass_signal": "signal=='ok'（ast.parse 通过）",
        },
        {
            "node": "gate_type", "label": NODE_LABEL_ZH["gate_type"], "kind": "program",
            "input": ["暂存区目标文件"],
            "output": "GateResult{gate, ok, signal, issues[], sec}",
            "pass_signal": "signal=='ok'（注解完备 + 名称解析全通过）",
        },
        {
            "node": "gate_test", "label": NODE_LABEL_ZH["gate_test"], "kind": "program",
            "input": ["暂存区整个目录", "测试命令"],
            "output": "GateResult{gate, ok, signal, issues[], raw{rc, tests_run, failures}}",
            "pass_signal": "signal=='ok'（测试进程返回 0）",
        },
    ]


def contract_summary() -> Dict[str, Any]:
    return {
        "node_chain": list(NODE_CHAIN),
        "nodes": node_contract_table(),
        "fallback_rules": FALLBACK_RULES,
        "budgets": {
            "max_recon_turns": MAX_RECON_TURNS,
            "max_edit_turns": MAX_EDIT_TURNS,
            "max_repair_rounds": DEFAULT_MAX_REPAIR_ROUNDS,
            "violation_kill_after": VIOLATION_KILL_AFTER,
        },
        "write_scope": WRITE_SCOPE,
        "truth_source": "gate（模型自评不入决策，R8）",
    }
