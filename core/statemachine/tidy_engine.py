# -*- coding: utf-8 -*-
"""tidy_engine.py —— 文件整理状态机的内核（scan → plan → check_gate → apply → verify_gate）。

与代码工单（engine.py）的关系：共用三层——画像、节点授权、回退循环——
但**不共用工具集**：整理节点只拿到「看一眼清单」的能力，没有任何删除/写入工具；
移动动作由框架执行（`tidy.apply_plan`），模型只输出一份**方案 JSON**。

这是有意为之的：让模型碰不到文件系统，"想干坏事"就没有着力点。
模型唯一的输出是一段文本，由方案闸逐条审过、再由框架用零覆盖的方式执行。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
_COMPAT = os.path.join(_PROJECT, "compatibility")
for _p in (_HERE, _COMPAT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from contract import (STATUS_DONE, STATUS_ESCALATED, STATUS_FAILED, STATUS_NEEDS_INPUT,
                      STATUS_SECURITY_ABORT, Step, WorkOrder)
import tidy
from tidy import TidyOrder, apply_plan, check_plan, normalize_plan, parse_plan, scan_root

#: 整理节点的系统提示（模型只出方案，不碰文件）
PLAN_SYSTEM = """你是一个文件整理方案生成器。你看得到一份文件清单，需要输出一份**移动方案**。

输出格式（必须是合法 JSON，不要写任何解释文字，不要写代码块围栏外的内容）：
{"moves": [{"action": "move", "src": "原文件名", "dst": "目标目录/新文件名", "reason": "为什么"}]}

硬规则：
1) 默认只允许 move 动作；删除（delete/remove）仅在工单声明 allow_delete=true 时放行，
   且必走「人工确认 + 自动备份」，否则一律作废；
