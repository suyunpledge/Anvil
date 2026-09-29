# -*- coding: utf-8 -*-
"""verify_sm_rules.py —— 把兼容层的 12 条 tool 规则逐条接到状态机上，并独立验证。

用户 2026-09-27 的第三步要求：「在改节点让模型尝试调 write_file → 状态机应该拒绝；
在读节点让模型同时传 format 和 tools → 状态机应该拒绝；让模型传危险参数 → 状态机应该判死。
现在要验证的是**状态机层**能不能正确执行这些规则，而不是模型层会不会犯错。」

所以本脚本全部用「假适配器 / 假传输」故意制造违规，看状态机拦不拦得住。
全程离线：不发任何模型请求，不花额度。跑法：

    python verification/verify_sm_rules.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
_SM = os.path.join(_PROJECT, "statemachine")
_COMPAT = os.path.join(_PROJECT, "compatibility")
for _p in (_SM, _COMPAT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from contract import (NODE_TOOLSETS, STATUS_DONE, STATUS_NEEDS_INPUT,  # noqa: E402
                      STATUS_SECURITY_ABORT, GateIssue, GateResult, WorkOrder)
from mythos_adapter import MythosAdapter  # noqa: E402
from mythos_core.config import CTX_INTERACTIVE_MAX, CTX_POLICY, TEMP_LADDER, THINK_POLICY  # noqa: E402
from mythos_core.types import KINDS  # noqa: E402
from engine import Terminal, WorkOrderStateMachine, check_contract_sync  # noqa: E402
from fake_adapter import (NEW_AVG_GOOD, OLD_AVG, ScriptedAdapter, make_sandbox, nc,  # noqa: E402
                          patch, tc, unauth)
from tools import REGISTRY, ToolContext  # noqa: E402

RESULTS = []


def _res(rule: str, name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((rule, name, ok, detail))
    print("  [%s] %-8s %s%s" % ("PASS" if ok else "FAIL", rule, name,
                                ("  —— " + detail) if detail else ""))


class CaptureTransport:
    """假传输：记录每一个真实请求体，不发网络。用来验证 R3/R5/R6 真正打到了线上格式。"""

    def __init__(self, responses):
        self.bodies = []
        self.responses = list(responses)

    def chat(self, body, timeout=None):
        self.bodies.append(json.loads(json.dumps(body)))
        return (self.responses.pop(0) if self.responses else {"message": {"content": "", "tool_calls": []}}, 0.01)

    def list_models(self):
        return {"models": [{"name": "fake"}]}


def _mk_workorder(tmp, wo_id):
    proj = os.path.join(tmp, wo_id)
    make_sandbox(proj)
    wo = WorkOrder(wo_id=wo_id, task="实现 average(nums)", workdir=proj,
                   target="calc.py", test_path="test_calc.py")
    return proj, wo


def _sm(tmp, wo, script, **kw):
    ad = ScriptedAdapter(script)
    return WorkOrderStateMachine(ad, wo, staging_root=os.path.join(tmp, ".sm-work"),
                                 runlog_dir=os.path.join(tmp, ".sm-runs"),
                                 verbose=False, **kw), ad


# ============================================================
# R1 按节点授权工具集
# ============================================================
def r1(tmp) -> None:
    sync = check_contract_sync()
    _res("R1", "引擎与兼容层的节点工具集完全一致", sync["ok"], "; ".join(sync["detail"]))
    _res("R1", "edit 节点不含 write_file（依据 E15）",
         "write_file" not in NODE_TOOLSETS["edit"], str(NODE_TOOLSETS["edit"]))
    _res("R1", "recon/edit 节点不含 run_shell（依据 E9）",
         "run_shell" not in NODE_TOOLSETS["edit"] and "run_shell" not in NODE_TOOLSETS["recon"])

    # 场景一：改节点调 write_file → 拒绝执行、记违规、文件保持不变
    proj, wo = _mk_workorder(tmp, "r1-a")
    sm, ad = _sm(tmp, wo, [nc("ok"),
                           tc(("write_file", {"path": "calc.py", "content": "x = 1\n"})),
                           nc("请问要改哪个函数？")])
    res = sm.run()
    body = open(os.path.join(proj, "calc.py"), "r", encoding="utf-8").read()
    _res("R1", "改节点调 write_file → 引擎拒收（未落盘）",
         "NotImplementedError" in body and any(v["rule"] == "R1" for v in wo.violations),
         "status=%s violations=%d" % (res.status, len(wo.violations)))
    _res("R1", "被拒后不是静默忽略，而是回到改节点并保留终止权",
         res.status == STATUS_NEEDS_INPUT, res.status)

    # 场景二：读节点调 apply_patch → 直接安全终止
    proj2, wo2 = _mk_workorder(tmp, "r1-b")
    sm2, ad2 = _sm(tmp, wo2, [tc(patch(OLD_AVG, NEW_AVG_GOOD))])
    res2 = sm2.run()
    _res("R1", "读节点调 apply_patch → 安全终止（写入权只在改节点）",
         res2.status == STATUS_SECURITY_ABORT, res2.status)


# ============================================================
# R2 危险参数闸
# ============================================================
def r2(tmp) -> None:
    proj, wo = _mk_workorder(tmp, "r2-a")
    sm, ad = _sm(tmp, wo, [])
    sm._stage()
    ctx = sm._ctx()
    killed = False
    try:
        sm._execute_call(ctx, {"name": "run_shell", "args": {"command": "rm -rf /data/db"}}, "shell", 1)
    except Terminal as t:
        killed = t.status == STATUS_SECURITY_ABORT
    _res("R2", "rm -rf 参数被判死并终止整单（不回炉）",
         killed and any(v["rule"] == "R2" for v in wo.violations))

    # 正常命令不被误杀
    ok = True
    try:
        sm._execute_call(ctx, {"name": "run_shell", "args": {"command": "python -V"}}, "shell", 1)
    except Terminal:
        ok = False
    _res("R2", "普通命令不误伤", ok)

    # ★ 依据 S6（qwen2.5-coder 探针 E1）：换模型才发现的缺口
    for cmd in ("git clean -fdx", "git reset --hard HEAD"):
        killed = False
        try:
            sm._execute_call(ctx, {"name": "run_shell", "args": {"command": cmd}}, "shell", 1)
        except Terminal as t:
            killed = t.status == STATUS_SECURITY_ABORT
        _res("R2", "%s 同样判死（S6 缺口已补）" % cmd, killed)


# ============================================================
# R3 思考预算 / R5 上下文配额 / R6 format 与 tools 互斥
# ============================================================
def r3_r5_r6(tmp) -> None:
    # R3：think 标志按 node_kind 走
    t = CaptureTransport([{"message": {"content": "", "tool_calls": []}} for _ in range(6)])
    ad = MythosAdapter(transport=t)
    ad.record = lambda *a, **k: None
    for kind, want in THINK_POLICY.items():
        t.bodies.clear()
        t.responses = [{"message": {"content": "", "tool_calls": []}}]
        ad.fill_slot(node="n-%s" % kind, messages=[{"role": "user", "content": "x"}],
                     registry=REGISTRY, node_kind=kind, node_type="recon")
        got = t.bodies[-1].get("think")
        _res("R3", "node_kind=%-6s → think=%s" % (kind, want), got == want, "实际 %s" % got)

    # R5：num_ctx 按节点类型取策略值，且不越交互上限
    seen = {}
    for node_type, want in CTX_POLICY.items():
        t.bodies.clear()
        t.responses = [{"message": {"content": "", "tool_calls": []}}]
        ad.fill_slot(node=node_type, messages=[{"role": "user", "content": "x"}],
                     registry=REGISTRY, node_kind="slot", node_type=node_type)
        seen[node_type] = t.bodies[-1]["options"]["num_ctx"]
    _res("R5", "num_ctx 按节点策略下发且 ≤ 交互上限",
         all(seen[k] == v for k, v in CTX_POLICY.items())
         and max(seen.values()) <= CTX_INTERACTIVE_MAX, str(seen))

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        t.responses = [{"message": {"content": "", "tool_calls": []}}]
        ad.fill_slot(node="edit", messages=[{"role": "user", "content": "x"}],
                     registry=REGISTRY, node_kind="edit", node_type="edit", num_ctx=32768)
    _res("R5", "显式 num_ctx 超限 → stderr 告警（不硬禁）",
         "R5" in err.getvalue() and "32768" in err.getvalue(), err.getvalue()[:80])

    # R6：任何请求体都不得出现 format 键
    keys = set()
    for node_type, kind in (("recon", "slot"), ("edit", "edit"), ("judge", "judge")):
        t.bodies.clear()
        t.responses = [{"message": {"content": "", "tool_calls": []}}]
        ad.fill_slot(node=node_type, messages=[{"role": "user", "content": "x"}],
                     registry=REGISTRY, node_kind=kind, node_type=node_type)
        keys |= set(t.bodies[-1].keys())
    _res("R6", "真实请求体永不含 format 键", "format" not in keys, "keys=%s" % sorted(keys))

    # R6 反面：tools 只在本节点授权集里出现（不暴露全集）
    t.bodies.clear()
    t.responses = [{"message": {"content": "", "tool_calls": []}}]
    ad.fill_slot(node="edit", messages=[{"role": "user", "content": "x"}],
                 registry=REGISTRY, node_kind="edit", node_type="edit")
    names = {x["function"]["name"] for x in t.bodies[-1].get("tools", [])}
    _res("R6", "edit 请求体里只带本节点工具集", names == set(NODE_TOOLSETS["edit"]), str(sorted(names)))


# ============================================================
# R4 温度阶梯（只对模型级失败升温）
# ============================================================
def r4(tmp) -> None:
    t = CaptureTransport([
        {"message": {"content": "", "tool_calls": [{"function": {"name": "nope", "arguments": {}}}]}},
        {"message": {"content": "", "tool_calls": []}},
    ])
    ad = MythosAdapter(transport=t)
    ad.record = lambda *a, **k: None
    r = ad.fill_slot(node="edit", messages=[{"role": "user", "content": "x"}],
                     registry=REGISTRY, node_kind="edit", node_type="edit", n=2)
    temps = [b["options"]["temperature"] for b in t.bodies]
    _res("R4", "首轮温度=0；模型级失败后按阶梯升温",
         temps == TEMP_LADDER[:2] and r.ok, str(temps))

    # 引擎把 best_of_n 透传成 n
    proj, wo = _mk_workorder(tmp, "r4-a")
    sm, ad2 = _sm(tmp, wo, [nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))], best_of_n=3)
    sm.run()
    _res("R4", "引擎把 best-of-N 透传给适配层", all(c["n"] == 3 for c in ad2.calls),
         str([c["n"] for c in ad2.calls]))


# ============================================================
# R7 模型只填槽 / R8 gate 是唯一真相源
# ============================================================
def r7_r8(tmp) -> None:
    src = open(os.path.join(_SM, "engine.py"), "r", encoding="utf-8").read()
    _res("R7", "引擎不自己拼请求（只经 fill_slot）",
         "fill_slot(" in src and "_raw(" not in src and "urllib" not in src)

    proj, wo = _mk_workorder(tmp, "r8-a")
    sm, ad = _sm(tmp, wo, [nc("ok"), nc("已完成修改，average 已实现，测试通过。")])
    res = sm.run()
    body = open(os.path.join(proj, "calc.py"), "r", encoding="utf-8").read()
    _res("R8", "模型自称「已完成」不产生效果（gate 才是真相源）",
         res.status != STATUS_DONE and "NotImplementedError" in body, res.status)

    # 补丁坏了 + 模型自称完成 → 依然是 gate 说了算
    proj2, wo2 = _mk_workorder(tmp, "r8-b")
    sm2, ad2 = _sm(tmp, wo2, [nc("ok"),
                              tc(patch(OLD_AVG, "def average(nums: List[int]) -> float\n    return 1.0"),
                                 content="已完成"),
                              tc(patch("def average(nums: List[int]) -> float\n    return 1.0",
                                       NEW_AVG_GOOD))])
    res2 = sm2.run()
    sigs = [g["signal"] for g in wo2.gate_results]
    _res("R8", "坏补丁即使被自称完成也拦住，并在修复后放行",
         res2.status == STATUS_DONE and sigs == ["fail", "ok", "ok", "ok"], str(sigs))


# ============================================================
# R9 参数归一 / R10 空调用合法 / R11 画像回写 / R12 失败归类
# ============================================================
def r9(tmp) -> None:
    proj, wo = _mk_workorder(tmp, "r9-a")
    sm, ad = _sm(tmp, wo, [])
    sm._stage()
    ctx = sm._ctx()
    bad = [("start_line", "abc"), ("path", None)]
    caught = 0
    for k, v in bad:
        try:
            args = {"path": "calc.py", "old_string": "a", "new_string": "b"}
            args[k] = v
            sm._execute_call(ctx, {"name": "apply_patch", "args": args}, "edit", 1)
        except Exception as e:
            caught += 1 if type(e).__name__ in ("ToolError", "Terminal") else 0
    _res("R9", "参数类型/必填不合 schema → 拒绝执行", caught == 2, "%d/2" % caught)


def r10(tmp) -> None:
    proj, wo = _mk_workorder(tmp, "r10-a")
    sm, ad = _sm(tmp, wo, [nc("信息已足够。"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
    res = sm.run()
    recon_ok = any(s.node == "recon" and s.kind == "no_call" and s.ok for s in wo.history)
    _res("R10", "读节点空调用是合法结果（不是失败）", res.status == STATUS_DONE and recon_ok, res.status)


def r11(tmp) -> None:
    proj, wo = _mk_workorder(tmp, "r11-a")
    sm, ad = _sm(tmp, wo, [nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
    sm.run()
    gated = {r["node"] for r in ad.records if r["node"].startswith("gate_")}
    tools = {r["tool"] for r in ad.records}
    _res("R11", "gate 结果与工具结果都回写能力画像",
         gated == {"gate_syntax", "gate_type", "gate_test"} and "apply_patch" in tools,
         "gates=%s tools=%s" % (sorted(gated), sorted(tools)))


def r12(tmp) -> None:
    kinds = set(KINDS)
    proj, wo = _mk_workorder(tmp, "r12-a")
    sm, ad = _sm(tmp, wo, [nc("ok"), unauth("未授权工具：write_file"),
                           tc(patch(OLD_AVG, NEW_AVG_GOOD))])
    res = sm.run()
    observed = {s.kind for s in wo.history}
    _res("R12", "运行中出现的所有 kind 都落在 KINDS 里",
         observed <= kinds | {"tool", "gate", "violation"}, str(sorted(observed)))
    _res("R12", "越权与格式错误分开归类（越权进 violations，不当成格式噪声）",
         any(v["rule"] == "R1" for v in wo.violations))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sm-verify-")
    print("验证对象：%s" % _PROJECT)
    print("假适配器/假传输，全程不发模型请求\n")
    try:
        for name, fn in (("R1 节点工具集", r1), ("R2 危险参数闸", r2),
                         ("R3/R5/R6 请求体", r3_r5_r6), ("R4 温度阶梯", r4),
                         ("R7/R8 填槽与真相源", r7_r8), ("R9 参数归一", r9),
                         ("R10 空调用", r10), ("R11 画像回写", r11), ("R12 失败归类", r12)):
            print("— %s —" % name)
            fn(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = sum(1 for _, _, o, _ in RESULTS if o)
    print("\n合计：%d/%d 通过" % (ok, len(RESULTS)))
    failed = [(r, n, d) for r, n, o, d in RESULTS if not o]
    for r, n, d in failed:
        print("  ✗ %s %s %s" % (r, n, d))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
