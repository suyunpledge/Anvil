# -*- coding: utf-8 -*-
"""gates.py —— 三个确定性 gate（唯一的真相源，R8）。

约定：每个 gate 只返回 :class:`contract.GateResult`，通过信号只有 ``ok/signal`` 两态。
框架其它部件不得解析 ``raw`` 里的自然语言来做判断——那是给人看的证据。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from contract import GateIssue, GateResult
from tools import ToolContext, run_tests

import typecheck


def _target_abs(ctx: ToolContext) -> str:
    return ctx.resolve(ctx.target)


# ============================================================
# 语法 gate
# ============================================================
def gate_syntax(ctx: ToolContext) -> GateResult:
    t0 = time.time()
    p = _target_abs(ctx)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError as e:
        return GateResult("gate_syntax", False, error="读不到目标文件：%s" % e,
                          sec=time.time() - t0)
    try:
        ast.parse(src, filename=ctx.target)
        return GateResult("gate_syntax", True, sec=time.time() - t0,
                          raw={"lines": src.count("\n") + 1})
    except SyntaxError as e:
        issue = GateIssue(type="syntax_error",
                          message="%s" % (e.msg or "语法错误"),
                          line=e.lineno or 0, col=e.offset or 0,
                          hint="按行号修正该行语句本身")
        return GateResult("gate_syntax", False, issues=[issue], sec=time.time() - t0,
                          raw={"text": (e.text or "").rstrip()})


# ============================================================
# 类型 gate（v1 近似：名称解析 + 注解完备 + 重复定义）
# ============================================================
def gate_type(ctx: ToolContext, require_annotations: bool = True) -> GateResult:
    t0 = time.time()
    p = _target_abs(ctx)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError as e:
        return GateResult("gate_type", False, error="读不到目标文件：%s" % e,
                          sec=time.time() - t0)
    try:
        raw = typecheck.check_source(src, filename=ctx.target,
                                     require_annotations=require_annotations)
    except SyntaxError as e:   # 语法 gate 应在前面拦住；这里兜底
        return GateResult("gate_type", False,
                          issues=[GateIssue("syntax_error", str(e.msg), e.lineno or 0)],
                          sec=time.time() - t0)

    # ★ 保留性检查：与框架在暂存区留的原始副本对比，拦住「静默删除无关代码」
    removed = 0
    orig_path = os.path.join(ctx.root, ".sm_original_target.py")
    if os.path.exists(orig_path):
        try:
            with open(orig_path, "r", encoding="utf-8", errors="replace") as f:
                orig_src = f.read()
            pres = typecheck.preservation_issues(orig_src, src)
            removed = len(pres)
            raw = list(raw) + pres
        except OSError:
            pass

    issues = [GateIssue(type=i["type"], message=i["message"], line=i["line"],
                        col=i["col"], hint=i.get("hint", "")) for i in raw]
    return GateResult("gate_type", not issues, issues=issues, sec=time.time() - t0,
                      raw={"checked": True, "issue_count": len(issues),
                           "removed_symbols": removed})


# ============================================================
# 测试 gate
# ============================================================
def gate_test(ctx: ToolContext, test_path: Optional[str] = None,
              test_cmd: Optional[List[str]] = None,
              timeout_sec: Optional[int] = None) -> GateResult:
    """测试 gate。

    ★ 安全：这里执行的是**模型改写过的代码**，因此子进程一律用 `sandbox.sanitized_env()`
    给的干净环境（丢代理与凭据、HOME 指向临时目录）。仍是缓解而非沙箱——
    真正阻断网络/文件需要 OS 级手段，见 `sandbox.real_isolation_available()`。
    """
    import sandbox as _sb
    t0 = time.time()
    clean_env = _sb.sanitized_env()
    if test_cmd:
        cmd = [c.replace("{python}", sys.executable) for c in test_cmd]
        try:
            proc = subprocess.run(cmd, cwd=ctx.root, capture_output=True, text=True,
                                  timeout=int(timeout_sec or ctx.timeout_sec),
                                  encoding="utf-8", errors="replace", env=clean_env)
        except subprocess.TimeoutExpired:
            return GateResult("gate_test", False,
                              issues=[GateIssue("test_timeout", "测试超时")],
                              sec=time.time() - t0, raw={"cmd": cmd,
                                                          "sandbox": _sb.summary()})
        out = (proc.stdout or "") + (proc.stderr or "")
        raw = {"rc": proc.returncode, "cmd": cmd, "output": out[-4000:],
               "sandbox": _sb.summary()}
        ok = proc.returncode == 0
        issues = [] if ok else [GateIssue("test_failure", "测试命令返回 %d" % proc.returncode)]
        return GateResult("gate_test", ok, issues=issues, sec=time.time() - t0, raw=raw)

    if not test_path and not test_cmd:
        return GateResult("gate_test", False,
                          issues=[GateIssue("no_test_declared",
                                            "工单没有声明测试文件（v1 要求 test_path）——"
                                            "无法形成测试门，拒绝空跑通过")],
                          sec=time.time() - t0, raw={"rc": None})
    res = run_tests(ctx, test_path=test_path or ctx.target, timeout_sec=timeout_sec)
    ok = bool(res.get("ok"))
    issues: List[GateIssue] = []
    if not ok:
        issues.append(GateIssue(
            type="test_failure",
            message="测试未通过：rc=%s 运行 %s 个用例，失败 %s，错误 %s"
                    % (res.get("rc"), res.get("tests_run"), res.get("failures"), res.get("errors")),
            hint="按下面的失败输出定位问题；只改目标文件，不要改测试"))
    import sandbox as _sb
    return GateResult("gate_test", ok, issues=issues, sec=time.time() - t0,
                      raw={"rc": res.get("rc"), "tests_run": res.get("tests_run"),
                           "failures": res.get("failures"), "errors": res.get("errors"),
                           "output": (res.get("output") or "")[-4000:],
                           "cmd": res.get("cmd"), "sandbox": _sb.summary()})


# ============================================================
# 统一入口
# ============================================================
GATES = {"gate_syntax": gate_syntax, "gate_type": gate_type, "gate_test": gate_test}


def run_gate(name: str, ctx: ToolContext, *, test_path: Optional[str] = None,
             test_cmd: Optional[List[str]] = None,
             require_annotations: bool = True,
             timeout_sec: Optional[int] = None) -> GateResult:
    if name == "gate_syntax":
        return gate_syntax(ctx)
    if name == "gate_type":
        return gate_type(ctx, require_annotations=require_annotations)
    if name == "gate_test":
        return gate_test(ctx, test_path=test_path, test_cmd=test_cmd, timeout_sec=timeout_sec)
    raise ValueError("未知 gate：%s" % name)


def test_failure_signature(res: GateResult) -> str:
    """测试失败的「签名」——用于判断「同样的失败重复出现」（据此升级到 recon）。"""
    raw = res.raw or {}
    out = (raw.get("output") or "")
    lines = [l.strip() for l in out.split("\n")
             if l.startswith(("FAIL:", "ERROR:"))]
    if lines:
        return "|".join(sorted(set(lines))[:6])
    return "rc=%s|tests=%s|f=%s|e=%s" % (raw.get("rc"), raw.get("tests_run"),
                                         raw.get("failures"), raw.get("errors"))