2) src 必须是清单里**逐字出现**的文件名（含扩展名），不要自己编路径、不要加前缀；
3) dst 必须是「目录/文件名」，目录只能从允许的目标目录里选；
4) 一个文件只能出现一次；
5) 不要给已经放好的文件再排动作；
6) 不确定的文件就**不要动它**——不动作永远比动错安全。
"""


@dataclass
class TidyResult:
    status: str
    order: TidyOrder
    plan: Dict[str, Any] = field(default_factory=dict)
    applied: Dict[str, Any] = field(default_factory=dict)
    gates: List[Dict[str, Any]] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""
    runlog_path: str = ""
    elapsed: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "order": self.order.to_dict(), "plan": self.plan,
                "applied": self.applied, "gates": self.gates, "history": self.history,
                "error": self.error, "runlog_path": self.runlog_path, "elapsed": self.elapsed,
                "semantic_notes": getattr(self, "semantic_notes", {})}


class TidyStateMachine:
    """文件整理：模型只出方案，框架执行，闸在前后各一道。"""

    def __init__(self, adapter: Any, order: TidyOrder, runlog_dir: Optional[str] = None,
                 verbose: bool = True, on_event=None, semantic: bool = False,
                 category_hints: Optional[Dict[str, str]] = None) -> None:
        self.ad = adapter
        self.order = order
        self.runlog_dir = runlog_dir or os.path.join(_PROJECT, ".sm-runs")
        self.verbose = verbose
        self.on_event = on_event
        #: 是否用本地嵌入给「按内容归类」的建议（零外网；失败则静默不提供）
        self.semantic = bool(semantic)
        #: 类别 → 描述（语义建议的候选集，也当作对模型的提示）
        self.category_hints = dict(category_hints or {})
        self.semantic_notes: Dict[str, Any] = {}
        self.events: List[str] = []
        self.history: List[Dict[str, Any]] = []
        self.gates: List[Dict[str, Any]] = []

    # ---------------- 基础设施 ----------------
    def _log(self, msg: str) -> None:
        self.events.append(msg)
        if self.verbose:
            print("[tidy] " + msg, flush=True)
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                pass

    def _step(self, node: str, round_no: int, kind: str, ok: bool, detail: str = "") -> None:
        self.history.append({"node": node, "round": round_no, "kind": kind, "ok": ok,
                             "detail": detail[:300]})

    def _gate(self, g) -> None:
        d = g.to_dict()
        self.gates.append(d)
        try:
            self.ad.record(node=g.gate, tool_name="gate", ok=g.ok, sec=g.sec)
        except Exception:
            pass

    # ---------------- 语义辅助（本地嵌入，零外网） ----------------
    def _semantic_suggest(self, scan: Dict[str, Any], root: str) -> str:
        """给模型一份「按内容」的参考建议。失败就不提供，不阻断流程。"""
        if not self.semantic or not self.category_hints:
            return ""
        try:
            import semantic_tidy as st
        except Exception:
            return ""
        files = [{"name": f["name"], "path": os.path.join(root, f["name"])}
                 for f in scan.get("files", [])]
        got = st.suggest_categories(files, self.category_hints)
        if not got:
            return ""
        self.semantic_notes = got
        lines = []
        for name, r in got.items():
            if r.get("suggest"):
                lines.append("- %s → %s（相似度 %.3f）" % (name, r["suggest"], r["score"]))
            else:
                lines.append("- %s → 不确定（%s）" % (name, r.get("reason", "")))
        self._log("语义建议（本地嵌入，零外网）：%d 个文件" % len(lines))
        return ("# 按内容相似度给出的参考建议（本地嵌入计算，仅供参考）\n"
                + "\n".join(lines)
                + "\n注意：这只是参考。若与文件名/扩展名明显矛盾，以你能看到的清单为准。")

    # ---------------- 提示组装 ----------------
    def _user_plan(self, scan: Dict[str, Any], feedback: str = "") -> str:
        names = [f["name"] for f in scan["files"]]
        detail = [{"name": f["name"], "ext": f["ext"], "size": f["size"], "mtime": f["mtime"]}
                  for f in scan["files"]]
        parts = ["# 任务", self.order.task,
                 "# 待整理的文件（共 %d 个）" % scan["total"],
                 json.dumps(detail, ensure_ascii=False, indent=1)]
        if self.order.target_dirs:
            parts.append("# 允许归入的目标目录（只能从这些里选）\n%s"
                         % json.dumps(self.order.target_dirs, ensure_ascii=False))
        if scan.get("existing_dirs"):
            parts.append("# 目录下已有的子目录（不要往里放，除非它就是目标目录）\n%s"
                         % json.dumps(scan["existing_dirs"], ensure_ascii=False))
        sem = getattr(self, "_semantic_text", "")
        if sem:
            parts.append(sem)
        if feedback:
            parts.append("# 上一次方案被方案闸拒绝的原因（必须优先修正）\n%s" % feedback)
        parts.append("# 输出\n只输出 JSON：{\"moves\": [...]}。不确定的文件不要动。")
        return "\n\n".join(parts)

    # ---------------- 主流程 ----------------
    def run(self) -> TidyResult:
        t0 = time.time()
        root = os.path.abspath(self.order.root)
        self._log("整理工单 %s 开始：%s" % (self.order.wo_id, self.order.task))
        if not os.path.isdir(root):
            return self._finish(TidyResult(STATUS_FAILED, self.order,
                                           error="根目录不存在：%s" % root), t0)

        scan = scan_root(root, self.order.filters)
        self._step("scan", 0, "scan", True, "扫到 %d 个文件" % scan["total"])
        self._log("扫描：%d 个待整理文件，%d 个跳过" % (scan["total"], len(scan["skipped"])))
        # 语义建议（可选；本地嵌入算，失败则静默跳过）
        self._semantic_text = self._semantic_suggest(scan, root)
        if scan["total"] == 0:
            return self._finish(TidyResult(STATUS_DONE, self.order, gates=self.gates,
                                           history=self.history, elapsed=time.time() - t0), t0)

        feedback = ""
        plan: Dict[str, Any] = {}
        max_rounds = int(self.order.constraints.get("max_repair_rounds", 2))
        for round_no in range(0, max_rounds + 1):
            messages = [{"role": "system", "content": PLAN_SYSTEM},
                        {"role": "user", "content": self._user_plan(scan, feedback)}]
            r = self.ad.ask(messages, think=False)
            self._step("plan", round_no, r.kind, r.ok, (r.error or "")[:200])
            if not r.ok:
                self._log("规划节点失败（%s）：%s" % (r.kind, r.error))
                return self._finish(TidyResult(STATUS_FAILED, self.order, gates=self.gates,
                                               history=self.history, error=r.error,
                                               elapsed=time.time() - t0), t0)

            parsed = parse_plan(r.content)
            if parsed is None:
                feedback = "上一次输出不是可解析的 JSON 方案。请只输出 {\"moves\": [...]}。"
                self._log("方案解析失败（第 %d 轮）" % round_no)
                self._step("plan", round_no, "parse_failed", False, r.content[:200])
                continue
            plan = normalize_plan(parsed)
            self._log("方案：%d 个动作" % len(plan["moves"]))

            g = check_plan(root, plan, self.order)
            self._gate(g)
            self._step("check_gate", round_no, "gate", g.ok, g.signal)
            self._log("方案闸 → %s（%d 条问题）" % (g.signal, len(g.issues)))
            if g.ok:
                break
            # 删除类动作是安全边界：**未开启 allow_delete 时直接判死、不回炉**（与 R2 同口径）。
            #   2026-10-03 起支持「条件删除」：建单声明 allow_delete=true 时方案闸不再报
            #   delete_not_allowed，删除走「人工确认 + safeops 备份校验」的正常路径；
            #   而 overwrite / rmtree 这类会绕过备份的写法仍然一律判死。
            if any(i.type in ("delete_not_allowed", "forbidden_action") for i in g.issues):
                _why = ("方案含删除类动作，而本工单未开启 allow_delete（默认零删除）"
                        if any(i.type == "delete_not_allowed" for i in g.issues)
                        else "方案含禁止类动作（覆盖/递归删除），已拒绝执行")
                self._step("check_gate", round_no, "security", False, _why)
                return self._finish(TidyResult(
                    STATUS_SECURITY_ABORT, self.order, plan=plan, gates=self.gates,
                    history=self.history, error=_why,
                    elapsed=time.time() - t0), t0)
            feedback = g.feedback_text()
        else:
            return self._finish(TidyResult(
                STATUS_ESCALATED, self.order, plan=plan, gates=self.gates,
                history=self.history, error="方案闸连续 %d 轮未通过，降级人审" % (max_rounds + 1),
                elapsed=time.time() - t0), t0)

        # 通过方案闸
        if self.order.mode != "execute":
            self._log("dry_run 模式：方案已通过闸，未执行")
            return self._finish(TidyResult(STATUS_DONE, self.order, plan=plan, gates=self.gates,
                                           history=self.history,
                                           elapsed=time.time() - t0), t0)

        moved = apply_plan(root, plan, log=[])
        self._step("apply", 0, "apply", True, "移动 %d 个" % len(moved["moved"]))
        self._log("执行：%s" % ("；".join(moved["log"][:6]) or "无"))
        vg = tidy.verify_after(root, plan, scan, moved)
        self._gate(vg)
        self._step("verify_gate", 0, "gate", vg.ok, vg.signal)
        self._log("复核闸 → %s" % vg.signal)
        status = STATUS_DONE if vg.ok else STATUS_ESCALATED
        return self._finish(TidyResult(status, self.order, plan=plan, applied=moved,
                                       gates=self.gates, history=self.history,
                                       error="" if vg.ok else "复核闸未通过",
                                       elapsed=time.time() - t0), t0)

    def _finish(self, res: TidyResult, t0: float) -> TidyResult:
        res.elapsed = round(time.time() - t0, 2)
        try:
            os.makedirs(self.runlog_dir, exist_ok=True)
            p = os.path.join(self.runlog_dir, "%s.json" % self.order.wo_id)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(res.to_dict(), f, ensure_ascii=False, indent=2)
            res.runlog_path = p
        except Exception:
            pass
        return res
